"""Skill bridges — the shortest warm-introduction chain to a skill you lack.

The question this answers is not "who has skill X" (search already does that,
and a list is the right shape for it). It is "how do I REACH someone who has
X, through people I already have something in common with" — which is a path
question, cannot be answered by a filter, and is the one thing about skills
in this directory that is genuinely graph-shaped.

    You --[you both work on Payroll Annual Planning]--> Priya Sharma
        --[you both know FHIR Interoperability]------> Amir Haddad  (Expert)

WHAT COUNTS AS A BRIDGE
-----------------------
An edge between two people has to be something a real person could open a
conversation with. Two rules, and the second is the one that matters:

  shared project   Both currently assigned to it. Specific, verifiable, and
                   the strongest possible opener.
  same team        Both in the same org unit. People sit in their most
                   specific unit, so this is a real team (max ~18 people),
                   not a division of 132.
  past project     Both were on it, one or both since rolled off. Weaker
                   than a current one -- "we worked together on X" rather
                   than "we're working on X" -- so it is labelled as past
                   and sorts below.
  shared skill     Both hold it at Working or above, AND it is DISTINCTIVE —
                   see SKILL_BRIDGE_MAX_HOLDERS. "We both know SQL" (110
                   holders) is not an introduction; "we both know FHIR
                   Interoperability" (1) is.

The first three matter more than they look. Built on distinctive skills
alone the graph was uselessly sparse for exactly the people most likely to
need it: an HR director whose five skills are Advanced Excel, Employment
Law, English, SaaS Metrics and Change Management holds nothing distinctive
enough to bridge on, sits alone in her own org unit, and came out with four
edges and no route to anything.

Languages are excluded outright. English is held by 505 of 545 people, so
bridging on it would connect very nearly everybody to everybody: 127,000
edges that all mean "we are both employees here". The distinctiveness cap
would exclude it anyway; the category check states the intent.

Deliberately NOT an edge: the reporting line. Manager/report would be a
strong bridge, but who reports to whom is the one relationship in this
schema with an asymmetric visibility rule (upward is public, downward is
manager-and-HR — see app/policy.py's can_see_direct_reports), and a path
that quietly traverses it would leak the org chart to a caller who cannot
read it directly. Skills and project membership are BASE_FIELDS, visible to
anyone who can see the record at all, so a route built only from those
discloses nothing the traveller could not already look up person by person.

DETERMINISTIC. No model, no embeddings, no ranking heuristic pretending to
be relevance: a shortest path is a shortest path, and where several are
equally short they are ordered by a stated rule (see _route_sort_key).
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog, Employee, EmployeeProject, EmployeeSkill, OrgUnit, Project, Skill,
)
from app.models.enums import AvailabilityStatus, SkillCategory, SkillLevel
from app.people import resolve_skill
from app.permissions import ViewMode, effective_role
from app.schemas import (
    PersonRef, SkillRoute, SkillRouteHop, SkillRouteResult, SkillTarget, SuggestedSkill,
)

# A skill is a usable bridge only if few enough people hold it that sharing
# it says something. Chosen against the real distribution: at 25 this admits
# 50 of the 81 non-language skills and 3,681 person-pairs, while excluding
# SQL (110), Advanced Excel (104), Python (86) and the rest of the long tail
# that everybody has. Raising it to 40 triples the edge count to 11,885 and
# buys nothing but weaker introductions.
SKILL_BRIDGE_MAX_HOLDERS = 25

# Capability, not familiarity: a Learning-level holder is not somebody you
# get introduced to about a skill, and not somebody who can teach it.
CAPABLE = (SkillLevel.working, SkillLevel.expert)

# How far to walk. Past three hops an "introduction" is a chain letter, and
# the answer stops being actionable.
MAX_HOPS = 3

# Routes returned per request. Several genuinely different ways in is useful;
# ten near-identical ones is a list nobody reads.
MAX_ROUTES = 3


class RouteDenied(Exception):
    """The caller may not ask for routes from this person."""


def _require_self_or_privileged(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode
) -> Employee:
    """Routes are computed FROM somebody, and that somebody is normally the
    caller.

    HR (in work mode) may ask on anyone's behalf — "how would this new joiner
    reach our one FHIR person" is an HR question. Everyone else may only ask
    about themselves, not because the route reveals anything privileged (it
    is built entirely from BASE_FIELDS), but because "who does this person
    have a way in to" is a question about them, and the honest default for a
    question about somebody else is no.
    """
    person = db.get(Employee, person_id)
    if person is None or not person.is_active:
        raise RouteDenied("No such person")
    if caller.id == person_id:
        return person
    if effective_role(caller.role, view_mode) == "hr":
        return person
    raise RouteDenied("You can only look up routes from your own profile")


def _visible_employees(db: Session, caller: AuthenticatedUser, view_mode: ViewMode) -> dict[str, Employee]:
    """Every active employee the caller may see at all.

    Restricted records are absent rather than present-but-unnamed: a route
    that stepped through somebody the caller cannot see would disclose their
    existence, which is exactly what the restriction is for. HR keeps its
    exemption in work mode, same as everywhere else.
    """
    rows = db.execute(select(Employee).where(Employee.is_active == True)).scalars().all()  # noqa: E712
    hr = effective_role(caller.role, view_mode) == "hr"
    return {
        e.id: e for e in rows
        if hr or e.availability_status is not AvailabilityStatus.restricted or e.id == caller.id
    }


# ---------------------------------------------------------------------------
# The bridge graph
# ---------------------------------------------------------------------------

class _Bridges:
    """Adjacency over people, with the reason for each edge kept alongside it.

    The reason IS the feature — a path with unlabelled edges tells you to go
    talk to a stranger, and a path that says "you two are both on Payroll
    Annual Planning" tells you how to open. So edges carry (kind, label) and
    the BFS keeps whichever it arrived by.
    """

    def __init__(self) -> None:
        self.adj: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    def add(self, a: str, b: str, kind: str, label: str) -> None:
        self.adj[a].append((b, kind, label))
        self.adj[b].append((a, kind, label))


def _build_bridges(db: Session, people: dict[str, Employee]) -> _Bridges:
    bridges = _Bridges()

    # --- shared project, current and past --------------------------------
    current: dict[int, list[str]] = defaultdict(list)
    past: dict[int, list[str]] = defaultdict(list)
    for row in db.execute(
        select(EmployeeProject.project_id, EmployeeProject.employee_id, EmployeeProject.end_date)
    ).all():
        if row.employee_id not in people:
            continue
        (current if row.end_date is None else past)[row.project_id].append(row.employee_id)

    touched = set(current) | set(past)
    names = {
        p.id: p.name for p in db.execute(
            select(Project).where(Project.id.in_(touched or {0}))
        ).scalars().all()
    }
    for project_id, ids in current.items():
        label = names.get(project_id, "a shared project")
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                bridges.add(a, b, "project", label)
    # Past membership connects the people who were on it to each other AND
    # to whoever is on it now -- "you worked on what they are working on" is
    # a real opener, and dropping it left long-tenured people with almost no
    # edges at all.
    for project_id, ids in past.items():
        label = names.get(project_id, "a shared project")
        everyone = [*ids, *current.get(project_id, [])]
        for i, a in enumerate(everyone):
            for b in everyone[i + 1:]:
                if a != b:
                    bridges.add(a, b, "past_project", label)

    # --- same team --------------------------------------------------------
    units: dict[int, list[str]] = defaultdict(list)
    for person in people.values():
        units[person.org_unit_id].append(person.id)
    unit_names = {
        u.id: u.name for u in db.execute(
            select(OrgUnit).where(OrgUnit.id.in_(units.keys() or {0}))
        ).scalars().all()
    }
    for unit_id, ids in units.items():
        label = unit_names.get(unit_id, "the same team")
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                bridges.add(a, b, "team", label)

    # --- shared distinctive skill ----------------------------------------
    holders: dict[int, list[str]] = defaultdict(list)
    skill_rows = {s.id: s for s in db.execute(select(Skill)).scalars().all()}
    for row in db.execute(
        select(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level)
        .where(EmployeeSkill.level.in_(CAPABLE))
    ).all():
        if row.employee_id not in people:
            continue
        skill = skill_rows.get(row.skill_id)
        if skill is None or skill.category is SkillCategory.language:
            continue
        canonical = skill_rows.get(skill.canonical_id) if skill.canonical_id else skill
        if canonical is None:
            continue
        holders[canonical.id].append(row.employee_id)

    for skill_id, ids in holders.items():
        if len(ids) > SKILL_BRIDGE_MAX_HOLDERS:
            continue
        label = skill_rows[skill_id].name
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                bridges.add(a, b, "skill", label)

    return bridges


def _capable_holders(db: Session, skill: Skill, people: dict[str, Employee]) -> dict[str, SkillLevel]:
    """Who can actually do it, and at what level. Synonyms fold into the
    canonical skill, so asking for "SRE" finds the people recorded against
    "Site Reliability Engineering"."""
    alias_ids = [
        s.id for s in db.execute(
            select(Skill).where((Skill.id == skill.id) | (Skill.canonical_id == skill.id))
        ).scalars().all()
    ]
    out: dict[str, SkillLevel] = {}
    for row in db.execute(
        select(EmployeeSkill.employee_id, EmployeeSkill.level)
        .where(EmployeeSkill.skill_id.in_(alias_ids), EmployeeSkill.level.in_(CAPABLE))
    ).all():
        if row.employee_id not in people:
            continue
        # Expert wins if somebody somehow has two rows for the same skill.
        current = out.get(row.employee_id)
        if current is None or row.level is SkillLevel.expert:
            out[row.employee_id] = row.level
    return out


def _ref(e: Employee) -> PersonRef:
    return PersonRef(id=e.id, full_name=e.full_name)


def _route_sort_key(route: SkillRoute) -> tuple:
    """Shortest first; then Expert destinations ahead of Working ones; then
    routes that open on a shared PROJECT ahead of ones that open on a shared
    skill, because "we're on the same project" is a warmer first line than
    "we both know the same thing". Name last, purely so the order is stable
    between identical requests rather than dependent on dict iteration."""
    warmth = {"project": 0, "team": 1, "past_project": 2, "skill": 3}
    first_kind = route.hops[0].via_kind if route.hops else "project"
    return (
        len(route.hops),
        0 if route.level == SkillLevel.expert.value else 1,
        warmth.get(first_kind, 9),
        route.target.full_name,
    )


def find_routes(
    db: Session,
    caller: AuthenticatedUser,
    person_id: str,
    skill_name: str,
    view_mode: ViewMode = "work",
    *,
    max_hops: int = MAX_HOPS,
    limit: int = MAX_ROUTES,
) -> SkillRouteResult:
    """Shortest introduction chains from `person_id` to somebody capable in
    `skill_name`.

    Breadth-first, so the first time a capable person is reached is by a
    shortest path to them, and no longer route to the same destination is
    ever considered. Several DIFFERENT destinations are collected (up to
    `limit`) rather than several routes to one, because "here are three
    people you could reach" is more useful than three ways to reach one.
    """
    person = _require_self_or_privileged(db, caller, person_id, view_mode)
    skill = resolve_skill(db, skill_name)
    if skill is None:
        return SkillRouteResult(
            skill=None, requested=skill_name, from_person=_ref(person),
            already_capable=False, routes=[], unreachable_holder_count=0,
        )

    people = _visible_employees(db, caller, view_mode)
    holders = _capable_holders(db, skill, people)

    target = SkillTarget(
        skill_id=skill.id, skill=skill.name, category=skill.category.value,
        capable_count=len(holders),
    )

    # Already have it: say so rather than routing somebody to themselves.
    if person_id in holders:
        _audit(db, caller, person_id, skill.name, 0)
        return SkillRouteResult(
            skill=target, requested=skill_name, from_person=_ref(person),
            already_capable=True, routes=[], unreachable_holder_count=0,
        )

    bridges = _build_bridges(db, people)

    # --- BFS --------------------------------------------------------------
    routes: list[SkillRoute] = []
    seen = {person_id}
    # (person, hops so far) with the trail that got us there.
    queue: deque[tuple[str, list[SkillRouteHop]]] = deque([(person_id, [])])
    reached: set[str] = set()

    while queue:
        current, trail = queue.popleft()
        if len(trail) >= max_hops:
            continue
        for neighbour, kind, label in bridges.adj.get(current, ()):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            hop = SkillRouteHop(
                person=_ref(people[neighbour]),
                job_title=people[neighbour].job_title,
                via_kind=kind,
                via=label,
            )
            path = [*trail, hop]
            if neighbour in holders and neighbour not in reached:
                reached.add(neighbour)
                routes.append(SkillRoute(
                    target=_ref(people[neighbour]),
                    job_title=people[neighbour].job_title,
                    level=holders[neighbour].value,
                    hops=path,
                ))
            queue.append((neighbour, path))

    routes.sort(key=_route_sort_key)
    kept = routes[:limit]
    _audit(db, caller, person_id, skill.name, len(kept))
    return SkillRouteResult(
        skill=target,
        requested=skill_name,
        from_person=_ref(person),
        already_capable=False,
        routes=kept,
        # Holders no chain of shared work or shared distinctive skills
        # reaches within the hop limit. Reported rather than hidden: "three
        # people have this and none of them are connected to you" is a real
        # and useful answer, and silently returning an empty list would look
        # like the skill has no holders at all.
        unreachable_holder_count=max(0, len(holders) - len(reached)),
    )


def suggest_skills(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode = "work",
    *, limit: int = 6,
) -> list[SuggestedSkill]:
    """Skills worth asking about, for the empty state.

    Two sources, both from work this person is already doing rather than from
    a generic "popular skills" list:

      required by their own current projects, and they don't have it
      held by nobody-much org-wide, so knowing who has it is worth having

    A suggestion always says WHY it is being suggested, because "learn this"
    with no reason attached is horoscope advice.
    """
    person = _require_self_or_privileged(db, caller, person_id, view_mode)
    people = _visible_employees(db, caller, view_mode)

    skill_rows = {s.id: s for s in db.execute(select(Skill)).scalars().all()}

    def canonical(skill_id: int) -> Skill | None:
        s = skill_rows.get(skill_id)
        if s is None:
            return None
        return skill_rows.get(s.canonical_id) if s.canonical_id else s

    mine = set()
    for row in db.execute(
        select(EmployeeSkill.skill_id).where(EmployeeSkill.employee_id == person.id)
    ).all():
        c = canonical(row.skill_id)
        if c is not None:
            mine.add(c.id)

    my_projects = [
        row.project_id for row in db.execute(
            select(EmployeeProject.project_id)
            .where(EmployeeProject.employee_id == person.id, EmployeeProject.end_date.is_(None))
        ).all()
    ]

    counts: dict[int, int] = defaultdict(int)
    for row in db.execute(
        select(EmployeeSkill.employee_id, EmployeeSkill.skill_id)
        .where(EmployeeSkill.level.in_(CAPABLE))
    ).all():
        if row.employee_id not in people:
            continue
        c = canonical(row.skill_id)
        if c is not None:
            counts[c.id] += 1

    out: list[SuggestedSkill] = []
    seen: set[int] = set()

    if my_projects:
        from app.models import ProjectSkillRequirement
        for row in db.execute(
            select(ProjectSkillRequirement.skill_id, Project.name)
            .join(Project, Project.id == ProjectSkillRequirement.project_id)
            .where(ProjectSkillRequirement.project_id.in_(my_projects))
        ).all():
            c = canonical(row.skill_id)
            if c is None or c.id in mine or c.id in seen:
                continue
            seen.add(c.id)
            out.append(SuggestedSkill(
                skill_id=c.id, skill=c.name, capable_count=counts.get(c.id, 0),
                reason=f"{row.name} needs it and you don't have it recorded",
            ))

    if len(out) < limit:
        scarce = sorted(
            (sid for sid, n in counts.items() if sid not in mine and sid not in seen and n > 0),
            key=lambda sid: (counts[sid], skill_rows[sid].name),
        )
        for sid in scarce:
            if len(out) >= limit:
                break
            s = skill_rows[sid]
            if s.category is SkillCategory.language:
                continue
            seen.add(sid)
            n = counts[sid]
            out.append(SuggestedSkill(
                skill_id=sid, skill=s.name, capable_count=n,
                reason=f"only {n} {'person' if n == 1 else 'people'} in the directory can do it",
            ))

    return out[:limit]


def _audit(db: Session, caller: AuthenticatedUser, person_id: str, skill: str, count: int) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action="skill_routes",
        query_text=f"from={person_id};skill={skill}", result_count=count,
        fields_returned=json.dumps(["full_name", "job_title", "skills", "project_history"]),
        timestamp=datetime.now(),
    ))
    db.commit()
