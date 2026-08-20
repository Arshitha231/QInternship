"""Workforce analytics behind the HR and manager dashboards.

Deterministic throughout. Every number here is counted or divided; every
verdict ("understaffed", "single point of failure", "overdue") comes from a
named threshold at the top of this file, not from a model. Same split of
responsibilities as app/continuity.py: facts are code, classification is
code, and the only thing the UI does is render them.

SCOPE IS THE WHOLE DESIGN
-------------------------
There is one dashboard engine, not two. HR and a manager ask the same
questions — how many people, which skills are thin, who is behind on
training — and differ only in *which employees the question is about*. That
difference is resolved once, up front, by resolve_scope(), and everything
downstream takes a Scope and never looks at caller.role again.

    HR (work mode)   the whole company, or any org unit's subtree, or any
                     manager's reporting line — HR picks.
    manager          their own reporting subtree. Not a default they can
                     change: resolve_scope() ignores whatever scope the
                     request asked for and substitutes theirs, so there is
                     no parameter a manager can send to widen it.
    everyone else    DashboardForbidden.

That last point is why scoping lives here and not in the route layer. A
route that forgot to pass a filter would leak the company; a resolver that
*constructs* the employee set from the caller cannot.

WHAT A MANAGER MAY SEE, AND WHY
-------------------------------
A manager reading their team's course compliance is not a new privilege
being invented here. app/permissions.py's training_extra_fields already
grants training_status to anyone in the target's upward reporting chain, on
the reasoning that the notification triggers already tell that same chain —
so a manager can plainly already know. This module aggregates exactly that
audience's data and no wider: the scope IS the reporting subtree, so every
person counted in a manager's numbers is someone whose training status they
could already read one profile at a time.

Skills, projects and org structure are BASE_FIELDS — visible to every
caller who can see the record at all — so aggregating those needs no
additional grant. Nothing in this module returns salary, date of birth,
cost centre, or any other INTERNAL_FIELDS value, at any scope, for any
role. There is no code path here that reads those columns.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.certifications.requirements import course_due_dates_bulk
from app.models import (
    AuditLog, Employee, EmployeeCourseStatus, EmployeeProject, EmployeeSkill,
    OrgUnit, Project, ProjectSkillRequirement, Skill, TrainingCourse,
)
from app.models.enums import CourseStatus, SkillLevel
from app.notifications import ReminderTarget, send_course_reminders
from app.permissions import ViewMode, effective_role
from app.schemas import (
    DashboardOverview, DashboardScope, OrgUnitOption, ProjectCoverage, ReminderResult,
    SkillDetail, SkillHolder, SkillProjectUse, SkillSupplyDemand, TrainingAnalytics,
    TrainingBreakdown, TrainingBuckets, TrainingPersonRow, TrainingRoster, WorkforceInsight,
)

# --- Thresholds -----------------------------------------------------------
#
# Written as module constants rather than a YAML file (the shape
# app/continuity.py uses) because nothing here is a compliance rule with a
# versioned audit trail behind it — these are display bands on a dashboard.
# If HR ever needs to tune them without a deploy, they lift into config the
# same way continuity's did, and every use site already reads through a name.

# Capable supply per unit of project demand. Below 1.0, the org has fewer
# Working/Expert holders than active projects depending on the skill; above
# 2.5 it has more than twice the depth its current work calls for.
UNDERSTAFFED_RATIO = 1.0
OVERREPRESENTED_RATIO = 2.5

# A skill no active project depends on is "unused" rather than
# overrepresented until this many people hold it — one person with an
# unused skill is a hobby, a dozen is bench capacity worth naming.
BENCH_MIN_HOLDERS = 3

# Capable holders at or below this, against real demand, is a bus-factor
# problem regardless of ratio: 1 person covering 1 project scores a
# perfectly healthy ratio of 1.0 and is still one resignation from a gap.
SINGLE_POINT_MAX_CAPABLE = 1

# Default lookahead for the "due soon" training bucket.
DUE_SOON_DAYS = 30

# Level weights for the skill-maturity score. Expert is the unit; Working is
# most of the way there for delivery purposes; Learning is real but not yet
# something a project can be staffed on.
LEVEL_WEIGHT: dict[SkillLevel, float] = {
    SkillLevel.expert: 1.0,
    SkillLevel.working: 0.6,
    SkillLevel.learning: 0.25,
}

# Display order for the supply/demand table. Not a severity ranking —
# "unused" below "overrepresented" is about which block a reader wants
# first, not about which is worse.
VERDICT_ORDER: dict[str, int] = {
    "understaffed": 0, "healthy": 1, "overrepresented": 2, "unused": 3,
}

MATURITY_BANDS: list[tuple[int, str]] = [
    (80, "Deep"), (60, "Established"), (40, "Developing"), (0, "Emerging"),
]

# Display caps. Not business rules — the same spirit as app/people.py's
# MAX_RESULTS: an aggregate is still correct when the list under it is
# truncated, and every response that truncates says so.
MAX_ROSTER_ROWS = 500
MAX_SKILL_HOLDERS = 200
MAX_INSIGHT_EVIDENCE = 5

# org_units is company -> division -> department -> team, so four hops is
# the real ceiling; generous bound so malformed parent_id data can't loop.
ORG_UNIT_MAX_DEPTH = 10


class DashboardForbidden(Exception):
    """Raised by resolve_scope for a caller with no dashboard of their own —
    an employee, an it caller, or a manager with nobody reporting to them."""


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    """Which employees a dashboard question is about, and what to call them.

    `employee_ids` is always concrete — never None-meaning-everyone — so no
    downstream function has to remember that a missing filter means the
    whole company. The org-wide scope is simply the set of every active
    employee, and a query that forgets to apply it is a bug that shows up as
    a wrong number rather than as a leak.
    """

    kind: Literal["org", "org_unit", "team"]
    label: str
    employee_ids: frozenset[str]
    org_unit_id: int | None = None
    manager_id: str | None = None
    # Set when the caller asked for one scope and policy substituted
    # another — a manager sending ?org_unit_id=. Surfaced to the UI so the
    # header says whose data is on screen rather than silently disagreeing
    # with the control the user just clicked.
    substituted: bool = False


def _all_units(db: Session) -> dict[int, OrgUnit]:
    return {u.id: u for u in db.execute(select(OrgUnit)).scalars().all()}


def unit_subtree_ids(db: Session, unit_id: int) -> set[int]:
    """A unit and every unit beneath it.

    Employees only ever sit in their most-specific unit, so a department
    filter that didn't descend would return nobody — the same problem
    app/people.py's _org_unit_and_descendant_ids solves for the search
    filter, resolved here without a recursive CTE because this module
    already loads the whole 75-row table for its labels and walking it in
    Python is cheaper than a second round trip.
    """
    units = _all_units(db)
    children: dict[int | None, list[int]] = defaultdict(list)
    for unit in units.values():
        children[unit.parent_id].append(unit.id)

    out: set[int] = set()
    frontier = [unit_id]
    depth = 0
    while frontier and depth < ORG_UNIT_MAX_DEPTH:
        nxt: list[int] = []
        for uid in frontier:
            if uid in out:
                continue
            out.add(uid)
            nxt.extend(children.get(uid, ()))
        frontier = nxt
        depth += 1
    return out


def _reporting_subtree_ids(db: Session, manager_id: str) -> set[str]:
    """Everyone below `manager_id` on employees.manager_id, at any depth.

    Excludes the manager. "Your team" on a manager dashboard means the
    people you are accountable for, and folding the manager's own row into
    their team's headcount and skill mix makes every one-person-deep team
    read as two.

    Reuses org_chart's traversal rather than a second walk of the same
    column — "the reporting chain" means one thing in this codebase.
    """
    from app.org_chart import _traverse, MAX_DEPTH

    return {emp_id for emp_id, _depth in _traverse(db, manager_id, "down", MAX_DEPTH)}


def _active_employee_ids(db: Session, unit_ids: set[int] | None = None) -> set[str]:
    stmt = select(Employee.id).where(Employee.is_active == True)  # noqa: E712
    if unit_ids is not None:
        stmt = stmt.where(Employee.org_unit_id.in_(unit_ids))
    return {row.id for row in db.execute(stmt).all()}


def resolve_scope(
    db: Session,
    caller: AuthenticatedUser,
    view_mode: ViewMode = "work",
    *,
    org_unit_id: int | None = None,
    manager_id: str | None = None,
) -> Scope:
    """The one gate. Every public function in this module starts here.

    HR in work mode chooses freely — the whole org, a department subtree, or
    any manager's line. Every other role gets their own reporting subtree
    and nothing else: the requested scope is not validated against their
    permissions, it is *discarded*, so there is no combination of parameters
    that widens a manager's view. `substituted` records that this happened
    so the UI can say so.

    Work mode matters for the HR branch for the same reason it does in
    app/continuity.py: employee mode is "what an ordinary colleague sees",
    and an org-wide compliance dashboard is precisely what an ordinary
    colleague does not see. Routed through effective_role rather than an
    inline view_mode check so it cannot drift from the collapse the rest of
    the pipeline performs.
    """
    if effective_role(caller.role, view_mode) == "hr":
        if manager_id is not None:
            manager = db.get(Employee, manager_id)
            if manager is None or not manager.is_active:
                raise DashboardForbidden("No such manager")
            ids = _reporting_subtree_ids(db, manager_id) & _active_employee_ids(db)
            return Scope(kind="team", label=f"{manager.full_name}'s organization",
                         employee_ids=frozenset(ids), manager_id=manager_id,
                         org_unit_id=manager.org_unit_id)
        if org_unit_id is not None:
            unit = db.get(OrgUnit, org_unit_id)
            if unit is None:
                raise DashboardForbidden("No such org unit")
            unit_ids = unit_subtree_ids(db, org_unit_id)
            return Scope(kind="org_unit", label=unit.name,
                         employee_ids=frozenset(_active_employee_ids(db, unit_ids)),
                         org_unit_id=org_unit_id)
        return Scope(kind="org", label="Organization-wide",
                     employee_ids=frozenset(_active_employee_ids(db)))

    # Everyone else is their own scope, or has no dashboard at all. Note
    # this is keyed on having reports, not on holding the "manager" role
    # claim: the role arrives on the request and the reports are a fact
    # about the data, and it is the fact that decides whose numbers these
    # are. Same conservative signal app/notifications.py's _role_for uses.
    reports = _reporting_subtree_ids(db, caller.id) & _active_employee_ids(db)
    if not reports:
        raise DashboardForbidden("This dashboard is for HR and for managers with direct reports")
    me = db.get(Employee, caller.id)
    label = f"{me.full_name}'s team" if me is not None else "Your team"
    return Scope(
        kind="team", label=label, employee_ids=frozenset(reports), manager_id=caller.id,
        org_unit_id=me.org_unit_id if me is not None else None,
        substituted=org_unit_id is not None or (manager_id is not None and manager_id != caller.id),
    )


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, scope: Scope,
                 result_count: int, fields: list[str]) -> None:
    """One row per public call, same convention as app/continuity.py's.
    `query_text` carries identifiers and the scope, never a person's name or
    a course result."""
    target = f"scope={scope.kind};unit={scope.org_unit_id};manager={scope.manager_id};n={len(scope.employee_ids)}"
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=target, result_count=result_count,
        fields_returned=json.dumps(sorted(fields)), timestamp=datetime.now(),
    ))
    db.commit()


def _scope_out(db: Session, scope: Scope) -> DashboardScope:
    unit = db.get(OrgUnit, scope.org_unit_id) if scope.org_unit_id is not None else None
    return DashboardScope(
        kind=scope.kind, label=scope.label, headcount=len(scope.employee_ids),
        org_unit_id=scope.org_unit_id, org_unit=unit.name if unit else None,
        manager_id=scope.manager_id, substituted=scope.substituted,
    )


def scope_for(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
) -> DashboardScope:
    """The resolved scope on its own, with none of the analysis.

    For callers that need to say WHOSE data something is without paying for
    a full pass over it — the insights narrative, which needs the scope
    label and headcount to write a sentence and nothing else. Raises
    DashboardForbidden exactly as every other entry point here does, so a
    caller cannot use it as a cheaper way past the gate.

    No audit row: this reads no employee data beyond the scope resolution
    every other public function in this module already writes a row for, and
    a second entry for the same request would double-count.
    """
    return _scope_out(db, resolve_scope(db, caller, view_mode,
                                        org_unit_id=org_unit_id, manager_id=manager_id))


def org_unit_options(db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work") -> list[OrgUnitOption]:
    """The department selector's contents: every unit the caller may scope
    to, with the headcount each one resolves to.

    Headcount is subtree headcount, not the unit's own rows — picking
    "Engineering" from this list scopes to everyone under Engineering, so
    the number beside it has to be the number they will then see. Computing
    it here also means the selector can hide units that resolve to nobody
    instead of offering a dead option.
    """
    scope = resolve_scope(db, caller, view_mode)
    units = _all_units(db)
    headcounts = dict(db.execute(
        select(Employee.org_unit_id, func.count(Employee.id))
        .where(Employee.is_active == True)  # noqa: E712
        .group_by(Employee.org_unit_id)
    ).all())

    out: list[OrgUnitOption] = []
    for unit in units.values():
        subtree = unit_subtree_ids(db, unit.id)
        total = sum(headcounts.get(uid, 0) for uid in subtree)
        if total == 0:
            continue
        # A non-HR caller only ever sees their own scope, so the selector is
        # narrowed to units their own people actually sit in rather than
        # offering the company's org tree to someone who cannot scope to it.
        if scope.kind == "team":
            in_scope = db.execute(
                select(func.count(Employee.id)).where(
                    Employee.id.in_(scope.employee_ids), Employee.org_unit_id.in_(subtree)
                )
            ).scalar_one()
            if in_scope == 0:
                continue
            total = in_scope
        out.append(OrgUnitOption(
            id=unit.id, name=unit.name, unit_type=unit.unit_type,
            parent_id=unit.parent_id, headcount=total,
        ))
    out.sort(key=lambda o: (o.unit_type != "division", o.unit_type != "department", o.name))
    return out


# ---------------------------------------------------------------------------
# Shared retrieval — one pass over the scope, reused by every section
# ---------------------------------------------------------------------------

@dataclass
class _Facts:
    """Everything the sections below need, fetched once.

    Each public function assembles this for its scope and then does pure
    arithmetic. The alternative — every section running its own queries —
    produced a dashboard where the headline "412 people have a skill
    profile" and the skills table's own totals were computed by different
    code and could disagree.
    """

    employees: list[Employee]
    by_id: dict[str, Employee]
    unit_names: dict[int, str]
    # skill id -> canonical skill row (synonyms already folded)
    skills: dict[int, Skill]
    # skill id -> level -> employee ids, scope-restricted
    holders: dict[int, dict[SkillLevel, list[str]]] = field(default_factory=dict)
    # project id -> current member ids (any scope), and the project row
    projects: dict[int, Project] = field(default_factory=dict)
    project_members: dict[int, list[str]] = field(default_factory=dict)
    # active projects that at least one in-scope person is currently on
    scope_project_ids: set[int] = field(default_factory=set)
    # skill id -> project ids depending on it, split by how we know
    declared_demand: dict[int, set[int]] = field(default_factory=dict)
    inferred_demand: dict[int, set[int]] = field(default_factory=dict)


def _canonical_skills(db: Session) -> dict[int, Skill]:
    """Every skill id mapped to its canonical row.

    Skill.canonical_id folds synonyms (SRE -> Site Reliability Engineering).
    Counting the two separately would report a shortage in one and a surplus
    in the other for what is one capability, so every count in this module
    goes through this map first.
    """
    rows = {s.id: s for s in db.execute(select(Skill)).scalars().all()}
    resolved: dict[int, Skill] = {}
    for skill_id, skill in rows.items():
        target = skill
        hops = 0
        while target.canonical_id is not None and target.canonical_id in rows and hops < 5:
            target = rows[target.canonical_id]
            hops += 1
        resolved[skill_id] = target
    return resolved


def _gather(db: Session, scope: Scope) -> _Facts:
    employees = list(db.execute(
        select(Employee).where(Employee.id.in_(scope.employee_ids)).order_by(Employee.full_name)
    ).scalars().all()) if scope.employee_ids else []

    facts = _Facts(
        employees=employees,
        by_id={e.id: e for e in employees},
        unit_names={u.id: u.name for u in db.execute(select(OrgUnit)).scalars().all()},
        skills=_canonical_skills(db),
    )

    # --- supply: who holds what, in scope -------------------------------
    holders: dict[int, dict[SkillLevel, list[str]]] = defaultdict(lambda: defaultdict(list))
    if scope.employee_ids:
        for row in db.execute(
            select(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level)
            .where(EmployeeSkill.employee_id.in_(scope.employee_ids))
        ).all():
            canonical = facts.skills.get(row.skill_id)
            if canonical is None:
                continue
            holders[canonical.id][row.level].append(row.employee_id)
    facts.holders = {k: dict(v) for k, v in holders.items()}

    # --- current project membership -------------------------------------
    # end_date IS NULL is what "current" means everywhere in this codebase
    # (see EmployeeProject's own comment), so a project is active iff
    # somebody is currently on it. There is no status column to consult.
    members: dict[int, list[str]] = defaultdict(list)
    for row in db.execute(
        select(EmployeeProject.project_id, EmployeeProject.employee_id)
        .where(EmployeeProject.end_date.is_(None))
    ).all():
        members[row.project_id].append(row.employee_id)
    facts.project_members = dict(members)
    facts.scope_project_ids = {
        pid for pid, ids in members.items() if any(i in scope.employee_ids for i in ids)
    }
    if facts.scope_project_ids:
        facts.projects = {
            p.id: p for p in db.execute(
                select(Project).where(Project.id.in_(facts.scope_project_ids))
            ).scalars().all()
        }

    # --- demand: which active projects depend on which skill ------------
    #
    # Two tiers, and which one produced a number is always reported
    # alongside it (`basis`), never quietly merged — the same declared-vs-
    # inferred discipline app/continuity.py applies to delivery
    # dependencies, for the same reason: an inferred dependency overcounts,
    # and a dashboard that hides which it used invites a staffing decision
    # made on a guess.
    declared: dict[int, set[int]] = defaultdict(set)
    projects_with_declarations: set[int] = set()
    if facts.scope_project_ids:
        for row in db.execute(
            select(ProjectSkillRequirement.project_id, ProjectSkillRequirement.skill_id)
            .where(ProjectSkillRequirement.project_id.in_(facts.scope_project_ids))
        ).all():
            canonical = facts.skills.get(row.skill_id)
            if canonical is None:
                continue
            declared[canonical.id].add(row.project_id)
            projects_with_declarations.add(row.project_id)
    facts.declared_demand = {k: set(v) for k, v in declared.items()}

    # Inferred, only for active projects that declared nothing: a skill one
    # of its current members holds at Working or above is treated as a
    # dependency of that project. Overcounts by construction — a skill on
    # somebody's profile that this project never needed still lands here —
    # which is exactly why it is kept in its own bucket and labelled.
    undeclared = facts.scope_project_ids - projects_with_declarations
    inferred: dict[int, set[int]] = defaultdict(set)
    if undeclared:
        member_ids = {i for pid in undeclared for i in facts.project_members.get(pid, ())}
        member_skills: dict[str, set[int]] = defaultdict(set)
        if member_ids:
            for row in db.execute(
                select(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level)
                .where(EmployeeSkill.employee_id.in_(member_ids),
                       EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]))
            ).all():
                canonical = facts.skills.get(row.skill_id)
                if canonical is not None:
                    member_skills[row.employee_id].add(canonical.id)
        for pid in undeclared:
            for emp_id in facts.project_members.get(pid, ()):
                for skill_id in member_skills.get(emp_id, ()):
                    inferred[skill_id].add(pid)
    facts.inferred_demand = {k: set(v) for k, v in inferred.items()}

    return facts


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _n(count: int, singular: str, plural: str | None = None) -> str:
    """"1 skill" / "4 skills". Every string in the insights below is built
    from real counts, and a count of exactly one is the common case for the
    findings that matter most — a single point of failure is by definition
    one person. "1 skills rest on a single person" undercuts the finding it
    is reporting, so the agreement is done here rather than by appending an
    's' at each of the dozen call sites."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


