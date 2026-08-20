"""Four askable agents that sit on top of existing directory primitives.

Coverage, escalation, training recommendations, and a person brief — each
returns a small structured answer shaped for /search's assisted overview,
not a second permission system. Visibility still goes through get_person /
get_org_chain / find_project_owner the same way every other tool does.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.certifications.service import employee_training_status
from app.directory_tools import find_project_owner
from app.models import Employee, EmployeeProject, EmployeeSkill, OrgUnit, Project, Skill
from app.models.enums import AvailabilityStatus, ProjectClassification, SkillLevel
from app.org_chart import get_org_chain, manager_chain_ids, resolve_person
from app.people import get_person, resolve_skill
from app.permissions import ViewMode, can_see_confidential_project, is_record_visible
from app.project_search import (
    _assignment_rank,
    _project_excerpts,
    _reason,
    rank_projects,
)
from app.schemas import (
    AmbiguousPersonMatch,
    AmbiguousProjectMatch,
    OrgChainNode,
    PersonChoice,
    PersonDetail,
    PersonRef,
    ProjectOwnerResult,
    TrainingStatusItem,
    UnknownPerson,
)

# Cap on returned upskill candidates — same spirit as find_experts.
_MAX_TRAINEES = 8
# How many projects to consider before hopping to people.
_MAX_UPSKILL_PROJECTS = 8

# Soft career adjacency: skills that often co-occur on the same work.
# Used to find related project work AND to prefer people who already hold
# a neighbor at Working/Expert (Terraform is a natural next step for them).
_SKILL_NEIGHBORS: dict[str, list[str]] = {
    "Terraform": [
        "AWS", "Azure", "GCP", "Kubernetes", "Docker", "CI/CD",
        "Site Reliability Engineering",
    ],
    "Kubernetes": [
        "Docker", "AWS", "Azure", "GCP", "Terraform", "CI/CD",
        "Site Reliability Engineering",
    ],
    "Docker": ["Kubernetes", "CI/CD", "AWS", "Azure", "Terraform"],
    "AWS": ["Terraform", "Kubernetes", "Docker", "Azure", "GCP", "CI/CD"],
    "Azure": ["Terraform", "Kubernetes", "Docker", "AWS", "CI/CD"],
    "GCP": ["Terraform", "Kubernetes", "Docker", "AWS", "CI/CD"],
    "CI/CD": ["Docker", "Kubernetes", "Terraform", "AWS", "Azure"],
    "Site Reliability Engineering": [
        "Kubernetes", "Terraform", "AWS", "Azure", "CI/CD", "Docker",
    ],
    "React": ["TypeScript", "JavaScript", "Node.js", "CSS"],
    "TypeScript": ["React", "JavaScript", "Node.js"],
    "Python": ["SQL", "Data Engineering", "Machine Learning"],
    "SQL": ["Python", "Data Engineering", "Power BI"],
}


class CoverageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: PersonRef
    subject_title: str
    availability_status: str
    cover: PersonRef | None = None
    cover_role: str | None = None  # "delegate" | "manager" | None
    note: str


class EscalationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: PersonRef
    job_title: str
    availability_status: str
    via: str  # "owner" | "manager" | "delegate"


class EscalationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_label: str
    first_contact: PersonRef | None = None
    steps: list[EscalationStep]
    note: str


class TrainingRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_code: str
    course_name: str
    display_status: str
    display_label: str


class TrainingRecommendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: PersonRef
    recommendations: list[TrainingRecommendation]
    note: str


class PersonBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: PersonDetail
    managers_above: list[OrgChainNode]
    note: str


def _person_choices(db: Session, query: str, candidate_ids: tuple[str, ...]) -> AmbiguousPersonMatch:
    choices: list[PersonChoice] = []
    for pid in candidate_ids:
        emp = db.get(Employee, pid)
        if emp is None:
            continue
        choices.append(PersonChoice(
            id=emp.id,
            full_name=emp.full_name,
            job_title=emp.job_title or "",
            org_unit=emp.org_unit.name if emp.org_unit else "",
        ))
    return AmbiguousPersonMatch(query=query, matches=choices)


def _resolve_named(
    db: Session, person: str | None, caller: AuthenticatedUser,
) -> str | UnknownPerson | AmbiguousPersonMatch:
    if not person or person == "self":
        return caller.id
    outcome = resolve_person(db, person)
    if outcome is None or outcome.is_unknown:
        return UnknownPerson(query=person)
    if outcome.is_ambiguous:
        return _person_choices(db, person, outcome.candidates)
    return outcome.person_id


def find_coverage(
    db: Session, caller: AuthenticatedUser, *, person: str, view_mode: ViewMode = "work",
) -> CoverageResult | UnknownPerson | AmbiguousPersonMatch:
    """Who is covering for a named person (or self) right now."""
    resolved = _resolve_named(db, person, caller)
    if isinstance(resolved, (UnknownPerson, AmbiguousPersonMatch)):
        return resolved

    detail = get_person(db, caller, person_id=resolved, view_mode=view_mode)
    if detail is None:
        return UnknownPerson(query=person if person != "self" else caller.name or caller.id)

    subject = PersonRef(id=detail.id, full_name=detail.full_name)
    status = detail.availability_status or "available"

    if detail.delegate is not None:
        return CoverageResult(
            subject=subject,
            subject_title=detail.job_title or "",
            availability_status=status,
            cover=PersonRef(id=detail.delegate.id, full_name=detail.delegate.full_name),
            cover_role="delegate",
            note=(
                f"{detail.full_name} is {status} — "
                f"{detail.delegate.full_name} is listed as their cover."
                if status != "available"
                else (
                    f"{detail.full_name} is available. If they go away, "
                    f"{detail.delegate.full_name} is their listed cover."
                )
            ),
        )

    if status == "away" and detail.manager is not None:
        return CoverageResult(
            subject=subject,
            subject_title=detail.job_title or "",
            availability_status=status,
            cover=PersonRef(id=detail.manager.id, full_name=detail.manager.full_name),
            cover_role="manager",
            note=(
                f"{detail.full_name} is away with no delegate on file — "
                f"escalate to their manager, {detail.manager.full_name}."
            ),
        )

    if status == "available":
        return CoverageResult(
            subject=subject,
            subject_title=detail.job_title or "",
            availability_status=status,
            cover=None,
            cover_role=None,
            note=f"{detail.full_name} is available right now. No delegate is listed.",
        )

    return CoverageResult(
        subject=subject,
        subject_title=detail.job_title or "",
        availability_status=status,
        cover=None,
        cover_role=None,
        note=(
            f"{detail.full_name} is marked {status}, and no cover is "
            "visible for your role."
        ),
    )


def _usable_contact(db: Session, emp: Employee) -> tuple[Employee, str]:
    """Nearest person who can act: the employee if not away, else their delegate."""
    if emp.availability_status != AvailabilityStatus.away:
        return emp, "manager" if emp.manager_id else "owner"
    if emp.delegate_id:
        delegate = db.get(Employee, emp.delegate_id)
        if (
            delegate is not None and delegate.is_active
            and delegate.availability_status != AvailabilityStatus.away
        ):
            return delegate, "delegate"
    return emp, "manager"


def find_escalation(
    db: Session,
    caller: AuthenticatedUser,
    *,
    person: str | None = None,
    project: str | None = None,
    view_mode: ViewMode = "work",
) -> EscalationResult | UnknownPerson | AmbiguousPersonMatch | AmbiguousProjectMatch:
    """Who to escalate to first for a person or a named project/system."""
    start_id: str | None = None
    subject_label = ""

    if project:
        owner = find_project_owner(db, caller, name=project)
        if isinstance(owner, AmbiguousProjectMatch):
            return owner
        if owner is None:
            return EscalationResult(
                subject_label=project,
                first_contact=None,
                steps=[],
                note=f'No owner found for "{project}".',
            )
        assert isinstance(owner, ProjectOwnerResult)
        start_id = owner.owner_id
        subject_label = f"{owner.project_name} (owner: {owner.owner_name})"
    elif person:
        resolved = _resolve_named(db, person, caller)
        if isinstance(resolved, (UnknownPerson, AmbiguousPersonMatch)):
            return resolved
        start_id = resolved
        emp = db.get(Employee, start_id)
        subject_label = emp.full_name if emp else person
    else:
        start_id = caller.id
        subject_label = caller.name or "you"

    steps: list[EscalationStep] = []
    # Include the starting person (owner / subject) as step 0 context, then
    # walk the manager chain the same way maker-checker approval does.
    start = db.get(Employee, start_id)
    if start is None or not start.is_active:
        return UnknownPerson(query=subject_label)

    contact, via = _usable_contact(db, start)
    if project:
        steps.append(EscalationStep(
            person=PersonRef(id=contact.id, full_name=contact.full_name),
            job_title=contact.job_title or "",
            availability_status=contact.availability_status.value,
            via="owner" if via != "delegate" else "delegate",
        ))

    for mid in manager_chain_ids(db, start_id):
        mgr = db.get(Employee, mid)
        if mgr is None or not mgr.is_active:
            continue
        contact, via = _usable_contact(db, mgr)
        steps.append(EscalationStep(
            person=PersonRef(id=contact.id, full_name=contact.full_name),
            job_title=contact.job_title or "",
            availability_status=contact.availability_status.value,
            via=via if via == "delegate" else "manager",
        ))
        if len(steps) >= 4:
            break

    # Permission filter: only keep people the caller can see via get_org_chain
    # upward from start (already RBAC-gated). Re-fetch chain and intersect.
    try:
        chain = get_org_chain(db, caller, person_id=start_id, direction="up", depth=10, view_mode=view_mode)
    except Exception:
        chain = []
    visible_ids = {n.id for n in (chain or [])} | {start_id}
    filtered = [s for s in steps if s.person.id in visible_ids]
    if not filtered and steps:
        # Caller can't see the chain — still admit first_contact if it's start
        filtered = [s for s in steps if s.person.id == start_id][:1]

    first = filtered[0].person if filtered else None
    if not filtered:
        note = f"No escalation path is visible from {subject_label} for your role."
    else:
        note = (
            f"Escalate {subject_label} to {filtered[0].person.full_name} first"
            + (f" ({filtered[0].job_title})" if filtered[0].job_title else "")
            + "."
        )
    return EscalationResult(
        subject_label=subject_label,
        first_contact=first,
        steps=filtered,
        note=note,
    )


def recommend_training(
    db: Session, caller: AuthenticatedUser, *, person: str = "self", view_mode: ViewMode = "work",
) -> TrainingRecommendResult | UnknownPerson | AmbiguousPersonMatch:
    """Outstanding expected courses for a person (default: the caller)."""
    resolved = _resolve_named(db, person, caller)
    if isinstance(resolved, (UnknownPerson, AmbiguousPersonMatch)):
        return resolved

    # Must be able to see the profile (training_status is policy-gated).
    detail = get_person(db, caller, person_id=resolved, view_mode=view_mode)
    if detail is None:
        if person in (None, "self") or resolved == caller.id:
            return TrainingRecommendResult(
                person=PersonRef(id=caller.id, full_name=caller.name or "you"),
                recommendations=[],
                note="Sign in as a directory employee to see your training recommendations.",
            )
        return UnknownPerson(query=person if person != "self" else caller.name or caller.id)

    emp = db.get(Employee, resolved)
    if emp is None:
        return UnknownPerson(query=person if person != "self" else caller.name or caller.id)

    items = employee_training_status(db, emp)
    person_ref = PersonRef(id=detail.id, full_name=detail.full_name)

    if items is None:
        return TrainingRecommendResult(
            person=person_ref,
            recommendations=[],
            note="Training records aren't available right now.",
        )

    # Prefer expected + incomplete. TrainingStatusItem uses display_status
    # completed / not_completed (see schemas).
    open_items = [
        i for i in items
        if i.expected and i.display_status != "completed"
    ]
    recs = [
        TrainingRecommendation(
            course_code=i.course_code,
            course_name=i.course_name,
            display_status=i.display_status,
            display_label=i.display_label,
        )
        for i in open_items[:8]
    ]
    if not recs:
        return TrainingRecommendResult(
            person=person_ref,
            recommendations=[],
            note=f"{detail.full_name} has no outstanding required training.",
        )
    listed = ", ".join(r.course_name for r in recs[:3])
    more = f" (+{len(recs) - 3} more)" if len(recs) > 3 else ""
    return TrainingRecommendResult(
        person=person_ref,
        recommendations=recs,
        note=f"Next training for {detail.full_name}: {listed}{more}.",
    )


class SkillTraineeCandidate(BaseModel):
    """Someone whose project work suggests learning a named skill next.

    Same card shape as ProblemExpert so the UI can show person + project,
    but ranked for career upskill (adjacent tech, not already capable), not
    for answering a stuck-on-problem question.
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    org_unit: str
    availability_status: str
    skill: str
    project_id: int
    project_name: str
    role: str
    current: bool
    reason: str
    adjacent_skills: list[str]
    excerpt: str | None = None


