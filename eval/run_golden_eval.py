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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAIError  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.directory_tools import find_mentor, skill_gap, skill_scarcity  # noqa: E402
from app.people import find_people  # noqa: E402
from app.tool_calling import (  # noqa: E402
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
# matters -- it's what "top 3" means for recall@3/precision@3.
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

DYNAMIC_CALLS = {
    "find_mentor": lambda db, caller, args: find_mentor(db, caller, skill=args["skill"], caller_id=caller.id),
    "skill_gap": lambda db, caller, args: skill_gap(db, caller, required_skills=args["required_skills"]),
    "skill_scarcity": lambda db, caller, args: skill_scarcity(db, caller, **args),
    "find_people": lambda db, caller, args: find_people(db, caller, **args),
}


def score(relevant: set, returned: list) -> tuple[float, float]:
    """recall@3, precision@3 over an ordered `returned` list against the
    full `relevant` ground-truth set. Empty-relevant is a valid case (the
    correct answer is "nothing") and scores 1.0/1.0 only if nothing came
    back either -- any returned item is then a straightforward false
    positive.
    """
    top3 = returned[:3]
    hit = len(relevant & set(top3))
    if not relevant:
        return (1.0, 1.0) if not top3 else (0.0, 0.0)
    recall = hit / len(relevant)
    precision = hit / len(top3) if top3 else 0.0
    return recall, precision


def run() -> list[dict]:
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
        else:
            relevant = set(gt)

        recall, precision = score(relevant, returned_list)
        record.update(
            relevant_count=len(relevant), returned_count=len(returned_list),
            top3_returned=returned_list[:3], exec_error=exec_error,
            recall_at_3=recall, precision_at_3=precision,
        )
        results.append(record)
        RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"    -> {record['tool_call']}({record['arguments']}) recall@3={recall:.2f} precision@3={precision:.2f}",
              flush=True)
        time.sleep(CALL_DELAY_SECONDS)

    db.close()
    return results


def summarize(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    for tier in (1, 2, 3):
        tier_results = [r for r in results if r["tier"] == tier and "recall_at_3" in r]
        if not tier_results:
            continue
        avg_recall = sum(r["recall_at_3"] for r in tier_results) / len(tier_results)
        avg_precision = sum(r["precision_at_3"] for r in tier_results) / len(tier_results)
        print(f"\nTier {tier}  (n={len(tier_results)})")
        print(f"  recall@3:    {avg_recall:.3f}")
        print(f"  precision@3: {avg_precision:.3f}")
        weak = sorted(tier_results, key=lambda r: r["recall_at_3"] + r["precision_at_3"])[:5]
        print("  weakest questions:")
        for r in weak:
            print(f"    [{r['id']}] recall={r['recall_at_3']:.2f} precision={r['precision_at_3']:.2f}"
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