# ---------------------------------------------------------------------------
# Skill supply vs demand
# ---------------------------------------------------------------------------

def _verdict(capable: int, demand: int, holder_count: int) -> str:
    """Four outcomes, not three. "Unused" is separated from
    "overrepresented" because they call for different actions: a skill no
    active project needs is bench capacity to deploy or let lapse, whereas
    an overrepresented one is depth beyond what its real demand justifies.
    Folding them together produced a list where the top entries were all
    skills nobody was using, burying the actual surpluses."""
    if demand == 0:
        # Keyed on holders, not capable: a skill a dozen people are still
        # LEARNING and no project needs is just as much idle capability as
        # one a dozen people have mastered, and the capable-only test filed
        # every learning-only skill under "healthy", which is a verdict
        # about supply meeting demand and says nothing true here.
        return "unused" if holder_count >= BENCH_MIN_HOLDERS else "healthy"
    ratio = capable / demand
    if ratio < UNDERSTAFFED_RATIO:
        return "understaffed"
    if ratio > OVERREPRESENTED_RATIO:
        return "overrepresented"
    return "healthy"


def _maturity(expert: int, working: int, learning: int) -> tuple[float, str]:
    holders = expert + working + learning
    if holders == 0:
        return 0.0, "None"
    weighted = (expert * LEVEL_WEIGHT[SkillLevel.expert]
                + working * LEVEL_WEIGHT[SkillLevel.working]
                + learning * LEVEL_WEIGHT[SkillLevel.learning])
    pct = round(100.0 * weighted / holders, 1)
    label = next(name for floor, name in MATURITY_BANDS if pct >= floor)
    return pct, label


