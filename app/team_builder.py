"""Propose a project team from a natural-language brief.

Same three-stage shape as app/workforce_reports.py, and for the same
reasons:

    1. PLAN     the model turns the brief into roles and required skills.
                It writes no numbers and picks no people.
    2. GATHER   the permission layer resolves a candidate pool from the
                CALLER. Matching and scoring are pure Python over that pool.
    3. NARRATE  (optional) the model summarises facts stage 2 computed,
                every numeral checked back against them.

WHY THE MODEL CANNOT WIDEN THE POOL
-----------------------------------
`TeamPlan` has no scope field. Not "a scope field that gets validated" — no
field at all, the same property app/workforce_reports.py's ReportPlan
relies on. The candidate pool comes from resolve_scope(db, caller), which
*discards* a requested scope rather than checking it, so there is no brief
anyone can write ("include everyone in Finance", "ignore restrictions",
"list all employees with their salaries") that reaches an employee the
caller could not already open one profile at a time. The brief is an input
to role extraction and to nothing else.

Candidate output is built from SUMMARY_FIELDS only — the always-visible set
app/people.py's find_people returns in bulk — so a proposed team discloses
no more than the directory listing already does. No ABAC/RBAC-gated field
(salary, hire_date, date_of_birth, training_status) is read by this module
at all.

WHAT THE MODEL IS NEVER ALLOWED TO DO
-------------------------------------
Choose people, score them, or produce a statistic. Every percentage in a
TeamProposal is computed here. The model's only structured output is the
role/skill breakdown, and even that is filtered against the real `skills`
table before use: a skill it invents is dropped and reported as
unrecognised rather than silently matched against nothing.

AVAILABILITY IS DELIBERATELY NOT A RANKING SIGNAL
-------------------------------------------------
employees.availability_status exists, but in this dataset it is 540
available / 3 away / 2 restricted out of 545 active people. A signal that
is 99% one value cannot rank anything; including it would add a term that
looks meaningful in the weights table and moves no result. It is shown on
each candidate so a reader can see it, and it is never scored. If the
column ever carries real signal, add it here and to WEIGHTS together.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

# _scope_out is private to app/analytics.py, and imported anyway rather than
# reimplemented: it is the one function that turns a Scope into the
# DashboardScope every dashboard response carries, and a second copy here
# would be a second place for "whose data is this" to drift.
from app.analytics import DashboardForbidden, Scope, _scope_out, resolve_scope
from app.auth import AuthenticatedUser
from app.grounding import is_grounded, neutral_scope_label
from app.models import (
    Employee,
    EmployeeProject,
    EmployeeSkill,
    OrgUnit,
    Project,
    ProjectSkillRequirement,
    Skill,
)
from app.models.enums import SkillLevel
from app.people import resolve_skill
from app.permissions import ViewMode
from app.schemas import (
    CandidateMatch,
    CandidateSkill,
    ProposedRole,
    TeamConcentrationRisk,
    TeamConstraintsOut,
    TeamCoverage,
    TeamCoverageSkill,
    TeamPlanInput,
    TeamProposal,
)

# How much a held skill counts toward a role that requires it. The gap
# between Expert and Working is deliberately wider than between Working and
# Learning: "can lead this" vs "can do this" is a bigger difference to a
# staffing decision than "can do this" vs "is picking it up".
LEVEL_WEIGHT: dict[SkillLevel, float] = {
    SkillLevel.expert: 1.0,
    SkillLevel.working: 0.6,
    SkillLevel.learning: 0.25,
}

# Score composition, in the priority order the feature was specified with.
# Proficiency is not a separate term -- it is inside `skills`, via
# LEVEL_WEIGHT, which is what makes "has Azure at Expert" outrank "has Azure
# at Learning" without letting proficiency outweigh having the skill at all.
WEIGHTS: dict[str, float] = {
    "skills": 0.55,      # 1. required skills, 2. proficiency
    "experience": 0.20,  # 3. relevant project experience
    "title": 0.15,       # 4. job role / title
    "org": 0.10,         # 5. organisational context
}

# A team is only as good as its shortlist is long. Capped because the
# alternatives ride along in the response (replacement needs no round trip)
# and an unbounded list would put most of the directory on the wire.
ALTERNATIVES_PER_ROLE = 5
MAX_ROLES = 8
MAX_SKILLS_PER_ROLE = 6

# A skill one person carries more than this share of is a key-person risk.
# 0.6 rather than a majority: at 60% the team has a bus-factor problem
# already, and waiting for 100% to say so means only ever flagging the
# single-holder case, which the reader can see for themselves.
CONCENTRATION_THRESHOLD = 0.6

# ...but a high share is only a RISK where the team had a chance to spread
# the skill and did not. With one person per role, most required skills are
# held by exactly one member by construction -- flagging all of them
# produced five "100% concentrated" warnings on a four-person team, which
# is a restatement of the team's shape, not a finding. So a risk is reported
# only where the skill matters to more than one role, or more than one
# member holds it and one still dominates. Single-holder skills are still
# visible: TeamCoverageSkill.holder_count says 1 without calling it a risk.
MIN_ROLES_FOR_SINGLE_HOLDER_RISK = 2

# Words that appear in so many job titles that matching on them would score
# every candidate identically -- which is the same as not scoring at all.
#
# The second group is the one that matters and it was found by running the
# matcher: "Senior QA Engineer" was scoring a title match against the role
# "DevOps Engineer", on the strength of the word `engineer` alone. Generic
# role nouns are removed so what is left is the DISCRIMINATING half of a
# title -- "Cloud Engineer" vs "Data Engineer" then compares cloud against
# data, which is the actual question. Nouns that genuinely distinguish a
# role (architect, scientist, designer) are deliberately NOT here.
_TITLE_STOPWORDS = frozenset({
    "senior", "junior", "lead", "principal", "staff", "chief", "head",
    "of", "the", "and", "ii", "iii", "iv", "sr", "jr", "associate",
    "engineer", "developer", "manager", "specialist", "analyst",
    "consultant", "officer", "coordinator",
})


class TeamBuildUnavailable(Exception):
    """The caller has no candidate pool -- not an error in the brief."""


# ---------------------------------------------------------------------------
# 1. Planning -- brief to roles. The model's only structured turn.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleSpec:
    role: str
    required_skills: tuple[str, ...]


@dataclass(frozen=True)
class TeamConstraints:
    """Optional natural-language constraints, structured.

    Every field is a *preference the matcher applies*, never a filter that
    could reach outside the pool -- max_per_department can only ever remove
    candidates, never admit one.
    """

    prefer_expert: bool = False
    minimize_concentration: bool = False
    max_per_department: int | None = None
    prefer_experience_with: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.prefer_expert or self.minimize_concentration
                    or self.max_per_department or self.prefer_experience_with)


@dataclass(frozen=True)
class TeamPlan:
    """Deliberately has no scope, org_unit, employee or department field.

    See the module docstring: the absence is the security property. Adding
    one here would make the brief able to address people, which is exactly
    what the permission gate exists to prevent.
    """

    project_type: str
    roles: tuple[RoleSpec, ...]
    constraints: TeamConstraints = TeamConstraints()
    # Skills the model named that no `skills` row matches. Surfaced rather
    # than dropped silently: "we have nobody with Kubernetes Networking" and
    # "Kubernetes Networking is not a skill this directory tracks" are
    # different answers and the reader is owed the difference.
    unrecognised_skills: tuple[str, ...] = ()
    source: str = "model"


_PLANNER_TOOL = {
    "type": "function",
    "function": {
        "name": "plan_project_team",
        "description": (
            "Break a project brief into the roles it needs and the skills each role "
            "requires. You are describing the SHAPE of a team, not staffing it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_type": {
                    "type": "string",
                    "description": "Short label for the kind of work, e.g. 'Azure cloud migration'.",
                },
                "roles": {
                    "type": "array",
                    "maxItems": MAX_ROLES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "description": "Job role, e.g. 'Cloud Engineer'."},
                            "required_skills": {
                                "type": "array",
                                "maxItems": MAX_SKILLS_PER_ROLE,
                                "items": {"type": "string"},
                                "description": (
                                    "Technical or domain skills this role needs. Name skills, "
                                    "not people, seniority or headcount."
                                ),
                            },
                        },
                        "required": ["role", "required_skills"],
                        "additionalProperties": False,
                    },
                },
                "prefer_expert": {
                    "type": "boolean",
                    "description": "True only if the brief asks to prioritise Expert-level skills.",
                },
                "minimize_concentration": {
                    "type": "boolean",
                    "description": "True only if the brief asks to spread skills or avoid key-person risk.",
                },
                "max_per_department": {
                    "type": "integer",
                    "description": "Set only if the brief caps how many people may come from one department.",
                },
                "prefer_experience_with": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string"},
                    "description": "Kinds of prior work the brief prefers, e.g. 'Azure migration'.",
                },
            },
            "required": ["project_type", "roles"],
        },
    },
}

_PLANNER_SYSTEM = """You turn a project brief into the roles a team needs.

