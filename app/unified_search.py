"""Unified search: merges the structured /people path and the model-backed
/ask path behind one endpoint and one discriminated response shape, for the
Search+Ask UI merge. The frontend is a pure renderer — this module is what
actually decides "direct" vs "assisted", deterministically, with no model
call for the direct case.

That decision is intentionally narrow — question SHAPE (a trailing question
mark or an interrogative opener) or a described PROBLEM (see
needs_assistant) — not a model call and not fuzzy NLU. A missed
classification just falls through to find_people's own free-text hybrid
search rather than erroring, so a false negative degrades gracefully
instead of failing outright.

The problem half was added after mode 3 shipped: question shape alone
excluded exactly the phrasing find_experts exists to serve, because a
described problem is a statement, not a question.

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

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import Employee
from app.people import MAX_RESULTS, SUMMARY_FIELDS, find_people, search_people_by_plan
from app.text_filters import plan_from_text
from app.permissions import ViewMode
from app.schemas import (
    AmbiguousPersonMatch, AmbiguousProjectMatch, MentorCandidate, OrgChainNode, PersonDetail, PersonRef, PersonSummary, ProblemExpert, ProjectOwnerResult,
    UnknownPerson,
)
from app.vocabulary import snap_tool_arguments
from app.tool_calling import (
    OUT_OF_SCOPE_MESSAGE,
    TOOLS,
    describes_a_problem,
    _deterministic_resolve,
    names_a_real_person,
    ResolvedToolCall,
    execute_chain,
    execute_with_fallback,
    execute_with_retry,
    resolve_intent,
)

_QUESTION_MARKERS = re.compile(
    r"\?\s*$|^\s*(who|what|where|when|why|how|is|are|does|do|can|could|should|would)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    """A trailing question mark, or an interrogative sentence opener.
    Everything else (a bare name, a skill, "engineering managers in
    Bangalore") stays structured and goes straight to find_people, which
    already handles free-text description queries via hybrid search with
    zero chat-model involvement.
    """
    return bool(_QUESTION_MARKERS.search(text.strip()))


def needs_assistant(text: str) -> bool:
    """The direct/assisted gate. Question SHAPE, or a described PROBLEM.

    The problem half is not redundant. A described problem is a statement,
    not a question -- "our kubernetes cluster keeps dropping pods and
    networking is flaky" opens with "our" and has no question mark, so
    is_question() is False for precisely the phrasing mode 3 exists to
    serve. Measured on the deployed app before this: that text fell to
    direct free-text employee search and returned five loosely-related
    engineers, while the identical text with a "?" appended routed to
    find_experts and returned the Networking Team Manager who actually led
    the cluster migration.

    describes_a_problem() is imported from app.tool_calling rather than
    re-implemented, so this gate and the deterministic router can never
    disagree about what counts as a problem -- a query this lets through
    is exactly a query the router has a find_experts branch for.
    """
    return is_question(text) or describes_a_problem(text)


# A coordination between alternatives ("Bangalore or Singapore") is the one
# request shape find_people structurally cannot say: its parameters take a
# single value each, so an OR across values needs search_people's explicit
# filter list. Narrow on purpose -- this is not a general "looks complicated"
# heuristic, it names one specific expressiveness gap.
_COORDINATION = re.compile(r"\b(?:or|either)\b", re.IGNORECASE)


def _is_exact_identifier(db: Session, text: str) -> bool:
    """True when the whole query is exactly one person's unique identifier.

    Guards the routing gate below against a name that happens to contain a
    relationship keyword: the deterministic router reads "Riley Report" as a
    question about someone called Riley, because "Report" matches its
    reports-to pattern. An exact identifier is a lookup, never a question,
    so it is settled here before the router is ever consulted.

    Same predicate find_people already short-circuits on (app/people.py), so
    a query this returns True for is one the direct path answers exactly.
    """
    value = text.strip().lower()
    if not value:
        return False
    handles = {value, value.lstrip("@"), f"@{value.lstrip('@')}"}
    return db.execute(
        select(Employee.id).where(
            Employee.is_active == True,  # noqa: E712
            or_(
                func.lower(Employee.full_name) == value,
                func.lower(Employee.preferred_name) == value,
                func.lower(Employee.work_email) == value,
                func.lower(Employee.slack_handle).in_(handles),
            ),
        ).limit(1)
    ).first() is not None


def _wants_assistant(db: Session, text: str) -> bool:
    """The routing gate: is this query worth taking the assisted path?

    Question SHAPE used to be the whole test, which made a trailing "?" the
    difference between an answer and nothing at all -- "anyone in Bangalore
    or Singapore who knows Kubernetes" returned 0 results, and the identical
    text with a "?" returned 7. Statement-shaped requests are not rarer than
    question-shaped ones; they were just unroutable.

    Three questions now, cheapest first:

      1. Is the whole query one person's identifier? Then it is a lookup.
         Settled before anything else, so a surname like "Report" can't be
         read as a relationship question.
      2. Can the deterministic router answer it? Then the assisted path is
         FREE (~4ms, no model call) and there is no reason punctuation
         should decide whether to take it.
      3. Otherwise, fall back to the original test -- question shape or a
         described problem -- since from here the assisted path costs a
         real model call and should be asked for, not guessed at.

    A query that passes none of these still is not stranded: unified_search
    escalates it after the direct path comes back empty.
    """
    if _is_exact_identifier(db, text):
        return False
    if _has_confident_route(db, text):
        return True
    if _COORDINATION.search(text):
        return True
    return needs_assistant(text)


def _has_confident_route(db: Session, text: str) -> bool:
    """The deterministic router matched AND, when it named a person, that
    person actually exists.

    The existence check is what makes the router safe to consult on
    statement-shaped text. Its relationship branch keys on the bare word
    "report", and the name group is greedy, so "someone good with dashboards
    and reporting" resolves as a manager question about a person called
    "someone good with dashboards and". That never surfaced while question
    shape gated the router; it does the moment statements reach it.

    A name that resolves to SEVERAL people still counts as confident -- "which
    of the three Michaels did you mean?" is a correct answer, not a failure.
    Only a name matching nobody disqualifies the route.
    """
    turn = _deterministic_resolve(text)
    if turn is None:
        return False
    # Shared with resolve_intent, so the gate and the router can never
    # disagree about whether a route is trustworthy.
    return names_a_real_person(db, turn)


# Short, static, per-tool descriptions of *why* a tool was selected. Not the
# model's own reasoning — nothing in this stack asks the model to explain
# itself, which would cost a second call — just a fixed, honest sentence
# about what each tool did.
#
# Written out rather than derived from the tool schemas. These used to be
# `description.split(".")[0]`, which assumed every schema description opens
# with one short sentence. search_people's does not: its first period is
# ~400 characters in, so the trace rendered a paragraph of spec prose about
# `op=in`, `filter_groups` and `job_title contains 'Architect'` — which
# reads like a query dump, because that is what it is. A schema description
# is written to steer a model; a trace line is written for a person, and
# the two are not the same text.
_TOOL_REASONS = {
    "find_people": "Searched the directory for people matching that.",
    "search_people": "Filtered the directory on the specific attributes asked for.",
    "get_person": "Looked up that person's profile.",
    "get_org_chain": "Walked the reporting chain.",
    "find_project_owner": "Looked up who owns that project.",
    "find_mentor": "Ranked people who could mentor you in that skill.",
    "skill_gap": "Checked how well those skills are covered.",
    "skill_scarcity": "Checked how rare that skill is across the company.",
    "find_experts": "Matched the problem against what our projects actually did, "
                    "then found the people who worked on them.",
}

# Every tool must have one -- a tool added without a reason would otherwise
# silently fall back to the generic string and nobody would notice.
assert set(_TOOL_REASONS) == {t["function"]["name"] for t in TOOLS}, (
    "every tool needs a trace reason: "
    f"{set(_TOOL_REASONS) ^ {t['function']['name'] for t in TOOLS}}"
)


def unified_search(
    db: Session, caller: AuthenticatedUser, *, q: str | None, filters: dict[str, Any],
    view_mode: ViewMode = "work",
) -> dict:
    text = (q or "").strip()
    clean_filters = {k: v for k, v in filters.items() if v is not None}

    if text and _wants_assistant(db, text):
        return _assisted(db, caller, text, clean_filters, view_mode)

    # The Filters panel is free-text inputs, not dropdowns fed by the real
    # vocabulary, so "bangalore" and "cloud operations team" arrive exactly
    # as typed. The assisted path snaps the model's arguments the same way
    # (app/tool_calling.py); doing it here too means a filter chip and a
    # question that mean the same thing resolve the same way.
    clean_filters, _notes = snap_tool_arguments(db, clean_filters)

    results = find_people(db, caller, query=text or None, view_mode=view_mode, **clean_filters)

    # Unique-identifier misses (an exact name/slack/email match attempted
    # and nothing came back, or came back but isn't visible to this caller)
    # stay a flat, honest empty result — no AI escalation. Only a
    # genuinely fuzzy attribute — skill — gets the "find similar"
    # treatment, and it's find_people's own semantic search doing the
    # finding (see execute_with_fallback), never a second, separate
    # AI system.
    if not results and clean_filters.get("skill"):
        # mode stays "direct" here, not "assisted" -- this bypasses
        # resolve_intent()'s router entirely (it's GET /search's own
        # filter-miss escalation, never touches the model) and must not
        # render as an AI Overview: no model call happened, so there's no
        # AI reasoning to show a trace of. raw["message"] (built by
        # _finish_with_broadening) becomes a plain `note` instead of an
        # `overview.answer`, which is the one field the frontend is allowed
        # to show without the Sparkles/"AI Overview" framing.
        tool_call = ResolvedToolCall(name="find_people", arguments=clean_filters, routed_via="direct")
        raw = execute_with_fallback(db, caller, tool_call, f"(direct query, skill miss) {clean_filters['skill']}",
                                    view_mode)
        return {"mode": "direct", "results": raw["result"] or [], "note": raw["message"]}

    # The direct path found nothing and there was free text to interpret.
    # Before reporting "no such people", try reading that text as the
    # structured request it may well be.
    #
    # What this rescues: statement-shaped attribute queries. "engineers in
    # Austin" is not question-shaped, describes no problem, and matches no
    # deterministic route, so _wants_assistant() (correctly) declines to
    # spend a model call on it -- and then find_people(query=...) can only
    # match it against NAMES, because that is all its SQL fallback does.
    # Zero results, while 27 Austin engineers sit in the table. The same
    # text with a "?" has always worked, which is RC5 (ARCHITECTURE_2.md
    # §2) leaking back in: punctuation still decides some answers.
    #
    # Deliberately NOT a model call. Three tests here assert "model must
    # not be called" for ordinary free text, and they are right to -- that
    # cost decision was made on purpose. app.text_filters answers the same
    # queries deterministically, for no tokens, off real database
    # vocabulary only, and returns None the moment the text names nothing
    # real. So `mode` stays "direct" throughout: this is the direct path
    # getting better at reading, not a second route to the assistant.
    #
    # Only reachable on an already-empty result, which is what bounds the
    # blast radius: it cannot change an answer that currently works, only
    # supply one where there is currently none.
    #
    # An exact identifier is excluded -- find_people short-circuits on it,
    # so empty there means "that person exists but you may not see them",
    # an honest flat empty that no amount of re-reading improves.
    if not results and text and not _is_exact_identifier(db, text):
        plan = plan_from_text(db, text, select_fields=sorted(SUMMARY_FIELDS), limit=MAX_RESULTS)
        if plan is not None:
            try:
                results = search_people_by_plan(db, caller, plan, view_mode)
            except ValueError:
                # enforce()/validate() refused this plan for this caller.
                # Invariant 5 (ARCHITECTURE_2.md §1): degrade, don't error
                # -- the flat empty result below is still a correct answer.
                pass

    return {"mode": "direct", "results": results}


def _assisted(
    db: Session, caller: AuthenticatedUser, text: str,
    clean_filters: dict[str, Any] | None = None, view_mode: ViewMode = "work",
) -> dict:
    started = time.monotonic()
    turn = resolve_intent(text, db)
    if turn.tool_call is None:
        return {
            "mode": "assisted",
            "results": [],
            "overview": {"answer": turn.message or OUT_OF_SCOPE_MESSAGE, "citations": [], "trace": []},
        }
    # UI filter chips share a vocabulary with find_people's own arguments —
    # without this they narrowed the direct path (above) but were silently
    # dropped the instant a question-shaped query took this path instead.
    # Model-extracted args win ties (spread second): if the model pulled a
    # more specific value out of the question text itself, a stale filter
    # chip shouldn't silently override what the user just typed.
    if clean_filters and turn.tool_call.name == "find_people":
        turn.tool_call.arguments = {**clean_filters, **turn.tool_call.arguments}
    # The bounded multi-step chain was built, tested and eval'd, and then
    # only ever reachable from POST /ask -- which the frontend never calls.
    # So "who on Priya's team knows Terraform" was unanswerable in the
    # actual product while working in an endpoint nothing used. Same
    # trigger as app.tool_calling.answer(): only when the model itself
    # asked for a follow-up in its first response, so single-call requests
    # keep costing exactly one call.
    #
    # Either way the result is reshaped by the same _build_assisted: a
    # chain reports every step it ran, and _trace turns that into the
    # overview's existing trace list, which until now only ever held one
    # entry because this path never chained.
    if turn.tool_call.needs_followup:
        raw = execute_chain(db, caller, turn.tool_call, text, view_mode)
    else:
        raw = execute_with_retry(db, caller, turn.tool_call, text, view_mode)
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
            "trace": _trace(raw, reason, elapsed_ms),
        },
    }


_OP_PHRASES = {
    "eq": "is", "ne": "is not", "in": "is one of", "contains": "includes",
}


def _humanize_args(tool_name: str, args: dict) -> dict:
    """Turn a tool's raw arguments into something a person can read.

    Only search_people needs it, and it needs it badly: its arguments are a
    list of {field, op, value} objects, so the trace rendered

        filters  [{"field":"office","op":"in","value":["Bangalore","Singapore"]},
                  {"field":"skills","op":"contains","value":"Kubernetes"}]

    which is a query dump in the one place meant to explain in plain
    language. Fixing the `reason` line alone left this untouched.

    Returns the same {label: value} shape the frontend already renders as
    chips, so nothing changes on that side -- the labels just become field
    names and the values become English.
    """
    if tool_name != "search_people":
        return args

    def phrase(f: dict) -> tuple[str, str]:
        field = str(f.get("field", "?")).replace("_", " ")
        op = _OP_PHRASES.get(f.get("op"), str(f.get("op")))
        value = f.get("value")
        if isinstance(value, list):
            rendered = " or ".join(str(v) for v in value)
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = str(value)
        return field, f"{op} {rendered}"

    out: dict[str, str] = {}
    for f in args.get("filters") or []:
        label, value = phrase(f)
        # Two filters on the same field are ANDed; join rather than clobber.
        out[label] = f"{out[label]} and {value}" if label in out else value

    for i, group in enumerate(args.get("filter_groups") or [], 1):
        parts = []
        for f in group:
            label, value = phrase(f)
            parts.append(f"{label} {value}")
        out[f"any of (group {i})"] = " and ".join(parts)

    if args.get("order_by"):
        out["sorted by"] = str(args["order_by"]).replace("_", " ")
    if args.get("limit"):
        out["at most"] = str(args["limit"])
    return out or args


def _trace(raw: dict, reason: str, elapsed_ms: int) -> list[dict]:
    """One entry per call actually made. A chained answer that showed only
    its final step read as though the question had been answered by a
    filter nobody asked for -- the step that resolved "Priya's team" into
    an actual team is most of the explanation.

    latency_ms is per step and really measured -- execute_chain times each
    one -- so the trace never presents a guess as a measurement. The
    single-call case has no steps list and uses the whole turn's elapsed
    time, which for one call is the same thing.
    """
    steps = raw.get("steps")
    if not steps:
        return [{"tool": raw["tool_call"], "reason": reason,
                 "args": _humanize_args(raw["tool_call"], raw["arguments"] or {}),
                 "latency_ms": elapsed_ms}]
    trace = []
    for i, step in enumerate(steps):
        last = i == len(steps) - 1
        trace.append({
            "tool": step["tool"],
            "reason": reason if last else (
                f"{_TOOL_REASONS.get(step['tool'], 'Matched a directory function.')} "
                f"Its result filled in the next step."),
            "args": _humanize_args(step["tool"], step["arguments"] or {}),
            "latency_ms": step.get("latency_ms", 0),
        })
    return trace


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

    # A name that resolved to nobody, or to too many people, produced no
    # results to render — the answer prose is the whole response. Guarded
    # here rather than in each branch below because these types are
    # BaseModels, and the get_org_chain branch would try to iterate one.
    if isinstance(result, (AmbiguousPersonMatch, UnknownPerson)):
        return [], []

    # search_people_by_plan returns list[PersonSummary], exactly like
    # find_people -- it was simply never listed here, so every structured
    # plan the model built came back with zero cards no matter how many
    # people it actually matched. The whole mode-1 path was invisible in the
    # UI; nothing routed to it often enough to notice until the routing gate
    # stopped requiring a question mark.
    if tool_name in ("find_people", "search_people"):
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

    if tool_name == "find_experts":
        # ProblemExpert already carries every PersonSummary field, so cards
        # are built directly rather than re-resolved through find_people the
        # way MentorCandidate has to be — one fewer query per result, and no
        # chance of a name that no longer resolves silently dropping a
        # person the hop legitimately found. Order is the ranking's, kept.
        experts = [e for e in result if isinstance(e, ProblemExpert)]
        summaries = [
            PersonSummary(
                id=e.id, full_name=e.full_name, job_title=e.job_title,
                org_unit=e.org_unit, availability_status=e.availability_status,
            )
            for e in experts
        ]
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


def _phrase_ambiguous_person(match: AmbiguousPersonMatch) -> str:
    """Names the candidates instead of picking one. Job title and team are
    what actually tell two people apart — for the duplicated full names in
    this directory ("Priya Sharma" twice) the name alone disambiguates
    nothing, so listing bare names would be a prompt the caller can't act on.
    """
    if not match.matches:
        return f'No active employee matches "{match.query}".'
    shown = [
        f"{c.full_name}" + (f" ({c.job_title}{f', {c.org_unit}' if c.org_unit else ''})" if c.job_title else "")
        for c in match.matches[:5]
    ]
    more = f", and {len(match.matches) - 5} more" if len(match.matches) > 5 else ""
    return (f'"{match.query}" matches {len(match.matches)} people — '
            f"{'; '.join(shown)}{more}. Which one did you mean?")


def _phrase_experts(experts: list[ProblemExpert]) -> str:
    """The one place in this module where a longer explanation earns its
    keep. Every other tool answers a question with a fact ("X reports to
    Y"); this one answers "I'm stuck, who can help?", where the useful reply
    is who hit the same thing, what happened, and whether they can actually
    be asked right now.

    Availability is the half that was missing. The old phrasing named the
    top match and stopped, so it would confidently point at someone who is
    away without saying so -- the single most useless way to answer this
    question. It now leads with someone reachable when there is one, and
    says so plainly when there is not.

    No pronouns anywhere: the directory does not record them, and this
    sentence names real colleagues.
    """
    top = experts[0]
    available = [e for e in experts if e.availability_status == "available"]
    # Names the retrieval that actually ran: a keyword-only answer (the
    # project corpus not embedded yet) is never phrased as a semantic match.
    qualifier = "" if top.retrieval == "semantic+keyword" else f" ({top.retrieval} match only)"

    named = 1
    if top.availability_status == "available":
        parts = [f"{top.full_name} {top.reason}, and is available{qualifier}."]
    elif available:
        # The closest match can't be reached, but somebody who worked on the
        # same thing can -- name both, in that order, so the ranking stays
        # visible rather than silently reshuffled by availability.
        alt = available[0]
        named = 2
        alt_clause = (f"{alt.full_name} also worked on it and is available."
                      if alt.project_id == top.project_id
                      else f"{alt.full_name} {alt.reason}, and is available.")
        parts = [f"{top.full_name} {top.reason}, but is {top.availability_status}{qualifier}.", alt_clause]
    else:
        # Nobody is reachable. The count of everyone else is folded into
        # this clause rather than appended after it -- "nobody else is free"
        # followed by "and 1 other worked on related projects" reads as a
        # contradiction even though both are true.
        named = len(experts)
        if len(experts) == 1:
            rest = "nobody else in our project history has worked on this"
        elif len(experts) == 2:
            rest = "the one other person who worked on it isn't free either"
        else:
            rest = f"none of the {len(experts) - 1} others who worked on it are free either"
        parts = [f"{top.full_name} {top.reason} — the closest match, but is "
                 f"{top.availability_status}, and {rest}{qualifier}."]

    # Counted from everyone NOT already named, so this never re-counts the
    # person just offered as the reachable alternative.
    remaining = len(experts) - named
    if remaining > 0:
        parts.append(f"{remaining} other{'s' if remaining != 1 else ''} worked on related projects.")

    # Last, so the verbatim quotation ends the answer -- an excerpt is a
    # sentence lifted whole from a project description and may or may not
    # carry its own final punctuation, which makes it awkward to place
    # anywhere a following sentence has to butt up against it.
    if top.excerpt:
        parts.append(f'That project hit: "{top.excerpt}"')
    else:
        parts.append("Nothing in that project's write-up directly mentions what you described, "
                     "so this is a looser match.")

    return " ".join(parts)

    # Lifted verbatim from the project's own description (see
    # app/project_search.py's _project_excerpts) -- appended, never blended
    # into the sentence above, so it stays visibly a quotation rather than
    # something this function composed. Its ABSENCE is meaningful too:
    # nothing in that project's write-up overlapped the problem, so the link
    # is thinner than the ranking alone suggests, and saying so beats
    # asserting a match at full confidence.



def _phrase(tool_name: str, args: dict, result: Any) -> str:
    """Builds the overview's prose server-side from the already-filtered
    result — the same job app/../frontend's old client-side phraseAnswer()
    did, moved here because the frontend is meant to be a pure renderer now.
    Never invents anything the tool didn't actually return.
    """
    if tool_name == "search_people":
        # Was falling through to the catch-all "Done." -- the structured
        # path had no phrasing at all. Same shape as find_people's summary
        # below; the filter list itself is deliberately NOT recited back,
        # that belongs in the trace, not the answer.
        people = result or []
        if not people:
            return "No one in the directory matched those criteria."
        names = [p.full_name for p in people[:5]]
        extra = f", and {len(people) - 5} more" if len(people) > 5 else ""
        return f"Found {len(people)} match{'es' if len(people) != 1 else ''}: {', '.join(names)}{extra}."

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
        # Name resolution failures are answered as themselves, before the
        # chain phrasing below ever runs. Previously all three collapsed
        # into that single "nobody found above them" sentence, which is a
        # confidently wrong answer for two of them.
        if isinstance(result, UnknownPerson):
            return f'No active employee matches "{result.query}".'
        if isinstance(result, AmbiguousPersonMatch):
            return _phrase_ambiguous_person(result)
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
        # A single-hop downward walk is a direct-reports question, and its
        # answer is a count plus who they are -- "3 people below them in the
        # reporting chain" reads like a graph statistic, not like an answer
        # to "how many people report to X".
        if args.get("depth") == 1:
            subject = args.get("person")
            who = subject if subject and subject != "self" else "They"
            verb = "has" if who != "They" else "have"
            names = ", ".join(node.full_name for node in nodes[:5])
            more = f", and {n - 5} more" if n > 5 else ""
            return f"{who} {verb} {n} direct report{'s' if n != 1 else ''}: {names}{more}."
        return f"{n} {'person' if n == 1 else 'people'} {label} in the reporting chain."

    if tool_name == "find_project_owner":
        if isinstance(result, AmbiguousProjectMatch):
            # Says which ones, rather than picking. "Migration" matches 16
            # projects in this directory; the old code answered with
            # whichever sorted first and gave no hint the others existed.
            shown = ", ".join(result.matches)
            return (f'"{result.query}" matches several projects — {shown}. '
                    f"Which one did you mean?")
        if result is None:
            return "Couldn't find an owner for that."
        return f"{result.owner_name} owns {result.project_name} ({result.project_type})."

    if tool_name == "find_mentor":
        candidates = result or []
        if not candidates:
            return "No mentor candidates matched that skill right now."
        return (f"{len(candidates)} potential mentor{'s' if len(candidates) != 1 else ''} found, "
                f"starting with {candidates[0].full_name} ({candidates[0].level}).")

    if tool_name == "find_experts":
        experts = result or []
        if not experts:
            return "Nothing in our project history matches that problem."
        return _phrase_experts(experts)

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
