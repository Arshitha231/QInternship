"""find_project_owner, find_mentor, skill_gap, skill_scarcity — the four
tool functions step 9 adds on top of find_people/get_person/get_org_chain.

Same rules as everywhere else: permission filtering happens in Python
after retrieval, restricted records are simply absent (never a 403), and
every call writes an audit_log row regardless of outcome. None of these
rank people by anything except the stated mechanical criteria — no
function here scores "best candidate," since that depends on performance
and ambition, which aren't in the directory.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, EmployeeProject, EmployeeSkill, Project, Skill
from app.models.enums import AvailabilityStatus, ProjectClassification, SkillLevel
from app.org_chart import MAX_DEPTH
from app.org_chart import _traverse as org_traverse
from app.people import MAX_SEARCH_RESULTS, resolve_skill
from app.permissions import can_see_confidential_project, is_record_visible
from app.schemas import MentorCandidate, ProjectOwnerResult, SkillGapItem, SkillScarcityItem


def _tenure_days(emp: Employee) -> int:
    return (date.today() - emp.hire_date).days


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, query_text: str,
                  result_count: int, fields_returned: set[str]) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(sorted(fields_returned)), timestamp=datetime.now(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# find_project_owner(name) — covers project | system | function | policy
# ---------------------------------------------------------------------------

def find_project_owner(db: Session, caller: AuthenticatedUser, name: str) -> ProjectOwnerResult | None:
    result: ProjectOwnerResult | None = None
    try:
        project = db.query(Project).filter(Project.name.ilike(f"%{name}%")).order_by(Project.name).first()
        if project is None:
            return None

        # Confidential: visible to members only. Searching a confidential
        # project name returns no results, not an access-denied message —
        # same rule as any other restricted field or record.
        if (project.classification == ProjectClassification.confidential
                and not can_see_confidential_project(db, caller, project.id)):
            return None

        owner = db.get(Employee, project.owner_id)
        if owner is None or not owner.is_active or not is_record_visible(caller, owner):
            return None

        result = ProjectOwnerResult(
            project_name=project.name, project_type=project.type.value,
            classification=project.classification.value, owner_id=owner.id, owner_name=owner.full_name,
        )
        return result
    finally:
        fields = {"project_name", "project_type", "classification", "owner_id", "owner_name"} if result else set()
        _write_audit(db, caller, "find_project_owner", f"name={name}", 1 if result else 0, fields)


# ---------------------------------------------------------------------------
# find_mentor(skill, caller_id)
#
# Ranking (fixed priority order): Expert level -> available -> caller's own
# reporting chain ranked LOWER (never excluded) -> shared (non-confidential)
# project history -> tenure band as tiebreaker. Every result carries why it
# ranked where it did.
# ---------------------------------------------------------------------------

def find_mentor(db: Session, caller: AuthenticatedUser, skill: str, caller_id: str) -> list[MentorCandidate]:
    results: list[MentorCandidate] = []
    try:
        resolved = resolve_skill(db, skill)
        if resolved is None:
            return []

        rows = (
            db.query(EmployeeSkill, Employee)
            .join(Employee, EmployeeSkill.employee_id == Employee.id)
            .filter(
                EmployeeSkill.skill_id == resolved.id,
                EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
                # `.is_(True)` renders as `IS 1` on SQL Server, which
                # T-SQL rejects (IS only works with NULL) -- `== True`
                # renders as `= 1` everywhere, including here.
                Employee.is_active == True,
                Employee.id != caller_id,
            )
            .all()
        )
        candidates = [(es, e) for es, e in rows if is_record_visible(caller, e)]
        if not candidates:
            return []

        # Caller's reporting chain, both directions — de-prioritized, not excluded.
        chain_ids = {i for i, _d in org_traverse(db, caller_id, "up", MAX_DEPTH)}
        chain_ids |= {i for i, _d in org_traverse(db, caller_id, "down", MAX_DEPTH)}

        caller_project_ids = {
            pid for (pid,) in db.query(EmployeeProject.project_id)
            .join(Project, EmployeeProject.project_id == Project.id)
            .filter(EmployeeProject.employee_id == caller_id,
                    Project.classification != ProjectClassification.confidential)
        }

        def shared_project_count(emp_id: str) -> int:
            their_ids = {
                pid for (pid,) in db.query(EmployeeProject.project_id)
                .join(Project, EmployeeProject.project_id == Project.id)
                .filter(EmployeeProject.employee_id == emp_id,
                        Project.classification != ProjectClassification.confidential)
            }
            return len(caller_project_ids & their_ids)

        scored = []
        for es, e in candidates:
            is_expert = es.level == SkillLevel.expert
            is_available = e.availability_status == AvailabilityStatus.available
            in_chain = e.id in chain_ids
            shared = shared_project_count(e.id)
            tenure = _tenure_days(e)

            sort_key = (0 if is_expert else 1, 0 if is_available else 1, 1 if in_chain else 0, -shared, -tenure)

            reasons = [f"{resolved.name} at {es.level.value} level"]
            reasons.append("available" if is_available else f"currently {e.availability_status.value}")
            if in_chain:
                reasons.append("in your reporting chain")
            if shared:
                reasons.append(f"{shared} shared project{'s' if shared != 1 else ''}")

            scored.append((sort_key, MentorCandidate(
                id=e.id, full_name=e.full_name, job_title=e.job_title,
                level=es.level.value, reason=", ".join(reasons),
            )))

        scored.sort(key=lambda pair: pair[0])
        results = [candidate for _key, candidate in scored][:MAX_SEARCH_RESULTS]
        return results
    finally:
        fields = {"id", "full_name", "job_title", "level", "reason"} if results else set()
        _write_audit(db, caller, "find_mentor", f"skill={skill};caller_id={caller_id}", len(results), fields)


# ---------------------------------------------------------------------------
# skill_gap(required_skills[]) — coverage for a specific named list, e.g.
# "do we have what a project needs".
# ---------------------------------------------------------------------------

def skill_gap(db: Session, caller: AuthenticatedUser, required_skills: list[str]) -> list[SkillGapItem]:
    items: list[SkillGapItem] = []
    try:
        for name in required_skills:
            resolved = resolve_skill(db, name)
            if resolved is None:
                items.append(SkillGapItem(skill=name, recognized=False, expert_count=0,
                                          working_count=0, learning_count=0, gap=True))
                continue
            visible_rows = _visible_skill_holders(db, caller, resolved.id)
            expert = sum(1 for es, _e in visible_rows if es.level == SkillLevel.expert)
            working = sum(1 for es, _e in visible_rows if es.level == SkillLevel.working)
            learning = sum(1 for es, _e in visible_rows if es.level == SkillLevel.learning)
            items.append(SkillGapItem(
                skill=resolved.name, recognized=True, expert_count=expert,
                working_count=working, learning_count=learning, gap=(expert + working) == 0,
            ))
        return items
    finally:
        fields = {"skill", "recognized", "expert_count", "working_count", "learning_count", "gap"}
        _write_audit(db, caller, "skill_gap", json.dumps(required_skills), len(items), fields)


# ---------------------------------------------------------------------------
# skill_scarcity(skill?) — one named skill's coverage, or (no argument) the
# org's scarcest skills overall.
# ---------------------------------------------------------------------------

def skill_scarcity(db: Session, caller: AuthenticatedUser, skill: str | None = None) -> list[SkillScarcityItem]:
    items: list[SkillScarcityItem] = []
    try:
        if skill:
            resolved = resolve_skill(db, skill)
            skills_to_check = [resolved] if resolved else []
        else:
            # Org-wide scan: canonical skills only — alias rows (SRE, K8s,
            # ...) are never assigned to anyone, so they'd always show as
            # 100% scarce, which isn't a real signal.
            skills_to_check = db.query(Skill).filter(Skill.canonical_id.is_(None)).all()

        for sk in skills_to_check:
            visible_rows = _visible_skill_holders(db, caller, sk.id)
            expert = sum(1 for es, _e in visible_rows if es.level == SkillLevel.expert)
            working = sum(1 for es, _e in visible_rows if es.level == SkillLevel.working)
            learning = sum(1 for es, _e in visible_rows if es.level == SkillLevel.learning)
            items.append(SkillScarcityItem(skill=sk.name, expert_count=expert, working_count=working,
                                           learning_count=learning, capable_count=expert + working))

        if not skill:
            items.sort(key=lambda i: i.capable_count)
            items = items[:MAX_SEARCH_RESULTS * 2]  # a short "here's where we're thin" list, not the whole catalog
        return items
    finally:
        fields = {"skill", "expert_count", "working_count", "learning_count", "capable_count"}
        _write_audit(db, caller, "skill_scarcity", skill or "(org-wide scan)", len(items), fields)


def _visible_skill_holders(db: Session, caller: AuthenticatedUser, skill_id: int) -> list[tuple[EmployeeSkill, Employee]]:
    rows = (
        db.query(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .filter(EmployeeSkill.skill_id == skill_id, Employee.is_active == True)
        .all()
    )
    return [(es, e) for es, e in rows if is_record_visible(caller, e)]