You do not have the employee directory and you never will. You do not choose \
people, you do not estimate percentages, and you do not decide who is allowed \
to see what. You describe roles and the skills those roles require.

Name skills the way an engineer would list them on a CV (e.g. "Terraform", \
"Cloud Security", "SQL"). Do not invent skills to pad a role out, and do not \
name a person, a team, a department, or anyone's manager. If the text is not \
a project brief at all, return zero roles."""


# Enough of a fallback to be useful offline and in tests, not an attempt to
# reimplement the model. Recognises the shape "<skills> for <a project>" that
# most briefs actually take, and otherwise defers.
_SKILL_SPLIT = re.compile(r",| and | & |/", re.I)
_ROLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("architect", ("Cloud Architecture",)),
    ("security", ("Cloud Security",)),
    ("data", ("Data Engineering", "SQL")),
    ("devops", ("Terraform", "Kubernetes")),
    ("frontend", ("React", "TypeScript")),
)


def _deterministic_plan(db: Session, brief: str) -> TeamPlan | None:
    """Build a plan from skills the brief literally names.

    Only fires when the brief names at least two real skills, because one
    skill is a search, not a team. Produces one role per skill cluster,
    which is a cruder shape than the model's -- it exists so the feature
    degrades to something useful without Azure OpenAI, not to compete.
    """
    named = _skills_named_in(db, brief)
    if len(named) < 2:
        return None

    roles: list[RoleSpec] = []
    for canonical in named[:MAX_ROLES]:
        role_name = _role_name_for(canonical)
        roles.append(RoleSpec(role=role_name, required_skills=(canonical,)))

    return TeamPlan(
        project_type=_project_type_from(brief),
        roles=tuple(roles),
        constraints=parse_constraints(brief),
        source="derived",
    )