def _capable_employee_ids(db: Session, skill_id: int) -> set[str]:
    rows = (
        db.query(EmployeeSkill.employee_id)
        .filter(
            EmployeeSkill.skill_id == skill_id,
            EmployeeSkill.level.in_((SkillLevel.working, SkillLevel.expert)),
        )
        .all()
    )
    return {r[0] for r in rows}


def _adjacent_held(
    db: Session, employee_id: str, neighbor_ids: list[int],
) -> list[str]:
    if not neighbor_ids:
        return []
    held: list[str] = []
    for es in (
        db.query(EmployeeSkill)
        .filter(
            EmployeeSkill.employee_id == employee_id,
            EmployeeSkill.skill_id.in_(neighbor_ids),
            EmployeeSkill.level.in_((SkillLevel.working, SkillLevel.expert)),
        )
        .all()
    ):
        sk = db.get(Skill, es.skill_id)
        if sk is not None:
            held.append(sk.name)
    return held


def _upskill_query(skill_name: str, neighbors: list[str]) -> str:
    """Text that steers project ranking toward related work, not just the
    skill noun. Includes IaC / cloud wording so infra projects surface even
    when the description never names Terraform explicitly."""
    bits = [skill_name, *neighbors[:5]]
    lowered = skill_name.lower()
    if lowered in ("terraform", "kubernetes", "docker", "aws", "azure", "gcp", "ci/cd"):
        bits.extend([
            "infrastructure", "cloud operations", "infrastructure-as-code",
            "deploy", "cluster", "automation",
        ])
    return " ".join(bits)


