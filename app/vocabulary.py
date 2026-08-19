"""Validator and vocabulary snapper (ARCHITECTURE_2.md §9). Two different
jobs, deliberately not merged into one function:

  * validate() rejects on STRUCTURE -- the plan itself is malformed
    (unknown field, illegal operator for that field, wrong value type, or a
    field that's registered but unlabelled and therefore can never appear
    in any plan regardless of role -- see app/registry.py's `sensitivity =
    None` case). Structural problems are reject, never guess.

  * snap() corrects on VALUES -- a real, legal field/operator combination
    whose value doesn't exactly match what's in the database
    ("Cloud Infrastructure" -> "Infrastructure", ARCHITECTURE_2.md's own
    RC4 example). Value problems are snap, never reject outright; an
    unresolvable value is reported, not silently dropped.

Role plays no part in either function here. Unlabelled-field rejection in
validate() is role-independent by construction (personal_mobile can never
appear in a plan for ANY role -- app/registry.py is explicit that it's
only ever reachable through the separate ABAC post-retrieval mechanism).
Per-role visibility for labelled fields is enforce()'s job (app/policy.py),
not this module's -- keeping "is this plan well-formed" and "is this
caller allowed to ask it" as two separate questions, not one blurred check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Office, OrgUnit, Skill
from app.models.enums import SkillCategory
from app.org_chart import FUZZY_MATCH_THRESHOLD
from app.query_plan import PeopleQuery
from app.registry import REGISTRY

# Fields whose filter values are free text matched against a real, finite
# DB vocabulary -- worth snapping. skill/level are structural (a fixed
# enum), not snapped; a bad level is a validate() rejection, not a fuzzy
# guess at which level was meant.
SNAPPABLE_FIELDS: frozenset[str] = frozenset({"org_unit", "office", "skills", "languages"})


# ---------------------------------------------------------------------------
# validate() -- structural only
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _scalar_type_matches(value: object, type_: str) -> bool:
    if type_ == "str":
        return isinstance(value, str)
    if type_ == "bool":
        return isinstance(value, bool)
    # "int" / "date": no filterable registry field has either type today
    # (Filter.value's own type -- str | list[str] | bool -- can't even
    # carry a raw int), but this stays correct rather than silently
    # accepting on unknown types if the registry ever grows one.
    if type_ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "date":
        return isinstance(value, str)
    return False


def _value_type_matches(op: str, value: object, type_: str) -> bool:
    """Legal value shape depends on the OPERATOR, not just the field's own
    declared type -- a list[str] field's element type is str, so `contains`
    ("does skills include this one name") takes a plain str, while `in`
    ("does skills include any of these") takes a list[str]. A scalar field
    follows the same rule: `eq`/`ne`/`contains` take one value of its type,
    `in` takes a list of them. Conflating "the field's type" with "the
    value's type" 1:1 would wrongly reject the ordinary case of filtering a
    list-valued field by a single element.
    """
    element_type = "str" if type_ == "list[str]" else type_
    if op == "in":
        return isinstance(value, list) and all(_scalar_type_matches(v, element_type) for v in value)
    return _scalar_type_matches(value, element_type)


def _check_field(name: str, errors: list[str], *, context: str) -> bool:
    """Shared unlisted/unlabelled check for select/filter/order_by fields.
    Returns True if the field is real and labelled -- safe to check
    further (operator, value type) -- False if already rejected."""
    spec = REGISTRY.get(name)
    if spec is None:
        errors.append(f"unknown {context} field '{name}'")
        return False
    if spec.sensitivity is None:
        errors.append(f"{context} field '{name}' is unlabelled -- can never appear in a plan")
        return False
    return True


def _validate_filter(filt: Filter, errors: list[str], *, context: str) -> None:
    if not _check_field(filt.field, errors, context=context):
        return
    spec = REGISTRY[filt.field]
    if not spec.filterable:
        errors.append(f"field '{filt.field}' is not filterable")
        return
    if filt.op not in spec.ops:
        errors.append(f"operator '{filt.op}' not legal for field '{filt.field}' (allowed: {sorted(spec.ops)})")
    elif not _value_type_matches(filt.op, filt.value, spec.type):
        errors.append(f"value for '{filt.field}' does not match expected type '{spec.type}' for op '{filt.op}'")


def validate(plan: PeopleQuery) -> ValidationResult:
    errors: list[str] = []

    for f in plan.select:
        _check_field(f, errors, context="select")

    for filt in plan.filters:
        _validate_filter(filt, errors, context="filter")

    # Bounded DNF (app/query_plan.py's PeopleQuery docstring): each group is
    # its own AND-list, checked with the identical per-filter rule as the
    # flat `filters` case above -- a group is structurally just `filters`
    # with a different combinator, not a different vocabulary. An empty
    # group is rejected here rather than silently compiling to "OR true",
    # which would neutralize every other group's restriction.
    for group in plan.filter_groups:
        if not group:
            errors.append("filter_groups group must contain at least one filter")
            continue
        for filt in group:
            _validate_filter(filt, errors, context="filter_groups")

    if plan.order_by is not None and _check_field(plan.order_by, errors, context="order_by"):
        spec = REGISTRY[plan.order_by]
        if not spec.filterable:
            errors.append(f"order_by field '{plan.order_by}' is not filterable/sortable")

    return ValidationResult(valid=not errors, errors=errors)


# ---------------------------------------------------------------------------
# snap() -- value correction against the real DB vocabulary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapNote:
    field: str
    original: str
    resolved: str | None  # None => unresolvable, reported not guessed


def _load_vocab(db: Session, field_name: str) -> list[str]:
    if field_name == "org_unit":
        return list(db.execute(select(OrgUnit.name)).scalars().all())
    if field_name == "office":
        names = db.execute(select(Office.name)).scalars().all()
        cities = db.execute(select(Office.city)).scalars().all()
        return list(names) + list(cities)
    if field_name == "skills":
        return list(db.execute(
            select(Skill.name).where(Skill.category != SkillCategory.language)
        ).scalars().all())
    if field_name == "languages":
        return list(db.execute(
            select(Skill.name).where(Skill.category == SkillCategory.language)
        ).scalars().all())
    return []


def _snap_one(value: str, vocab: list[str]) -> str | None:
    """Exact -> case-insensitive -> rapidfuzz above threshold -> None. Same
    three-tier shape as app.org_chart.resolve_person_name, same threshold
    constant -- not a second tolerance invented independently for a
    different vocabulary."""
    if value in vocab:
        return value
    lowered = value.lower()
    for candidate in vocab:
        if candidate.lower() == lowered:
            return candidate
    if not vocab:
        return None
    match = process.extractOne(value, vocab, scorer=fuzz.WRatio, score_cutoff=FUZZY_MATCH_THRESHOLD)
    return match[0] if match else None


def _snap_filters(db: Session, filters: list[Filter]) -> tuple[list[Filter], list[SnapNote]]:
    notes: list[SnapNote] = []
    new_filters = []
    for filt in filters:
        if filt.field not in SNAPPABLE_FIELDS:
            new_filters.append(filt)
            continue
        vocab = _load_vocab(db, filt.field)
        if isinstance(filt.value, list):
            resolved_list: list[str] = []
            for item in filt.value:
                resolved = _snap_one(item, vocab)
                notes.append(SnapNote(field=filt.field, original=item, resolved=resolved))
                resolved_list.append(resolved if resolved is not None else item)
            new_filters.append(filt.model_copy(update={"value": resolved_list}))
        elif isinstance(filt.value, str):
            resolved = _snap_one(filt.value, vocab)
            notes.append(SnapNote(field=filt.field, original=filt.value, resolved=resolved))
            new_filters.append(filt.model_copy(update={"value": resolved if resolved is not None else filt.value}))
        else:
            # No SNAPPABLE_FIELDS entry has a bool-typed FieldSpec today
            # (validate() would already reject that combination structurally
            # before snap() ever runs it) -- passed through unchanged rather
            # than assumed impossible, so this stays correct if that changes.
            new_filters.append(filt)
    return new_filters, notes


def snap(db: Session, plan: PeopleQuery) -> tuple[PeopleQuery, list[SnapNote]]:
    new_filters, notes = _snap_filters(db, plan.filters)

    new_groups: list[list[Filter]] = []
    for group in plan.filter_groups:
        new_group, group_notes = _snap_filters(db, group)
        new_groups.append(new_group)
        notes.extend(group_notes)

    return plan.model_copy(update={"filters": new_filters, "filter_groups": new_groups}), notes


# ---------------------------------------------------------------------------
# snap_tool_arguments() -- the same value correction, for find_people's
# NAMED arguments rather than a PeopleQuery's filter list.
# ---------------------------------------------------------------------------

# find_people kwarg -> the registry field whose vocabulary governs it.
#
# `language` is deliberately absent. Its miss path is already handled, and
# handled better: app.people.find_related_language_speakers offers speakers
# of a linguistically related language and says explicitly that it is a
# related suggestion, never a substitution. Snapping "Telugu" to whichever
# seeded language scores highest would replace a careful, honest fallback
# with a silent guess.
#
# `level` is absent for the reason SNAPPABLE_FIELDS gives: it is a fixed
# enum, so a bad level is a rejection, not a fuzzy guess at which one was
# meant.
TOOL_ARGUMENT_VOCABULARIES: dict[str, str] = {
    "org_unit": "org_unit",
    "office": "office",
    "skill": "skills",
}


def snap_tool_arguments(db: Session, arguments: dict) -> tuple[dict, list[SnapNote]]:
    """Correct free-text values in a find_people-shaped kwargs dict against
    the real database vocabulary.

    The gap this closes: snap() only ever ran on a PeopleQuery, so
    search_people got vocabulary correction and find_people -- the tool the
    router reaches for overwhelmingly more often -- did not. A model asked
    to guess exact strings from a 75-unit hierarchy it has never been shown
    emits plausible near-misses: "Cloud Infrastructure" for a department
    actually called "Infrastructure". find_people's org_unit resolution then
    returns nothing and it hard-empties, so a question with 7 correct
    answers returned 0 (golden eval t2-07).

    A value that is already exact is returned unchanged -- _snap_one's first
    tier is an exact match -- so this cannot alter a request that was
    already right. A value that resolves to nothing stays as it was and the
    caller still gets its honest empty result: "Quantum Computing" is not a
    seeded skill and does not snap to a real one, which is exactly the
    behaviour that keeps "do we have anyone who knows X" answerable with
    "no".
    """
    snapped = dict(arguments)
    notes: list[SnapNote] = []
    for arg_name, field_name in TOOL_ARGUMENT_VOCABULARIES.items():
        value = arguments.get(arg_name)
        if not isinstance(value, str) or not value.strip():
            continue
        resolved = _snap_one(value, _load_vocab(db, field_name))
        if resolved is not None and resolved != value:
            snapped[arg_name] = resolved
        notes.append(SnapNote(field=arg_name, original=value, resolved=resolved))
    return snapped, notes
