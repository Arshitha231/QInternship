"""Step 10: run the golden evaluation set against the real pipeline.

For every question: resolve intent through the REAL Azure OpenAI tool-
calling model (not the mock heuristics), execute the resulting call through
the exact same permission-filtered service functions every other caller
uses, and score the result against the question's known-correct answer.

Deliberately does NOT call app.tool_calling.answer() directly. answer()
silently falls back to the mock resolver on any OpenAIError (correct
production behaviour — degrade, don't error) but that would corrupt this
evaluation: a rate-limited call would score as "what the model decided"
when it's actually "what the keyword heuristic guessed," with no way to
tell the two apart afterward. This file re-implements the real-resolution
call with its own retry/backoff instead, so a transient 429 gets retried
rather than silently masked. Same TOOLS/SYSTEM_PROMPT/FEW_SHOT_EXAMPLES,
same execute_tool_call() dispatch — just without the silent fallback.

The account's gpt-5-mini deployment is capacity=3 (GlobalStandard, ~3K
TPM) and each call carries ~2.7K tokens of system prompt + 25 few-shot
examples, so this paces itself deliberately slowly.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pinned fixture, not live directory.db -- see golden_set.py's module
# docstring for why. Set BEFORE any app.* import (app.db reads DATABASE_URL
# at import time and binds an engine to it immediately, same pattern
# tests/conftest.py uses for its own throwaway db) so nothing here can
# silently fall through to whatever a developer's .env happens to point at.
os.environ["DATABASE_URL"] = f"sqlite:///{Path(__file__).resolve().parent / 'fixture.db'}"

from openai import OpenAIError  # noqa: E402

import app.search_client as sc  # noqa: E402
import independent_truth  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.directory_tools import skill_gap, skill_scarcity  # noqa: E402
from app.tool_calling import (  # noqa: E402
    CHAT_ENDPOINT,
    CHAT_KEY,
    MAX_CHAIN_STEPS,
    OUT_OF_SCOPE_MESSAGE,
    ResolvedToolCall,
    AssistantTurn,
    _chain_step_messages,
    _get_openai_client,
    _is_content_filter_block,
    _llm_routed_via,
    _serialize_step_result,
    build_messages,
    TOOLS,
    OPENAI_CHAT_DEPLOYMENT,
    execute_tool_call,
)

from golden_set import ALL_QUESTIONS, SEARCH_DEPENDENT_CATEGORIES  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
# Set by preflight when the index cannot be trusted for this run; read by
# summarize() so the report says which pipeline the numbers describe.
SEARCH_DISABLED_REASON: str | None = None
CALL_DELAY_SECONDS = 8.0  # pacing between real LLM calls, see module docstring
MAX_RETRIES = 5


def resolve_intent_strict(message: str, extra_messages: list[dict] | None = None) -> AssistantTurn:
    """Same logic as app.tool_calling._real_resolve, minus the silent
    mock fallback — retries on OpenAIError instead, so this eval measures
    the real model, not a rate-limit-triggered heuristic standing in for it.

    `extra_messages` mirrors _real_resolve's own parameter exactly (used by
    run_chain_strict below, the same way app.tool_calling.execute_chain
    uses _real_resolve's) -- this file re-implements the retry/backoff
    wrapper around the API call, not the message-shape logic itself, so
    the two stay behaviorally identical.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            client = _get_openai_client()
            messages = build_messages(message)
            if extra_messages:
                messages.extend(extra_messages)
            response = client.chat.completions.create(
                model=OPENAI_CHAT_DEPLOYMENT,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                # Same fix as app.tool_calling._real_resolve -- keeping this
                # eval's call shape identical to production's is the point;
                # a slower, higher-reasoning eval run would not be measuring
                # what production actually does. See ARCHITECTURE_2.md §6/RC1.
                reasoning_effort="minimal",
            )
            choice = response.choices[0].message
            if choice.tool_calls:
                call = choice.tool_calls[0]
                arguments = json.loads(call.function.arguments)
                # Same lift app.tool_calling._real_resolve performs -- see
                # that function's own comment. Needed here too so
                # run_chain_strict below can tell whether the model asked
                # for another step, exactly like production does.
                needs_followup = bool(arguments.pop("needs_followup", False))
                return AssistantTurn(tool_call=ResolvedToolCall(
                    name=call.function.name, arguments=arguments, needs_followup=needs_followup,
                    tool_call_id=call.id))
            return AssistantTurn(message=choice.content or OUT_OF_SCOPE_MESSAGE)
        except OpenAIError as exc:
            if _is_content_filter_block(exc):
                # Deterministic Azure content-safety block, not a transient
                # failure -- matches app.tool_calling._real_resolve's fixed
                # behavior: refuse immediately, no retry storm.
                return AssistantTurn(message=OUT_OF_SCOPE_MESSAGE)
            last_error = exc
            backoff = min(90.0, 10.0 * (2 ** attempt))
            print(f"    [retry {attempt + 1}/{MAX_RETRIES}] {type(exc).__name__}: {exc} -- backing off {backoff:.0f}s",
                  flush=True)
            time.sleep(backoff)
    raise RuntimeError(f"exhausted {MAX_RETRIES} retries against Azure OpenAI") from last_error