def _supply_demand_rows(facts: _Facts, scope: Scope) -> list[SkillSupplyDemand]:
    headcount = len(scope.employee_ids)
    skill_ids = set(facts.holders) | set(facts.declared_demand) | set(facts.inferred_demand)

    rows: list[SkillSupplyDemand] = []
    for skill_id in skill_ids:
        skill = facts.skills.get(skill_id)
        if skill is None:
            continue
        levels = facts.holders.get(skill_id, {})
        expert = len(levels.get(SkillLevel.expert, ()))
        working = len(levels.get(SkillLevel.working, ()))
        learning = len(levels.get(SkillLevel.learning, ()))
        capable = expert + working
        holder_count = expert + working + learning

        declared = facts.declared_demand.get(skill_id, set())
        inferred = facts.inferred_demand.get(skill_id, set())
        demand_projects = declared | inferred
        demand = len(demand_projects)
        basis = "declared" if declared else ("inferred" if inferred else "none")

        maturity_pct, maturity_label = _maturity(expert, working, learning)
        rows.append(SkillSupplyDemand(
            skill_id=skill_id, skill=skill.name, category=skill.category.value,
            expert_count=expert, working_count=working, learning_count=learning,
            capable_count=capable, holder_count=holder_count,
            demand_project_count=demand, demand_basis=basis,
            declared_project_count=len(declared),
            supply_per_project=round(capable / demand, 2) if demand else None,
            verdict=_verdict(capable, demand, holder_count),
            single_point_of_failure=demand > 0 and capable <= SINGLE_POINT_MAX_CAPABLE,
            coverage_pct=_pct(holder_count, headcount),
            maturity_pct=maturity_pct, maturity_label=maturity_label,
        ))

    # Verdict first, shortfall second. Sorting on shortfall alone put every
    # skill with no demand and no capable holders — a shortfall of zero —
    # directly beneath the real shortages, which is where a reader's eye
    # goes and the least informative thing in the table. Grouping by verdict
    # means the understaffed block is the block at the top, and inside it
    # the widest gap leads; ties break on demand, so a skill six projects
    # depend on outranks one that two do at the same shortfall.
    rows.sort(key=lambda r: (
        # Skills with no capable holder AND no demand go last whatever
        # their verdict. They are vocabulary, not a finding — names that
        # exist in the skills table because a document mentioned them once.
        # Left in the natural verdict order they surfaced directly beneath
        # the real shortages, which is the most-read part of the table.
        r.capable_count == 0 and r.demand_project_count == 0,
        VERDICT_ORDER.get(r.verdict, 9),
        -(r.demand_project_count - r.capable_count),
        -r.demand_project_count, -r.holder_count, r.skill,
    ))
    return rows


