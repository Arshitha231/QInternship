"""Ranks an already permission-filtered candidate pool against a typed
query Interpretation (app/query_entities.py) -- SEARCH_RANKING_PROPOSAL.md
/ SEARCH_RANKING_IMPLEMENTATION_PLAN.md step 4 (step 6's cap folds in here
too -- see MAX_RANKED_RESULTS/SCORE_THRESHOLD below, design decision 4).

Same three-part shape as app/team_builder.py: a Pool loaded once from ids
the caller may already see, pure scoring functions over it, and an
explanation built from the same values that produced the score -- nothing
here queries the database a second time with a different filter, and
nothing here can admit an id that wasn't already in `employee_ids`.

Why this module renormalizes and app/team_builder.py doesn't. team_builder's
WEIGHTS is a straight weighted sum -- every term there describes something
a staffing BRIEF names, so an inapplicable one (no project history, say)
just scores 0 and eats its share, and that 0 is always earned. Here,
seniority and recency describe the QUERY, not the candidate: "senior data
engineer" carries a seniority term because the caller typed "senior", but
"data engineer" alone carries none. Scoring an absent seniority term as 0
would penalise every candidate for a criterion nobody asked about, which is
a different failure from team_builder's. So a term whose entity the query
never named is DROPPED and the remaining weights renormalised to sum to 1,
never scored as 0 -- see score_candidate.

Seniority carries a second, narrower drop rule on top of that: even when
the query DOES name a band, a candidate whose own title carries no
seniority word at all (most titles don't) is also dropped for that term,
not scored 0 -- an untitled band is "unspecified", not "definitely the
wrong band". A title that DOES name a band and it's simply the wrong one is
still scored, for real: see _seniority_score.

Availability is deliberately not a weight, same reasoning and the same
call app/team_builder.py's docstring already made: 495 available / 3 away
/ 2 restricted of 500 active means the signal is 99% one value and cannot
rank anything. It is carried on the result for display, never scored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, EmployeeProject, EmployeeSkill, ProjectSkillRequirement, Skill
from app.models.enums import SkillLevel
from app.query_entities import Interpretation, SENIORITY_BANDS

# Weight composition for a ranked search result. Sums to 1 when every term
# applies; a query missing a term (no seniority named, nobody's projects
# touch a requested skill, ...) renormalises over whichever terms DO apply
# -- see the module docstring for why this departs from team_builder.py.
SEARCH_WEIGHTS: dict[str, float] = {
    "skills": 0.45, "role": 0.30, "seniority": 0.15, "recency": 0.10,
}

# Same LEVEL_WEIGHT shape as app/team_builder.py -- "1 of 2 skills at
# Expert" should outrank "1 of 2 at Learning", and both lose to "2 of 2".
LEVEL_WEIGHT: dict[SkillLevel, float] = {
    SkillLevel.expert: 1.0, SkillLevel.working: 0.7, SkillLevel.learning: 0.4,
}

# A ranked result below this score isn't a match worth showing, whatever
# the retrieval pool contained -- folds in SEARCH_RANKING_IMPLEMENTATION_
# PLAN.md's step 6 (the score-threshold cap is inherent to rank_candidates
# returning a correctly-bounded list, not a follow-up edit to a different
# function; see that plan's design decision 4).
SCORE_THRESHOLD = 0.40

# Replaces the old flat MAX_SEARCH_RESULTS=5 cutoff for THIS path only --
# a computed score is interpretable enough to cut on directly ("everything
# above 0.40, up to 20") instead of a blind top-5. MAX_SEARCH_RESULTS
# itself is untouched: Azure Search's RRF path still uses it, because RRF's
# score doesn't separate a right answer from four wrong ones the way a
# computed weighted sum does (see app/people.py's own comment on it).
MAX_RANKED_RESULTS = 20

# Linear decay: a project whose most recent relevant date is this many
# months in the past or more contributes nothing to the recency term. No
# formula was named in SEARCH_RANKING_PROPOSAL.md beyond "decayed" --
# SEARCH_RANKING_IMPLEMENTATION_PLAN.md's design decision 7 picks this one.
_RECENCY_DECAY_MONTHS = 24


@dataclass
class Pool:
    """Everything scoring is allowed to look at, loaded once from ids the
    caller can already see. Same shape and reasoning as
    app/team_builder.py's Pool -- built strictly from `employee_ids`, so
    nothing below can widen who gets scored."""

    employees: dict[str, Employee] = field(default_factory=dict)
    skills_by_employee: dict[str, dict[str, SkillLevel]] = field(default_factory=dict)
    # One entry per (employee, project) that has a DECLARED skill
    # requirement (app/models/project_skill_requirement.py) -- a project
    # with no declared requirement contributes nothing to the recency term
    # rather than a guessed one, same "declared over inferred" reasoning
    # app/continuity.py already applies to this table. `most_recent_date`
    # is the project's end_date, or today if it's still ongoing (current
    # work counts as maximally recent).
    projects_by_employee: dict[str, list[tuple[frozenset[str], date]]] = field(default_factory=dict)


def load_pool(db: Session, employee_ids: frozenset[str]) -> Pool:
    pool = Pool()
    if not employee_ids:
        return pool

    ids = list(employee_ids)
    rows = db.execute(select(Employee).where(Employee.id.in_(ids))).scalars().all()
    pool.employees = {e.id: e for e in rows}

    skill_rows = db.execute(
        select(EmployeeSkill.employee_id, Skill.name, EmployeeSkill.level)
        .join(Skill, Skill.id == EmployeeSkill.skill_id)
        .where(EmployeeSkill.employee_id.in_(ids))
    ).all()
    for emp_id, skill_name, level in skill_rows:
        pool.skills_by_employee.setdefault(emp_id, {})[skill_name] = level

    proj_rows = db.execute(
        select(EmployeeProject.employee_id, EmployeeProject.project_id,
               EmployeeProject.end_date)
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

    today = date.today()
    for emp_id, pid, end_date in proj_rows:
        required = req_by_project.get(pid)
        if not required:
            continue
        pool.projects_by_employee.setdefault(emp_id, []).append(
            (frozenset(required), end_date or today)
        )
    return pool


# ---------------------------------------------------------------------------
# Term scores -- each is (score, evidence) for a term the query actually
# named, or None when the term should be DROPPED (renormalised away, never
# scored as 0). See the module docstring for why role and seniority differ
# on when None applies.
# ---------------------------------------------------------------------------

def _skill_score(
    held: dict[str, SkillLevel], requested: tuple[str, ...],
) -> tuple[float, list[str], list[str]]:
    """Coverage of `requested`, weighted by level on each one held -- the
    fraction of asked-for skills the candidate actually has, not whether
    they have "a" skill. Holding every requested skill at Expert is the
    only way to reach 1.0; holding a subset, or holding one at less than
    Expert, scores proportionally lower without ever excluding the
    candidate here (exclusion, if any, happens at the SCORE_THRESHOLD cut
    in rank_candidates, over the WHOLE score, not this term alone)."""
    matched: list[str] = []
    missing: list[str] = []
    total = 0.0
    for name in requested:
        level = held.get(name)
        if level is None:
            missing.append(name)
            continue
        total += LEVEL_WEIGHT[level]
        matched.append(f"{name} ({level.value})")
    return total / len(requested), matched, missing


def _role_score(job_title: str, role: str | None) -> tuple[float, str] | None:
    """None only when the query named no role at all -- once a role IS
    named, a title that doesn't match it is a real 0, not dropped (unlike
    seniority, a job title is never "unspecified" with respect to a named
    role)."""
    if role is None:
        return None
    jt_lower = job_title.lower()
    role_lower = role.lower()
    if role_lower and role_lower in jt_lower:
        return 1.0, f"title matches {role}"
    role_words = role.split()
    jt_words = {w.lower().strip(",.") for w in job_title.split()}
    head_noun = role_words[-1].lower() if role_words else ""
    if head_noun and head_noun in jt_words:
        return 0.5, f"title ({job_title}) shares \"{role_words[-1]}\" with {role}"
    return 0.0, f"title ({job_title}) doesn't match {role}"


def _seniority_score(job_title: str, seniority: str | None) -> tuple[float, str] | None:
    """None when the query named no seniority band, OR when the
    candidate's own title carries no seniority word at all -- the second
    case is what keeps an unqualified title ("Data Engineer") from being
    penalised as though it had asserted the OPPOSITE band. A title that
    DOES carry a band, and it's the wrong one, is a real score."""
    if seniority is None:
        return None
    jt_words = [w.lower().strip(",.") for w in job_title.split()]
    bands_in_title = [w for w in jt_words if w in SENIORITY_BANDS]
    if not bands_in_title:
        return None
    band = seniority.lower()
    if band in bands_in_title:
        return 1.0, f"title includes \"{seniority}\""
    idx = SENIORITY_BANDS.index(band)
    adjacent = {SENIORITY_BANDS[i] for i in (idx - 1, idx + 1) if 0 <= i < len(SENIORITY_BANDS)}
    hit = next((b for b in bands_in_title if b in adjacent), None)
    if hit is not None:
        return 0.6, f"title includes \"{hit}\", adjacent to \"{seniority}\""
    return 0.0, f"title includes {', '.join(bands_in_title)}, not \"{seniority}\""