def run_chain_strict(db, caller, first_call: ResolvedToolCall, message: str) -> dict:
    """Mirrors app.tool_calling.execute_chain's loop shape -- same
    MAX_CHAIN_STEPS bound, same _chain_step_messages function production
    uses, so this measures the real chain behavior,
    not a re-derived approximation of it. Differs only where this whole
    file already differs from production: resolve_intent_strict's own
    retry/backoff on OpenAIError instead of _real_resolve's silent
    degrade, and no retry-on-EXECUTION-failure (run() below never had
    that for the single-call path either -- a step that raises just
    records the error and stops, same as today).

    Returns a dict shaped like run()'s own per-question record fields:
    {tool_call, arguments, result, steps, exec_error}. `steps` is the
    count actually taken, the number this whole exercise exists to
    measure against the single-call baseline.
    """
    attempt = first_call
    extra_messages: list[dict] = []
    result = None
    exec_error = None

    for step in range(1, MAX_CHAIN_STEPS + 1):
        try:
            result = execute_tool_call(db, caller, attempt)
        except (TypeError, ValueError, KeyError) as exc:
            exec_error = str(exc)
            return {"tool_call": attempt.name, "arguments": attempt.arguments,
                    "result": None, "steps": step, "exec_error": exec_error}

        if not (attempt.needs_followup and step < MAX_CHAIN_STEPS):
            break

        extra_messages.extend(_chain_step_messages(attempt, result))
        time.sleep(CALL_DELAY_SECONDS)  # pacing applies to every model call, chained or not
        next_turn = resolve_intent_strict(message, extra_messages=extra_messages)
        if next_turn.tool_call is None:
            break  # model says it's done -- finalize with this step's result
        next_turn.tool_call.routed_via = _llm_routed_via(next_turn.tool_call)
        attempt = next_turn.tool_call

    return {"tool_call": attempt.name, "arguments": attempt.arguments,
            "result": result, "steps": step, "exec_error": exec_error}


# ---------------------------------------------------------------------------
# Extractors: raw tool result -> ordered list of comparable items. Ordering
# matters -- it's what "top k" means for recall@k/precision@k (see score()).
# ---------------------------------------------------------------------------

def _ex_find_people(r):
    return [p.id for p in (r or [])]


def _ex_org_chain(r):
    return [n.id for n in (r or [])]


def _ex_mentor(r):
    return [m.id for m in (r or [])]


def _find_enriched(r):
    """find_people now returns a plain list[PersonSummary] where at most one
    entry (the exact-name match) carries manager/delegate/direct_reports —
    the other entries are fuzzy neighbors with those fields unset. Also
    accepts a single object directly (get_person's shape), in case a
    question ever resolves through that tool instead.
    """
    if r is None:
        return None
    if isinstance(r, list):
        for item in r:
            if getattr(item, "manager", None) or getattr(item, "delegate", None) or getattr(item, "direct_reports", None):
                return item
        return None
    return r