def _role_name_for(skill_name: str) -> str:
    lowered = skill_name.lower()
    for hint, _ in _ROLE_HINTS:
        if hint in lowered:
            return f"{skill_name} Specialist"
    return f"{skill_name} Specialist"


def _project_type_from(brief: str) -> str:
    cleaned = re.sub(r"^\s*(build|create|assemble|staff)\s+(me\s+)?(a|an)\s+team\s+(for|to)\s+",
                     "", brief.strip(), flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] if cleaned else "Project team"


def _skills_named_in(db: Session, text: str) -> list[str]:
    """Canonical skill names the text mentions, in the order mentioned.

    Matched against the real `skills` table via resolve_skill, so synonyms
    resolve the same way they do everywhere else and nothing invented can
    survive this function.
    """
    seen: dict[str, int] = {}
    for raw in _SKILL_SPLIT.split(text):
        for phrase in _phrases(raw):
            skill = resolve_skill(db, phrase)
            if skill is not None and skill.name not in seen:
                seen[skill.name] = text.lower().find(phrase.lower())
    return [name for name, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def _phrases(chunk: str) -> list[str]:
    """Every 1-3 word window in a chunk, longest first.

    Longest-first matters: "cloud security" must be tried before "security",
    or a two-word skill resolves to whichever single word happens to exist.
    """
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z+#.\-]*", chunk) if w]
    out: list[str] = []
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            out.append(" ".join(words[i:i + size]))
    return out


_CONSTRAINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prefer_expert", re.compile(r"\bexpert[- ]level\b|\bprioriti[sz]e expert|\bonly expert", re.I)),
    ("minimize_concentration", re.compile(
        r"minimi[sz]e (skill )?concentration|spread (the )?(skills|risk)|avoid key[- ]person|bus factor", re.I)),
)
# "more than" is accepted without a preceding "no", because the phrasing
# people actually type is "Don't use more than two from the same department"
# -- the negation sits on the verb, several words earlier, and matching it
# properly would mean parsing the clause. In a constraints box, "more than N
# from the same department" is a cap in every realistic phrasing; there is no
# reading of it that asks for MORE concentration.
_MAX_DEPT = re.compile(
    r"(?:no more than|more than|at most|maximum(?: of)?|max)\s+(\d+|one|two|three|four|five)\s+"
    r"(?:people|persons?|members?)?\s*(?:from|in)\s+(?:the\s+)?same\s+(?:department|team|org)", re.I)
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_PREFER_EXPERIENCE = re.compile(r"prefer (?:people |someone |candidates )?with ([^.;]{3,60})", re.I)


def parse_constraints(text: str) -> TeamConstraints:
    """Deterministic constraint parse.

    Runs on every brief regardless of which planner produced the roles, so a
    constraint typed in the constraints box is honoured even when the model
    is unavailable. The model may also set these; _model_plan ORs the two
    together rather than letting either silently win.
    """
    prefer_expert = bool(_CONSTRAINT_PATTERNS[0][1].search(text))
    minimize = bool(_CONSTRAINT_PATTERNS[1][1].search(text))

    max_dept: int | None = None
    m = _MAX_DEPT.search(text)
    if m:
        raw = m.group(1).lower()
        max_dept = _WORD_NUMBERS.get(raw) or (int(raw) if raw.isdigit() else None)
        if max_dept is not None and not 1 <= max_dept <= 20:
            max_dept = None

    experience: tuple[str, ...] = ()
    e = _PREFER_EXPERIENCE.search(text)
    if e:
        phrase = re.sub(r"\s+experience\b", "", e.group(1), flags=re.I).strip()
        if phrase:
            experience = (phrase[:60],)

    return TeamConstraints(
        prefer_expert=prefer_expert,
        minimize_concentration=minimize,
        max_per_department=max_dept,
        prefer_experience_with=experience,
    )