def skill_supply_demand(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
    verdict: str | None = None, limit: int | None = None,
) -> list[SkillSupplyDemand]:
    """Every skill in scope, with its supply, its demand, and a verdict.

    Supply is people in scope; demand is active projects in scope. Both
    sides move with the scope, which is what makes the same function serve
    an org-wide HR view and one manager's team without the numbers meaning
    something different in each.
    """
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    rows = _supply_demand_rows(_gather(db, scope), scope)
    if verdict:
        rows = [r for r in rows if r.verdict == verdict]
    if limit is not None:
        rows = rows[:limit]
    _write_audit(db, caller, "dashboard_skill_supply_demand", scope, len(rows),
                 ["skill", "levels", "demand_project_count", "verdict"])
    return rows


def skill_detail(
    db: Session, caller: AuthenticatedUser, skill_id: int, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
) -> SkillDetail | None:
    """Everything behind one slice of the team-skills chart.

    Returns None for a skill nobody in scope holds and no project in scope
    depends on — a 404 rather than an all-zeros card, because a card full of
    zeros looks like a measurement and this is an absence.
    """
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)

    skill = facts.skills.get(skill_id)
    if skill is None:
        return None
    canonical_id = skill.id
    levels = facts.holders.get(canonical_id, {})
    declared = facts.declared_demand.get(canonical_id, set())
    inferred = facts.inferred_demand.get(canonical_id, set())
    if not levels and not declared and not inferred:
        return None

    expert = levels.get(SkillLevel.expert, [])
    working = levels.get(SkillLevel.working, [])
    learning = levels.get(SkillLevel.learning, [])
    capable = len(expert) + len(working)
    holder_count = capable + len(learning)
    demand = len(declared | inferred)

    holders: list[SkillHolder] = []
    for level, ids in ((SkillLevel.expert, expert), (SkillLevel.working, working),
                       (SkillLevel.learning, learning)):
        for emp_id in ids:
            person = facts.by_id.get(emp_id)
            if person is None:
                continue
            holders.append(SkillHolder(
                id=person.id, full_name=person.full_name, job_title=person.job_title,
                org_unit=facts.unit_names.get(person.org_unit_id, ""), level=level.value,
            ))
    # Expert first, then alphabetical inside each level — the order someone
    # scanning for "who can I ask" reads in.
    order = {SkillLevel.expert.value: 0, SkillLevel.working.value: 1, SkillLevel.learning.value: 2}
    holders.sort(key=lambda h: (order[h.level], h.full_name))
    truncated = len(holders) > MAX_SKILL_HOLDERS

    capable_ids = set(expert) | set(working)
    uses: list[SkillProjectUse] = []
    for pid in sorted(declared | inferred):
        project = facts.projects.get(pid)
        if project is None:
            continue
        member_ids = facts.project_members.get(pid, [])
        uses.append(SkillProjectUse(
            project_id=pid, project_name=project.name,
            basis="declared" if pid in declared else "inferred",
            member_count=len(member_ids),
            capable_member_count=sum(1 for m in member_ids if m in capable_ids),
        ))
    uses.sort(key=lambda u: (u.basis != "declared", u.capable_member_count, u.project_name))

    risk, risk_reason = _skill_risk(capable, len(expert), demand, uses)
    maturity_pct, maturity_label = _maturity(len(expert), len(working), len(learning))

    _write_audit(db, caller, "dashboard_skill_detail", scope, holder_count,
                 ["skill", "levels", "holders", "projects", "risk"])
    return SkillDetail(
        scope=_scope_out(db, scope), skill_id=canonical_id, skill=skill.name,
        category=skill.category.value,
        expert_count=len(expert), working_count=len(working), learning_count=len(learning),
        capable_count=capable, holder_count=holder_count,
        coverage_pct=_pct(holder_count, len(scope.employee_ids)),
        maturity_pct=maturity_pct, maturity_label=maturity_label,
        demand_project_count=demand,
        supply_per_project=round(capable / demand, 2) if demand else None,
        verdict=_verdict(capable, demand, holder_count),
        risk=risk, risk_reason=risk_reason,
        holders=holders[:MAX_SKILL_HOLDERS], holders_truncated=truncated,
        projects=uses,
    )