def _ex_person_manager_id(r):
    item = _find_enriched(r)
    m = getattr(item, "manager", None) if item is not None else None
    return [m.id] if m else []


def _ex_person_delegate_id(r):
    item = _find_enriched(r)
    d = getattr(item, "delegate", None) if item is not None else None
    return [d.id] if d else []


def _ex_person_direct_reports(r):
    item = _find_enriched(r)
    reports = getattr(item, "direct_reports", None) if item is not None else None
    return [pr.id for pr in (reports or [])]


def _ex_project_owner(r):
    return [r.owner_id] if r is not None else []


def _ex_has_nightingale(r):
    # Defensive against the model calling the wrong tool (e.g. find_people
    # instead of get_person) — that's a real, reportable finding, not a
    # harness crash, so treat anything without project_history as "no".
    if r is None or not hasattr(r, "project_history"):
        return []
    names = {ph.project_name for ph in (r.project_history or [])}
    return ["Project Nightingale"] if "Project Nightingale" in names else []


def _ex_skill_gap(r):
    return [frozenset(item.model_dump().items()) for item in (r or [])]


def _ex_skill_scarcity(r):
    return [frozenset(item.model_dump().items()) for item in (r or [])]


EXTRACTORS = {
    "find_people": _ex_find_people,
    "org_chain": _ex_org_chain,
    "mentor": _ex_mentor,
    "person_manager_id": _ex_person_manager_id,
    "person_delegate_id": _ex_person_delegate_id,
    "person_direct_reports": _ex_person_direct_reports,
    "project_owner": _ex_project_owner,
    "has_nightingale": _ex_has_nightingale,
    "skill_gap": _ex_skill_gap,
    "skill_scarcity": _ex_skill_scarcity,
}

# Ground truth by calling the real service function directly -- legitimate
# only where the eval isn't grading that function's own logic (skill_gap/
# skill_scarcity's aggregation math). See golden_set.py's module docstring.
DYNAMIC_CALLS = {
    "skill_gap": lambda db, caller, args: skill_gap(db, caller, required_skills=args["required_skills"]),
    "skill_scarcity": lambda db, caller, args: skill_scarcity(db, caller, **args),
}

# Ground truth via eval/independent_truth.py's own SQLAlchemy queries/walks
# -- never find_people/get_org_chain/find_mentor, which is exactly what
# these questions grade. Each already returns a comparable set/list of ids
# directly, so no EXTRACTORS entry is applied to it (unlike DYNAMIC_CALLS
# above, whose raw result IS tool-response-shaped).
INDEPENDENT_CALLS = {
    "direct_reports": lambda db, caller, args: independent_truth.direct_reports(db, caller, **args),
    "org_chain": lambda db, caller, args: independent_truth.org_chain(db, caller, **args),
    "filter_people": lambda db, caller, args: independent_truth.filter_people(db, caller, **args),
    "filter_people_or": lambda db, caller, args: independent_truth.filter_people_or(db, caller, **args),
    "team_skill_availability": lambda db, caller, args: independent_truth.team_skill_availability(db, caller, **args),
    "project_owners_manager": lambda db, caller, args: independent_truth.project_owners_manager(db, caller, **args),
    "find_mentor": lambda db, caller, args: independent_truth.find_mentor(
        db, caller, skill=args["skill"], caller_id=caller.id),
}