def _model_plan(db: Session, brief: str) -> TeamPlan | None:
    """The model's turn. One tool, constrained arguments, no free text.

    Text trying to do something other than describe a team cannot produce a
    valid call to this tool, so it produces nothing -- the same
    prompt-injection defence app/tool_calling.py's TOOLS relies on.
    """
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    if _mode() != "real":
        return None
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": brief},
            ],
            tools=[_PLANNER_TOOL],
            tool_choice="auto",
            reasoning_effort="minimal",
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            return None
        args = json.loads(calls[0].function.arguments or "{}")
    except (OpenAIError, json.JSONDecodeError, AttributeError, IndexError):
        return None

    return _plan_from_args(db, brief, args)


def _plan_from_args(db: Session, brief: str, args: dict) -> TeamPlan | None:
    """Validate the model's arguments against real data.

    Split out from _model_plan so the validation -- which is the part that
    matters -- is testable without an Azure OpenAI client.
    """
    raw_roles = args.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        return None

    roles: list[RoleSpec] = []
    unrecognised: list[str] = []
    for entry in raw_roles[:MAX_ROLES]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("role") or "").strip()[:80]
        raw_skills = entry.get("required_skills")
        if not name or not isinstance(raw_skills, list):
            continue
        canonical: list[str] = []
        for s in raw_skills[:MAX_SKILLS_PER_ROLE]:
            if not isinstance(s, str) or not s.strip():
                continue
            resolved = resolve_skill(db, s.strip())
            if resolved is None:
                if s.strip() not in unrecognised:
                    unrecognised.append(s.strip()[:60])
            elif resolved.name not in canonical:
                canonical.append(resolved.name)
        # A role whose every named skill was invented is not a role we can
        # staff -- dropping it is honest, and unrecognised_skills says why.
        if canonical:
            roles.append(RoleSpec(role=name, required_skills=tuple(canonical)))

    if not roles:
        return TeamPlan(project_type=str(args.get("project_type") or "Project team")[:80],
                        roles=(), unrecognised_skills=tuple(unrecognised[:12]), source="model")

    typed = parse_constraints(brief)
    model_dept = args.get("max_per_department")
    constraints = TeamConstraints(
        prefer_expert=typed.prefer_expert or bool(args.get("prefer_expert")),
        minimize_concentration=typed.minimize_concentration or bool(args.get("minimize_concentration")),
        max_per_department=typed.max_per_department or (
            model_dept if isinstance(model_dept, int) and 1 <= model_dept <= 20 else None),
        prefer_experience_with=typed.prefer_experience_with or tuple(
            str(x)[:60] for x in (args.get("prefer_experience_with") or [])
            if isinstance(x, str) and x.strip()
        )[:4],
    )
    return TeamPlan(
        project_type=str(args.get("project_type") or "Project team")[:80],
        roles=tuple(roles),
        constraints=constraints,
        unrecognised_skills=tuple(unrecognised[:12]),
        source="model",
    )


def plan_team(db: Session, brief: str, extra_constraints: str = "") -> TeamPlan:
    """Model first, deterministic second. No third fallback on purpose.

    Unlike app/workforce_reports.py's planner there is no default plan: a
    brief nobody could parse has no roles, and inventing roles for it would
    mean proposing a team for a project the reader never described. An empty
    plan is the honest answer and the caller surfaces it as such.
    """
    text = f"{brief}\n{extra_constraints}".strip()

    plan = _model_plan(db, text)
    if plan is not None and plan.roles:
        return plan

    derived = _deterministic_plan(db, text)
    if derived is not None:
        # Keep the model's unrecognised-skill list if it produced one --
        # it is the better explanation of why a brief came back thin.
        if plan is not None and plan.unrecognised_skills:
            return TeamPlan(
                project_type=derived.project_type, roles=derived.roles,
                constraints=derived.constraints,
                unrecognised_skills=plan.unrecognised_skills, source="derived",
            )
        return derived

    if plan is not None:
        return plan
    return TeamPlan(project_type=_project_type_from(text), roles=(),
                    constraints=parse_constraints(text), source="derived")


# ---------------------------------------------------------------------------
# 2. Candidate pool -- permission-filtered before anything is scored
# ---------------------------------------------------------------------------

@dataclass
class Pool:
    """Everything matching is allowed to look at, loaded once.

    Built strictly from scope.employee_ids. Nothing below re-queries the
    employees table with a different filter, so there is exactly one place
    where "who is a candidate" is decided.
    """

    employees: dict[str, Employee] = field(default_factory=dict)
    skills_by_employee: dict[str, dict[str, SkillLevel]] = field(default_factory=dict)
    projects_by_employee: dict[str, list[tuple[str, frozenset[str]]]] = field(default_factory=dict)
    unit_names: dict[int, str] = field(default_factory=dict)
    # Canonical skill name -> set of unit ids whose members hold it. Backs
    # the organisational-context term without a per-candidate query.
    units_with_skill: dict[str, set[int]] = field(default_factory=dict)


