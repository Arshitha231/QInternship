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
    OUT_OF_SCOPE_MESSAGE,
    ResolvedToolCall,
    AssistantTurn,
    _get_openai_client,
    _is_content_filter_block,
    build_messages,
    TOOLS,
    OPENAI_CHAT_DEPLOYMENT,
    execute_tool_call,
)

from golden_set import ALL_QUESTIONS  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
CALL_DELAY_SECONDS = 8.0  # pacing between real LLM calls, see module docstring
MAX_RETRIES = 5


def resolve_intent_strict(message: str) -> AssistantTurn:
    """Same logic as app.tool_calling._real_resolve, minus the silent
    mock fallback — retries on OpenAIError instead, so this eval measures
    the real model, not a rate-limit-triggered heuristic standing in for it.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            client = _get_openai_client()
            response = client.chat.completions.create(
                model=OPENAI_CHAT_DEPLOYMENT,
                messages=build_messages(message),
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
                return AssistantTurn(tool_call=ResolvedToolCall(name=call.function.name, arguments=arguments))
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
        if turn.tool_call is not None:
            try:
                raw_result = execute_tool_call(db, caller, turn.tool_call)
            except (TypeError, ValueError, KeyError) as exc:
                exec_error = str(exc)

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
        record.update(
            relevant_count=len(relevant), returned_count=len(returned_list),
            topk_returned=returned_list[:k], exec_error=exec_error,
            recall_at_k=recall, precision_at_k=precision,
        )
        results.append(record)
        RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"    -> {record['tool_call']}({record['arguments']}) recall@{k}={recall:.2f} precision@{k}={precision:.2f}",
              flush=True)
        time.sleep(CALL_DELAY_SECONDS)

    db.close()
    return results


def summarize(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    for tier in (1, 2, 3):
        tier_results = [r for r in results if r["tier"] == tier and "recall_at_k" in r]
        if not tier_results:
            continue
        avg_recall = sum(r["recall_at_k"] for r in tier_results) / len(tier_results)
        avg_precision = sum(r["precision_at_k"] for r in tier_results) / len(tier_results)
        print(f"\nTier {tier}  (n={len(tier_results)})")
        print(f"  recall@k:    {avg_recall:.3f}")
        print(f"  precision@k: {avg_precision:.3f}")
        weak = sorted(tier_results, key=lambda r: r["recall_at_k"] + r["precision_at_k"])[:5]
        print("  weakest questions:")
        for r in weak:
            print(f"    [{r['id']}] recall={r['recall_at_k']:.2f} precision={r['precision_at_k']:.2f}"
                  f"  \"{r['text']}\"  (tool={r.get('tool_call')})")

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
