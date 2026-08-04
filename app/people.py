"""find_people and get_person — the only two ways employee data leaves this
service in step 4. Both follow the same fixed pipeline:

    retrieve -> filter records -> filter fields -> department check
             -> cap results -> write audit_log -> respond

The model (step 9) will call these same two functions and nothing else — it
never touches the database directly, and permission logic never runs inside
the model's reach.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog,
    Employee,
    EmployeeProject,
    EmployeeSkill,
    Office,
    OrgUnit,
    Project,
    Skill,
)
from app.models.enums import AvailabilityStatus, SkillLevel
from app.permissions import can_see_confidential_project, is_record_visible, visible_fields
from app.schemas import OfficeOut, PersonDetail, PersonRef, PersonSummary, ProjectHistoryItem, SkillOut

# Query-level restriction: one colleague's email is a lookup, every
# employee's email in one response is bulk extraction.
MAX_RESULTS = 50

# The fixed, always-visible field set PersonSummary is built from — see the
# comment in find_people() for why no per-record field filtering is needed.
SUMMARY_FIELDS = {"id", "full_name", "preferred_name", "job_title", "org_unit", "office", "availability_status"}


def _tenure_band(hire_date: date) -> str:
    years = (date.today() - hire_date).days // 365
    if years < 1:
        return "<1 year"
    return f"{years} year" if years == 1 else f"{years} years"


def _month(d: date | None) -> str | None:
    return d.strftime("%Y-%m") if d else None


def resolve_skill(db: Session, name: str) -> Skill | None:
    """Case-insensitive lookup that resolves a synonym straight to its
    canonical skill, so a caller never needs to know which name is the
    alias — "SRE" and "Site Reliability Engineering" find the same people.
    """
    skill = db.query(Skill).filter(Skill.name.ilike(name)).first()
    if skill is None:
        return None
    if skill.canonical_id is not None:
        return db.get(Skill, skill.canonical_id)
    return skill


def _parse_level(level: str) -> SkillLevel | None:
    return next((m for m in SkillLevel if m.value.lower() == level.lower()), None)


def _office_out(office: Office | None) -> OfficeOut | None:
    if office is None:
        return None
    return OfficeOut(id=office.id, name=office.name, city=office.city, country=office.country)


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, query_text: str,
                  result_count: int, fields_returned: set[str]) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(sorted(fields_returned)), timestamp=datetime.now(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# find_people(name?, skill?, level?, org_unit?, office?, language?, available?)
# ---------------------------------------------------------------------------

def find_people(
    db: Session,
    caller: AuthenticatedUser,
    *,
    name: str | None = None,
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
) -> list[PersonSummary]:
    filters_used = {k: v for k, v in dict(
        name=name, skill=skill, level=level, org_unit=org_unit,
        office=office, language=language, available=available,
    ).items() if v is not None}

    # 1. retrieve
    query = select(Employee).where(Employee.is_active.is_(True))

    if name:
        pattern = f"%{name}%"
        query = query.where((Employee.full_name.ilike(pattern)) | (Employee.preferred_name.ilike(pattern)))
    if org_unit:
        query = query.join(OrgUnit, Employee.org_unit_id == OrgUnit.id).where(OrgUnit.name.ilike(org_unit))
    if office:
        query = query.join(Office, Employee.office_id == Office.id).where(
            (Office.name.ilike(f"%{office}%")) | (Office.city.ilike(f"%{office}%"))
        )
    if available:
        # Only the positive filter is exposed. "Show me everyone who's away"
        # is exactly the restricted aggregate-absence query the spec calls
        # out, so `available=False` is a silent no-op, never a bulk listing.
        query = query.where(Employee.availability_status == AvailabilityStatus.available)
    if skill:
        resolved = resolve_skill(db, skill)
        if resolved is None:
            return []  # no such skill -> no matches, not an error
        skill_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved.id)
        if level:
            parsed_level = _parse_level(level)
            if parsed_level is None:
                return []
            skill_subq = skill_subq.where(EmployeeSkill.level == parsed_level)
        query = query.where(Employee.id.in_(skill_subq))
    if language:
        resolved_lang = resolve_skill(db, language)
        if resolved_lang is None:
            return []
        lang_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved_lang.id)
        query = query.where(Employee.id.in_(lang_subq))

    query = query.order_by(Employee.full_name)
    candidates = db.execute(query).scalars().all()

    # 2. filter records
    visible_records = [e for e in candidates if is_record_visible(caller, e)]

    # 3. filter fields / 4. department check — PersonSummary only ever
    # carries SUMMARY_FIELDS, which is a subset of BASE_FIELDS visible to
    # every role with no ABAC/department gating. Per-record field filtering
    # is therefore provably a no-op for this shape; full RBAC/ABAC/
    # department filtering happens in get_person's detail view instead.

    # 5. cap results
    capped = visible_records[:MAX_RESULTS]

    results = [
        PersonSummary(
            id=e.id, full_name=e.full_name, preferred_name=e.preferred_name,
            job_title=e.job_title, org_unit=_org_unit_name(db, e.org_unit_id),
            office=_office_out(db.get(Office, e.office_id) if e.office_id else None),
            availability_status=e.availability_status.value,
        )
        for e in capped
    ]

    # 6. write to audit_log
    _write_audit(db, caller, "find_people", json.dumps(filters_used), len(results), SUMMARY_FIELDS)

    # 7. respond
    return results


def _org_unit_name(db: Session, org_unit_id: int) -> str:
    unit = db.get(OrgUnit, org_unit_id)
    return unit.name if unit else ""


# ---------------------------------------------------------------------------
# get_person(person_id)
# ---------------------------------------------------------------------------

def get_person(db: Session, caller: AuthenticatedUser, person_id: str) -> PersonDetail | None:
    fields_returned: set[str] = set()
    found = False
    try:
        # 1. retrieve
        target = db.get(Employee, person_id)
        if target is None or not target.is_active:
            return None

        # 2. filter records — identical response (None -> 404) whether the
        # id doesn't exist or the record is record-level restricted.
        if not is_record_visible(caller, target):
            return None

        # 3. filter fields (RBAC + ABAC) / 4. department check
        fields = visible_fields(db, caller, target)
        fields_returned = fields
        found = True

        # 5. cap results — not applicable to a single-record lookup.
        return _build_detail(db, caller, target, fields)
    finally:
        # 6. write to audit_log — logged either way; the audit trail is
        # allowed to know more than the caller's response reveals.
        _write_audit(db, caller, "get_person", f"person_id={person_id}",
                     1 if found else 0, fields_returned)
    # 7. respond (via the return above)


def _build_detail(db: Session, caller: AuthenticatedUser, target: Employee, fields: set[str]) -> PersonDetail:
    kwargs: dict = {"id": target.id, "full_name": target.full_name}

    if "preferred_name" in fields:
        kwargs["preferred_name"] = target.preferred_name
    if "job_title" in fields:
        kwargs["job_title"] = target.job_title
    if "org_unit" in fields:
        kwargs["org_unit"] = _org_unit_name(db, target.org_unit_id)
    if "work_email" in fields:
        kwargs["work_email"] = target.work_email
    if "work_phone" in fields:
        kwargs["work_phone"] = target.work_phone
    if "slack_handle" in fields:
        kwargs["slack_handle"] = target.slack_handle

    office = db.get(Office, target.office_id) if target.office_id else None
    if "effective_timezone" in fields:
        kwargs["effective_timezone"] = target.timezone or (office.timezone if office else None)
    if "office" in fields:
        kwargs["office"] = _office_out(office)

    if "employment_type" in fields:
        kwargs["employment_type"] = target.employment_type.value
    if "photo_url" in fields:
        kwargs["photo_url"] = target.photo_url

    if "manager" in fields and target.manager_id:
        manager = db.get(Employee, target.manager_id)
        if manager:
            kwargs["manager"] = PersonRef(id=manager.id, full_name=manager.full_name)
    if "delegate" in fields and target.delegate_id:
        delegate = db.get(Employee, target.delegate_id)
        if delegate:
            kwargs["delegate"] = PersonRef(id=delegate.id, full_name=delegate.full_name)

    if "availability_status" in fields:
        kwargs["availability_status"] = target.availability_status.value
    if "away_until_month" in fields:
        kwargs["away_until_month"] = _month(target.away_until)
    if "tenure_band" in fields:
        kwargs["tenure_band"] = _tenure_band(target.hire_date)
    if "bio" in fields:
        kwargs["bio"] = target.bio

    if "skills" in fields or "languages" in fields:
        rows = (
            db.query(EmployeeSkill, Skill)
            .join(Skill, EmployeeSkill.skill_id == Skill.id)
            .filter(EmployeeSkill.employee_id == target.id)
            .all()
        )
        skills_out: list[SkillOut] = []
        langs_out: list[SkillOut] = []
        for es, sk in rows:
            item = SkillOut(name=sk.name, category=sk.category.value, level=es.level.value, source=es.source.value)
            (langs_out if sk.category.value == "language" else skills_out).append(item)
        if "skills" in fields:
            kwargs["skills"] = skills_out
        if "languages" in fields:
            kwargs["languages"] = langs_out

    if "project_history" in fields:
        kwargs["project_history"] = _project_history(db, caller, target)

    if "hire_date" in fields:
        kwargs["hire_date"] = target.hire_date
    if "cost_centre" in fields:
        kwargs["cost_centre"] = target.cost_centre
    if "personal_mobile" in fields:
        kwargs["personal_mobile"] = target.personal_mobile

    return PersonDetail(**kwargs)


def _project_history(db: Session, caller: AuthenticatedUser, target: Employee) -> list[ProjectHistoryItem]:
    rows = (
        db.query(EmployeeProject, Project)
        .join(Project, EmployeeProject.project_id == Project.id)
        .filter(EmployeeProject.employee_id == target.id)
        .all()
    )
    items: list[ProjectHistoryItem] = []
    for ep, proj in rows:
        # Confidential projects: visible to members only. Members stay
        # fully visible in the directory — only this membership edge hides.
        if proj.classification.value == "confidential" and not can_see_confidential_project(db, caller, proj.id):
            continue
        items.append(ProjectHistoryItem(
            project_name=proj.name, project_type=proj.type.value, role=ep.role,
            start_month=_month(ep.start_date), end_month=_month(ep.end_date),
            current=ep.end_date is None,
        ))
    return items
