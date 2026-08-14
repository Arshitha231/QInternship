"""Unified search: merges the structured /people path and the model-backed
/ask path behind one endpoint and one discriminated response shape, for the
Search+Ask UI merge. The frontend is a pure renderer — this module is what
actually decides "direct" vs "assisted", deterministically, with no model
call for the direct case.

That decision is intentionally narrow (surface shape only: a trailing
question mark, or an interrogative sentence opener), not a model call and
not fuzzy NLU — a missed classification just falls through to find_people's
own free-text hybrid search rather than erroring, so a false negative
degrades gracefully instead of failing outright.

Wraps find_people (app.people) and resolve_intent/execute_with_fallback
(app.tool_calling) rather than duplicating either's retrieval or
permission-filtering logic — every person that ends up in `results` or
`overview.citations` already passed through is_record_visible exactly once,
inside whichever of those two functions actually produced it. Nothing in
this module re-decides visibility; it only reshapes already-filtered data
for the frontend to render.
"""
from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.people import find_people
from app.permissions import ViewMode
from app.schemas import MentorCandidate, OrgChainNode, PersonDetail, PersonRef, PersonSummary, ProjectOwnerResult
from app.tool_calling import (
    OUT_OF_SCOPE_MESSAGE,
    TOOLS,
    ResolvedToolCall,
    execute_with_fallback,
    resolve_intent,
)