def _recency_score(
    projects: list[tuple[frozenset[str], date]], requested_skills: tuple[str, ...],
) -> tuple[float, str] | None:
    """None when the query named no skill to be recent about, or when the
    candidate has no DECLARED-requirement project touching any of them --
    dropped, not scored, same as every other absent term here."""
    if not requested_skills or not projects:
        return None
    wanted = {s.lower() for s in requested_skills}
    relevant_dates = [d for required, d in projects if {s.lower() for s in required} & wanted]
    if not relevant_dates:
        return None
    most_recent = max(relevant_dates)
    today = date.today()
    months_since = max(0, (today.year - most_recent.year) * 12 + (today.month - most_recent.month))
    score = max(0.0, 1 - months_since / _RECENCY_DECAY_MONTHS)
    evidence = (
        "currently on a project requiring one of the requested skills" if months_since == 0
        else f"worked on a matching project {months_since} month{'s' if months_since != 1 else ''} ago"
    )
    return score, evidence


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RankedCandidate:
    employee_id: str
    score_pct: int
    matched: list[str]
    missing: list[str]
    #: Whether this candidate holds EVERY skill the query asked for --
    #: feeds the "nobody holds all of X and Y" note (SEARCH_RANKING_
    #: PROPOSAL.md §6.4); meaningless (left True) when the query asked for
    #: no skill at all.
    holds_all_requested_skills: bool = True


