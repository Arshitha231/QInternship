"""Staffing Continuity Intelligence — HR-only. ARCHITECTURE_2.md §10 (Mode
5) plus the fuller continuity design doc it's paired with.

Connects HR-verified work-authorization review events
(WorkAuthorizationRecord) to this organization's active client engagements
and workforce capability graph. Answers "does an upcoming HR review
intersect an ongoing engagement, and if so, where is delivery
low-redundancy" -- not "whose visa is expiring". The unit classified is
always the ENGAGEMENT (a project), never the employee: UI/API language is
"Project Apollo — High continuity exposure", never "<person> — HIGH RISK".

Deterministic throughout. No model call anywhere in this module — facts
(overlap days, redundancy counts) and severity classification are both
code, from versioned config (config/continuity_thresholds.yml), same split
of responsibilities ARCHITECTURE_2.md §10 lays out.

Same shape as app/directory_tools.py: small functions, one AuditLog row per
public call via a local _write_audit, typed Pydantic returns. No ABAC —
continuity's gate is the simple binary caller.role == "hr", checked in
every public function here (not only at the route layer — ARCHITECTURE_2.md
invariant #3) and *again* at the route layer in app/main.py for the HTTP
status shape, matching the existing double-check pattern at
/notifications/date-milestones and /people/{id}/training/{code}.

Deliberately outside app/query_plan.py/app/policy.py entirely — this table
is never joined into find_people/PeopleQuery, so it's structurally
unreachable through the general query pipeline. See app/registry.py's
DERIVED_HR comment for the reasoning, and tests/test_continuity_isolation.py
for the tests that prove it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.config import continuity_thresholds_path
from app.models import (
    AuditLog, Employee, EmployeeProject, EmployeeSkill, Project, ProjectSkillRequirement, WorkAuthorizationRecord,
)
from app.models.enums import SkillCategory, SkillLevel, VerificationStatus
from app.models.office import Office
from app.models.org_unit import OrgUnit
from app.models.skill import Skill
from app.schemas import (
    AuthorizationRecordOut, BackupCandidate, ContinuityOverview, DeliveryDependency,
    EmployeeContinuityDetail, EngagementExposure, HrReviewQueueItem, PersonRef,
)

# Display cap on backup candidates shown per dependency -- not a business
# rule, same spirit as app/people.py's MAX_SEARCH_RESULTS. org_backup_count
# itself is never capped; only how many candidates are listed.
MAX_BACKUP_CANDIDATES = 5

_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


class ContinuityForbidden(Exception):
    """Raised by every public function in this module for a non-hr caller."""


def _require_hr(caller: AuthenticatedUser) -> None:
    if caller.role != "hr":
        raise ContinuityForbidden("Continuity data is an HR-only view")


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, target: str,
                  result_count: int, fields_returned: set[str]) -> None:
    """Reuses the existing AuditLog table rather than a second one -- there
    is currently no HR-only audit *viewer* in this codebase to make
    co-mingling with directory_tools.py's rows unsafe (grepped: AuditLog is
    write-only today). `target` is an identifier only (employee/project id
    plus filter values) -- never a raw authorization type or date, same
    discipline directory_tools.py already applies."""
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=target, result_count=result_count,
        fields_returned=json.dumps(sorted(fields_returned)), timestamp=datetime.now(),
    ))
    db.commit()


# --- Thresholds --------------------------------------------------------

@dataclass(frozen=True)
class ThresholdConfig:
    rule_version: int
    lookahead_days: int
    high_max_redundancy: int    # org_backup_count <= this -> high
    medium_max_redundancy: int  # org_backup_count <= this (and > high) -> medium
    # anything above medium_max_redundancy -> low (a floor, not a ceiling)


def _load_thresholds(path: Path | None = None) -> ThresholdConfig:
    """Re-read on every call, not cached at import time -- app/config.py's
    own convention, and what lets a test point
    CONTINUITY_THRESHOLDS_PATH at a fixture file without reimporting this
    module."""
    raw = yaml.safe_load((path or continuity_thresholds_path()).read_text())
    severity = raw["severity"]
    high = severity["high"]["redundancy"]
    medium = severity["medium"]["redundancy"]
    return ThresholdConfig(
        rule_version=raw["rule_version"],
        lookahead_days=raw["lookahead_days"],
        high_max_redundancy=high if isinstance(high, int) else max(high),
        medium_max_redundancy=medium[-1] if isinstance(medium, list) else medium,
    )


def _severity_for_redundancy(org_backup_count: int, cfg: ThresholdConfig) -> str:
    if org_backup_count <= cfg.high_max_redundancy:
        return "high"
    if org_backup_count <= cfg.medium_max_redundancy:
        return "medium"
    return "low"


# --- Authorization history ----------------------------------------------

def _get_current_authorization_record(db: Session, employee_id: str) -> WorkAuthorizationRecord | None:
    """The one record the continuity engine ever reasons about: is_current
    AND verified, checked as two explicit conditions rather than trusting
    is_current alone -- defends against a future bug that flips one
    without the other, so a pending/unverified update can never smuggle
    itself into organizational analysis."""
    return (
        db.query(WorkAuthorizationRecord)
        .filter(
            WorkAuthorizationRecord.employee_id == employee_id,
            # `.is_(True)` renders as `IS 1` on SQL Server, which T-SQL
            # rejects outside a NULL comparison; `== True` renders as `= 1`
            # everywhere -- same convention as Employee.is_active elsewhere.
            WorkAuthorizationRecord.is_current == True,  # noqa: E712
            WorkAuthorizationRecord.verification_status == VerificationStatus.verified,
        )
        .first()
    )


def _to_record_out(record: WorkAuthorizationRecord) -> AuthorizationRecordOut:
    return AuthorizationRecordOut(
        id=record.id, authorization_type=record.authorization_type.value,
        effective_from=record.effective_from, effective_until=record.effective_until,
        next_hr_review_date=record.next_hr_review_date,
        verification_status=record.verification_status.value,
        is_current=record.is_current, verified_at=record.verified_at,
    )


# --- Engagement intersection ---------------------------------------------

def _get_relevant_client_assignments(db: Session, employee_id: str, today: date) -> list[EmployeeProject]:
    """This employee's currently-active-or-open-ended assignments on
    is_client_engagement=True projects. An assignment that already ended
    isn't a live continuity signal, so it's excluded here rather than left
    for the intersection check below to discard."""
    return (
        db.query(EmployeeProject)
        .join(Project, EmployeeProject.project_id == Project.id)
        .filter(
            EmployeeProject.employee_id == employee_id,
            Project.is_client_engagement == True,  # noqa: E712
            or_(EmployeeProject.end_date.is_(None), EmployeeProject.end_date >= today),
        )
        .all()
    )


def _review_intersects_assignment(review_date: date, assignment: EmployeeProject) -> bool:
    """The primary trigger (design doc §9): does the HR review date fall
    before the assignment ends? Open-ended (end_date is None) always
    intersects. A review landing exactly on the assignment's last day
    counts as NOT intersecting -- nothing remains after it."""
    if assignment.end_date is None:
        return True
    return review_date < assignment.end_date


# --- Delivery dependencies and backups ------------------------------------

def _project_member_ids(db: Session, project_id: int) -> set[str]:
    rows = db.query(EmployeeProject.employee_id).filter(EmployeeProject.project_id == project_id).all()
    return {r[0] for r in rows}


def _skill_holder_ids(db: Session, skill_id: int, exclude_employee_id: str) -> list[str]:
    rows = (
        db.query(EmployeeSkill.employee_id)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .filter(
            EmployeeSkill.skill_id == skill_id,
            EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
            Employee.is_active == True,  # noqa: E712
            EmployeeSkill.employee_id != exclude_employee_id,
        )
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def _role_holder_ids(db: Session, role: str, exclude_employee_id: str) -> list[str]:
    """Scoped to other client engagements only -- holding "Lead" on a
    purely-internal housekeeping project isn't evidence of client-facing
    redeployability."""
    rows = (
        db.query(EmployeeProject.employee_id)
        .join(Employee, EmployeeProject.employee_id == Employee.id)
        .join(Project, EmployeeProject.project_id == Project.id)
        .filter(
            EmployeeProject.role == role,
            Project.is_client_engagement == True,  # noqa: E712
            Employee.is_active == True,  # noqa: E712
            EmployeeProject.employee_id != exclude_employee_id,
        )
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def _split_backup_holders(db: Session, holder_ids: list[str], project_id: int) -> tuple[int, int, list[str]]:
    """(project_backup_count, org_backup_count, org-wide holder ids).
    Project members count toward project_backup_count (design doc §13:
    "does anyone already on this project satisfy the same dependency").
    Everyone else counts toward org_backup_count and is a candidate the
    backup search surfaces (design doc §14: "look outside the
    engagement") -- severity is keyed on org_backup_count specifically,
    since someone already committed to the project isn't a redeployable
    backup in the reassignment sense."""
    members = _project_member_ids(db, project_id)
    on_project = [h for h in holder_ids if h in members]
    elsewhere = [h for h in holder_ids if h not in members]
    return len(on_project), len(elsewhere), elsewhere


def _backup_candidates(db: Session, holder_ids: list[str], evidence: str) -> list[BackupCandidate]:
    shown = holder_ids[:MAX_BACKUP_CANDIDATES]
    if not shown:
        return []
    rows = db.query(Employee).filter(Employee.id.in_(shown)).all()
    by_id = {e.id: e for e in rows}
    return [
        BackupCandidate(id=h, full_name=by_id[h].full_name, matching_evidence=evidence)
        for h in shown if h in by_id
    ]


def _find_internal_backups(
    db: Session, dependency_type: str, dependency_name: str, project_id: int, exclude_employee_id: str,
) -> tuple[int, int, list[BackupCandidate]]:
    """Dependency-specific search only -- never "people similar to this
    employee" (design doc §15). Returns (project_backup_count,
    org_backup_count, capped list of org-wide backup candidates)."""
    if dependency_type == "skill":
        skill = db.query(Skill).filter(Skill.name == dependency_name).first()
        holder_ids = _skill_holder_ids(db, skill.id, exclude_employee_id) if skill else []
        evidence = f"Holds {dependency_name} at Working level or above"
    else:  # project_role
        holder_ids = _role_holder_ids(db, dependency_name, exclude_employee_id)
        evidence = f"Currently holds the {dependency_name} role on another client engagement"

    project_backup_count, org_backup_count, elsewhere_ids = _split_backup_holders(db, holder_ids, project_id)
    candidates = _backup_candidates(db, elsewhere_ids, evidence)
    return project_backup_count, org_backup_count, candidates


_SKILL_LEVEL_RANK = {SkillLevel.learning: 0, SkillLevel.working: 1, SkillLevel.expert: 2}


def _meets_level(level: SkillLevel, minimum: SkillLevel) -> bool:
    return _SKILL_LEVEL_RANK[level] >= _SKILL_LEVEL_RANK[minimum]


def _project_required_skills(db: Session, project_id: int) -> dict[int, SkillLevel]:
    """skill_id -> minimum level, from app/project_skills.py's
    ProjectSkillRequirement rows. Empty when nobody has recorded this
    project's requirements yet -- the caller falls back to the inferred
    heuristic in that case, never to an error."""
    rows = (
        db.query(ProjectSkillRequirement)
        .filter(ProjectSkillRequirement.project_id == project_id)
        .all()
    )
    return {r.skill_id: r.minimum_level for r in rows}


def _compute_delivery_dependencies(
    db: Session, employee: Employee, project: Project, assignment: EmployeeProject,
) -> tuple[list[DeliveryDependency], dict[str, list[BackupCandidate]]]:
    """MVP = skill + project_role only, per the design doc's own
    "Recommended MVP" -- industry/domain dependency is skipped because no
    industry concept exists anywhere in this schema.

    Skill dependencies come from app/project_skills.py's declared
    requirements when the project has any recorded: only skills the
    project actually needs, that this employee holds at or above the
    required level, count -- source="declared". When no requirements have
    been recorded for this project at all, this falls back to the
    original heuristic -- every Working/Expert technical/domain skill the
    employee happens to hold while staffed here -- source="inferred", so
    the response never claims more precision than the underlying data
    supports. Language skills are excluded from the inferred path: a
    delivery dependency is a specialized capability an engagement could be
    short-staffed on, and general language fluency isn't that.

    project_role is always source="declared": employee_projects.role is a
    real recorded fact, not an inference over unrelated data.
    """
    dependencies: list[DeliveryDependency] = []
    backups: dict[str, list[BackupCandidate]] = {}
    person_ref = PersonRef(id=employee.id, full_name=employee.full_name)

    required = _project_required_skills(db, project.id)
    if required:
        skill_rows = (
            db.query(EmployeeSkill, Skill)
            .join(Skill, EmployeeSkill.skill_id == Skill.id)
            .filter(EmployeeSkill.employee_id == employee.id, EmployeeSkill.skill_id.in_(required.keys()))
            .all()
        )
        skill_source = "declared"
        skill_rows = [(es, skill) for es, skill in skill_rows if _meets_level(es.level, required[skill.id])]
    else:
        skill_rows = (
            db.query(EmployeeSkill, Skill)
            .join(Skill, EmployeeSkill.skill_id == Skill.id)
            .filter(
                EmployeeSkill.employee_id == employee.id,
                EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
                Skill.category != SkillCategory.language,
            )
            .all()
        )
        skill_source = "inferred"

    for _es, skill in skill_rows:
        project_backup_count, org_backup_count, candidates = _find_internal_backups(
            db, "skill", skill.name, project.id, employee.id)
        dependencies.append(DeliveryDependency(
            type="skill", name=skill.name, project_id=project.id, employee=person_ref,
            project_backup_count=project_backup_count, org_backup_count=org_backup_count,
            source=skill_source,
        ))
        backups[skill.name] = candidates

    project_backup_count, org_backup_count, candidates = _find_internal_backups(
        db, "project_role", assignment.role, project.id, employee.id)
    dependencies.append(DeliveryDependency(
        type="project_role", name=assignment.role, project_id=project.id, employee=person_ref,
        project_backup_count=project_backup_count, org_backup_count=org_backup_count,
        source="declared",
    ))
    backups[assignment.role] = candidates

    return dependencies, backups


def _severity_for_dependency(dep: DeliveryDependency, cfg: ThresholdConfig) -> str:
    """Two-tier, per design doc §13-14: project-level redundancy is checked
    FIRST. If anyone else already on this same engagement covers the
    dependency (project_backup_count > 0), that's "adequate project
    redundancy" and the dependency is low-risk regardless of org-wide
    backup count -- the org-wide search only matters for dependencies that
    are single-person AT THE PROJECT LEVEL, which is exactly when design
    doc §14 says to "look outside the engagement" in the first place."""
    if dep.project_backup_count > 0:
        return "low"
    return _severity_for_redundancy(dep.org_backup_count, cfg)


def _worst_severity(dependencies: list[DeliveryDependency], cfg: ThresholdConfig) -> str:
    if not dependencies:
        return "low"  # intersects, but no dependency found at all -> low, per design doc §16
    return max(
        (_severity_for_dependency(d, cfg) for d in dependencies),
        key=lambda s: _SEVERITY_RANK[s],
    )


def _dependency_reasons(project_name: str, dependencies: list[DeliveryDependency], cfg: ThresholdConfig) -> list[str]:
    """Factual, mechanical sentences only (design doc §17) -- never
    speculative about the employee. Only thin (medium/high) dependencies
    get a sentence; adequately-redundant ones stay visible in the
    dependencies list without needing one."""
    reasons = []
    for dep in dependencies:
        severity = _severity_for_dependency(dep, cfg)
        if severity == "high":
            reasons.append(
                f"The {project_name} engagement currently has a single-person "
                f"{dep.name} dependency with no identified internal backup."
            )
        elif severity == "medium":
            plural = "s" if dep.org_backup_count != 1 else ""
            reasons.append(
                f"The {project_name} engagement's {dep.name} dependency has only "
                f"{dep.org_backup_count} internal backup{plural} identified."
            )
    return reasons


# --- Exposure: one (employee, project, assignment) triple -----------------

def _build_engagement_exposure_entry(
    db: Session, employee: Employee, project: Project, assignment: EmployeeProject,
    record: WorkAuthorizationRecord, cfg: ThresholdConfig, today: date,
) -> EngagementExposure:
    """Always returns an entry -- exposure="none" with no dependencies when
    the review doesn't intersect this assignment. Used directly by the
    employee drill-down (which shows every assignment, intersecting or
    not) and as the per-employee building block
    _compute_engagement_exposure aggregates for the org-wide view."""
    if record.next_hr_review_date is None or not _review_intersects_assignment(
        record.next_hr_review_date, assignment
    ):
        return EngagementExposure(
            project_id=project.id, project_name=project.name, exposure="none",
            rule_version=cfg.rule_version,
            reasons=["No HR review event currently intersects this engagement."],
            intersecting_review_count=0, dependencies=[], backups={},
        )

    dependencies, backups = _compute_delivery_dependencies(db, employee, project, assignment)
    severity = _worst_severity(dependencies, cfg)
    reasons = [
        f"An upcoming HR review event intersects the remaining {project.name} engagement period."
    ] + _dependency_reasons(project.name, dependencies, cfg)

    days_until_review = (record.next_hr_review_date - today).days
    days_remaining_after = (
        None if assignment.end_date is None
        else (assignment.end_date - record.next_hr_review_date).days
    )
    return EngagementExposure(
        project_id=project.id, project_name=project.name, exposure=severity,
        rule_version=cfg.rule_version, reasons=reasons, intersecting_review_count=1,
        days_until_hr_review=days_until_review,
        days_of_assignment_remaining_after_review=days_remaining_after,
        dependencies=dependencies, backups=backups,
    )


def _compute_employee_engagement_entries(
    db: Session, employee: Employee, record: WorkAuthorizationRecord | None,
    cfg: ThresholdConfig, today: date,
) -> list[EngagementExposure]:
    """Every one of this employee's current client-engagement assignments,
    each rendered on its own -- unlike the org-wide view, non-intersecting
    assignments are NOT skipped (design doc §25's own example: "Phoenix:
    Assignment ends before HR review" is exactly as informative to HR as
    "Apollo: Assignment continues beyond HR review"). Empty when there's no
    current verified record -- nothing to check intersection against."""
    if record is None:
        return []
    entries = []
    for assignment in _get_relevant_client_assignments(db, employee.id, today):
        project = db.get(Project, assignment.project_id)
        if project is None:
            continue
        entries.append(_build_engagement_exposure_entry(db, employee, project, assignment, record, cfg, today))
    return entries


def _compute_engagement_exposure(
    db: Session, project: Project, cfg: ThresholdConfig, today: date,
) -> EngagementExposure | None:
    """Org-wide entry point for one client-engagement project. Aggregates
    across every currently-assigned employee with a current, verified
    record; returns None (skip from the org-wide list) if nobody's review
    intersects this engagement at all -- "None" is a precondition checked
    before the severity thresholds are consulted, not one of the bands."""
    assignments = (
        db.query(EmployeeProject)
        .filter(
            EmployeeProject.project_id == project.id,
            or_(EmployeeProject.end_date.is_(None), EmployeeProject.end_date >= today),
        )
        .all()
    )
    entries: list[EngagementExposure] = []
    for assignment in assignments:
        employee = db.get(Employee, assignment.employee_id)
        if employee is None or not employee.is_active:
            continue
        record = _get_current_authorization_record(db, employee.id)
        if record is None:
            continue
        entry = _build_engagement_exposure_entry(db, employee, project, assignment, record, cfg, today)
        if entry.exposure != "none":
            entries.append(entry)

    if not entries:
        return None

    merged_dependencies = [dep for e in entries for dep in e.dependencies]
    merged_backups: dict[str, list[BackupCandidate]] = {}
    for e in entries:
        merged_backups.update(e.backups)
    merged_reasons = [r for e in entries for r in e.reasons]
    nearest = min(entries, key=lambda e: e.days_until_hr_review)
    severity = max((e.exposure for e in entries), key=lambda s: _SEVERITY_RANK[s])

    return EngagementExposure(
        project_id=project.id, project_name=project.name, exposure=severity,
        rule_version=cfg.rule_version, reasons=merged_reasons,
        intersecting_review_count=len(entries),
        days_until_hr_review=nearest.days_until_hr_review,
        days_of_assignment_remaining_after_review=nearest.days_of_assignment_remaining_after_review,
        dependencies=merged_dependencies, backups=merged_backups,
    )


def _employee_ids_in_office(db: Session, office_name: str) -> set[str]:
    rows = (
        db.query(Employee.id).join(Office, Employee.office_id == Office.id)
        .filter(Office.name.ilike(f"%{office_name}%"))
        .all()
    )
    return {r[0] for r in rows}


def _employee_ids_in_org_unit(db: Session, org_unit_name: str) -> set[str]:
    rows = (
        db.query(Employee.id).join(OrgUnit, Employee.org_unit_id == OrgUnit.id)
        .filter(OrgUnit.name.ilike(f"%{org_unit_name}%"))
        .all()
    )
    return {r[0] for r in rows}


def _list_engagement_exposures(
    db: Session, cfg: ThresholdConfig, today: date, window_days: int | None = None, *,
    exposure: str | None = None, client: str | None = None, project_name: str | None = None,
    office: str | None = None, org_unit: str | None = None, dependency_type: str | None = None,
) -> list[EngagementExposure]:
    """Shared filtered/sorted list builder behind both /continuity/exposure
    (wrapped into a ContinuityOverview) and /continuity/engagement-exposure
    (returned as-is)."""
    effective_window = window_days if window_days is not None else cfg.lookahead_days

    query = db.query(Project).filter(Project.is_client_engagement == True)  # noqa: E712
    if client:
        query = query.filter(Project.name.ilike(f"%{client}%"))
    if project_name:
        query = query.filter(Project.name.ilike(f"%{project_name}%"))
    projects = query.all()

    office_ids = _employee_ids_in_office(db, office) if office else None
    org_unit_ids = _employee_ids_in_org_unit(db, org_unit) if org_unit else None

    results: list[EngagementExposure] = []
    for project in projects:
        entry = _compute_engagement_exposure(db, project, cfg, today)
        if entry is None:
            continue
        if entry.days_until_hr_review is not None and entry.days_until_hr_review > effective_window:
            continue
        if exposure and entry.exposure != exposure:
            continue
        if dependency_type and not any(d.type == dependency_type for d in entry.dependencies):
            continue
        if office_ids is not None and not any(d.employee.id in office_ids for d in entry.dependencies):
            continue
        if org_unit_ids is not None and not any(d.employee.id in org_unit_ids for d in entry.dependencies):
            continue
        results.append(entry)

    results.sort(key=lambda e: (-_SEVERITY_RANK[e.exposure], e.days_until_hr_review or 0))
    return results


# --- HR Review Queue -------------------------------------------------------
# The original HR pain point: "who is nearing a review date", full stop --
# independent of whether that review happens to intersect a client
# engagement. _list_engagement_exposures above deliberately never surfaces
# an employee whose review doesn't intersect anything (design doc's Case D);
# this is the complementary view that does, because HR still needs to
# track and verify that record regardless of its delivery consequence.

def _list_review_queue_items(
    db: Session, cfg: ThresholdConfig, today: date, window_days: int | None = None,
) -> list[HrReviewQueueItem]:
    effective_window = window_days if window_days is not None else cfg.lookahead_days

    rows = (
        db.query(WorkAuthorizationRecord, Employee)
        .join(Employee, WorkAuthorizationRecord.employee_id == Employee.id)
        .filter(
            WorkAuthorizationRecord.is_current == True,  # noqa: E712
            WorkAuthorizationRecord.verification_status == VerificationStatus.verified,
            WorkAuthorizationRecord.next_hr_review_date.isnot(None),
            Employee.is_active == True,  # noqa: E712
        )
        .all()
    )

    items: list[HrReviewQueueItem] = []
    for record, employee in rows:
        days_until = (record.next_hr_review_date - today).days
        if days_until > effective_window:
            continue
        entries = _compute_employee_engagement_entries(db, employee, record, cfg, today)
        intersecting = [e for e in entries if e.exposure != "none"]
        highest = max(
            (e.exposure for e in intersecting), key=lambda s: _SEVERITY_RANK[s], default="none",
        )
        items.append(HrReviewQueueItem(
            employee=PersonRef(id=employee.id, full_name=employee.full_name),
            current_record=_to_record_out(record),
            days_until_hr_review=days_until,
            engagements_affected=len(intersecting),
            highest_exposure=highest,
        ))

    items.sort(key=lambda i: i.days_until_hr_review)
    return items


# --- Public functions -- each checks caller.role == "hr", each writes ----
# --- exactly one AuditLog row via _write_audit ----------------------------

def get_org_exposure(db: Session, caller: AuthenticatedUser, window_days: int | None = None) -> ContinuityOverview:
    _require_hr(caller)
    cfg = _load_thresholds()
    today = date.today()
    engagements = _list_engagement_exposures(db, cfg, today, window_days)
    effective_window = window_days if window_days is not None else cfg.lookahead_days

    by_severity = {"high": 0, "medium": 0, "low": 0}
    for e in engagements:
        if e.exposure in by_severity:
            by_severity[e.exposure] += 1

    overview = ContinuityOverview(
        rule_version=cfg.rule_version, window_days=effective_window,
        by_severity=by_severity, engagements=engagements,
    )
    _write_audit(db, caller, "continuity.overview_view", f"window_days={effective_window}",
                 len(engagements), {"rule_version", "window_days", "by_severity", "engagements"})
    return overview


def get_engagement_exposure(
    db: Session, caller: AuthenticatedUser, *, exposure: str | None = None, client: str | None = None,
    project: str | None = None, office: str | None = None, org_unit: str | None = None,
    dependency_type: str | None = None, window_days: int | None = None,
) -> list[EngagementExposure]:
    _require_hr(caller)
    cfg = _load_thresholds()
    today = date.today()
    results = _list_engagement_exposures(
        db, cfg, today, window_days, exposure=exposure, client=client, project_name=project,
        office=office, org_unit=org_unit, dependency_type=dependency_type,
    )
    _write_audit(
        db, caller, "continuity.engagement_view",
        f"exposure={exposure};client={client};project={project};office={office};"
        f"org_unit={org_unit};dependency_type={dependency_type}",
        len(results), {"project_id", "project_name", "exposure", "dependencies", "backups"},
    )
    return results


def get_hr_review_queue(
    db: Session, caller: AuthenticatedUser, window_days: int | None = None,
) -> list[HrReviewQueueItem]:
    """The proactive "who is nearing a review date" list -- every employee
    with a current, HR-verified record and a scheduled review, whether or
    not it has any client-engagement consequence. Complements
    get_engagement_exposure, which only ever surfaces the subset whose
    review does intersect something."""
    _require_hr(caller)
    cfg = _load_thresholds()
    today = date.today()
    items = _list_review_queue_items(db, cfg, today, window_days)
    effective_window = window_days if window_days is not None else cfg.lookahead_days
    _write_audit(
        db, caller, "continuity.review_queue_view", f"window_days={effective_window}",
        len(items), {"employee", "current_record", "days_until_hr_review",
                     "engagements_affected", "highest_exposure"},
    )
    return items


def get_employee_continuity(db: Session, caller: AuthenticatedUser, employee_id: str) -> EmployeeContinuityDetail | None:
    _require_hr(caller)
    employee = db.get(Employee, employee_id)
    result: EmployeeContinuityDetail | None = None
    try:
        if employee is None or not employee.is_active:
            return None

        cfg = _load_thresholds()
        today = date.today()
        current = _get_current_authorization_record(db, employee_id)
        history_rows = (
            db.query(WorkAuthorizationRecord)
            .filter(WorkAuthorizationRecord.employee_id == employee_id)
            .order_by(WorkAuthorizationRecord.effective_from.desc())
            .all()
        )
        engagements = _compute_employee_engagement_entries(db, employee, current, cfg, today)

        result = EmployeeContinuityDetail(
            employee=PersonRef(id=employee.id, full_name=employee.full_name),
            current_record=_to_record_out(current) if current else None,
            history=[_to_record_out(r) for r in history_rows],
            engagements=engagements,
        )
        return result
    finally:
        _write_audit(
            db, caller, "continuity.employee_view", employee_id,
            1 if result else 0,
            {"employee", "current_record", "history", "engagements"} if result else set(),
        )
