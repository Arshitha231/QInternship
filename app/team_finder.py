"""Recommend an EXISTING team for a technical need.

The sibling of app/team_builder.py and deliberately not the same thing:

    Find People    "who can help me?"        -> app/people.py
    Build Team     "who would I staff?"      -> app/team_builder.py
    Find a Team    "who do I go and ask?"    -> here

Nothing in this module creates, modifies or proposes a team. It ranks the
org units that already exist and hands back the one to knock on, with the
manager to knock on it with.

WHICH PERMISSION GATE, AND WHY NOT THE OTHER ONE
------------------------------------------------
This uses is_record_visible(caller, employee, view_mode) per employee --
the employee-DISCOVERY gate that app/directory_tools.py's skill_scarcity
and find_experts already aggregate behind, org-wide, for every caller.

It deliberately does NOT use app/analytics.py's resolve_scope, which
app/team_builder.py does use. That gate confines a manager to their own
reporting line, which is right for a workforce dashboard and wrong here:
the entire question is "which OTHER team should I talk to", and a version
of it that could only ever answer "one of yours" would not be the feature.
The two gates are different because the two questions are different --
aggregate analytics about your people versus finding a colleague to ask.

Everything returned is already universally visible: unit names, and each
manager's name, job title and work_email, all of which sit in
app/permissions.py's BASE_FIELDS. No ABAC/RBAC-gated field is read here.
Restricted employees are excluded by is_record_visible, so they are absent
from every count as well as from every name -- a headcount that included
someone the caller cannot see would leak their existence.

Confidential projects are filtered separately, through
can_see_confidential_project, because project visibility is a members-only
rule that employee visibility says nothing about.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import unit_subtree_ids
from app.auth import AuthenticatedUser
from app.models import (
    Employee,
    EmployeeProject,
    EmployeeSkill,
    OrgUnit,
    Project,
    ProjectSkillRequirement,
    Skill,
)
from app.models.enums import ProjectClassification, SkillLevel
from app.permissions import ViewMode, can_see_confidential_project, is_record_visible
from app.schemas import (
    TeamMatchSkill,
    TeamRecommendation,
    TeamRecommendationResult,
    TeamManagerRef,
)
from app.team_builder import LEVEL_WEIGHT, _skills_named_in

# Score composition, in the priority order the feature was specified with.
#
# coverage dominates because the question is "does this team know the
# thing", and depth/breadth/projects are all refinements of "how well".
# A team with one Expert beats a team with four Learners, which is what
# putting level weight inside coverage buys.
WEIGHTS: dict[str, float] = {
    "coverage": 0.50,   # relevant technical skills, weighted by best level
    "depth": 0.25,      # Expert/Working density across the needed skills
    "projects": 0.15,   # relevant projects the team actually works on
    "breadth": 0.10,    # how many people, not just how good the best one is
}

# Depth, breadth and project credit are scored RELATIVE to the strongest
# candidate for this particular question, not against fixed thresholds.
#
# Fixed thresholds were tried first and do not work. "Which team has the
# strongest Kubernetes expertise?" is a single-skill question, and any
# competent team clears an absolute bar of "two capable people, five
# relevant people, two projects" -- so the top three all came back at 100%
# and the ordering was decided by a tie-break the reader cannot see. A
# comparative question deserves a comparative score.
#
# coverage stays ABSOLUTE, because "do they know this at Expert level" is a
# real quality judgement rather than a comparison, and normalising it would
# award full marks to the least-bad team in a company where nobody knows
# the skill at all.
#
# Consequence worth stating: the leader scores 100 by construction when it
# leads on every term. match_pct means "best available match for this
# question", not "perfect team" -- which is why the response ships the raw
# Expert/Working/Learning counts beside it rather than the number alone.

MAX_RESULTS = 5
MAX_NEEDED_SKILLS = 6

# Only these are ranked. "company" is not an answer to "who should I ask",
# and "division" is too coarse to walk over to.
RANKABLE_UNIT_TYPES = frozenset({"team", "department"})


class TeamSearchUnavailable(Exception):
    """No skills could be read out of the question."""


# ---------------------------------------------------------------------------
# 1. What is being asked for -- the model's only turn
# ---------------------------------------------------------------------------

_NEED_TOOL = {
    "type": "function",
    "function": {
        "name": "name_required_skills",
        "description": (
            "Name the technical skills a question is really about, so an existing "
            "team that holds them can be found. You are naming capabilities, not people."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "maxItems": MAX_NEEDED_SKILLS,
                    "items": {"type": "string"},
                    "description": (
                        "Skills as an engineer would list them on a CV -- 'Kubernetes', "
                        "'Azure', 'Networking'. Include the ones the question implies as "
                        "well as the ones it says. Do not name a person, team or department."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": "Three or four words for what is being asked about.",
                },
            },
            "required": ["skills"],
        },
    },
}

_NEED_SYSTEM = """You turn a technical question into the skills it is about.