def load_pool(db: Session, employee_ids: frozenset[str]) -> Pool:
    pool = Pool()
    if not employee_ids:
        return pool

    ids = list(employee_ids)
    rows = db.execute(select(Employee).where(Employee.id.in_(ids))).scalars().all()
    pool.employees = {e.id: e for e in rows}

    pool.unit_names = {
        u.id: u.name for u in db.execute(select(OrgUnit)).scalars().all()
    }

    skill_rows = db.execute(
        select(EmployeeSkill.employee_id, Skill.name, EmployeeSkill.level)
        .join(Skill, Skill.id == EmployeeSkill.skill_id)
        .where(EmployeeSkill.employee_id.in_(ids))
    ).all()
    for emp_id, skill_name, level in skill_rows:
        pool.skills_by_employee.setdefault(emp_id, {})[skill_name] = level
        unit_id = pool.employees[emp_id].org_unit_id if emp_id in pool.employees else None
        if unit_id is not None:
            pool.units_with_skill.setdefault(skill_name, set()).add(unit_id)

    # A project's skills come from its DECLARED requirements where they
    # exist -- app/models/project_skill_requirement.py explains why a
    # declared requirement is worth more than an inferred one. Projects with
    # no declared requirements contribute their name only, which still
    # supports "prefer people with Azure migration experience".
    proj_rows = db.execute(
        select(EmployeeProject.employee_id, Project.id, Project.name)
        .join(Project, Project.id == EmployeeProject.project_id)
        .where(EmployeeProject.employee_id.in_(ids))
    ).all()
    project_ids = {pid for _, pid, _ in proj_rows}
    req_by_project: dict[int, set[str]] = {}
    if project_ids:
        for pid, sname in db.execute(
            select(ProjectSkillRequirement.project_id, Skill.name)
            .join(Skill, Skill.id == ProjectSkillRequirement.skill_id)
            .where(ProjectSkillRequirement.project_id.in_(list(project_ids)))
        ).all():
            req_by_project.setdefault(pid, set()).add(sname)

    for emp_id, pid, pname in proj_rows:
        pool.projects_by_employee.setdefault(emp_id, []).append(
            (pname, frozenset(req_by_project.get(pid, set())))
        )
    return pool


# ---------------------------------------------------------------------------
# 3. Scoring -- pure functions, no model, no database
# ---------------------------------------------------------------------------

def _skill_score(held: dict[str, SkillLevel], required: tuple[str, ...],
                 prefer_expert: bool) -> tuple[float, list[CandidateSkill], list[str]]:
    """Fraction of the role's required skills the candidate covers, weighted
    by level. Also returns the evidence rows and what is missing."""
    if not required:
        return 0.0, [], []
    total = 0.0
    matched: list[CandidateSkill] = []
    missing: list[str] = []
    for skill_name in required:
        level = held.get(skill_name)
        if level is None:
            missing.append(skill_name)
            continue
        weight = LEVEL_WEIGHT[level]
        if prefer_expert and level is not SkillLevel.expert:
            # Halved, not zeroed: "prioritise Expert" is a preference, and a
            # Working candidate with the skill still beats one without it.
            weight *= 0.5
        total += weight
        matched.append(CandidateSkill(skill=skill_name, level=level.value, required=True))
    return total / len(required), matched, missing


def _experience_score(projects: list[tuple[str, frozenset[str]]], required: tuple[str, ...],
                      prefer_with: tuple[str, ...]) -> tuple[float, list[str]]:
    """Projects whose declared requirements overlap the role's skills, or
    whose name matches a preferred kind of work."""
    if not projects:
        return 0.0, []
    wanted = {s.lower() for s in required}
    prefer_tokens = {t.lower() for phrase in prefer_with for t in phrase.split() if len(t) > 3}
    relevant: list[str] = []
    for name, req_skills in projects:
        overlap = {s.lower() for s in req_skills} & wanted
        name_hit = bool(prefer_tokens) and any(t in name.lower() for t in prefer_tokens)
        if overlap or name_hit:
            relevant.append(name)
    if not relevant:
        return 0.0, []
    # Two relevant projects is treated as full marks. A third does not make
    # someone meaningfully more suitable, and letting it keep scaling would
    # let long tenure outrank actually holding the skill.
    return min(1.0, len(relevant) / 2.0), relevant[:4]


def _title_score(job_title: str, role: str) -> float:
    """Token overlap between the person's title and the proposed role."""
    def tokens(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]+", text.lower())
                if w not in _TITLE_STOPWORDS and len(w) > 2}

    role_tokens = tokens(role)
    if not role_tokens:
        return 0.0
    return len(tokens(job_title) & role_tokens) / len(role_tokens)


def _org_score(pool: Pool, emp: Employee, required: tuple[str, ...]) -> float:
    """Does this person sit in a unit that does this kind of work?

    Cheap proxy for organisational context: the share of the role's skills
    that anyone in the candidate's own org unit holds. Someone in a team
    already doing Azure work is easier to second onto an Azure project than
    an equally-skilled person in an unrelated department, and this is the
    part of that which is actually in the data.
    """
    if not required or emp.org_unit_id is None:
        return 0.0
    hits = sum(1 for s in required if emp.org_unit_id in pool.units_with_skill.get(s, set()))
    return hits / len(required)