def _skill_risk(capable: int, experts: int, demand: int,
                uses: list[SkillProjectUse]) -> tuple[str, str]:
    """Risk for one skill, stated with the count that produced it.

    Every branch returns its own reason rather than a shared template: a
    dashboard that says "high risk" without saying which number made it high
    is asking to be ignored, and the numbers differ by branch.
    """
    uncovered = [u for u in uses if u.capable_member_count == 0]
    if demand > 0 and capable == 0:
        return "high", (f"{_n(demand, 'active project')} "
                        f"{'depends' if demand == 1 else 'depend'} on this skill and nobody in "
                        "scope holds it at Working or above.")
    if demand > 0 and capable <= SINGLE_POINT_MAX_CAPABLE:
        return "high", f"One person covers this skill across {_n(demand, 'active project')}."
    if uncovered:
        names = ", ".join(u.project_name for u in uncovered[:3])
        return "high", (f"{_n(len(uncovered), 'active project')} using it "
                        f"{'has' if len(uncovered) == 1 else 'have'} no capable member "
                        f"assigned ({names}).")
    if demand > 0 and capable < demand:
        return "medium", (f"{_n(capable, 'capable person', 'capable people')} against "
                          f"{_n(demand, 'active project')}.")
    if experts == 0 and capable > 0:
        return "medium", "Working-level coverage only — no Expert to escalate to."
    if demand == 0:
        return "low", "No active project in scope currently depends on this skill."
    return "low", (f"{_n(capable, 'capable person', 'capable people')} against "
                   f"{_n(demand, 'active project')}.")


# ---------------------------------------------------------------------------
# Training / course compliance
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _TrainingCell:
    """One (person, expected course) pair, bucketed."""

    employee: Employee
    course: TrainingCourse
    status: CourseStatus
    due_on: date | None
    bucket: str
    has_record: bool


def _bucket(status: CourseStatus, due_on: date | None, today: date, due_soon_days: int) -> str:
    """Four mutually exclusive buckets, evaluated in this order.

    Exclusive matters: these render as one donut, and a person who is both
    overdue and not-started must be counted once. "Outstanding" is the
    residual — not completed, and either no deadline is recorded or the
    deadline is far enough out that it isn't yet news. A course with no
    recorded deadline can never be overdue; nothing in the data says when it
    was due, so nothing here decides it was.
    """
    if status is CourseStatus.completed:
        return "completed"
    if due_on is not None and due_on < today:
        return "overdue"
    if due_on is not None and today <= due_on <= today + timedelta(days=due_soon_days):
        return "due_soon"
    return "outstanding"


def _training_cells(db: Session, facts: _Facts, *, today: date, due_soon_days: int) -> list[_TrainingCell]:
    """Expectations joined to reported status, for everyone in scope.

    Reads employee_course_statuses directly rather than going through a
    CertificationProvider, which is the one place in this codebase that
    abstraction is stepped around. The provider interface is per-employee by
    design (get_status / list_statuses), and an org-wide dashboard would
    turn that into 545 provider calls per page load. The table is the
    synthetic provider's own source of truth today and, once
    ENABLE_TRAINING_API_SYNC is on, a cache of the last status we saw from
    the training system — which is the right thing for an aggregate to read
    anyway, since a dashboard should not fan out live calls to another
    team's API. `no_record_count` in the response reports how much of the
    picture came from absence rather than a reported row, so the UI never
    presents an inference as a measurement.
    """
    if not facts.employees:
        return []

    due_by_employee = course_due_dates_bulk(db, facts.employees)
    courses = {c.code: c for c in db.execute(select(TrainingCourse)).scalars().all()}

    reported: dict[tuple[str, int], EmployeeCourseStatus] = {
        (row.employee_id, row.course_id): row
        for row in db.execute(
            select(EmployeeCourseStatus)
            .where(EmployeeCourseStatus.employee_id.in_([e.id for e in facts.employees]))
        ).scalars().all()
    }

    cells: list[_TrainingCell] = []
    for employee in facts.employees:
        for code, due_on in due_by_employee.get(employee.id, {}).items():
            course = courses.get(code)
            if course is None:
                continue
            row = reported.get((employee.id, course.id))
            # No row against an EXPECTED course means not started — the one
            # place that inference is legitimate, and the same one
            # app/certifications/synthetic.py makes. It is counted
            # separately (has_record) so the total can be qualified.
            status = row.status if row is not None else CourseStatus.not_started
            cells.append(_TrainingCell(
                employee=employee, course=course, status=status, due_on=due_on,
                bucket=_bucket(status, due_on, today, due_soon_days),
                has_record=row is not None,
            ))
    return cells


def _buckets_of(cells: list[_TrainingCell]) -> TrainingBuckets:
    counts = defaultdict(int)
    for cell in cells:
        counts[cell.bucket] += 1
    expected = len(cells)
    completed = counts["completed"]
    return TrainingBuckets(
        expected=expected,
        completed=completed,
        incomplete=expected - completed,
        overdue=counts["overdue"],
        due_soon=counts["due_soon"],
        outstanding=counts["outstanding"],
        compliance_pct=_pct(completed, expected),
    )


