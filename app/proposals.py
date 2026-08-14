"""Review workflow for AI-extracted changes. IT only, work mode only.

This is the *only* module that moves extracted content into EmployeeProject
and EmployeeSkill. app/doc_extraction.py stages everything as `pending`;
nothing there commits, and nothing here runs without a reviewer's explicit
action.

The invisible-until-accepted guarantee is structural rather than enforced by
a filter: a pending row lives in `proposed_changes`, a table that
build_profile_text has never heard of and that no retrieval path queries. It
becomes searchable at exactly the moment accept() writes it into a real
table and re-indexes — not before, and not by anyone forgetting a WHERE
clause.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog,
    Employee,
    EmployeeProject,
    EmployeeSkill,
    Project,
    ProposedChange,
    Skill,
)
from app.models.enums import (
    ProjectClassification,
    ProjectType,
    ProposedChangeStatus,
    ProposedFieldType,
    SkillCategory,
    SkillLevel,
    SkillSource,
)
from app.permissions import ViewMode
from app.search_reindex import reindex_employee_id

# Everything committed out of this module carries this provenance in the
# audit trail, so "which of these edits came from a document?" is one query.
AI_EXTRACTION_SOURCE = "ai_extraction"


class ReviewDenied(Exception):
    """Caller is not it, or not in work mode."""


class ProposalNotFound(Exception):
    pass


class ProposalNotActionable(Exception):
    """Already reviewed, or missing something accept() requires."""


def _authorize(caller: AuthenticatedUser, view_mode: ViewMode) -> None:
    """Reviewing is an IT action, in work mode.

    Checked here rather than in the route for the same reason app/writes.py
    does it: this is the enforcement point, and a rule living only in a
    FastAPI decorator applies only to callers who came through FastAPI.
    """
    if caller.role != "it" or view_mode != "work":
        raise ReviewDenied(
            f"Reviewing proposed changes is an IT action in work mode "
            f"(role={caller.role}, view_mode={view_mode})"
        )


def _audit(
    db: Session, caller: AuthenticatedUser, action: str, proposal: ProposedChange,
    fields: list[str], source: str | None = None,
) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action,
        query_text=f"proposed_change_id={proposal.id} employee_id={proposal.employee_id}",
        result_count=1, fields_returned=json.dumps(sorted(fields)),
        source=source, timestamp=datetime.now(),
    ))
    db.commit()


def _load_pending(db: Session, proposal_id: int) -> ProposedChange:
    proposal = db.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ProposalNotFound(str(proposal_id))
    if proposal.status is not ProposedChangeStatus.pending:
        raise ProposalNotActionable(
            f"proposed change {proposal_id} is already {proposal.status.value}"
        )
    return proposal


# ---------------------------------------------------------------------------
# GET /proposed_changes?doc_id= — grouped by employee.
# ---------------------------------------------------------------------------

def list_proposals(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    doc_id: int | None = None, status: str | None = None,
) -> list[dict]:
    """Review queue, grouped by employee.

    Unresolved rows (employee_id IS NULL) group under a single null-keyed
    bucket and sort first — they're the ones that need a human most, and
    burying them under thirty resolved groups is how they get accepted
    unread.
    """
    _authorize(caller, view_mode)

    query = db.query(ProposedChange)
    if doc_id is not None:
        query = query.filter(ProposedChange.source_doc_id == doc_id)
    if status is not None:
        query = query.filter(ProposedChange.status == ProposedChangeStatus(status))
    rows = query.order_by(ProposedChange.confidence.desc(), ProposedChange.id).all()

    groups: dict[str | None, dict] = {}
    for row in rows:
        key = row.employee_id
        if key not in groups:
            employee = db.get(Employee, key) if key else None
            groups[key] = {
                "employee_id": key,
                "employee_name": employee.full_name if employee else None,
                "unresolved": key is None,
                "changes": [],
            }
        groups[key]["changes"].append({
            "id": row.id,
            "field_type": row.field_type.value,
            "status": row.status.value,
            "confidence": row.confidence,
            "proposed_content": json.loads(row.proposed_content),
            "raw_extraction": json.loads(row.raw_extraction),
            "source_doc_id": row.source_doc_id,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at,
        })

    return sorted(groups.values(), key=lambda g: (not g["unresolved"], g["employee_name"] or ""))


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/accept — the one path that writes real rows.
# ---------------------------------------------------------------------------

def accept(
    db: Session, caller: AuthenticatedUser, proposal_id: int, view_mode: ViewMode
) -> ProposedChange:
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    if proposal.employee_id is None:
        # An unresolved proposal has nowhere to go. Reassign it first —
        # that's what /reassign is for, and refusing here is what stops a
        # reviewer clicking accept on a row whose subject is "Priya" and
        # producing a membership attached to nobody.
        raise ProposalNotActionable(
            f"proposed change {proposal_id} has no employee — reassign it before accepting"
        )

    employee = db.get(Employee, proposal.employee_id)
    if employee is None or not employee.is_active:
        raise ProposalNotActionable(f"employee {proposal.employee_id} is not an active record")

    content = json.loads(proposal.proposed_content)
    if proposal.field_type is ProposedFieldType.project:
        _commit_project(db, proposal.employee_id, content)
    else:
        _commit_skill(db, proposal.employee_id, content)

    # `edited` rather than `accepted` when a human changed the content —
    # both are live, and the distinction is the only measure of how good
    # the extraction actually is. See ProposedChangeStatus.
    proposal.status = (
        ProposedChangeStatus.edited if content.get("_edited") else ProposedChangeStatus.accepted
    )
    proposal.reviewed_by = caller.id
    proposal.reviewed_at = datetime.now()
    db.commit()
    db.refresh(proposal)

    # Rule 6 — and the moment this content becomes searchable at all.
    reindex_employee_id(db, proposal.employee_id)

    _audit(db, caller, "accept_proposed_change", proposal,
           [proposal.field_type.value], source=AI_EXTRACTION_SOURCE)
    return proposal


def _commit_project(db: Session, employee_id: str, content: dict) -> None:
    """Attach the person to the project, creating the project if the
    document names one the directory doesn't have.

    New projects are created as `internal`, never `confidential`: a
    classification decides who may see something, and inferring "this is
    secret" from a status document that merely didn't say otherwise is
    exactly the wrong direction to guess in. `internal` is the ordinary
    default the seeded data uses.
    """
    project_name = (content.get("project") or "").strip()
    if not project_name:
        raise ProposalNotActionable("proposed project has no name")

    project = (
        db.query(Project).filter(Project.name.ilike(project_name)).first()
    )
    if project is None:
        employee = db.get(Employee, employee_id)
        project = Project(
            name=project_name, type=ProjectType.project, description=None,
            owning_unit_id=employee.org_unit_id, owner_id=employee_id,
            classification=ProjectClassification.internal,
        )
        db.add(project)
        db.flush()

    existing = (
        db.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id,
                EmployeeProject.project_id == project.id)
        .first()
    )
    contribution = content.get("contribution") or None
    if existing is not None:
        # Accepting a second document about the same project updates the
        # contribution rather than creating a duplicate membership.
        existing.contribution = contribution or existing.contribution
        return

    db.add(EmployeeProject(
        employee_id=employee_id, project_id=project.id,
        role=(content.get("role") or "Contributor")[:150],
        contribution=contribution,
        start_date=_parse_start(content) or date.today(), end_date=None,
    ))


def _parse_start(content: dict) -> date | None:
    raw = content.get("start_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _commit_skill(db: Session, employee_id: str, content: dict) -> None:
    """Skills land as `self`-sourced at `Learning`, never higher.

    A document saying somebody used Terraform is evidence they touched it,
    not that they're an expert in it — and `source` is the confidence axis
    the directory already has for exactly this distinction. Anything
    stronger would let an uploaded status report manufacture expertise that
    find_mentor then recommends people to.
    """
    skill_name = (content.get("skill") or "").strip()
    if not skill_name:
        raise ProposalNotActionable("proposed skill has no name")

    skill = db.query(Skill).filter(Skill.name.ilike(skill_name)).first()
    if skill is None:
        skill = Skill(name=skill_name, category=SkillCategory.technical, canonical_id=None)
        db.add(skill)
        db.flush()

    # Follow an alias to its canonical skill, so "SRE" doesn't become a
    # second, separate holding from "Site Reliability Engineering".
    skill_id = skill.canonical_id or skill.id

    existing = (
        db.query(EmployeeSkill)
        .filter(EmployeeSkill.employee_id == employee_id, EmployeeSkill.skill_id == skill_id)
        .first()
    )
    if existing is not None:
        return  # never downgrade a level somebody already holds

    db.add(EmployeeSkill(
        employee_id=employee_id, skill_id=skill_id,
        level=SkillLevel.learning, source=SkillSource.self_reported, verified_at=None,
    ))


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/reassign — re-resolve to a different employee.
# ---------------------------------------------------------------------------

def reassign(
    db: Session, caller: AuthenticatedUser, proposal_id: int, employee_id: str,
    view_mode: ViewMode,
) -> ProposedChange:
    """Attach an unresolved (or wrongly-resolved) proposal to a person.

    Stays pending: reassigning says who this is about, not that the claim
    is true. Accepting is a separate, deliberate second action.
    """
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    employee = db.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise ProposalNotActionable(f"no active employee {employee_id}")

    proposal.employee_id = employee_id
    # raw_extraction is never rewritten — what the model read stays on the
    # record. Only the proposed content carries the human's correction.
    content = json.loads(proposal.proposed_content)
    content["_reassigned_from"] = content.get("member_name_guess")
    content["_edited"] = True
    proposal.proposed_content = json.dumps(content)
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "reassign_proposed_change", proposal, ["employee_id"])
    return proposal


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/correct — back through the function-calling loop.
# ---------------------------------------------------------------------------

def correct(
    db: Session, caller: AuthenticatedUser, proposal_id: int, instruction: str,
    view_mode: ViewMode,
) -> ProposedChange:
    """Send a reviewer's correction back through the extraction loop.

    The reviewer describes what's wrong in words; the model re-emits a
    typed propose_project_update call against the original document text
    plus that instruction. The model still never writes anything — the
    corrected call replaces proposed_content on a row that is still pending,
    and still needs an explicit accept.
    """
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    from app.doc_extraction import correct_call

    original = json.loads(proposal.proposed_content)
    corrected = correct_call(db, proposal, instruction)
    corrected["_edited"] = True
    corrected["_correction"] = instruction
    # Preserve the reassignment marker if one was already applied, so a
    # correct-after-reassign doesn't quietly drop who this is about.
    if "_reassigned_from" in original:
        corrected["_reassigned_from"] = original["_reassigned_from"]

    proposal.proposed_content = json.dumps(corrected)
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "correct_proposed_change", proposal, sorted(corrected.keys()))
    return proposal


# ---------------------------------------------------------------------------
# DELETE /proposed_changes/{id} — reject.
# ---------------------------------------------------------------------------

def reject(
    db: Session, caller: AuthenticatedUser, proposal_id: int, view_mode: ViewMode
) -> ProposedChange:
    """Reject a proposal.

    A status change, not a DELETE, despite the HTTP verb: the directory
    soft-deletes employees for the same reason, and a rejected proposal is
    the most informative row in the table when someone asks why extraction
    quality is poor. IT's fallback is the manual edit endpoint
    (PATCH /employees/{id}, PUT /projects/{id}/description).
    """
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    proposal.status = ProposedChangeStatus.rejected
    proposal.reviewed_by = caller.id
    proposal.reviewed_at = datetime.now()
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "reject_proposed_change", proposal, [])
    return proposal