You do not have the employee directory. You do not name people, teams or \
departments, and you do not decide who anyone is allowed to see. You name \
capabilities.

"Which team should I talk to about an Azure networking problem?" is about \
Azure and Networking. If the text names no technical capability at all, \
return an empty list."""


@dataclass(frozen=True)
class Need:
    """Deliberately carries skills and a topic and nothing else.

    Same absence as app/team_builder.py's TeamPlan: no unit, no employee, no
    scope field, so there is nothing in the model's output that could
    address a team directly rather than describe a capability.
    """

    skills: tuple[str, ...]
    topic: str
    unrecognised: tuple[str, ...] = ()
    source: str = "model"


def _model_need(db: Session, query: str) -> Need | None:
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    if _mode() != "real":
        return None
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _NEED_SYSTEM},
                {"role": "user", "content": query},
            ],
            tools=[_NEED_TOOL],
            tool_choice="auto",
            reasoning_effort="minimal",
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            return None
        args = json.loads(calls[0].function.arguments or "{}")
    except (OpenAIError, json.JSONDecodeError, AttributeError, IndexError):
        return None
    return _need_from_args(db, query, args)


def _need_from_args(db: Session, query: str, args: dict) -> Need | None:
    """Validate the model's skills against the real table.

    Split out so the validation is testable without an Azure OpenAI client,
    same as app/team_builder.py's _plan_from_args.
    """
    raw = args.get("skills")
    if not isinstance(raw, list):
        return None
    canonical: list[str] = []
    unrecognised: list[str] = []
    for s in raw[:MAX_NEEDED_SKILLS]:
        if not isinstance(s, str) or not s.strip():
            continue
        resolved = _resolve(db, s.strip())
        if resolved is None:
            if s.strip() not in unrecognised:
                unrecognised.append(s.strip()[:60])
        elif resolved not in canonical:
            canonical.append(resolved)
    if not canonical:
        return Need(skills=(), topic=_topic_of(query), unrecognised=tuple(unrecognised[:8]),
                    source="model")
    return Need(skills=tuple(canonical), topic=str(args.get("topic") or _topic_of(query))[:60],
                unrecognised=tuple(unrecognised[:8]), source="model")


def _resolve(db: Session, name: str) -> str | None:
    from app.people import resolve_skill

    skill = resolve_skill(db, name)
    return skill.name if skill is not None else None


def _topic_of(query: str) -> str:
    cleaned = re.sub(
        r"^\s*(which|what)\s+(team|department|group)\s+(should i (talk to|ask|contact)\s*)?"
        r"(has|have|about|for|with)?\s*",
        "", query.strip(), flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.")
    return (cleaned[:60] or "this capability")


def read_need(db: Session, query: str) -> Need:
    """Model first, literal skill names second.

    No invented default: a question naming no capability this directory
    tracks has no answer, and ranking every team by nothing would produce a
    confident-looking list that means nothing.
    """
    need = _model_need(db, query)
    if need is not None and need.skills:
        return need

    named = _skills_named_in(db, query)
    if named:
        return Need(skills=tuple(named[:MAX_NEEDED_SKILLS]), topic=_topic_of(query),
                    unrecognised=need.unrecognised if need else (), source="derived")
    return Need(skills=(), topic=_topic_of(query),
                unrecognised=need.unrecognised if need else (), source="derived")


# ---------------------------------------------------------------------------
# 2. The visible workforce -- loaded once, filtered before anything is counted
# ---------------------------------------------------------------------------

@dataclass
class Workforce:
    employees: dict[str, Employee] = field(default_factory=dict)
    by_unit: dict[int, list[str]] = field(default_factory=dict)
    levels: dict[str, dict[str, SkillLevel]] = field(default_factory=dict)
    units: dict[int, OrgUnit] = field(default_factory=dict)
    # project id -> (project, current member ids) for projects the caller may see
    projects: dict[int, tuple[Project, set[str]]] = field(default_factory=dict)
    declared: dict[int, set[str]] = field(default_factory=dict)


def load_workforce(db: Session, caller: AuthenticatedUser, view_mode: ViewMode) -> Workforce:
    """Every active employee the caller is permitted to discover.

    The visibility filter runs HERE, before any aggregation, which is what
    makes the counts safe: a headcount computed over all employees and then
    displayed to someone who cannot see one of them still discloses that
    person's existence.
    """
    wf = Workforce()
    rows = db.execute(select(Employee).where(Employee.is_active == True)).scalars().all()  # noqa: E712
    for e in rows:
        if not is_record_visible(caller, e, view_mode):
            continue
        wf.employees[e.id] = e
        if e.org_unit_id is not None:
            wf.by_unit.setdefault(e.org_unit_id, []).append(e.id)

    wf.units = {u.id: u for u in db.execute(select(OrgUnit)).scalars().all()}

    visible_ids = list(wf.employees)
    if not visible_ids:
        return wf

    for emp_id, skill_name, level in db.execute(
        select(EmployeeSkill.employee_id, Skill.name, EmployeeSkill.level)
        .join(Skill, Skill.id == EmployeeSkill.skill_id)
        .where(EmployeeSkill.employee_id.in_(visible_ids))
    ).all():
        wf.levels.setdefault(emp_id, {})[skill_name] = level

    # "Active" here means a CURRENT membership (end_date is null) -- the same
    # reading app/analytics.py uses. A finished project says less about who
    # to go and ask today.
    members: dict[int, set[str]] = {}
    for emp_id, pid in db.execute(
        select(EmployeeProject.employee_id, EmployeeProject.project_id)
        .where(EmployeeProject.employee_id.in_(visible_ids),
               EmployeeProject.end_date.is_(None))
    ).all():
        members.setdefault(pid, set()).add(emp_id)

    if members:
        for project in db.execute(
            select(Project).where(Project.id.in_(list(members)))
        ).scalars().all():
            # Confidential projects are members-only, with no role or
            # manager bypass -- a separate rule from employee visibility,
            # so it gets a separate check.
            if (project.classification is ProjectClassification.confidential
                    and not can_see_confidential_project(db, caller, project.id)):
                continue
            wf.projects[project.id] = (project, members[project.id])

        for pid, sname in db.execute(
            select(ProjectSkillRequirement.project_id, Skill.name)
            .join(Skill, Skill.id == ProjectSkillRequirement.skill_id)
            .where(ProjectSkillRequirement.project_id.in_(list(wf.projects)))
        ).all():
            wf.declared.setdefault(pid, set()).add(sname)

    return wf


def unit_head(wf: Workforce, member_ids: list[str]) -> Employee | None:
    """Who to actually contact.

    org_units has no manager column, so the head is derived: among the
    unit's own members, whoever does not report to someone else inside it,
    tie-broken by how many of the unit's people report to them.

    Checked against the seeded org before being relied on -- 43 of 52 teams
    have exactly one member reporting outside, and the reports-inside
    tie-break resolves the remaining 9 and every department, for 75/75
    units with a unique head. Falls back to the most-reported-to member so
    a unit whose head is invisible to this caller still gets a contact
    rather than none.
    """
    members = [wf.employees[i] for i in member_ids if i in wf.employees]
    if not members:
        return None
    inside = {m.id for m in members}
    heads = [m for m in members if m.manager_id not in inside] or members
    return sorted(
        heads,
        key=lambda h: (-sum(1 for m in members if m.manager_id == h.id), h.full_name),
    )[0]


# ---------------------------------------------------------------------------
# 3. Ranking -- pure arithmetic over the filtered workforce
# ---------------------------------------------------------------------------

@dataclass
class _Raw:
    """A unit's unnormalised parts, before the comparative pass."""

    rec: TeamRecommendation
    coverage: float
    capable: int
    relevant: int
    projects: int


