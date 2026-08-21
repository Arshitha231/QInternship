"""Derive a structured PeopleQuery from plain free text, using only values
that actually exist in this database.

Why this exists. "engineers in Austin" is a structured filter request
wearing plain English: no question mark, no described problem, and no
deterministic route, so app.unified_search's gate sends it down the direct
path -- where find_people's SQL fallback only ever matches literal
substrings of a NAME. Zero results, while 27 Austin engineers sit in the
table. The identical text with a "?" appended has always worked, which is
RC5 (ARCHITECTURE_2.md §2) resurfacing in a different spot: punctuation
still decides whether some queries get answered.

Why not just call the model on an empty result. Because three tests say
not to, deliberately -- "model must not be called" for ordinary free text
(tests/test_unified_search.py). Escalating every zero-result search to the
assistant is a real cost decision this codebase already made and rejected.
This module answers the same queries for no tokens and ~5ms instead.

The rule this follows, from ARCHITECTURE_2.md §3 decision 3: "Widening a
regex to catch a near-miss is the wrong fix." So there are no query-shape
patterns here at all -- nothing matches "<title> in <city>" or any other
sentence template. Every filter this produces comes from finding a real
office / org unit / skill / job-title word from THIS database inside the
text. A token that resolves to nothing produces nothing, and a text that
resolves to nothing at all returns None, which leaves the caller's flat
empty result exactly as it was. Same "return None rather than claim a
route you can't parse" contract the deterministic router already follows.

Scope note, which is what keeps this safe: app.unified_search only calls
this AFTER the direct path has already come back empty. It can therefore
never change the answer to a query that currently works -- only supply one
where there is currently nothing.

This module is now a thin adapter over app.query_entities.parse: the
vocabulary scan (offices, org units, skills, job-title n-grams, seniority
bands, and the non-overlapping-span rule that lets a real multi-word title
win its words before a generic one gets a turn) all live there, shared with
the ranked/chip path (SEARCH_RANKING_PROPOSAL.md steps 2-4). What's left
here is purely the translation from typed entities to this module's own
PeopleQuery shape: office/org_unit entities become required Filters the
same way they always did, and seniority entities are dropped here -- they
only matter to the ranked path, which reads them from query_entities.parse
directly rather than through this adapter.

role and skill(s) are different once both are present. A lone role or a
lone skill still becomes one plain `filters` entry, same shape as always
(op="contains" for one skill, op="in" for two-plus -- app/query_compiler.py's
skills branch ORs every value in an "in" list regardless of op, so
multi-skill text widens the candidate pool rather than narrowing it to
zero). But a role named ALONGSIDE skill(s) is two separate PREFERRED
criteria (SEARCH_RANKING_PROPOSAL.md design decision 1), not a hard AND
that can go to zero the moment nobody holding the skill(s) also carries
that exact title -- "senior data engineer with react, java" must not
require a Data Engineer who also holds React or Java when the fix's whole
point is that no such person may exist. So role+skill(s) together compile
to `filter_groups`: a UNION of "matches the role" and "holds a requested
skill", widening the pool for app.people_ranking (step 4) to score and
reorder afterward instead of narrowing it structurally.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import query_entities
from app.query_plan import Filter, PeopleQuery


def plan_from_interpretation(
    interpretation: query_entities.Interpretation, *, select_fields: list[str], limit: int,
) -> PeopleQuery | None:
    """A PeopleQuery built from an already-parsed Interpretation, or None
    when it names no filterable entity.

    Split out from plan_from_text so a caller that also needs the
    Interpretation itself (app.unified_search's `interpretation` response
    payload, SEARCH_RANKING_PROPOSAL.md step 3) can call query_entities.parse
    once and reuse the same result here, instead of parsing the text twice.
    """
    filters: list[Filter] = []
    skills: list[str] = []
    role_value: str | None = None
    for entity in interpretation.entities:
        if entity.label == "office":
            filters.append(Filter(field="office", op="contains", value=entity.value))
        elif entity.label == "org_unit":
            filters.append(Filter(field="org_unit", op="eq", value=entity.value))
        elif entity.label == "role":
            role_value = entity.value
        elif entity.label == "skill":
            skills.append(entity.value)
        # seniority entities carry no Filter of their own here -- they only
        # feed the ranking layer and the chip payload (steps 3-4).

    skill_filter: Filter | None = None
    if len(skills) == 1:
        skill_filter = Filter(field="skills", op="contains", value=skills[0])
    elif skills:
        skill_filter = Filter(field="skills", op="in", value=skills)
    role_filter = Filter(field="job_title", op="contains", value=role_value) if role_value else None

    if role_filter is not None and skill_filter is not None:
        # Both preferred criteria present -- see the module docstring for
        # why this is a UNION (filter_groups), not an AND. office/org_unit
        # (already collected into `filters` above) stay a required,
        # unconditional AND constraint layered on top of this OR.
        return PeopleQuery(
            select=list(select_fields), filters=filters,
            filter_groups=[[role_filter], [skill_filter]], limit=limit,
        )

    # Single-criterion case, unchanged: whichever of role/skill(s) is
    # actually present (if either) joins `filters` as a plain AND term,
    # same shape this module has always produced.
    if role_filter is not None:
        filters.append(role_filter)
    elif skill_filter is not None:
        filters.append(skill_filter)

    if not filters:
        return None
    return PeopleQuery(select=list(select_fields), filters=filters, limit=limit)


def plan_from_text(db: Session, text: str, *, select_fields: list[str], limit: int) -> PeopleQuery | None:
    """A PeopleQuery built from whatever real vocabulary `text` names, or
    None when it names none.

    Returns a plan, not results -- app.people.search_people_by_plan runs it
    through the same validate -> snap -> enforce -> compile pipeline every
    other retrieval uses, so nothing here needs to know what a caller is or
    which fields they may see. This module is inert on its own, exactly
    like app/query_plan.py's docstring says a plan should be.
    """
    if not text.strip():
        return None
    interpretation = query_entities.parse(db, text)
    return plan_from_interpretation(interpretation, select_fields=select_fields, limit=limit)