def score_candidate(pool: Pool, emp: Employee, interpretation: Interpretation) -> RankedCandidate:
    requested_skills = tuple(e.value for e in interpretation.entities if e.label == "skill")
    # Last one wins on a repeat label, matching app.text_filters.
    # plan_from_interpretation's own loop (an unconditional reassignment,
    # not a break) -- so if query_entities.parse ever emits two `role`
    # entities for one query, the filter that decided WHICH ids are in the
    # running and the score that decides HOW to rank them agree on the
    # same one, instead of silently picking different roles.
    role = None
    seniority = None
    for e in interpretation.entities:
        if e.label == "role":
            role = e.value
        elif e.label == "seniority":
            seniority = e.value

    held = pool.skills_by_employee.get(emp.id, {})
    job_title = emp.job_title or ""

    terms: dict[str, float] = {}
    matched: list[str] = []
    missing: list[str] = []
    holds_all_requested_skills = True

    if requested_skills:
        skill_val, skill_matched, skill_missing = _skill_score(held, requested_skills)
        terms["skills"] = skill_val
        matched.extend(skill_matched)
        missing.extend(skill_missing)
        holds_all_requested_skills = not skill_missing

    role_result = _role_score(job_title, role)
    if role_result is not None:
        role_val, evidence = role_result
        terms["role"] = role_val
        (matched if role_val > 0 else missing).append(evidence)

    seniority_result = _seniority_score(job_title, seniority)
    if seniority_result is not None:
        sen_val, evidence = seniority_result
        terms["seniority"] = sen_val
        (matched if sen_val > 0 else missing).append(evidence)

    recency_result = _recency_score(pool.projects_by_employee.get(emp.id, []), requested_skills)
    if recency_result is not None:
        rec_val, evidence = recency_result
        terms["recency"] = rec_val
        if rec_val > 0:
            matched.append(evidence)

    # Renormalise over the applicable terms' own weights -- see the module
    # docstring for why an absent term is dropped rather than scored 0.
    weight_sum = sum(SEARCH_WEIGHTS[k] for k in terms)
    total = (sum(SEARCH_WEIGHTS[k] * v for k, v in terms.items()) / weight_sum) if weight_sum else 0.0

    return RankedCandidate(
        employee_id=emp.id, score_pct=round(total * 100),
        matched=matched, missing=missing, holds_all_requested_skills=holds_all_requested_skills,
    )


def rank_candidates(
    pool: Pool, employee_ids: frozenset[str], interpretation: Interpretation,
) -> tuple[list[RankedCandidate], bool]:
    """Scores every id in `employee_ids` that actually loaded into `pool`,
    sorts descending, and keeps everything >= SCORE_THRESHOLD up to
    MAX_RANKED_RESULTS (step 6, folded in here -- see the module docstring).

    Returns (survivors, any_holds_all_requested_skills) -- the second value
    is what app.unified_search checks before attaching the "nobody holds
    both X and Y" note (SEARCH_RANKING_PROPOSAL.md §6.4): True whenever the
    query named no skill at all (nothing to report an overlap gap on), so a
    caller should only ever act on it when it also confirms a 2+-skill
    query actually ran.
    """
    requested_skills = [e.value for e in interpretation.entities if e.label == "skill"]

    scored = [
        score_candidate(pool, pool.employees[emp_id], interpretation)
        for emp_id in employee_ids
        if emp_id in pool.employees
    ]
    any_holds_all = (not requested_skills) or any(c.holds_all_requested_skills for c in scored)

    ranked = sorted(scored, key=lambda c: (-c.score_pct, pool.employees[c.employee_id].full_name))
    threshold_pct = round(SCORE_THRESHOLD * 100)
    survivors = [c for c in ranked if c.score_pct >= threshold_pct]
    return survivors[:MAX_RANKED_RESULTS], any_holds_all
