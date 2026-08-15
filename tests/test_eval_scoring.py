"""Unit coverage for eval/run_golden_eval.py's score(), independent of any
live model call. ARCHITECTURE_2.md's RC6/§14: a fixed top-3 cap made any
question with more than 3 correct answers structurally incapable of
scoring above 3/|relevant| -- t1-04 (14/14 correct direct reports)
scored 0.21. score() now uses k = |relevant| instead of a fixed 3.

eval/ is a script directory, not an installed package (matches how
eval/retest_one.py itself imports from eval/run_golden_eval.py -- both
rely on Python auto-adding the script's own directory to sys.path when
run directly). Added here explicitly since pytest doesn't do that for us.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

from run_golden_eval import score  # noqa: E402


def test_large_ground_truth_all_correct_scores_perfectly():
    # The exact t1-04 shape: 14 correct direct reports, all 14 returned.
    # Old fixed-top-3 metric: hit capped at 3, 3/14 = 0.214. New: 14/14 = 1.0.
    relevant = {f"id-{i}" for i in range(14)}
    returned = [f"id-{i}" for i in range(14)]
    recall, precision = score(relevant, returned)
    assert recall == 1.0
    assert precision == 1.0


def test_small_ground_truth_unchanged_from_top3_behavior():
    # k = max(len(relevant), 1) = 1 here -- same result the old fixed-3 cap
    # would have given for a single-answer question, so this is a
    # regression check, not new behavior.
    relevant = {"the-one"}
    returned = ["the-one"]
    assert score(relevant, returned) == (1.0, 1.0)


def test_empty_relevant_and_empty_returned_is_correct_refusal():
    assert score(set(), []) == (1.0, 1.0)


def test_empty_relevant_with_a_false_positive_scores_zero():
    assert score(set(), ["unexpected-id"]) == (0.0, 0.0)


def test_partial_match_within_k():
    # 5 relevant, only 2 of the top-5 returned are actually correct.
    relevant = {"a", "b", "c", "d", "e"}
    returned = ["a", "x", "b", "y", "z"]
    recall, precision = score(relevant, returned)
    assert recall == 2 / 5
    assert precision == 2 / 5


def test_extra_results_beyond_k_do_not_penalize_precision():
    # This is a deliberate property of recall@k/precision@k, not a bug:
    # the metric asks whether the top |relevant| results are the right
    # ones, so items past position k (here k=2) are outside its window.
    relevant = {"a", "b"}
    returned = ["a", "b", "c", "d", "e"]
    recall, precision = score(relevant, returned)
    assert recall == 1.0
    assert precision == 1.0


def test_wrong_top_results_score_zero_even_with_correct_ones_further_down():
    # Ranking quality matters: the right answers exist in `returned` but
    # not within the first k slots, so this should score low, not just
    # "did the right ids appear anywhere."
    relevant = {"a", "b"}
    returned = ["z", "y", "a", "b"]
    recall, precision = score(relevant, returned)
    assert recall == 0.0
    assert precision == 0.0