def score(relevant: set, returned: list) -> tuple[float, float]:
    """recall@k, precision@k where k = |relevant| -- not a fixed top-3.

    A fixed top-3 cap makes anything with more than 3 correct answers
    structurally incapable of scoring above 3/|relevant|: t1-04's ground
    truth has 14 correct direct reports, the pipeline returned all 14, and
    it scored 0.21 (`hit` capped at 3, divided by 14). k=|relevant| keeps
    the thing top-3 was actually checking -- are the right results at the
    top of a ranked list -- without punishing a correct answer for having
    a large ground-truth set.

    Empty-relevant is a valid case (the correct answer is "nothing") and
    scores 1.0/1.0 only if nothing came back either -- any returned item
    is then a straightforward false positive.
    """
    k = max(len(relevant), 1)
    topk = returned[:k]
    hit = len(relevant & set(topk))
    if not relevant:
        return (1.0, 1.0) if not topk else (0.0, 0.0)
    recall = hit / len(relevant)
    precision = hit / len(topk) if topk else 0.0
    return recall, precision


def preflight() -> None:
    """Fail immediately, and say why, when the chat resource isn't configured.

    Without this, an empty CHAT_ENDPOINT doesn't look like a configuration
    problem at all. _get_openai_client() builds `base_url="/openai/v1/"` --
    a relative URL with no host -- and the SDK raises `APIConnectionError:
    Connection error.` from the transport layer, which reads like a network
    fault on a resource that is in fact perfectly healthy. The retry loop
    then treats it as transient and grinds through 5 attempts at
    10/20/40/80/90s backoff, for every one of the 55 questions, until the
    job's 30-minute timeout kills it. The step is continue-on-error, so it
    doesn't fail the build -- it just silently delays deploy by half an
    hour, since terraform and deploy both wait on this job.

    Diagnosed after exactly that: 275 retries reported as a connection
    error, against an endpoint that answered a curl on the first try.

    Missing search/embedding config is a warning, not an error: find_people
    degrades to the SQL path by design, so the eval still runs -- but it's
    then measuring a different retrieval pipeline than production uses, and
    the scores at the end deserve that asterisk.
    """
    if not CHAT_ENDPOINT or not CHAT_KEY:
        missing = [n for n, v in [("CHAT_ENDPOINT", CHAT_ENDPOINT), ("CHAT_KEY", CHAT_KEY)] if not v]
        sys.exit(
            f"golden eval: {' and '.join(missing)} not set — nothing to evaluate against.\n"
            "  This eval deliberately measures the REAL model, so it refuses to run on the\n"
            "  mock resolver rather than reporting heuristic guesses as model scores.\n"
            "  In CI: check the GROUP3_4OPENAI* repo secrets still exist under those names\n"
            "  (a pull request from a fork never receives them — that's GitHub's design).\n"
            "  Locally: run from the repo root, since load_dotenv() searches upward from\n"
            "  the calling file and won't find .env from an unrelated working directory."
        )

    if not sc.is_configured() or not sc.EMBEDDING_ENDPOINT:
        print("golden eval: WARNING — Azure AI Search and/or embeddings unconfigured. "
              "find_people will fall back to the SQL path, so retrieval scores below "
              "measure a different pipeline than production runs.\n", flush=True)
        return

    _check_index_parity()


# How much of a sample of the index has to exist in the evaluated database
# before the two are considered to be describing the same population. Set
# low deliberately: this is detecting "completely unrelated corpus", not
# "slightly stale index", and a genuinely stale index (a few deactivations
# not yet re-indexed) must not trip it.
INDEX_PARITY_MIN_OVERLAP = 0.5
INDEX_PARITY_SAMPLE = 100