def training_analytics(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
    course_code: str | None = None, due_soon_days: int = DUE_SOON_DAYS,
    today: date | None = None,
) -> TrainingAnalytics:
    """Course compliance for the scope, whole and broken down.

    The unit counted is the (person, expected course) PAIR, not the person:
    someone expected to do three courses who has finished two is two-thirds
    compliant, and counting people would have to round that to a yes or a
    no. `employee_count` is reported alongside so the two are never
    confused.
    """
    today = today or date.today()
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)
    cells = _training_cells(db, facts, today=today, due_soon_days=due_soon_days)
    if course_code:
        cells = [c for c in cells if c.course.code == course_code]

    by_course: list[TrainingBreakdown] = []
    grouped: dict[str, list[_TrainingCell]] = defaultdict(list)
    for cell in cells:
        grouped[cell.course.code].append(cell)
    for code, group in grouped.items():
        by_course.append(TrainingBreakdown(
            key=code, label=group[0].course.name, buckets=_buckets_of(group),
            employee_count=len({c.employee.id for c in group}),
        ))
    by_course.sort(key=lambda b: (-b.buckets.overdue, -b.buckets.incomplete, b.label))

    by_unit: list[TrainingBreakdown] = []
    grouped_units: dict[int, list[_TrainingCell]] = defaultdict(list)
    for cell in cells:
        grouped_units[cell.employee.org_unit_id].append(cell)
    for unit_id, group in grouped_units.items():
        by_unit.append(TrainingBreakdown(
            key=str(unit_id), label=facts.unit_names.get(unit_id, "Unknown"),
            buckets=_buckets_of(group),
            employee_count=len({c.employee.id for c in group}),
        ))
    by_unit.sort(key=lambda b: (-b.buckets.overdue, -b.buckets.incomplete, b.label))

    courses = [
        TrainingBreakdown(key=code, label=group[0].course.name, buckets=_buckets_of(group),
                          employee_count=len({c.employee.id for c in group}))
        for code, group in sorted(grouped.items())
    ]

    result = TrainingAnalytics(
        scope=_scope_out(db, scope),
        buckets=_buckets_of(cells),
        employee_count=len({c.employee.id for c in cells}),
        no_record_count=sum(1 for c in cells if not c.has_record),
        no_deadline_count=sum(1 for c in cells if c.due_on is None),
        due_soon_days=due_soon_days,
        by_course=by_course,
        by_unit=by_unit[:25],
        courses=courses,
    )
    _write_audit(db, caller, "dashboard_training_analytics", scope, result.buckets.expected,
                 ["course", "display_status", "due_on", "org_unit"])
    return result


def training_roster(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
    bucket: str | None = None, course_code: str | None = None,
    due_soon_days: int = DUE_SOON_DAYS, today: date | None = None,
    limit: int = MAX_ROSTER_ROWS,
) -> TrainingRoster:
    """The named people behind a bucket — the drill-down, and the list
    reminders are selected from.

    Reports the two-value display status, never the four-value one. Which of
    not_started / in_progress / failed somebody is sitting in decides the
    wording of the reminder they receive (app/notifications.py) and is not
    management-facing data; the profile makes the same collapse for the same
    reason.
    """
    today = today or date.today()
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)
    cells = _training_cells(db, facts, today=today, due_soon_days=due_soon_days)

    if course_code:
        cells = [c for c in cells if c.course.code == course_code]
    if bucket == "incomplete":
        cells = [c for c in cells if c.bucket != "completed"]
    elif bucket:
        cells = [c for c in cells if c.bucket == bucket]

    cells.sort(key=lambda c: (
        c.due_on or date.max, c.employee.full_name, c.course.name,
    ))
    rows = [
        TrainingPersonRow(
            employee_id=c.employee.id, full_name=c.employee.full_name,
            job_title=c.employee.job_title,
            org_unit=facts.unit_names.get(c.employee.org_unit_id, ""),
            course_code=c.course.code, course_name=c.course.name,
            display_status="completed" if c.status is CourseStatus.completed else "not_completed",
            bucket=c.bucket, due_on=c.due_on.isoformat() if c.due_on else None,
            days_overdue=(today - c.due_on).days if c.due_on and c.due_on < today and c.bucket == "overdue" else None,
            has_record=c.has_record,
        )
        for c in cells[:limit]
    ]
    _write_audit(db, caller, "dashboard_training_roster", scope, len(rows),
                 ["full_name", "job_title", "org_unit", "course", "display_status", "due_on"])
    return TrainingRoster(rows=rows, total=len(cells), truncated=len(cells) > limit)


def send_reminders(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    employee_ids: list[str], course_code: str | None = None,
    org_unit_id: int | None = None, manager_id: str | None = None,
    due_soon_days: int = DUE_SOON_DAYS, today: date | None = None,
) -> ReminderResult:
    """Nudge the selected people about their outstanding courses.

    Two gates, not one. resolve_scope decides whose dashboard this is, and
    then every requested employee id is checked against that scope — so a
    manager cannot remind someone outside their line by posting an id the UI
    never offered them, and HR scoped to one department cannot accidentally
    mail the company by leaving a stale selection in the request. Ids
    outside scope are dropped and counted, not rejected: the same
    redact-never-reject stance the rest of the API takes.

    With no course_code, every outstanding course for each selected person
    is reminded — one notification per (person, course), which is how the
    employee's inbox already works.
    """
    today = today or date.today()
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)
    cells = _training_cells(db, facts, today=today, due_soon_days=due_soon_days)

    requested = list(dict.fromkeys(employee_ids))
    in_scope = [i for i in requested if i in scope.employee_ids]
    out_of_scope = [i for i in requested if i not in scope.employee_ids]

    selected = set(in_scope)
    targets = [
        ReminderTarget(employee=c.employee, course=c.course, status=c.status, due_on=c.due_on)
        for c in cells
        if c.employee.id in selected
        and c.bucket != "completed"
        and (course_code is None or c.course.code == course_code)
    ]

    sent = send_course_reminders(db, actor_id=caller.id, targets=targets, on_date=today)
    db.commit()

    reminded = {n.recipient_id for n in sent}
    _write_audit(db, caller, "dashboard_send_reminders", scope, len(sent),
                 ["recipient_id", "course", "body"])
    return ReminderResult(
        requested=len(requested),
        eligible=len(targets),
        sent=len(sent),
        recipients_notified=len(reminded),
        out_of_scope=len(out_of_scope),
        skipped=len(targets) - len(sent),
        detail=(
            "Already reminded today, or the recipient is no longer active."
            if len(targets) > len(sent) else ""
        ),
    )


# ---------------------------------------------------------------------------
# Project staffing coverage
# ---------------------------------------------------------------------------