def score_candidate(pool: Pool, emp: Employee, spec: RoleSpec,
                    constraints: TeamConstraints) -> CandidateMatch:
    held = pool.skills_by_employee.get(emp.id, {})
    projects = pool.projects_by_employee.get(emp.id, [])

    skills, matched, missing = _skill_score(held, spec.required_skills, constraints.prefer_expert)
    experience, relevant = _experience_score(projects, spec.required_skills,
                                             constraints.prefer_experience_with)
    title = _title_score(emp.job_title or "", spec.role)
    org = _org_score(pool, emp, spec.required_skills)

    total = (WEIGHTS["skills"] * skills + WEIGHTS["experience"] * experience
             + WEIGHTS["title"] * title + WEIGHTS["org"] * org)

    # Adjacent skills the candidate holds that the role did not ask for, but
    # which the rest of the team might need -- shown as context, never scored.
    adjacent = [
        CandidateSkill(skill=name, level=level.value, required=False)
        for name, level in sorted(held.items(), key=lambda kv: -LEVEL_WEIGHT[kv[1]])
        if name not in set(spec.required_skills)
    ][:3]

    return CandidateMatch(
        employee_id=emp.id,
        full_name=emp.full_name,
        job_title=emp.job_title or "",
        org_unit=pool.unit_names.get(emp.org_unit_id or -1, ""),
        availability_status=emp.availability_status.value,
        match_pct=round(total * 100),
        matched_skills=matched + adjacent,
        missing_skills=missing,
        relevant_projects=relevant,
        explanation=_explain(matched, missing, relevant, title, org),
    )


def _explain(matched: list[CandidateSkill], missing: list[str], projects: list[str],
             title: float, org: float) -> list[str]:
    """Why this person scored what they scored, in the order that matters.

    Built from the same values that produced the number, so an explanation
    cannot drift from its score -- the failure mode that makes generated
    justifications untrustworthy.
    """
    lines = [f"{s.level}: {s.skill}" for s in matched if s.required]
    if missing:
        lines.append(f"Missing: {', '.join(missing)}")
    if projects:
        lines.append(f"Relevant projects: {', '.join(projects)}")
    if title >= 0.5:
        lines.append("Job title matches the role")
    if org >= 0.5 and not lines[:1]:
        lines.append("Their team already works with these skills")
    return lines


# ---------------------------------------------------------------------------
# 4. Assembly -- one candidate per role, nobody twice
# ---------------------------------------------------------------------------

def _assign(pool: Pool, plan: TeamPlan,
            pinned: dict[int, str]) -> list[tuple[RoleSpec, CandidateMatch | None, list[CandidateMatch]]]:
    """Greedy, highest-scoring-first, with each person used at most once.

    Greedy rather than optimal (Hungarian) on purpose: an optimal assignment
    can move a person off the role they are clearly best at to raise the
    total by a point, which reads as a bug to whoever is looking at the
    screen. Roles are filled in descending order of how well their best
    candidate fits, so the most clear-cut assignment claims its person first.

    `pinned` holds manual replacements by role index. A pinned candidate is
    honoured even if they are not the top scorer -- that is the whole point
    of Replace -- but still cannot be assigned to two roles at once.
    """
    scored: dict[int, list[CandidateMatch]] = {}
    for i, spec in enumerate(plan.roles):
        ranked = sorted(
            (score_candidate(pool, emp, spec, plan.constraints) for emp in pool.employees.values()),
            key=lambda c: (-c.match_pct, c.full_name),
        )
        # Somebody with none of the required skills is not a candidate for
        # the role, whatever their title says.
        scored[i] = [c for c in ranked if any(s.required for s in c.matched_skills)]

    taken: set[str] = set()
    dept_count: dict[str, int] = {}
    results: list[tuple[RoleSpec, CandidateMatch | None, list[CandidateMatch]]] = [
        (spec, None, []) for spec in plan.roles
    ]

    # Pinned first -- a manual choice outranks the algorithm's ordering.
    for idx, emp_id in pinned.items():
        if not 0 <= idx < len(plan.roles) or emp_id in taken:
            continue
        match = next((c for c in scored[idx] if c.employee_id == emp_id), None)
        if match is None:
            emp = pool.employees.get(emp_id)
            if emp is None:
                # Outside the caller's pool. Silently ignored rather than
                # 404'd: the id came off a client the caller controls, and
                # answering "no such employee" differently from "not yours"
                # would make this endpoint a membership oracle.
                continue
            match = score_candidate(pool, emp, plan.roles[idx], plan.constraints)
        taken.add(emp_id)
        unit = match.org_unit
        dept_count[unit] = dept_count.get(unit, 0) + 1
        results[idx] = (plan.roles[idx], match, [])

    order = sorted(
        (i for i in range(len(plan.roles)) if results[i][1] is None),
        key=lambda i: -(scored[i][0].match_pct if scored[i] else 0),
    )
    for i in order:
        spec = plan.roles[i]
        chosen: CandidateMatch | None = None
        for cand in scored[i]:
            if cand.employee_id in taken:
                continue
            if plan.constraints.max_per_department is not None:
                if dept_count.get(cand.org_unit, 0) >= plan.constraints.max_per_department:
                    continue
            chosen = cand
            break
        if chosen is not None:
            taken.add(chosen.employee_id)
            dept_count[chosen.org_unit] = dept_count.get(chosen.org_unit, 0) + 1
        results[i] = (spec, chosen, [])

    # Alternatives are computed after every assignment is final, so the list
    # a reader sees for "Replace" never offers somebody already on the team.
    for i, (spec, chosen, _) in enumerate(results):
        alts = [c for c in scored[i]
                if c.employee_id not in taken][:ALTERNATIVES_PER_ROLE]
        results[i] = (spec, chosen, alts)
    return results