_QUESTION_MARKERS = re.compile(
    r"\?\s*$|^\s*(who|what|where|when|why|how|is|are|does|do|can|could|should|would)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    """The entire classifier. A trailing question mark, or an interrogative
    sentence opener. Everything else (a bare name, a skill, "engineering
    managers in Bangalore") stays structured and goes straight to
    find_people, which already handles free-text description queries via
    hybrid search with zero chat-model involvement — this rule only decides
    which of the two existing retrieval paths runs, it doesn't add a third.
    """
    return bool(_QUESTION_MARKERS.search(text.strip()))


# Short, static, per-tool descriptions of *why* a tool was selected. Not the
# model's own reasoning — nothing in this stack asks the model to explain
# itself, which would cost a second call — just a fixed, honest description
# of what each tool structurally does, taken from its own schema.
_TOOL_REASONS = {t["function"]["name"]: t["function"]["description"].split(".")[0] + "." for t in TOOLS}


def unified_search(
    db: Session, caller: AuthenticatedUser, *, q: str | None, filters: dict[str, Any],
    view_mode: ViewMode = "work",
) -> dict:
    text = (q or "").strip()
    clean_filters = {k: v for k, v in filters.items() if v is not None}

    if text and is_question(text):
        return _assisted(db, caller, text, view_mode)

    results = find_people(db, caller, query=text or None, view_mode=view_mode, **clean_filters)

    # Unique-identifier misses (an exact name/slack/email match attempted
    # and nothing came back, or came back but isn't visible to this caller)
    # stay a flat, honest empty result — no AI escalation. Only a
    # genuinely fuzzy attribute — skill — gets the "find similar"
    # treatment, and it's find_people's own semantic search doing the
    # finding (see execute_with_fallback), never a second, separate
    # AI system.
    if not results and clean_filters.get("skill"):
        tool_call = ResolvedToolCall(name="find_people", arguments=clean_filters)
        started = time.monotonic()
        raw = execute_with_fallback(db, caller, tool_call, f"(direct query, skill miss) {clean_filters['skill']}",
                                    view_mode)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return _build_assisted(db, caller, raw, elapsed_ms,
                               "No exact skill match — broadened to a semantic search across employee profiles.",
                               view_mode)

    return {"mode": "direct", "results": results}


def _assisted(
    db: Session, caller: AuthenticatedUser, text: str, view_mode: ViewMode = "work"
) -> dict:
    started = time.monotonic()
    turn = resolve_intent(text)
    if turn.tool_call is None:
        return {
            "mode": "assisted",
            "results": [],
            "overview": {"answer": turn.message or OUT_OF_SCOPE_MESSAGE, "citations": [], "trace": []},
        }
    raw = execute_with_fallback(db, caller, turn.tool_call, text, view_mode)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    reason = _TOOL_REASONS.get(raw["tool_call"], "Matched a directory function.")
    return _build_assisted(db, caller, raw, elapsed_ms, reason, view_mode)


def _build_assisted(
    db: Session, caller: AuthenticatedUser, raw: dict, elapsed_ms: int, reason: str,
    view_mode: ViewMode = "work",
) -> dict:
    tool_name = raw["tool_call"]
    result = raw["result"]
    results, citations = _people_and_citations(db, caller, tool_name, result, view_mode)
    answer_text = raw["message"] or _phrase(tool_name, raw["arguments"] or {}, result)
    return {
        "mode": "assisted",
        "results": results,
        "overview": {
            "answer": answer_text,
            "citations": citations,
            "trace": [{"tool": tool_name, "reason": reason, "args": raw["arguments"] or {}, "latency_ms": elapsed_ms}],
        },
    }


def _resolve_summaries(
    db: Session, caller: AuthenticatedUser, people: list[tuple[str, str]],
    view_mode: ViewMode = "work",
) -> list[PersonSummary]:
    """MentorCandidate and ProjectOwnerResult carry only id+name, not the
    full card fields (org_unit, office, availability_status, ...) —
    re-resolves each through find_people by exact name to get a real
    PersonSummary, reusing find_people's own field-building rather than
    duplicating it here. `people` is already known-visible (it came out of
    a tool's own permission-filtered result), so this is a data-shaping
    step, not a second visibility decision — a name that somehow doesn't
    resolve back (renamed between calls, extremely unlikely mid-request)
    is dropped rather than fabricated.
    """
    summaries: list[PersonSummary] = []
    seen: set[str] = set()
    for person_id, full_name in people:
        if person_id in seen:
            continue
        seen.add(person_id)
        match = next(
            (p for p in find_people(db, caller, name=full_name, view_mode=view_mode) if p.id == person_id),
            None,
        )
        if match:
            summaries.append(match)
    return summaries


def _people_and_citations(
    db: Session, caller: AuthenticatedUser, tool_name: str, result: Any,
    view_mode: ViewMode = "work",
) -> tuple[list[PersonSummary], list[PersonRef]]:
    """Every one of the seven tools already ran its own is_record_visible
    filtering before this function ever sees the result — this only
    reshapes already-filtered data into card-renderable PersonSummary
    objects, it makes no new visibility decision. Tools whose result
    genuinely has no people in it (skill_gap, skill_scarcity) return empty
    on purpose, not because of a permission check.
    """
    if result is None:
        return [], []

    if tool_name == "find_people":
        people = [p for p in result if isinstance(p, PersonSummary)]
        return people, [PersonRef(id=p.id, full_name=p.full_name) for p in people]

    if tool_name == "get_person" and isinstance(result, PersonDetail):
        summary = PersonSummary(
            id=result.id, full_name=result.full_name, preferred_name=result.preferred_name,
            job_title=result.job_title or "", org_unit=result.org_unit or "", office=result.office,
            availability_status=result.availability_status or "available",
            manager=result.manager, delegate=result.delegate,
        )
        return [summary], [PersonRef(id=summary.id, full_name=summary.full_name)]

    if tool_name == "find_mentor":
        candidates = [c for c in result if isinstance(c, MentorCandidate)]
        summaries = _resolve_summaries(db, caller, [(c.id, c.full_name) for c in candidates], view_mode)
        return summaries, [PersonRef(id=s.id, full_name=s.full_name) for s in summaries]

    if tool_name == "get_org_chain":
        # OrgChainNode already carries org_unit/availability_status as
        # plain strings, matching PersonSummary's required fields exactly
        # — no extra query needed to build real cards from it.
        nodes = [n for n in result if isinstance(n, OrgChainNode)]
        summaries = [
            PersonSummary(id=n.id, full_name=n.full_name, job_title=n.job_title,
                          org_unit=n.org_unit, availability_status=n.availability_status, delegate=n.delegate)
            for n in nodes
        ]
        return summaries, [PersonRef(id=s.id, full_name=s.full_name) for s in summaries]

    if tool_name == "find_project_owner" and isinstance(result, ProjectOwnerResult):
        resolved = _resolve_summaries(db, caller, [(result.owner_id, result.owner_name)], view_mode)
        if resolved:
            return resolved, [PersonRef(id=resolved[0].id, full_name=resolved[0].full_name)]
        summary = PersonSummary(
            id=result.owner_id, full_name=result.owner_name,
            job_title="", org_unit="", availability_status="available",
        )
        return [summary], [PersonRef(id=summary.id, full_name=summary.full_name)]

    # skill_gap / skill_scarcity: pure skill statistics, no people to show.
    return [], []


def _phrase(tool_name: str, args: dict, result: Any) -> str:
    """Builds the overview's prose server-side from the already-filtered
    result — the same job app/../frontend's old client-side phraseAnswer()
    did, moved here because the frontend is meant to be a pure renderer now.
    Never invents anything the tool didn't actually return.
    """
    if tool_name == "find_people":
        people = result or []
        if not people:
            return "No one in the directory matched that."
        # A single exact-name match carries manager/delegate/direct_reports
        # (find_people's own single-match enrichment) — surface whichever
        # of those the caller actually asked about instead of a bare name,
        # same as get_person's phrasing below. Without this, a correctly
        # resolved "who does X report to?" still read as if the question
        # went unanswered, even once routing stopped returning 5 fuzzy
        # matches for it.
        if len(people) == 1:
            person = people[0]
            bits = [person.full_name]
            if person.manager:
                bits.append(f"reports to {person.manager.full_name}")
            elif person.direct_reports:
                n = len(person.direct_reports)
                bits.append(f"has {n} direct report{'s' if n != 1 else ''}")
            if len(bits) > 1:
                return f"{bits[0]} {', '.join(bits[1:])}."
            return f"Found 1 match: {person.full_name}."
        names = [p.full_name for p in people[:5]]
        extra = f", and {len(people) - 5} more" if len(people) > 5 else ""
        return f"Found {len(people)} match{'es' if len(people) != 1 else ''}: {', '.join(names)}{extra}."

    if tool_name == "get_person":
        if result is None:
            return "Couldn't find that person."
        bits = [f"{result.full_name}{f', {result.job_title}' if result.job_title else ''}"
                f"{f' ({result.org_unit})' if result.org_unit else ''}."]
        if result.manager:
            bits.append(f"Reports to {result.manager.full_name}.")
        if result.availability_status == "away" and result.delegate:
            bits.append(f"Currently away — {result.delegate.full_name} is covering.")
        return " ".join(bits)

    if tool_name == "get_org_chain":
        nodes = result or []
        is_up = args.get("direction") == "up"
        label = "above them" if is_up else "below them"
        if not nodes:
            return f"Nobody found {label} in the org chart (or that direction is restricted for your role)."
        # An "up" walk — whether 1 hop ("my manager") or N ("my manager's
        # manager") — is asking for one specific person at the far end of
        # the chain, not a headcount; nodes are depth-ordered ascending, so
        # the last one is the answer. One code path for every depth, not a
        # separate single-hop branch that special-cases depth=1 phrasing
        # differently from depth=2+ (that split is what let "who is my
        # manager?" regress to a bare count while multi-hop stayed fixed).
        if is_up:
            top = nodes[-1]
            levels = len(nodes)
            hop = "their manager" if levels == 1 else f"{levels} levels up the reporting chain"
            return f"{top.full_name} ({top.job_title}), {hop}."
        n = len(nodes)
        return f"{n} {'person' if n == 1 else 'people'} {label} in the reporting chain."

    if tool_name == "find_project_owner":
        if result is None:
            return "Couldn't find an owner for that."
        return f"{result.owner_name} owns {result.project_name} ({result.project_type})."

    if tool_name == "find_mentor":
        candidates = result or []
        if not candidates:
            return "No mentor candidates matched that skill right now."
        return (f"{len(candidates)} potential mentor{'s' if len(candidates) != 1 else ''} found, "
                f"starting with {candidates[0].full_name} ({candidates[0].level}).")

    if tool_name == "skill_gap":
        items = result or []
        if not items:
            return "No skill gap data came back."
        gaps = [i.skill for i in items if i.gap]
        return f"Gap found in: {', '.join(gaps)}." if gaps else "No gaps — every skill checked has real coverage."

    if tool_name == "skill_scarcity":
        items = result or []
        if not items:
            return "No scarcity data came back."
        scarcest = min(items, key=lambda i: i.capable_count)
        return f"Scarcest: {scarcest.skill} ({scarcest.capable_count} people capable)."

    return "Done."
