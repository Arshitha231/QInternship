"""Re-run one or more golden-set questions by id against the real pipeline
and merge results into results.json. Usage:
    python eval/retest_one.py t2-18
    python eval/retest_one.py t2-06 t2-08 t2-09 t2-10 t2-11 t2-12 t2-16
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.tool_calling import OUT_OF_SCOPE_MESSAGE, execute_tool_call  # noqa: E402

from golden_set import ALL_QUESTIONS  # noqa: E402
from run_golden_eval import CALL_DELAY_SECONDS, DYNAMIC_CALLS, EXTRACTORS, resolve_intent_strict, score  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "results.json"

question_ids = sys.argv[1:]
db = SessionLocal()
results = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else []

for n, question_id in enumerate(question_ids):
    q = next(x for x in ALL_QUESTIONS if x["id"] == question_id)
    caller = q["caller"]

    print(f"({q['id']}, tier {q['tier']}, {q['category']}) {q['text']!r}", flush=True)
    turn = resolve_intent_strict(q["text"])
    record = {
        "id": q["id"], "tier": q["tier"], "category": q["category"], "text": q["text"],
        "caller": caller.name, "caller_role": caller.role,
        "tool_call": turn.tool_call.name if turn.tool_call else None,
        "arguments": turn.tool_call.arguments if turn.tool_call else None,
        "refusal_message": turn.message if turn.tool_call is None else None,
    }
    print(f"  -> tool_call: {record['tool_call']}({record['arguments']})", flush=True)

    if q["kind"] == "refusal":
        record["correct_refusal"] = turn.tool_call is None
        record["exact_wording"] = turn.message == OUT_OF_SCOPE_MESSAGE
        print(f"  -> correct_refusal={record['correct_refusal']} exact_wording={record['exact_wording']}", flush=True)
        results = [r for r in results if r["id"] != question_id]
        results.append(record)
        results.sort(key=lambda r: r["id"])
        RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
        print(f"  -> merged into {RESULTS_PATH}\n", flush=True)
        if n < len(question_ids) - 1:
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
    print(f"  -> relevant={len(relevant)} returned={len(returned_list)} "
          f"recall@3={recall:.2f} precision@3={precision:.2f}", flush=True)
    print(f"  -> top3_returned: {returned_list[:3]}", flush=True)

    results = [r for r in results if r["id"] != question_id]
    results.append(record)
    results.sort(key=lambda r: r["id"])
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"  -> merged into {RESULTS_PATH}\n", flush=True)

    if n < len(question_ids) - 1:
        time.sleep(CALL_DELAY_SECONDS)
