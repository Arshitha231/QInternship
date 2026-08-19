"""app.chain_budgets: per-plan-class chain budgets, and the startup drift
check that keeps a declared budget from ever exceeding the absolute
ceiling on any axis.
"""
import pytest

from app.chain_budgets import (
    CEILING,
    DEFAULT_PLAN_CLASS,
    PLAN_CLASS_BUDGETS,
    ChainBudget,
    assert_chain_budgets_within_ceiling,
    budget_for,
)


def test_the_real_configured_budgets_pass_the_drift_check():
    # Not a tautology -- this is the actual PLAN_CLASS_BUDGETS dict the
    # app boots with, the same one main.py's lifespan checks. A future
    # edit that raises a declared budget past CEILING breaks this test
    # before it ever reaches a deploy.
    assert_chain_budgets_within_ceiling()


def test_budget_for_returns_the_declared_budget():
    assert budget_for(DEFAULT_PLAN_CLASS) == PLAN_CLASS_BUDGETS[DEFAULT_PLAN_CLASS]


def test_budget_for_raises_clearly_for_an_undeclared_plan_class():
    with pytest.raises(KeyError, match="No chain budget declared"):
        budget_for("some_plan_class_nobody_registered")


def test_drift_check_fails_when_steps_exceeds_the_ceiling(monkeypatch):
    monkeypatch.setitem(
        PLAN_CLASS_BUDGETS, "test_over_steps",
        ChainBudget(steps=CEILING.steps + 1, max_records=1, max_wall_clock_ms=1))
    with pytest.raises(RuntimeError, match="steps"):
        assert_chain_budgets_within_ceiling()


def test_drift_check_fails_when_max_records_exceeds_the_ceiling(monkeypatch):
    monkeypatch.setitem(
        PLAN_CLASS_BUDGETS, "test_over_records",
        ChainBudget(steps=1, max_records=CEILING.max_records + 1, max_wall_clock_ms=1))
    with pytest.raises(RuntimeError, match="max_records"):
        assert_chain_budgets_within_ceiling()


def test_drift_check_fails_when_wall_clock_exceeds_the_ceiling(monkeypatch):
    monkeypatch.setitem(
        PLAN_CLASS_BUDGETS, "test_over_wall_clock",
        ChainBudget(steps=1, max_records=1, max_wall_clock_ms=CEILING.max_wall_clock_ms + 1))
    with pytest.raises(RuntimeError, match="max_wall_clock_ms"):
        assert_chain_budgets_within_ceiling()


def test_drift_check_names_every_axis_that_is_over_at_once(monkeypatch):
    # A single misconfigured plan class shouldn't need three separate
    # deploy-fail-fix cycles to fully diagnose.
    monkeypatch.setitem(
        PLAN_CLASS_BUDGETS, "test_over_everything",
        ChainBudget(
            steps=CEILING.steps + 1, max_records=CEILING.max_records + 1,
            max_wall_clock_ms=CEILING.max_wall_clock_ms + 1))
    with pytest.raises(RuntimeError) as exc_info:
        assert_chain_budgets_within_ceiling()
    message = str(exc_info.value)
    assert "steps" in message and "max_records" in message and "max_wall_clock_ms" in message


def test_a_budget_within_the_ceiling_on_every_axis_never_raises(monkeypatch):
    monkeypatch.setitem(
        PLAN_CLASS_BUDGETS, "test_within_ceiling",
        ChainBudget(steps=CEILING.steps, max_records=CEILING.max_records, max_wall_clock_ms=CEILING.max_wall_clock_ms))
    assert_chain_budgets_within_ceiling()  # exactly at the ceiling is allowed, not just under it