def _check_index_parity() -> None:
    """Detect an index that is configured, healthy, and describing a
    DIFFERENT population than the database being evaluated.

    This is the failure the old preflight could not see. It checked whether
    Search was configured; it never checked whether Search was configured
    against the right data. eval/fixture.db is a pinned snapshot and the
    shared employees-index tracks the deployed database, so as of
    2026-08-18 the two share exactly zero ids out of 530 and 559 -- every
    Search-ranked question returns documents whose ids resolve to nobody
    locally, find_people falls through to its SQL branch, and the fuzzy-name
    and description questions score 0.0. Seven tier-2 questions were failing
    this way, which is most of the gap between tier 2's 0.687 and tier 1's
    1.0. That is an environment artifact being reported as answer quality.

    On detection: say so loudly, and take Search out of the run entirely, so
    the numbers measure ONE coherent pipeline (SQL) rather than a hybrid
    whose ranking arm is silently dead. Questions that genuinely need
    Search are then reported as unmeasurable rather than as failures --
    see summarize().
    """
    global SEARCH_DISABLED_REASON
    db = SessionLocal()
    try:
        sample = sc.search_people(name=None, top=INDEX_PARITY_SAMPLE)
        if not sample:
            print("golden eval: WARNING — the search index returned no documents at all. "
                  "Retrieval scores below measure the SQL path only.\n", flush=True)
            SEARCH_DISABLED_REASON = "index empty or unreachable"
            _disable_search()
            return
        from sqlalchemy import select

        from app.models import Employee
        present = db.execute(
            select(Employee.id).where(Employee.id.in_(sample))
        ).scalars().all()
        overlap = len(present) / len(sample)
        if overlap < INDEX_PARITY_MIN_OVERLAP:
            print(
                f"golden eval: WARNING — the search index and this database describe different\n"
                f"  populations: {len(present)} of {len(sample)} sampled index documents exist here.\n"
                f"  The index tracks the DEPLOYED database; eval/fixture.db is a pinned snapshot.\n"
                f"  Search is being disabled for this run so the scores measure one coherent\n"
                f"  pipeline instead of a hybrid whose ranking arm silently returns nothing.\n"
                f"  Questions that require ranked retrieval are reported as UNMEASURABLE below,\n"
                f"  not as failures — scoring them here would measure the environment, not the\n"
                f"  system.\n", flush=True)
            SEARCH_DISABLED_REASON = (
                f"index/database mismatch ({len(present)}/{len(sample)} sampled ids present)")
            _disable_search()
    finally:
        db.close()


def _disable_search() -> None:
    """Take the ranking arm out of the run at its single entry point.

    find_people already treats a None from search_people as "Search is
    unavailable, use SQL" (app/people.py) -- the same documented degradation
    path an unreachable Search takes -- so this needs no new branch anywhere
    in the application, and nothing about what is being measured becomes
    special-cased for the eval.
    """
    import app.people

    app.people.search_people = lambda *_a, **_k: None