def _score_unit(wf: Workforce, unit: OrgUnit, member_ids: list[str],
                skills: tuple[str, ...], db: Session) -> "_Raw | None":
    matched: list[TeamMatchSkill] = []
    total_coverage = 0.0
    capable = 0
    relevant_people: set[str] = set()

    for skill in skills:
        holders = [(mid, wf.levels[mid][skill]) for mid in member_ids
                   if skill in wf.levels.get(mid, {})]
        if not holders:
            matched.append(TeamMatchSkill(skill=skill, expert=0, working=0, learning=0, total=0))
            continue
        expert = sum(1 for _, lv in holders if lv is SkillLevel.expert)
        working = sum(1 for _, lv in holders if lv is SkillLevel.working)
        learning = sum(1 for _, lv in holders if lv is SkillLevel.learning)
        # By LEVEL_WEIGHT, not by enum ordering -- SkillLevel is a str enum,
        # so max() on it compares alphabetically ("Working" > "Learning" >
        # "Expert") and would call a Learning holder the team's best.
        best = max((lv for _, lv in holders), key=lambda lv: LEVEL_WEIGHT[lv])
        total_coverage += LEVEL_WEIGHT[best]
        capable += expert + working
        relevant_people.update(mid for mid, _ in holders)
        matched.append(TeamMatchSkill(skill=skill, expert=expert, working=working,
                                      learning=learning, total=len(holders)))

    if not relevant_people:
        return None  # not a candidate at all -- nobody here holds any of it

    coverage = total_coverage / len(skills)

    wanted = set(skills)
    member_set = set(member_ids)
    projects: list[str] = []
    for pid, (project, current) in wf.projects.items():
        here = current & member_set
        if not here:
            continue
        # Relevant if the project DECLARES one of the needed skills, or if
        # someone from this unit on it holds one. Declared is the stronger
        # evidence (see app/models/project_skill_requirement.py); the second
        # is what keeps projects that were never annotated from vanishing.
        if wf.declared.get(pid, set()) & wanted or any(
                wanted & set(wf.levels.get(mid, {})) for mid in here):
            projects.append(project.name)
    projects.sort()

    head = unit_head(wf, member_ids)
    rec = TeamRecommendation(
        org_unit_id=unit.id,
        name=unit.name,
        unit_type=unit.unit_type,
        match_pct=0,  # filled by _apply_relative_scores once all units are in
        headcount=len(member_ids),
        relevant_people=len(relevant_people),
        skills=matched,
        projects=projects[:5],
        manager=_manager_ref(head),
        why=_why(unit, matched, projects, len(relevant_people)),
    )
    return _Raw(rec=rec, coverage=coverage, capable=capable,
                relevant=len(relevant_people), projects=len(projects))