def project_coverage(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
) -> list[ProjectCoverage]:
    """Active projects in scope, and whether their declared skill needs are
    met by the people currently on them.

    Only projects with DECLARED required skills get a coverage verdict.
    Inferring a project's requirements from what its members happen to know
    and then checking whether its members know it is circular — it would
    report 100% coverage for every project in the company and mean nothing.
    Those projects are returned with requirements_recorded=False so the UI
    can prompt for the missing data instead of showing a fabricated pass.
    """
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)
    if not facts.scope_project_ids:
        return []

    requirements: dict[int, list[tuple[int, SkillLevel]]] = defaultdict(list)
    for row in db.execute(
        select(ProjectSkillRequirement.project_id, ProjectSkillRequirement.skill_id,
               ProjectSkillRequirement.minimum_level)
        .where(ProjectSkillRequirement.project_id.in_(facts.scope_project_ids))
    ).all():
        canonical = facts.skills.get(row.skill_id)
        if canonical is not None:
            requirements[row.project_id].append((canonical.id, row.minimum_level))

    # Levels for every current member of every scope project — including
    # members outside the scope. Coverage is a fact about the PROJECT, and a
    # manager asking whether their engagement is covered is not helped by an
    # answer that ignores the two people on it from another team.
    member_ids = {i for pid in facts.scope_project_ids for i in facts.project_members.get(pid, ())}
    member_levels: dict[str, dict[int, SkillLevel]] = defaultdict(dict)
    if member_ids:
        for row in db.execute(
            select(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level)
            .where(EmployeeSkill.employee_id.in_(member_ids))
        ).all():
            canonical = facts.skills.get(row.skill_id)
            if canonical is not None:
                member_levels[row.employee_id][canonical.id] = row.level

    rank = {SkillLevel.learning: 0, SkillLevel.working: 1, SkillLevel.expert: 2}
    out: list[ProjectCoverage] = []
    for pid in facts.scope_project_ids:
        project = facts.projects.get(pid)
        if project is None:
            continue
        members = facts.project_members.get(pid, [])
        needs = requirements.get(pid, [])
        covered, gaps, thin = 0, [], []
        for skill_id, minimum in needs:
            who = [
                m for m in members
                if skill_id in member_levels.get(m, {})
                and rank[member_levels[m][skill_id]] >= rank[minimum]
            ]
            name = facts.skills[skill_id].name if skill_id in facts.skills else str(skill_id)
            if not who:
                gaps.append(name)
            else:
                covered += 1
                if len(who) == 1:
                    thin.append(name)
        out.append(ProjectCoverage(
            project_id=pid, project_name=project.name, project_type=project.type.value,
            is_client_engagement=project.is_client_engagement,
            member_count=len(members),
            in_scope_member_count=sum(1 for m in members if m in scope.employee_ids),
            requirements_recorded=bool(needs),
            required_skill_count=len(needs),
            covered_skill_count=covered,
            coverage_pct=_pct(covered, len(needs)) if needs else None,
            gap_skills=sorted(gaps),
            single_cover_skills=sorted(thin),
            risk=("high" if gaps else "medium" if thin else "low") if needs else "unknown",
        ))

    # Projects that can be judged come first, worst coverage at the top;
    # unrecorded ones sink to the bottom where they read as a data gap
    # rather than as a clean bill of health.
    out.sort(key=lambda p: (not p.requirements_recorded, p.coverage_pct if p.coverage_pct is not None else 101,
                            -len(p.gap_skills), p.project_name))
    _write_audit(db, caller, "dashboard_project_coverage", scope, len(out),
                 ["project_name", "required_skills", "coverage_pct"])
    return out


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def overview(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
    due_soon_days: int = DUE_SOON_DAYS, today: date | None = None,
) -> DashboardOverview:
    """The headline row, plus the summaries the sections below it expand on.

    Every figure here is recomputed from the same _Facts the detailed
    sections use, so the tile and the table under it cannot disagree.
    """
    today = today or date.today()
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)

    unit_ids = {e.org_unit_id for e in facts.employees}
    unit_rows = {u.id: u for u in db.execute(select(OrgUnit).where(OrgUnit.id.in_(unit_ids or {0}))).scalars().all()}
    # Departments a scope touches are counted by walking each employee's
    # unit UP to its department, not by counting distinct units: everybody
    # sits in a team, so counting units would report 52 "departments".
    from app.certifications.requirements import org_unit_ancestor_ids
    all_units = _all_units(db)
    departments: set[int] = set()
    divisions: set[int] = set()
    for unit_id in unit_ids:
        for ancestor in org_unit_ancestor_ids(db, unit_id):
            unit = all_units.get(ancestor)
            if unit is None:
                continue
            if unit.unit_type == "department":
                departments.add(unit.id)
            elif unit.unit_type == "division":
                divisions.add(unit.id)

    people_with_skills = len({
        emp_id
        for levels in facts.holders.values()
        for ids in levels.values()
        for emp_id in ids
    })
    skill_rows = sum(len(ids) for levels in facts.holders.values() for ids in levels.values())
    headcount = len(scope.employee_ids)

    sd = _supply_demand_rows(facts, scope)
    cells = _training_cells(db, facts, today=today, due_soon_days=due_soon_days)

    managers = sum(
        1 for e in facts.employees
        if db.execute(
            select(Employee.id).where(Employee.manager_id == e.id, Employee.is_active == True).limit(1)  # noqa: E712
        ).first() is not None
    ) if headcount <= 200 else None

    result = DashboardOverview(
        scope=_scope_out(db, scope),
        headcount=headcount,
        department_count=len(departments),
        division_count=len(divisions),
        team_count=len(unit_rows),
        manager_count=managers,
        active_project_count=len(facts.scope_project_ids),
        client_engagement_count=sum(1 for p in facts.projects.values() if p.is_client_engagement),
        skill_count=len([r for r in sd if r.holder_count > 0]),
        expert_count=sum(r.expert_count for r in sd),
        people_with_skills=people_with_skills,
        skill_profile_coverage_pct=_pct(people_with_skills, headcount),
        avg_skills_per_person=round(skill_rows / headcount, 1) if headcount else 0.0,
        understaffed_skill_count=sum(1 for r in sd if r.verdict == "understaffed"),
        healthy_skill_count=sum(1 for r in sd if r.verdict == "healthy"),
        overrepresented_skill_count=sum(1 for r in sd if r.verdict == "overrepresented"),
        unused_skill_count=sum(1 for r in sd if r.verdict == "unused"),
        single_point_skill_count=sum(1 for r in sd if r.single_point_of_failure),
        training=_buckets_of(cells),
        training_employee_count=len({c.employee.id for c in cells}),
        due_soon_days=due_soon_days,
    )
    _write_audit(db, caller, "dashboard_overview", scope, headcount,
                 ["headcount", "skills", "projects", "training"])
    return result


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