def find_skill_trainees(
    db: Session,
    caller: AuthenticatedUser,
    *,
    skills: list[str],
    view_mode: ViewMode = "work",
) -> list[SkillTraineeCandidate]:
    """Who should learn these skills next, based on related project work.

    Finds people on projects adjacent to the skill who are NOT already
    Working/Expert on it — career upskill candidates, not the already-
    Learning pipeline and not mentors (use find_mentor for that).
    """
    today = date.today()
    # employee_id -> best candidate ranking tuple + payload
    best: dict[str, tuple[tuple, SkillTraineeCandidate]] = {}

    for raw in skills:
        resolved = resolve_skill(db, raw)
        skill_name = resolved.name if resolved else raw.strip()
        neighbors = list(_SKILL_NEIGHBORS.get(skill_name, []))
        # Also try case-insensitive key match
        if not neighbors:
            for key, vals in _SKILL_NEIGHBORS.items():
                if key.lower() == skill_name.lower():
                    neighbors = list(vals)
                    skill_name = key
                    break

        capable: set[str] = set()
        neighbor_ids: list[int] = []
        if resolved is not None:
            capable = _capable_employee_ids(db, resolved.id)
            for n in neighbors:
                ns = resolve_skill(db, n)
                if ns is not None:
                    neighbor_ids.append(ns.id)

        query = _upskill_query(skill_name, neighbors)
        project_ids, _mode = rank_projects(db, query)
        project_ids = project_ids[:_MAX_UPSKILL_PROJECTS]
        if not project_ids:
            continue

        visible_projects: list[Project] = []
        for pid in project_ids:
            project = db.get(Project, pid)
            if project is None:
                continue
            if (
                project.classification == ProjectClassification.confidential
                and not can_see_confidential_project(db, caller, project.id)
            ):
                continue
            visible_projects.append(project)
        excerpts = _project_excerpts(db, query, visible_projects)

        for position, project_id in enumerate(project_ids):
            project = db.get(Project, project_id)
            if project is None:
                continue
            if (
                project.classification == ProjectClassification.confidential
                and not can_see_confidential_project(db, caller, project.id)
            ):
                continue

            assignments = (
                db.query(EmployeeProject)
                .filter(EmployeeProject.project_id == project.id)
                .all()
            )
            for assignment in assignments:
                employee = db.get(Employee, assignment.employee_id)
                if employee is None or not employee.is_active:
                    continue
                if employee.id in capable:
                    continue
                if not is_record_visible(caller, employee, view_mode):
                    continue

                held = _adjacent_held(db, employee.id, neighbor_ids)
                # Prefer people who already hold a neighbor skill — otherwise
                # anyone on a vaguely related project floods the list.
                # Still allow zero-adjacent if the project itself matched
                # strongly (position 0–1) so keyword/semantic hits aren't lost.
                if not held and position > 1:
                    continue

                assign_key = _assignment_rank(
                    assignment, today, assignment.employee_id == project.owner_id,
                )
                # Sort: more adjacent skills first, then better project match,
                # then stronger assignment.
                rank_key = (-len(held), position, assign_key)
                unit = db.get(OrgUnit, employee.org_unit_id) if employee.org_unit_id else None
                adj_note = (
                    f" — already strong on {', '.join(held[:3])}"
                    if held else ""
                )
                candidate = SkillTraineeCandidate(
                    id=employee.id,
                    full_name=employee.full_name,
                    job_title=employee.job_title or "",
                    org_unit=unit.name if unit else "",
                    availability_status=employee.availability_status.value,
                    skill=skill_name,
                    project_id=project.id,
                    project_name=project.name,
                    role=assignment.role or "",
                    current=assignment.end_date is None,
                    reason=f"{_reason(project, assignment)}{adj_note}",
                    adjacent_skills=held,
                    excerpt=excerpts.get(project.id),
                )
                current = best.get(employee.id)
                if current is None or rank_key < current[0]:
                    best[employee.id] = (rank_key, candidate)

    ordered = sorted(best.values(), key=lambda item: item[0])
    return [c for _k, c in ordered[:_MAX_TRAINEES]]


def brief_person(
    db: Session, caller: AuthenticatedUser, *, person: str, view_mode: ViewMode = "work",
) -> PersonBrief | UnknownPerson | AmbiguousPersonMatch:
    """Short profile brief: who they are + who sits above them."""
    resolved = _resolve_named(db, person, caller)
    if isinstance(resolved, (UnknownPerson, AmbiguousPersonMatch)):
        return resolved

    detail = get_person(db, caller, person_id=resolved, view_mode=view_mode)
    if detail is None:
        return UnknownPerson(query=person if person != "self" else caller.name or caller.id)

    try:
        chain = get_org_chain(db, caller, person_id=resolved, direction="up", depth=3, view_mode=view_mode) or []
    except Exception:
        chain = []

    bits = [detail.full_name]
    if detail.job_title:
        bits.append(detail.job_title)
    if detail.org_unit:
        bits.append(f"in {detail.org_unit}")
    if detail.manager:
        bits.append(f"reports to {detail.manager.full_name}")
    if detail.availability_status == "away" and detail.delegate:
        bits.append(f"currently away — {detail.delegate.full_name} is covering")
    note = ". ".join(bits) + "."
    return PersonBrief(person=detail, managers_above=list(chain), note=note)