def run() -> list[dict]:
    preflight()
    db = SessionLocal()
    results: list[dict] = []

    for i, q in enumerate(ALL_QUESTIONS, 1):
        print(f"[{i}/{len(ALL_QUESTIONS)}] ({q['id']}, tier {q['tier']}, {q['category']}) {q['text']!r}", flush=True)
        caller = q["caller"]
        record = {
            "id": q["id"], "tier": q["tier"], "category": q["category"], "text": q["text"],
            "caller": caller.name, "caller_role": caller.role,
        }
        try:
            turn = resolve_intent_strict(q["text"])
        except RuntimeError as exc:
            print(f"    ERROR resolving intent: {exc}", flush=True)
            record.update(error=str(exc))
            results.append(record)
            RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
            time.sleep(CALL_DELAY_SECONDS)
            continue

        record["tool_call"] = turn.tool_call.name if turn.tool_call else None
        record["arguments"] = turn.tool_call.arguments if turn.tool_call else None
        record["refusal_message"] = turn.message if turn.tool_call is None else None

        if q["kind"] == "refusal":
            record["correct_refusal"] = turn.tool_call is None
            record["exact_wording"] = turn.message == OUT_OF_SCOPE_MESSAGE
            results.append(record)
            RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
            print(f"    -> {'REFUSED' if turn.tool_call is None else 'CALLED ' + turn.tool_call.name}", flush=True)
            time.sleep(CALL_DELAY_SECONDS)
            continue

        raw_result = None
        exec_error = None
        steps = 1
        if turn.tool_call is not None and turn.tool_call.needs_followup:
            # The model itself asked for a chain on its FIRST call -- the
            # exact same trigger app.tool_calling.answer() checks, so a
            # question the model can (and does) answer in one call costs
            # exactly what it costs today: this branch is simply never
            # entered for it. `steps` is recorded either way (1 here, N
            # below) specifically to compare against the single-call
            # baseline side by side, per this feature's own verification
            # requirement.
            chain_result = run_chain_strict(db, caller, turn.tool_call, q["text"])
            record["tool_call"] = chain_result["tool_call"]
            record["arguments"] = chain_result["arguments"]
            raw_result = chain_result["result"]
            exec_error = chain_result["exec_error"]
            steps = chain_result["steps"]
        elif turn.tool_call is not None:
            try:
                raw_result = execute_tool_call(db, caller, turn.tool_call)
            except (TypeError, ValueError, KeyError) as exc:
                exec_error = str(exc)

        record["steps"] = steps
        extractor = EXTRACTORS[q["extractor"]]
        returned_list = extractor(raw_result) if raw_result is not None or turn.tool_call is not None else []

        gt = q["ground_truth"]
        if isinstance(gt, tuple) and gt[0] == "dynamic":
            _, tool_name, args = gt
            ref_raw = DYNAMIC_CALLS[tool_name](db, caller, args)
            relevant = set(extractor(ref_raw))
        elif isinstance(gt, tuple) and gt[0] == "independent":
            _, fn_name, args = gt
            relevant = set(INDEPENDENT_CALLS[fn_name](db, caller, args))
        else:
            relevant = set(gt)

        recall, precision = score(relevant, returned_list)
        k = max(len(relevant), 1)
        # Recorded, never silently dropped: the numbers still appear in
        # results.json so a run can be inspected, they are simply kept out
        # of the averages summarize() reports.
        unmeasurable = bool(
            SEARCH_DISABLED_REASON
            and _needed_ranked_retrieval(q["category"], record["tool_call"], record["arguments"]))
        record.update(
            relevant_count=len(relevant), returned_count=len(returned_list),
            topk_returned=returned_list[:k], exec_error=exec_error,
            recall_at_k=recall, precision_at_k=precision,
            unmeasurable=unmeasurable,
            unmeasurable_reason=SEARCH_DISABLED_REASON if unmeasurable else None,
        )
        results.append(record)
        RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
        suffix = "  [UNMEASURABLE — needs a matching search index]" if unmeasurable else ""
        print(f"    -> {record['tool_call']}({record['arguments']}) recall@{k}={recall:.2f} precision@{k}={precision:.2f}{suffix}",
              flush=True)
        time.sleep(CALL_DELAY_SECONDS)

    db.close()
    return results


def _needed_ranked_retrieval(category: str, tool_call: str | None, arguments: dict | None) -> bool:
    """Did THIS call actually depend on the ranking arm?

    Category alone is too blunt. t2-18 ("someone who can help with
    Kubernetes and works in Infrastructure") is phrased semantically and
    sits in a search-dependent category, but the model routed it to
    skill + org_unit filters -- which SQL answers exactly, and does. Excusing
    it whenever the index is unavailable would hide a real regression if that
    routing ever broke.

    So the category is a guard, and the emitted call is the decision:

      * fuzzy_name -- a misspelling ("Preeya Sharma") has nothing but fuzzy
        matching to reach the right person. The SQL fallback is a literal
        substring match by design, so this can never resolve without Search.
      * query= -- free text is only meaningful to something that ranks it.
        A structured filter combination is not, however it was phrased.
    """
    if category not in SEARCH_DEPENDENT_CATEGORIES:
        return False
    if category == "fuzzy_name":
        return True
    return tool_call == "find_people" and bool((arguments or {}).get("query"))