# ---------------------------------------------------------------------------
# 5. Coverage -- every number on the screen is computed here
# ---------------------------------------------------------------------------

def analyse_coverage(pool: Pool, plan: TeamPlan,
                     assigned: list[CandidateMatch]) -> TeamCoverage:
    required: list[str] = []
    roles_wanting: dict[str, int] = {}
    for spec in plan.roles:
        for s in spec.required_skills:
            if s not in required:
                required.append(s)
            roles_wanting[s] = roles_wanting.get(s, 0) + 1

    if not required:
        return TeamCoverage(coverage_pct=0, skills=[], covered=[], missing=[],
                            level_counts={}, risks=[])

    member_levels: dict[str, dict[str, SkillLevel]] = {
        c.employee_id: pool.skills_by_employee.get(c.employee_id, {}) for c in assigned
    }
    name_of = {c.employee_id: c.full_name for c in assigned}

    skills: list[TeamCoverageSkill] = []
    covered: list[str] = []
    missing: list[str] = []
    risks: list[TeamConcentrationRisk] = []
    level_counts: dict[str, int] = {"Expert": 0, "Working": 0, "Learning": 0}
    total_weight = 0.0

    for skill_name in required:
        holders = [(eid, levels[skill_name]) for eid, levels in member_levels.items()
                   if skill_name in levels]
        if not holders:
            missing.append(skill_name)
            skills.append(TeamCoverageSkill(skill=skill_name, best_level=None,
                                            holder_count=0, holders=[]))
            continue

        best = max(holders, key=lambda h: LEVEL_WEIGHT[h[1]])
        total_weight += LEVEL_WEIGHT[best[1]]
        for _, level in holders:
            level_counts[level.value] += 1
        # "Covered" means somebody can actually do the work. A skill held
        # only at Learning is present on the team and still a gap in
        # capability, so it counts toward coverage_pct at its 0.25 weight
        # but is NOT listed as covered -- the two questions are different.
        if best[1] is not SkillLevel.learning:
            covered.append(skill_name)

        skill_weight = sum(LEVEL_WEIGHT[level] for _, level in holders)
        top_share = LEVEL_WEIGHT[best[1]] / skill_weight if skill_weight else 0.0
        spreadable = (len(holders) > 1
                      or roles_wanting.get(skill_name, 1) >= MIN_ROLES_FOR_SINGLE_HOLDER_RISK)
        if top_share >= CONCENTRATION_THRESHOLD and len(assigned) > 1 and spreadable:
            risks.append(TeamConcentrationRisk(
                skill=skill_name,
                employee_id=best[0],
                full_name=name_of.get(best[0], ""),
                share_pct=round(top_share * 100),
                holder_count=len(holders),
            ))

        skills.append(TeamCoverageSkill(
            skill=skill_name, best_level=best[1].value, holder_count=len(holders),
            holders=[name_of.get(eid, "") for eid, _ in holders],
        ))

    return TeamCoverage(
        coverage_pct=round(total_weight / len(required) * 100),
        skills=skills,
        covered=covered,
        missing=missing,
        level_counts=level_counts,
        risks=sorted(risks, key=lambda r: -r.share_pct),
    )


# ---------------------------------------------------------------------------
# 6. Narrative -- summarises the numbers above, never produces one
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """You write two sentences summarising a proposed project team.

Every fact is given to you below. Use only those numbers -- do not compute, \
estimate, round, or infer any figure that is not written there. Name the \
biggest gap or risk if there is one. No preamble, no bullet points, no \
recommendation to "consider" anything."""


def _fact_text(plan: TeamPlan, coverage: TeamCoverage, assigned: list[CandidateMatch],
               scope_label: str) -> str:
    parts = [
        f"Scope: {scope_label}.",
        f"Project: {plan.project_type}.",
        f"Roles: {len(plan.roles)}. Filled: {len(assigned)}.",
        f"Coverage: {coverage.coverage_pct}%.",
    ]
    if coverage.covered:
        parts.append(f"Covered skills: {', '.join(coverage.covered)}.")
    if coverage.missing:
        parts.append(f"Missing skills: {', '.join(coverage.missing)}.")
    for r in coverage.risks[:2]:
        parts.append(f"{r.share_pct}% of {r.skill} sits with one person.")
    return " ".join(parts)