def _apply_relative_scores(raws: list["_Raw"]) -> list[TeamRecommendation]:
    """Second pass: normalise the comparative terms against the field.

    Runs only once every candidate is known, which is why scoring is split
    in two -- a unit cannot know how strong it is relative to units that
    have not been looked at yet.
    """
    if not raws:
        return []
    top_capable = max(r.capable for r in raws) or 1
    top_relevant = max(r.relevant for r in raws) or 1
    top_projects = max(r.projects for r in raws) or 1

    out: list[TeamRecommendation] = []
    for r in raws:
        score = (
            WEIGHTS["coverage"] * r.coverage
            + WEIGHTS["depth"] * (r.capable / top_capable)
            + WEIGHTS["projects"] * (r.projects / top_projects)
            + WEIGHTS["breadth"] * (r.relevant / top_relevant)
        )
        out.append(r.rec.model_copy(update={"match_pct": round(score * 100)}))
    return out


def _manager_ref(head: Employee | None) -> TeamManagerRef | None:
    """Name, title and work_email only -- all BASE_FIELDS, visible to every
    caller who can see the record at all. Nothing gated is read here."""
    if head is None:
        return None
    return TeamManagerRef(
        employee_id=head.id,
        full_name=head.full_name,
        job_title=head.job_title or "",
        work_email=head.work_email,
    )


def _why(unit: OrgUnit, matched: list[TeamMatchSkill], projects: list[str],
         people: int) -> str:
    """Deterministic, and built from the same counts that produced the
    score, so the sentence cannot claim something the numbers do not."""
    strong = [m for m in matched if m.expert > 0]
    working_only = [m for m in matched if m.expert == 0 and m.working > 0]
    bits: list[str] = []
    if strong:
        names = ", ".join(m.skill for m in strong[:3])
        bits.append(f"Expert-level {names}")
    if working_only:
        names = ", ".join(m.skill for m in working_only[:2])
        bits.append(f"working knowledge of {names}")
    if not bits:
        bits.append("some exposure to the skills asked about")
    # Capitalise the first CHARACTER only. str.capitalize() lowercases the
    # rest of the string, which turned "Expert-level Kubernetes" into
    # "Expert-level kubernetes" -- skill names are proper nouns here.
    sentence = " and ".join(bits)
    sentence = sentence[:1].upper() + sentence[1:]
    tail = f" across {people} people" if people > 1 else ""
    if projects:
        tail += f", on {len(projects)} current project{'s' if len(projects) != 1 else ''}"
    return f"{sentence}{tail}."


