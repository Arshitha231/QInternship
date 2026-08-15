"""Write paths for hr (internal employee fields) and it (project
descriptions).

Enforcement lives HERE, not in the route. The route is one caller; the
tool-calling layer, a future batch import, and any test that calls the
service directly are others, and a rule that only exists in a FastAPI
decorator is a rule that only applies to callers who happen to come through
FastAPI. Every function below re-derives (role, view_mode) permission from
app.permissions' EDITABLE table before touching a row — the read that the UI
performed first is not evidence of anything.

Each write follows the same four steps, in this order:

    1. authorize (role + view_mode + field, from the table)
    2. persist
    3. re-index (rule 6)
    4. audit

Audit last and unconditionally: a write that succeeded and then failed to
re-index is still a write that happened, and the audit row is what says so.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, EmployeeProject, Project
from app.models.enums import EmploymentType
from app.permissions import ViewMode, can_edit, editable_fields
from app.search_reindex import reindex_employee, reindex_employee_id


class WriteDenied(Exception):
    """Role/view_mode/field combination is not permitted.

    Distinct from "not found" on purpose. The redact-never-reject rule
    governs *reads* — a caller who may not see a record is told nothing
    exists. A write is different: the caller is asserting an intent to
    change data, and silently accepting it while doing nothing would be a
    worse answer than a plain refusal. Reads still 404; writes 403.
    """


class WriteTargetMissing(Exception):
    """No such employee/project/membership."""


# Fields whose values need coercion out of JSON into the column's type.
_DATE_FIELDS = {"date_of_birth", "hire_date"}
_DECIMAL_FIELDS = {"salary"}


def _coerce(field: str, value):
    if value is None:
        return None
    if field in _DATE_FIELDS:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if field in _DECIMAL_FIELDS:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
    if field == "employment_type":
        # The column is Enum(..., validate_strings=True), which would accept
        # the raw string — converting here anyway so the in-memory object
        # holds the same enum type it would after a refresh, rather than a
        # str that compares unequal to EmploymentType.fte until reloaded.
        return value if isinstance(value, EmploymentType) else EmploymentType(value)
    return value


def _audit(
    db: Session, caller: AuthenticatedUser, action: str, query_text: str,
    fields: set[str] | list[str], result_count: int = 1, source: str | None = None,
) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text,
        result_count=result_count, fields_returned=json.dumps(sorted(fields)),
        source=source, timestamp=datetime.now(),
    ))
    db.commit()


def _authorize(role: str, view_mode: ViewMode, fields) -> None:
    denied = sorted(f for f in fields if not can_edit(role, view_mode, f))
    if denied:
        raise WriteDenied(
            f"role '{role}' in {view_mode} mode may not edit: {', '.join(denied)}. "
            f"Editable here: {', '.join(sorted(editable_fields(role, view_mode))) or 'nothing'}"
        )


# ---------------------------------------------------------------------------
# HR, work mode: edit internal fields on any employee.
# ---------------------------------------------------------------------------

def update_employee(
    db: Session, caller: AuthenticatedUser, person_id: str, changes: dict,
    view_mode: ViewMode,
) -> Employee:
    """PATCH semantics: only the keys present are touched.

    An explicit null clears the field — which is why this takes the raw dict
    of supplied keys rather than a fully-populated model. "salary": null
    means "no salary on file", and must stay distinguishable from omitting
    salary entirely, the same distinction the read side maintains between an
    absent key and a null one.
    """
    if not changes:
        raise ValueError("no fields supplied")

    _authorize(caller.role, view_mode, changes.keys())

    # No self-service through the admin edit path, for anyone who reaches
    # it — today that's hr alone, since EDITABLE grants update_employee
    # fields to no other (role, view_mode) pair, but the check is written
    # against the endpoint's own rule ("edit anyone's record") rather than
    # hardcoded to the role, so it stays correct if EDITABLE ever grows a
    # second entry here. The obvious hole this closes: an hr caller giving
    # themselves a raise, or clearing their own cost_centre, through the
    # same endpoint that edits everyone else's.
    if person_id == caller.id:
        raise WriteDenied(
            f"role '{caller.role}' may edit any employee's record except their own "
            f"(person_id == caller.id)"
        )

    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        raise WriteTargetMissing(person_id)

    for field, raw in changes.items():
        setattr(target, field, _coerce(field, raw))
    db.commit()
    db.refresh(target)

    # full_name / preferred_name / job_title all feed build_profile_text.
    # Re-indexing on any change here rather than only on those three: the
    # cost is one request, and a list of "indexed fields" maintained by hand
    # in a second place is a rule 6 violation waiting to happen the next
    # time build_profile_text grows a line.
    reindex_employee(db, target)

    _audit(db, caller, "update_employee", f"person_id={person_id}", changes.keys())
    return target


# ---------------------------------------------------------------------------
# IT, work mode: CRUD on project descriptions.
#
# "CRUD on project entries" scoped to the description field specifically —
# EDITABLE gives it exactly {"project_desc"}, so creating or deleting the
# Project row itself (which would take name, type, classification, owner,
# owning unit — none of them editable by it) is out of scope by the same
# table that governs the edits. Remove therefore clears the description; it
# does not delete a project out from under the people staffed on it.
# ---------------------------------------------------------------------------

def set_project_description(
    db: Session, caller: AuthenticatedUser, project_id: int, description: str | None,
    view_mode: ViewMode, source: str | None = None,
) -> Project:
    _authorize(caller.role, view_mode, {"project_desc"})

    project = db.get(Project, project_id)
    if project is None:
        raise WriteTargetMissing(str(project_id))

    project.description = description
    db.commit()
    db.refresh(project)

    # Project descriptions are not in build_profile_text today, but project
    # NAMES are, and everyone staffed on the project has the project in
    # their profile text. Re-indexing the members keeps rule 6 true by the
    # letter rather than by the accident of which project fields the
    # profile text currently happens to include.
    _reindex_project_members(db, project_id)

    _audit(db, caller, "set_project_description", f"project_id={project_id}",
           {"project_desc"}, source=source)
    return project


def clear_project_description(
    db: Session, caller: AuthenticatedUser, project_id: int, view_mode: ViewMode
) -> Project:
    """The 'remove' of the CRUD set. Separate function rather than a null
    through set_project_description so the audit trail distinguishes "IT
    wrote an empty description" from "IT removed the description"."""
    _authorize(caller.role, view_mode, {"project_desc"})

    project = db.get(Project, project_id)
    if project is None:
        raise WriteTargetMissing(str(project_id))

    project.description = None
    db.commit()
    db.refresh(project)
    _reindex_project_members(db, project_id)
    _audit(db, caller, "clear_project_description", f"project_id={project_id}", {"project_desc"})
    return project


def _reindex_project_members(db: Session, project_id: int) -> None:
    member_ids = [
        row.employee_id for row in
        db.query(EmployeeProject).filter(EmployeeProject.project_id == project_id).all()
    ]
    for employee_id in member_ids:
        reindex_employee_id(db, employee_id)