def _derived_summary(plan: TeamPlan, coverage: TeamCoverage,
                     assigned: list[CandidateMatch]) -> str:
    bits = [
        f"{len(assigned)} of {len(plan.roles)} roles filled at {coverage.coverage_pct}% skill coverage."
    ]
    if coverage.missing:
        bits.append(f"No one on the team holds {', '.join(coverage.missing[:3])}.")
    elif coverage.risks:
        r = coverage.risks[0]
        bits.append(f"{r.share_pct}% of the team's {r.skill} capability sits with {r.full_name}.")
    else:
        bits.append("Every required skill has a Working or Expert holder.")
    return " ".join(bits)


def _narrate(plan: TeamPlan, coverage: TeamCoverage, assigned: list[CandidateMatch],
             scope: Scope) -> tuple[str, str]:
    """Returns (text, source). Falls back to the derived summary whenever
    the model writes a numeral that is not in the facts."""
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    derived = _derived_summary(plan, coverage, assigned)
    if _mode() != "real":
        return derived, "derived"

    # The Scope object, not its label: neutral_scope_label rewrites a
    # manager's "<name>'s team" so no employee name enters the prompt, and it
    # needs scope.kind to know that a rewrite is called for.
    facts = _fact_text(plan, coverage, assigned, neutral_scope_label(scope))
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": facts},
            ],
            reasoning_effort="minimal",
        )
        text = (response.choices[0].message.content or "").strip()
    except (OpenAIError, AttributeError, IndexError):
        return derived, "derived"

    if not text or len(text) > 600 or not is_grounded(text, facts):
        return derived, "derived"
    return text, "model"


# ---------------------------------------------------------------------------
# 7. Public entry point
# ---------------------------------------------------------------------------

def build_team(
    db: Session,
    caller: AuthenticatedUser,
    brief: str,
    view_mode: ViewMode = "work",
    *,
    constraints_text: str = "",
    assignments: dict[int, str] | None = None,
    plan_input: "TeamPlanInput | None" = None,
    narrate: bool = True,
) -> TeamProposal:
    """Brief in, proposed team out.

    The order of the first two statements is the security property: the
    scope is resolved from the caller BEFORE the brief is shown to anything,
    and the pool is built from that scope alone. Nothing downstream can
    widen it, because nothing downstream re-queries employees.
    """
    try:
        scope = resolve_scope(db, caller, view_mode)
    except DashboardForbidden as e:
        raise TeamBuildUnavailable(str(e)) from e

    pool = load_pool(db, scope.employee_ids)
    # A plan echoed back by the client is reused rather than re-derived. It
    # goes through the SAME validation the model's output does -- every
    # skill re-resolved against the `skills` table -- so it is no more
    # trusted than the model is, and it cannot carry anything but roles and
    # skills. See TeamPlanInput for why Replace needs this.
    plan = (
        _plan_from_args(db, f"{brief}\n{constraints_text}", {
            "project_type": plan_input.project_type,
            "roles": [{"role": r.role, "required_skills": r.required_skills}
                      for r in plan_input.roles],
        })
        if plan_input is not None and plan_input.roles
        else None
    ) or plan_team(db, brief, constraints_text)

    if not plan.roles:
        return TeamProposal(
            scope=_scope_out(db, scope), project_type=plan.project_type, roles=[],
            coverage=TeamCoverage(coverage_pct=0, skills=[], covered=[], missing=[],
                                  level_counts={}, risks=[]),
            constraints=_constraints_out(plan.constraints),
            unrecognised_skills=list(plan.unrecognised_skills),
            plan_source=plan.source, narrative="", narrative_source="derived",
            candidate_pool_size=len(pool.employees),
        )

    filled = _assign(pool, plan, assignments or {})
    assigned = [m for _, m, _ in filled if m is not None]
    coverage = analyse_coverage(pool, plan, assigned)

    narrative, narrative_source = ("", "derived")
    if narrate:
        narrative, narrative_source = _narrate(plan, coverage, assigned, scope)

    return TeamProposal(
        scope=_scope_out(db, scope),
        project_type=plan.project_type,
        roles=[
            ProposedRole(
                role=spec.role,
                required_skills=list(spec.required_skills),
                candidate=match,
                alternatives=alts,
            )
            for spec, match, alts in filled
        ],
        coverage=coverage,
        constraints=_constraints_out(plan.constraints),
        unrecognised_skills=list(plan.unrecognised_skills),
        plan_source=plan.source,
        narrative=narrative,
        narrative_source=narrative_source,
        candidate_pool_size=len(pool.employees),
    )


def _constraints_out(c: TeamConstraints) -> TeamConstraintsOut:
    return TeamConstraintsOut(
        prefer_expert=c.prefer_expert,
        minimize_concentration=c.minimize_concentration,
        max_per_department=c.max_per_department,
        prefer_experience_with=list(c.prefer_experience_with),
        applied=not c.is_empty(),
    )