def find_teams(
    db: Session,
    caller: AuthenticatedUser,
    query: str,
    view_mode: ViewMode = "work",
    *,
    limit: int = MAX_RESULTS,
) -> TeamRecommendationResult:
    """Rank existing org units for a technical question.

    Creates nothing and changes nothing -- every unit in the result already
    exists and is returned exactly as it is.
    """
    need = read_need(db, query)
    if not need.skills:
        return TeamRecommendationResult(
            query=query, topic=need.topic, skills=[], teams=[],
            unrecognised_skills=list(need.unrecognised), need_source=need.source)

    wf = load_workforce(db, caller, view_mode)

    raws: list[_Raw] = []
    for unit in wf.units.values():
        if unit.unit_type not in RANKABLE_UNIT_TYPES:
            continue
        # A department is scored over its whole subtree; a team is a leaf and
        # so scores over itself. Same call either way.
        member_ids = [
            mid for uid in unit_subtree_ids(db, unit.id) for mid in wf.by_unit.get(uid, [])
        ]
        if not member_ids:
            continue
        raw = _score_unit(wf, unit, member_ids, need.skills, db)
        if raw is not None:
            raws.append(raw)

    ranked = _apply_relative_scores(raws)
    prefer = preferred_unit_type(query)
    ranked = _drop_redundant_parents(db, wf, ranked, prefer)
    ranked.sort(key=lambda r: (r.unit_type != prefer if prefer else False,
                               -r.match_pct, -r.relevant_people, r.name))

    return TeamRecommendationResult(
        query=query,
        topic=need.topic,
        skills=list(need.skills),
        teams=ranked[:limit],
        unrecognised_skills=list(need.unrecognised),
        need_source=need.source,
        preferred_unit_type=prefer,
    )


# How much of a department's relevant headcount has to sit in ONE of its
# teams before the department is really just that team with a bigger name.
CONCENTRATED_IN_ONE_TEAM = 0.6

_ASKS_FOR_TEAM = re.compile(r"\b(team|squad|crew|group)\b", re.I)
_ASKS_FOR_DEPARTMENT = re.compile(r"\b(department|division|org(ani[sz]ation)?|function)\b", re.I)


def preferred_unit_type(query: str) -> str | None:
    """Which granularity the question actually asked for.

    Needed because a department aggregates its teams and therefore beats
    every one of them on depth, breadth and project count by construction --
    so without this, "which TEAM has the strongest Kubernetes expertise?"
    answers with a department, every time. Measured on the seeded org: all
    three of the specified example queries returned departments, including
    the two that say "team".

    A preference, not a filter. The other granularity still appears below,
    because "the department as a whole" is sometimes the better answer even
    when someone typed "team", and hiding it would be deciding that for
    them.
    """
    wants_dept = bool(_ASKS_FOR_DEPARTMENT.search(query))
    wants_team = bool(_ASKS_FOR_TEAM.search(query))
    if wants_dept and not wants_team:
        return "department"
    if wants_team and not wants_dept:
        return "team"
    return None


def _drop_redundant_parents(db: Session, wf: Workforce,
                            ranked: list[TeamRecommendation],
                            prefer: str | None = None) -> list[TeamRecommendation]:
    """Suppress a department whose capability is really one team's.

    The earlier version of this compared scores and was dead code: a
    department is a superset of its teams, so it scores at least as high as
    every one of them on every comparative term and the "child scored
    higher" condition could never fire.

    Concentration is the test that actually distinguishes the two cases. If
    most of the department's relevant people sit in a single team, "go to
    the department" is the same advice as "go to that team", only vaguer --
    so the team is kept and the department dropped. If the capability is
    spread, the department stands, because then it genuinely is the answer.
    """
    # Somebody who explicitly asked "which DEPARTMENT" gets departments.
    # Without this, a capability concentrated in one team suppresses every
    # department and the answer to a department question contains none.
    if prefer == "department":
        return ranked

    by_id = {r.org_unit_id: r for r in ranked}
    drop: set[int] = set()
    for rec in ranked:
        if rec.unit_type != "department" or rec.relevant_people == 0:
            continue
        children = [by_id[uid] for uid in unit_subtree_ids(db, rec.org_unit_id)
                    if uid != rec.org_unit_id and uid in by_id]
        if not children:
            continue
        best_child = max(children, key=lambda c: c.relevant_people)
        if best_child.relevant_people / rec.relevant_people >= CONCENTRATED_IN_ONE_TEAM:
            drop.add(rec.org_unit_id)
    return [r for r in ranked if r.org_unit_id not in drop]
