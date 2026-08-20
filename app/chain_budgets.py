"""Per-plan-class chain budgets (Conversational Assistant plan, "Steps is
the wrong single axis"). A chain's budget was one constant
(MAX_CHAIN_STEPS) applied everywhere; this replaces it with three
independently-exhausted axes, declared per plan class rather than chosen
by the model or hardcoded in execute_chain:

  steps             reasoning depth, and the cost of a confused plan
  max_records       exposure -- the dimension cross-query inference
                     actually runs along; a handful of broad queries can
                     return more than twenty narrow ones
  max_wall_clock_ms the user's time, and the blast radius of a slow
                     dependency

Whichever axis is exhausted first ends the chain -- execute_chain checks
all three every step, not just the step count. An absolute ceiling bounds
every plan class regardless of what it declares, so a misconfigured entry
can never produce an unbounded loop; assert_chain_budgets_within_ceiling()
(called at startup, app/main.py's _lifespan) fails loudly if a declared
budget ever exceeds it on any axis -- the same "a real deploy must say
what it means, checked before it ever serves a request" shape
assert_registry_covers_schema and assert_dev_auth_is_intentional already
hold elsewhere in this app.

This turns the constant into actual bounded-autonomy infrastructure: a
plan class that genuinely needs a longer chain (the doc's example --
"staff the Meridian engagement", whose step count can't be known in
advance) gets one via a registry entry here, not a code change to
execute_chain, and it still can't exceed CEILING however it's declared.
There is exactly one plan class today -- the general model-routed chain
reachable from /ask and /search's assisted mode -- so this is currently
one dict with one entry, not several; the shape is what makes a second
plan class a registry edit later instead of a new code path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChainBudget:
    steps: int
    max_records: int
    max_wall_clock_ms: int


# No plan class's declared budget may exceed this on any axis --
# enforced at startup, not discovered the first time someone's chain
# runs unexpectedly long. Headroom above the default plan class below is
# deliberate: it's where a future wider plan class (the "staff this
# engagement" case) has room to be declared without moving the ceiling.
CEILING = ChainBudget(steps=8, max_records=300, max_wall_clock_ms=20_000)

# The one plan class this system has today. Named rather than left
# implicit so a second plan class (a different registry key, a different
# ChainBudget) is additive -- nothing about execute_chain's shape assumes
# there is only ever one entry here.
DEFAULT_PLAN_CLASS = "assistant_chain"

PLAN_CLASS_BUDGETS: dict[str, ChainBudget] = {
    # steps=4, one above the 2-step shape every declared chain pattern
    # actually needs (resolve, then filter/fetch) -- the prior steps=3
    # gave a 2-step pattern exactly one step of margin, and reasoning_effort
    # ="minimal" routing is noisy enough that a step is sometimes spent on a
    # repeated call (e.g. find_people called twice before
    # get_people_with_projects) rather than genuine extra reasoning depth.
    # That left zero headroom for the intended pattern once the noise step
    # fired. steps=4 restores the same one-step margin the 2-step patterns
    # were designed around. max_records=100 is generous enough that an
    # ordinary 2-3 step chain over a normal-sized result set (a find_people
    # page is 50) doesn't spuriously truncate, while still bounding the
    # fan-out pattern cross-query inference actually runs along -- many
    # individually-small results accumulating past it. max_wall_clock_ms=8s
    # is comfortably above a real multi-step chain's measured latency (tens
    # to low hundreds of ms per step) with room for a slow step, not so high
    # it stops meaning anything.
    DEFAULT_PLAN_CLASS: ChainBudget(steps=4, max_records=100, max_wall_clock_ms=8_000),
}


def budget_for(plan_class: str) -> ChainBudget:
    """The one place execute_chain reads a budget from -- never a bare
    constant, and never a silent fallback here: a plan class absent from
    PLAN_CLASS_BUDGETS is a bug (a new plan class wired up without a
    budget declared for it), and this fails immediately and specifically
    rather than defaulting to something that would mask it."""
    try:
        return PLAN_CLASS_BUDGETS[plan_class]
    except KeyError:
        raise KeyError(
            f"No chain budget declared for plan class {plan_class!r}. "
            f"Add an entry to PLAN_CLASS_BUDGETS (app/chain_budgets.py)."
        ) from None


def assert_chain_budgets_within_ceiling() -> None:
    """Fail loudly at startup if a declared plan-class budget exceeds
    CEILING on any axis -- the same shape assert_registry_covers_schema
    holds for the field registry: a misconfiguration is caught before the
    app ever serves a request, not the first time someone's chain runs
    for 20 seconds and nobody knows why."""
    for plan_class, budget in PLAN_CLASS_BUDGETS.items():
        over = []
        if budget.steps > CEILING.steps:
            over.append(f"steps ({budget.steps} > {CEILING.steps})")
        if budget.max_records > CEILING.max_records:
            over.append(f"max_records ({budget.max_records} > {CEILING.max_records})")
        if budget.max_wall_clock_ms > CEILING.max_wall_clock_ms:
            over.append(f"max_wall_clock_ms ({budget.max_wall_clock_ms} > {CEILING.max_wall_clock_ms})")
        if over:
            raise RuntimeError(
                f"Chain budget for plan class {plan_class!r} exceeds the absolute ceiling on "
                f"{', '.join(over)}. Lower the declared budget in PLAN_CLASS_BUDGETS, or raise "
                f"CEILING deliberately (app/chain_budgets.py) if the ceiling itself is what's wrong."
            )