def insights(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work", *,
    org_unit_id: int | None = None, manager_id: str | None = None,
    due_soon_days: int = DUE_SOON_DAYS, today: date | None = None,
) -> list[WorkforceInsight]:
    """Risk and development signals, derived — never generated.

    This is the section a dashboard would normally hand to a language model.
    It doesn't. Every item below is a rule over data this app already holds,
    stated with the counts that triggered it, so an HR lead can check the
    claim against the table it came from. That also means an insight can be
    absent: when nothing crosses a threshold, this returns fewer items
    rather than padding the section with observations.

    Severity is comparable across kinds — "high" means the same urgency
    whether the subject is a skill or a course — so the UI can sort one list
    rather than five.
    """
    today = today or date.today()
    scope = resolve_scope(db, caller, view_mode, org_unit_id=org_unit_id, manager_id=manager_id)
    facts = _gather(db, scope)
    sd = _supply_demand_rows(facts, scope)
    cells = _training_cells(db, facts, today=today, due_soon_days=due_soon_days)
    headcount = len(scope.employee_ids)

    out: list[WorkforceInsight] = []

    # --- 1. skill shortage ------------------------------------------------
    short = [r for r in sd if r.verdict == "understaffed"][:MAX_INSIGHT_EVIDENCE]
    if short:
        worst = short[0]
        out.append(WorkforceInsight(
            kind="skill_shortage",
            severity="high" if worst.capable_count == 0 else "medium",
            title=(f"{_n(len([r for r in sd if r.verdict == 'understaffed']), 'skill')} "
                   f"{'has' if len([r for r in sd if r.verdict == 'understaffed']) == 1 else 'have'} "
                   "fewer capable people than active projects needing them"),
            detail=(f"{worst.skill} is the widest gap: {worst.capable_count} at Working or above "
                    f"against {_n(worst.demand_project_count, 'active project')}."),
            evidence=[f"{r.skill}: {_n(r.capable_count, 'capable person', 'capable people')} / "
                      f"{_n(r.demand_project_count, 'project')}" for r in short],
            skill_ids=[r.skill_id for r in short],
            recommendation="Prioritise hiring or targeted upskilling on these before taking on more work that depends on them.",
        ))

    # --- 2. concentrated expertise (bus factor) --------------------------
    concentrated = [r for r in sd if r.single_point_of_failure][:MAX_INSIGHT_EVIDENCE]
    if concentrated:
        out.append(WorkforceInsight(
            kind="skill_concentration",
            severity="high",
            title=(f"{_n(sum(1 for r in sd if r.single_point_of_failure), 'skill')} "
                   f"{'rests' if sum(1 for r in sd if r.single_point_of_failure) == 1 else 'rest'} "
                   "on a single person"),
            detail=("One departure or absence removes the capability entirely. "
                    f"{concentrated[0].skill} covers "
                    f"{_n(concentrated[0].demand_project_count, 'active project')} on one person."),
            evidence=[f"{r.skill}: 1 capable person, {_n(r.demand_project_count, 'project')}"
                      for r in concentrated],
            skill_ids=[r.skill_id for r in concentrated],
            recommendation="Pair a second person onto each of these — a Learning-level colleague on the same project is the cheapest cover.",
        ))

    # --- 3. training compliance ------------------------------------------
    buckets = _buckets_of(cells)
    if buckets.overdue > 0:
        by_unit: dict[int, int] = defaultdict(int)
        for cell in cells:
            if cell.bucket == "overdue":
                by_unit[cell.employee.org_unit_id] += 1
        worst_units = sorted(by_unit.items(), key=lambda kv: -kv[1])[:MAX_INSIGHT_EVIDENCE]
        overdue_rate = _pct(buckets.overdue, buckets.expected)
        out.append(WorkforceInsight(
            kind="training_compliance",
            severity="high" if overdue_rate >= 15 else "medium",
            title=(f"{_n(buckets.overdue, 'required course')} "
                   f"{'is' if buckets.overdue == 1 else 'are'} past their due date"),
            detail=(f"{overdue_rate}% of all course expectations in scope are overdue, and "
                    f"{buckets.due_soon} more fall due within {due_soon_days} days."),
            evidence=[f"{facts.unit_names.get(uid, 'Unknown')}: {n} overdue" for uid, n in worst_units],
            recommendation="Send reminders from the Training section — the overdue list is already filtered to exactly these people.",
        ))

    # --- 4. project staffing gaps ----------------------------------------
    coverage = [p for p in project_coverage(db, caller, view_mode,
                                            org_unit_id=org_unit_id, manager_id=manager_id)
                if p.requirements_recorded and p.gap_skills]
    if coverage:
        out.append(WorkforceInsight(
            kind="project_staffing_gap",
            severity="high",
            title=(f"{_n(len(coverage), 'active project')} "
                   f"{'is' if len(coverage) == 1 else 'are'} missing a declared required skill"),
            detail=(f"{coverage[0].project_name} has nobody assigned who meets "
                    f"{len(coverage[0].gap_skills)} of its "
                    f"{_n(coverage[0].required_skill_count, 'recorded requirement')}."),
            evidence=[f"{p.project_name}: missing {', '.join(p.gap_skills[:3])}" for p in coverage[:MAX_INSIGHT_EVIDENCE]],
            project_ids=[p.project_id for p in coverage[:MAX_INSIGHT_EVIDENCE]],
            recommendation="Staff or upskill against the missing skills, or correct the requirement if it no longer reflects the work.",
        ))

    # --- 5. skill-profile coverage ---------------------------------------
    people_with_skills = len({
        emp_id for levels in facts.holders.values() for ids in levels.values() for emp_id in ids
    })
    missing = headcount - people_with_skills
    if headcount and _pct(missing, headcount) >= 10:
        out.append(WorkforceInsight(
            kind="profile_coverage",
            severity="medium" if _pct(missing, headcount) < 25 else "high",
            title=(f"{_n(missing, 'person', 'people')} "
                   f"{'has' if missing == 1 else 'have'} no skills recorded"),
            detail=(f"{_pct(missing, headcount)}% of this scope is invisible to every skill query above — "
                    "shortages and concentrations here are understated by exactly that much."),
            recommendation="Ask them to add their own skills from their profile; it is self-service and needs no approval.",
        ))

    # --- 6. bench capacity ------------------------------------------------
    unused = [r for r in sd if r.verdict == "unused"][:MAX_INSIGHT_EVIDENCE]
    if unused:
        out.append(WorkforceInsight(
            kind="bench_capacity",
            severity="low",
            title=(f"{_n(sum(1 for r in sd if r.verdict == 'unused'), 'skill')} "
                   f"{'is' if sum(1 for r in sd if r.verdict == 'unused') == 1 else 'are'} "
                   "held but unused by current work"),
            detail="Capability that no active project in scope currently depends on — redeployable, or at risk of going stale.",
            evidence=[f"{r.skill}: {r.capable_count} capable, no active project" for r in unused],
            skill_ids=[r.skill_id for r in unused],
            recommendation="Worth checking against upcoming work before letting it lapse.",
        ))

    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda i: rank.get(i.severity, 3))
    _write_audit(db, caller, "dashboard_insights", scope, len(out), ["kind", "severity", "title"])
    return out
