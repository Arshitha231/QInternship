"""The query plan: the one shape a routed request takes when it's asking for
a set of people via filters, rather than invoking a named operation.
ARCHITECTURE_2.md §7 (Phase 3, Round 1 of the plan).

Every dangerous slot is generated from app/registry.py at import time, not
hand-typed a second time here -- Field is a Literal built from
REGISTRY.keys(), so a plan literally cannot name a field that doesn't exist:
the type itself won't permit it, before any policy check ever runs.

Schema only, in this file. Nothing here executes a query, checks a role, or
knows what a caller is -- that's app/policy.py (enforce) and, in Round 2,
app/query_compiler.py. A PeopleQuery is a proposal; it's inert until a
PolicyDecision says what's allowed to happen with it.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.registry import REGISTRY

# Literal[tuple(...)] is the same as Literal[*(...)] at runtime -- Python's
# subscript syntax passes a tuple either way -- so this really is generated
# from REGISTRY.keys(), not a second copy of the field list that could drift.
Field = Literal[tuple(REGISTRY.keys())]  # type: ignore[valid-type]

Op = Literal["eq", "ne", "in", "contains"]


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Field
    op: Op
    value: str | list[str] | bool


class PeopleQuery(BaseModel):
    """A structured retrieval request -- select/filter/sort/limit only.
    Deliberately cannot express free-text ranking (that stays Azure Search's
    job, per ARCHITECTURE_2.md's non-goal "moving structured filters into
    Search... reverted by mode 1") or a recursive/multi-step computation
    (that's NamedOperation, below).

    `filters` is always AND'd -- unchanged, and still the whole story for a
    plan with no `filter_groups`, so the common case stays on the flatter,
    easier-to-generate shape. `filter_groups` is bounded DNF for genuine
    cross-field OR ("knows Kubernetes OR works in Cloud Ops") that `filters`
    structurally cannot express (every entry in `filters` is AND'd, and
    `op="in"` only ORs multiple values of the SAME field): a list of groups,
    each group itself a flat list of Filters AND'd together, with the groups
    OR'd against each other. Exactly one level -- a group is `list[Filter]`,
    never `list[list[Filter]]` -- no recursive/arbitrary boolean expression
    tree; that's deliberately more than this needs. `filters` and
    `filter_groups` compose by AND when both are given: `filters` are a
    constraint every match must satisfy regardless of which group it came
    from, `filter_groups` (if present) is an additional OR-of-AND condition
    layered on top. A group must be non-empty -- an empty group would mean
    "OR unconditionally true," silently neutralizing every other group's
    restriction, so app.vocabulary.validate() rejects that shape rather than
    accepting a plan whose OR always fires.
    """

    model_config = ConfigDict(extra="forbid")

    select: list[Field]
    filters: list[Filter] = []
    filter_groups: list[list[Filter]] = []
    order_by: Field | None = None
    # Ignored when order_by is None. Every orderable field so far
    # (full_name, ...) only ever needed ascending -- hire_date is the first
    # with a real descending use ("who joined most recently"), so this
    # stays a plain default rather than something every caller must set.
    order_dir: Literal["asc", "desc"] = "asc"
    limit: int | None = None


class NamedOperation(BaseModel):
    """Operations that are computations, not retrievals -- a flat filter
    object can't express a recursive CTE (get_org_chain) or a multi-factor
    ranking (find_mentor). semantic_help/continuity_exposure are listed per
    ARCHITECTURE_2.md §7's schema even though nothing dispatches to them yet
    (Phase 6 / Phase 4 respectively) -- an unreachable enum value is inert;
    a schema that would need widening twice down the line isn't.
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["find_mentor", "get_org_chain", "semantic_help", "continuity_exposure"]
    params: dict