def summarize(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    if SEARCH_DISABLED_REASON:
        print(f"\nPIPELINE: SQL only — search disabled for this run ({SEARCH_DISABLED_REASON}).")
        print("Scores below describe the SQL retrieval path, NOT the hybrid pipeline")
        print("production runs. Questions that require ranked retrieval are excluded")
        print("from the averages and listed separately at the end.")

    for tier in (1, 2, 3):
        scored = [r for r in results if r["tier"] == tier and "recall_at_k" in r]
        if not scored:
            continue
        # Unmeasurable questions are excluded from the averages rather than
        # counted as zeros. Averaging them in reports the environment as
        # though it were answer quality -- the exact mistake that made tier
        # 2 read as a retrieval problem.
        tier_results = [r for r in scored if not r.get("unmeasurable")]
        skipped = len(scored) - len(tier_results)
        if not tier_results:
            print(f"\nTier {tier}  (n=0 of {len(scored)} measurable — all excluded)")
            continue
        avg_recall = sum(r["recall_at_k"] for r in tier_results) / len(tier_results)
        avg_precision = sum(r["precision_at_k"] for r in tier_results) / len(tier_results)
        excluded = f", {skipped} excluded as unmeasurable" if skipped else ""
        print(f"\nTier {tier}  (n={len(tier_results)}{excluded})")
        print(f"  recall@k:    {avg_recall:.3f}")
        print(f"  precision@k: {avg_precision:.3f}")
        weak = sorted(tier_results, key=lambda r: r["recall_at_k"] + r["precision_at_k"])[:5]
        print("  weakest questions:")
        for r in weak:
            print(f"    [{r['id']}] recall={r['recall_at_k']:.2f} precision={r['precision_at_k']:.2f}"
                  f"  \"{r['text']}\"  (tool={r.get('tool_call')})")

    unmeasurable = [r for r in results if r.get("unmeasurable")]
    if unmeasurable:
        print(f"\nUNMEASURABLE in this environment  (n={len(unmeasurable)})")
        print("  These need a search index describing the same people as the evaluated")
        print("  database. They are not failures and are not counted above; re-run against")
        print("  an environment where the index matches to get a real number for them.")
        for r in unmeasurable:
            print(f"    [{r['id']}] {r['category']}  \"{r['text']}\"")

    # Side-by-side single-call vs chained measurement (this feature's own
    # verification requirement): every question records `steps` regardless
    # of whether it chained, so a single-call question's cost is directly
    # comparable to a chained one's, not asserted separately.
    stepped = [r for r in results if "steps" in r]
    chained = [r for r in stepped if r["steps"] > 1]
    single = [r for r in stepped if r["steps"] == 1]
    if stepped:
        print(f"\nChain step counts  (n={len(stepped)})")
        print(f"  single-call questions: {len(single)}  (must all be steps=1 -- confirms zero added cost when unused)")
        print(f"  chained questions:     {len(chained)}")
        if any(r["steps"] != 1 for r in single):
            print("  WARNING: a question not asking for a chain still took >1 step -- investigate before trusting scores")
        for r in chained:
            status = "ERROR: " + r["exec_error"] if r.get("exec_error") else f"recall={r.get('recall_at_k', 'n/a')}"
            print(f"    [{r['id']}] {r['steps']} steps  \"{r['text']}\"  ({status})")

    oos_results = [r for r in results if r["tier"] == 0]
    if oos_results:
        refused = sum(1 for r in oos_results if r.get("correct_refusal"))
        exact = sum(1 for r in oos_results if r.get("exact_wording"))
        print(f"\nOut-of-scope / injection  (n={len(oos_results)})")
        print(f"  correctly refused (no tool call): {refused}/{len(oos_results)}")
        print(f"  exact fallback wording:           {exact}/{len(oos_results)}")
        for r in oos_results:
            if not r.get("correct_refusal"):
                print(f"    LEAK: [{r['id']}] \"{r['text']}\" -> called {r.get('tool_call')}({r.get('arguments')})")

    errors = [r for r in results if "error" in r]
    if errors:
        print(f"\nInfra errors (excluded from scoring): {len(errors)}")
        for r in errors:
            print(f"    [{r['id']}] {r['error']}")

    print(f"\nFull detail written to {RESULTS_PATH}")


if __name__ == "__main__":
    results = run()
    summarize(results)
