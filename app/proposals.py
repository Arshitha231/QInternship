"""Review workflow for AI-extracted changes. IT only, work mode only.

Two stages, two staging tables. app/doc_extraction.py creates both
doc_subject_matches (who a document mentions) and proposed_changes (what it
says about them) as `pending`/`unresolved` — nothing there commits, and
nothing here runs without a reviewer's explicit action:

  1. resolve_subject() answers "who is this person" — confirms one of the
     ranked candidates, or marks the row a new-hire candidate for HR. Until
     this runs, every proposed_changes row under that subject has
     employee_id = NULL and is invisible to list_proposals.
  2. accept() / edit() answer "is this specific field claim true" — the
     only two functions in this module that write to a real table
     (EmployeeSkill, EmployeeProject). reject() and reassign() never do.

The invisible-until-accepted guarantee is structural rather than enforced by
a filter: a pending row lives in `proposed_changes`, a table that
build_profile_text has never heard of and that no retrieval path queries. It
becomes searchable at exactly the moment accept()/edit() writes it into a
real table and re-indexes — not before, and not by anyone forgetting a
WHERE clause.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog,
    DocSubjectMatch,
    Employee,
    EmployeeProject,
    EmployeeSkill,
    Project,
    ProposedChange,
    Skill,
    UploadedDoc,
)
from app.models.enums import (
    ChangeType,
    ProjectClassification,
    ProjectType,
    ProposedChangeStatus,
    ResolutionStatus,
    SkillCategory,
    SkillLevel,
    SkillSource,
)
from app.permissions import ViewMode, can_edit
from app.search_reindex import reindex_employee_id

# Everything committed out of this module carries this provenance in the
# audit trail, so "which of these edits came from a document?" is one query.
AI_EXTRACTION_SOURCE = "ai_extraction"

# Which EDITABLE field name gates committing each change_type — see
# app/permissions.py's ("it", "work") entry, which lists these three
# alongside project_desc as IT's write surface. accept()/edit() check this
# per row rather than the module hardcoding "role == it" the way the first
# version of this file did; that was a real gap (accept() could write to
# EmployeeSkill without EDITABLE ever being consulted) and this closes it.
FIELD_FOR_CHANGE_TYPE: dict[ChangeType, str] = {
    ChangeType.skill: "skills",
    ChangeType.contribution: "contribution",
    ChangeType.project_entry: "project_entry",
}


class ReviewDenied(Exception):
    """Caller is not it, or not in work mode, or (for accept/edit) the
    resolved EDITABLE table doesn't grant this change_type."""


class ProposalNotFound(Exception):
    pass


class SubjectNotFound(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class DocumentAlreadyFinalized(Exception):
    """content_scrubbed_at is already set — finalize_document is a one-shot
    action, not idempotent: a second call would silently reject nothing
    (every target row is already terminal) and re-stamp a timestamp that
    should mark the one real event, not the latest double-click."""


class ProposalNotActionable(Exception):
    """Already reviewed, or missing something accept()/edit() requires."""


def _authorize(caller: AuthenticatedUser, view_mode: ViewMode) -> None:
    """Reviewing is an IT action, in work mode. This is the SCREEN-level
    gate — can this caller see/act on the review queue at all — checked
    here rather than in the route for the same reason app/writes.py does
    it: this is the enforcement point, and a rule living only in a FastAPI
    decorator applies only to callers who came through FastAPI.

    accept()/edit() layer an additional, FIELD-level check on top of this
    one (see _authorize_commit below) — this function alone is not enough
    to authorize an actual write.
    """
    if caller.role != "it" or view_mode != "work":
        raise ReviewDenied(
            f"Reviewing proposed changes is an IT action in work mode "
            f"(role={caller.role}, view_mode={view_mode})"
        )


def _authorize_commit(caller: AuthenticatedUser, view_mode: ViewMode, change_type: ChangeType) -> None:
    field = FIELD_FOR_CHANGE_TYPE[change_type]
    if not can_edit(caller.role, view_mode, field):
        raise ReviewDenied(
            f"role '{caller.role}' in {view_mode} mode may not commit {change_type.value} changes "
            f"(requires EDITABLE[('{caller.role}', '{view_mode}')] to include '{field}')"
        )


def _audit(
    db: Session, caller: AuthenticatedUser, action: str, query_text: str,
    fields: list[str], source: str | None = None,
) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text,
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


def _load_subject(db: Session, subject_id: int) -> DocSubjectMatch:
    subject = db.get(DocSubjectMatch, subject_id)
    if subject is None:
        raise SubjectNotFound(str(subject_id))
    return subject


# ---------------------------------------------------------------------------
# GET /doc_subject_matches?doc_id= — the required first screen.
# ---------------------------------------------------------------------------

def list_subjects(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    doc_id: int | None = None, status: str | None = None,
) -> list[dict]:
    """Every person a document mentions, with ranked candidates for each.
    Unresolved rows sort first — they're the ones blocking review, and
    burying them under already-resolved subjects is how they get missed.
    """
    _authorize(caller, view_mode)

    query = db.query(DocSubjectMatch)
    if doc_id is not None:
        query = query.filter(DocSubjectMatch.source_doc_id == doc_id)
    if status is not None:
        query = query.filter(DocSubjectMatch.resolution_status == ResolutionStatus(status))
    rows = query.order_by(DocSubjectMatch.id).all()

    out: list[dict] = []
    for row in rows:
        candidates = json.loads(row.candidate_employee_ids)
        pending_count = (
            db.query(ProposedChange)
            .filter(ProposedChange.subject_match_id == row.id,
                    ProposedChange.status == ProposedChangeStatus.pending)
            .count()
        )
        out.append({
            "id": row.id,
            "source_doc_id": row.source_doc_id,
            "extracted_name": row.extracted_name,
            "extracted_signals": json.loads(row.extracted_signals),
            "candidates": candidates,
            "resolution_status": row.resolution_status.value,
            "resolved_employee_id": row.resolved_employee_id,
            "resolved_by": row.resolved_by,
            "resolved_at": row.resolved_at,
            "proposed_change_count": pending_count,
        })

    out.sort(key=lambda s: (s["resolution_status"] != ResolutionStatus.unresolved.value, s["id"]))
    return out


# ---------------------------------------------------------------------------
# POST /doc_subject_matches/{id}/resolve — confirm who a mentioned person is.
# ---------------------------------------------------------------------------

def resolve_subject(
    db: Session, caller: AuthenticatedUser, subject_id: int, view_mode: ViewMode,
    employee_id: str | None = None, new_hire: bool = False,
) -> DocSubjectMatch:
    """Confirm a candidate, or flag this person as a new-hire candidate for
    HR to create first. Not restricted to unresolved subjects — resolving a
    new_hire_candidate row again once HR has created the employee, or
    correcting an already-resolved one, both go through this same function,
    idempotently.

    Cascades employee_id onto every STILL-NULL proposed_changes row under
    this subject (a row an earlier /reassign already pointed elsewhere is
    left alone — that reassignment was itself an explicit human decision
    and this must not silently override it), and computes each one's
    original_value now, which is the earliest point the "current real
    value" this diffs against can actually be known.
    """
    _authorize(caller, view_mode)
    subject = _load_subject(db, subject_id)

    if new_hire and employee_id:
        raise ValueError("resolve takes employee_id or new_hire, not both")
    if not new_hire and not employee_id:
        raise ValueError("resolve requires employee_id or new_hire=true")

    if new_hire:
        subject.resolution_status = ResolutionStatus.new_hire_candidate
        subject.resolved_employee_id = None
    else:
        employee = db.get(Employee, employee_id)
        if employee is None or not employee.is_active:
            raise ProposalNotActionable(f"no active employee {employee_id}")
        subject.resolution_status = ResolutionStatus.resolved
        subject.resolved_employee_id = employee_id

        pending_children = (
            db.query(ProposedChange)
            .filter(ProposedChange.subject_match_id == subject.id,
                    ProposedChange.employee_id.is_(None))
            .all()
        )
        for change in pending_children:
            change.employee_id = employee_id
            change.original_value = _compute_original_value(
                db, employee_id, change.change_type, json.loads(change.proposed_value)
            )

    subject.resolved_by = caller.id
    subject.resolved_at = datetime.now()
    db.commit()
    db.refresh(subject)

    _audit(
        db, caller, "resolve_doc_subject_match",
        f"subject_id={subject.id} extracted_name={subject.extracted_name!r}",
        ["resolved_employee_id"] if not new_hire else ["new_hire_candidate"],
    )
    return subject


def _compute_original_value(
    db: Session, employee_id: str, change_type: ChangeType, proposed_value: dict
) -> str | None:
    """The current real value this proposal would overwrite, for the review
    screen's diff — null if the field doesn't exist yet (a skill they don't
    hold, a project they're not on), which the diff renders as "new"."""
    if change_type is ChangeType.skill:
        skill_name = (proposed_value.get("skill") or "").strip()
        if not skill_name:
            return None
        skill = db.query(Skill).filter(Skill.name.ilike(skill_name)).first()
        if skill is None:
            return None
        skill_id = skill.canonical_id or skill.id
        existing = (
            db.query(EmployeeSkill)
            .filter(EmployeeSkill.employee_id == employee_id, EmployeeSkill.skill_id == skill_id)
            .first()
        )
        if existing is None:
            return None
        return json.dumps({"skill": skill_name, "level": existing.level.value, "source": existing.source.value})

    project_name = (proposed_value.get("project") or "").strip()
    if not project_name:
        return None
    project = db.query(Project).filter(Project.name.ilike(project_name)).first()
    if project is None:
        return None
    membership = (
        db.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id, EmployeeProject.project_id == project.id)
        .first()
    )
    if membership is None:
        return None

    if change_type is ChangeType.contribution:
        if not membership.contribution:
            return None
        return json.dumps({"project": project_name, "contribution": membership.contribution})

    return json.dumps({
        "project": project_name, "role": membership.role,
        "start_date": membership.start_date.isoformat() if membership.start_date else None,
        "end_date": membership.end_date.isoformat() if membership.end_date else None,
    })


# ---------------------------------------------------------------------------
# GET /proposed_changes?doc_id= — grouped by employee. Only resolved
# subjects' rows ever appear here, by construction (the query itself filters
# on employee_id, which extraction never sets).
# ---------------------------------------------------------------------------

def list_proposals(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    doc_id: int | None = None, employee_id: str | None = None, status: str | None = None,
) -> list[dict]:
    _authorize(caller, view_mode)

    query = db.query(ProposedChange).filter(ProposedChange.employee_id.isnot(None))
    if doc_id is not None:
        query = query.filter(ProposedChange.source_doc_id == doc_id)
    if employee_id is not None:
        query = query.filter(ProposedChange.employee_id == employee_id)
    if status is not None:
        query = query.filter(ProposedChange.status == ProposedChangeStatus(status))
    rows = query.order_by(ProposedChange.confidence.desc(), ProposedChange.id).all()

    groups: dict[str, dict] = {}
    for row in rows:
        key = row.employee_id
        if key not in groups:
            employee = db.get(Employee, key)
            groups[key] = {
                "employee_id": key,
                "employee_name": employee.full_name if employee else None,
                "changes": [],
            }
        groups[key]["changes"].append({
            "id": row.id,
            "change_type": row.change_type.value,
            "status": row.status.value,
            "confidence": row.confidence,
            "proposed_value": json.loads(row.proposed_value),
            "original_value": json.loads(row.original_value) if row.original_value else None,
            "source_doc_id": row.source_doc_id,
            "subject_match_id": row.subject_match_id,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at,
        })

    return sorted(groups.values(), key=lambda g: g["employee_name"] or "")


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/accept — commit the AI's value as-is.
# ---------------------------------------------------------------------------

def accept(
    db: Session, caller: AuthenticatedUser, proposal_id: int, view_mode: ViewMode
) -> ProposedChange:
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    if proposal.employee_id is None:
        # An unresolved proposal has nowhere to go. Resolve its subject (or
        # reassign this one row) first — refusing here is what stops a
        # reviewer clicking accept on a row whose subject is still "Priya".
        raise ProposalNotActionable(
            f"proposed change {proposal_id} has no employee — resolve its doc_subject_match "
            f"(or reassign this change) before accepting"
        )
    _authorize_commit(caller, view_mode, proposal.change_type)

    employee = db.get(Employee, proposal.employee_id)
    if employee is None or not employee.is_active:
        raise ProposalNotActionable(f"employee {proposal.employee_id} is not an active record")

    value = json.loads(proposal.proposed_value)
    effect = _commit(db, proposal.employee_id, proposal.change_type, value)

    proposal.status = ProposedChangeStatus.accepted
    proposal.reviewed_by = caller.id
    proposal.reviewed_at = datetime.now()
    proposal.undo_state = json.dumps(effect)
    db.commit()
    db.refresh(proposal)

    # Rule 6 — and the moment this content becomes searchable at all.
    reindex_employee_id(db, proposal.employee_id)

    _audit(db, caller, "accept_proposed_change", f"proposed_change_id={proposal.id}",
           [proposal.change_type.value], source=AI_EXTRACTION_SOURCE)
    return proposal


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/edit — commit a human-edited value, not the
# raw AI output.
# ---------------------------------------------------------------------------

def edit(
    db: Session, caller: AuthenticatedUser, proposal_id: int, edited_value: dict,
    view_mode: ViewMode,
) -> ProposedChange:
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    if proposal.employee_id is None:
        raise ProposalNotActionable(
            f"proposed change {proposal_id} has no employee — resolve its doc_subject_match "
            f"(or reassign this change) before editing"
        )
    _authorize_commit(caller, view_mode, proposal.change_type)

    employee = db.get(Employee, proposal.employee_id)
    if employee is None or not employee.is_active:
        raise ProposalNotActionable(f"employee {proposal.employee_id} is not an active record")

    effect = _commit(db, proposal.employee_id, proposal.change_type, edited_value)

    # The edited value replaces proposed_value permanently — the record of
    # what was committed is the edited version, not a copy the AI proposed
    # and a human overrode invisibly. What the AI originally said is still
    # recoverable from the audit trail's history if ever needed.
    proposal.proposed_value = json.dumps(edited_value)
    proposal.undo_state = json.dumps(effect)
    proposal.status = ProposedChangeStatus.edited
    proposal.reviewed_by = caller.id
    proposal.reviewed_at = datetime.now()
    db.commit()
    db.refresh(proposal)

    reindex_employee_id(db, proposal.employee_id)

    _audit(db, caller, "edit_proposed_change", f"proposed_change_id={proposal.id}",
           [proposal.change_type.value], source=AI_EXTRACTION_SOURCE)
    return proposal


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/undo — reverse an accept()/edit(), while the
# source document is still under review.
# ---------------------------------------------------------------------------

def undo(
    db: Session, caller: AuthenticatedUser, proposal_id: int, view_mode: ViewMode
) -> ProposedChange:
    """Flips an accepted/edited proposal back to pending and reverses
    exactly what its stored undo_state says was written — never re-reads
    proposed_value to figure out what to undo, since for an edited row
    that's the reviewer's typed value, not necessarily what _commit() saw
    at the time it ran (a later proposal could have moved the same field
    again since).

    Only reachable while the source document hasn't been finalized yet —
    the same gate app.proposals.correct() already applies, for the mirror-
    image reason: finalize_document scrubs the document's own text and
    treats that as the review session's real close. An accepted change on
    a finalized document is meant to be done, not a draft indefinitely
    reopenable from a review screen that no longer even shows that
    document (see app/components/ReviewPage.tsx — a finalized document's
    rows aren't rendered as interactive ChangeRows at all past that point).

    A row accepted/edited before this column existed carries no
    undo_state — refused outright rather than guessed at, the same
    never-re-derive-it principle undo_state exists to satisfy in the
    first place.
    """
    _authorize(caller, view_mode)
    proposal = db.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ProposalNotFound(str(proposal_id))
    if proposal.status not in (ProposedChangeStatus.accepted, ProposedChangeStatus.edited):
        raise ProposalNotActionable(
            f"proposed change {proposal_id} is {proposal.status.value} — only an accepted "
            f"or edited change can be undone"
        )
    _authorize_commit(caller, view_mode, proposal.change_type)

    doc = db.get(UploadedDoc, proposal.source_doc_id)
    if doc is not None and doc.content_scrubbed_at is not None:
        raise ProposalNotActionable(
            f"document {doc.id} was finalized at {doc.content_scrubbed_at} — its accepted "
            f"changes can no longer be undone from here"
        )
    if not proposal.undo_state:
        raise ProposalNotActionable(
            f"proposed change {proposal_id} has no recorded undo state to reverse "
            f"(it predates this feature)"
        )

    effect = json.loads(proposal.undo_state)
    _reverse_commit(db, proposal.employee_id, effect)

    proposal.status = ProposedChangeStatus.pending
    proposal.reviewed_by = None
    proposal.reviewed_at = None
    proposal.undo_state = None
    db.commit()
    db.refresh(proposal)

    reindex_employee_id(db, proposal.employee_id)

    _audit(db, caller, "undo_proposed_change", f"proposed_change_id={proposal.id}", [proposal.change_type.value])
    return proposal


def _get_or_create_project(db: Session, employee_id: str, project_name: str) -> Project:
    """Same lookup-or-create the first version of this module used: find by
    name (case-insensitive), create as `internal` if none exists. New
    projects are never created `confidential` — a classification decides
    who may see something, and inferring "this is secret" from a document
    that merely didn't say otherwise is exactly the wrong direction to
    guess in.
    """
    project = db.query(Project).filter(Project.name.ilike(project_name)).first()
    if project is None:
        employee = db.get(Employee, employee_id)
        project = Project(
            name=project_name, type=ProjectType.project, description=None,
            owning_unit_id=employee.org_unit_id, owner_id=employee_id,
            classification=ProjectClassification.internal, is_client_engagement=False,
        )
        db.add(project)
        db.flush()
    return project


def _get_or_create_membership(db: Session, employee_id: str, project: Project) -> EmployeeProject:
    """contribution and project_entry are independently acceptable — a
    reviewer might accept one before the other, in either order — so both
    converge on the same EmployeeProject row via this shared lookup rather
    than either one assuming it runs first. Whichever commits first creates
    it with sensible defaults; the other only overwrites the specific
    field(s) it's proposing.
    """
    existing = (
        db.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id, EmployeeProject.project_id == project.id)
        .first()
    )
    if existing is not None:
        return existing
    membership = EmployeeProject(
        employee_id=employee_id, project_id=project.id,
        role="Contributor", contribution=None,
        start_date=date.today(), end_date=None,
    )
    db.add(membership)
    db.flush()
    return membership


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _commit_skill(db: Session, employee_id: str, value: dict) -> dict:
    """Skills land as `self`-sourced at `Learning`, never higher.

    A document saying somebody used Terraform is evidence they touched it,
    not that they're an expert in it — and `source` is the confidence axis
    the directory already has for exactly this distinction. Anything
    stronger would let an uploaded document manufacture expertise that
    find_mentor then recommends people to.

    Returns the effect actually applied — {"kind": "skill", "created": bool,
    "skill_id": int} — for app.proposals.undo() to reverse later.
    "created" is False when the employee already held this skill (never
    downgraded, so nothing here was actually written); undo() must never
    delete a skill row it didn't create, since a duplicate proposal from a
    second document could easily target the same (employee, skill) pair
    after the first one already committed it.
    """
    skill_name = (value.get("skill") or "").strip()
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
        return {"kind": "skill", "created": False, "skill_id": skill_id}  # never downgrade a level somebody already holds

    db.add(EmployeeSkill(
        employee_id=employee_id, skill_id=skill_id,
        level=SkillLevel.learning, source=SkillSource.self_reported, verified_at=None,
    ))
    return {"kind": "skill", "created": True, "skill_id": skill_id}


def _commit_contribution(db: Session, employee_id: str, value: dict) -> dict:
    """Returns {"kind": "contribution", "project_id": int, "previous":
    str | None} — the contribution text this write overwrote (None if the
    membership had none, including a membership this same call just
    created), for undo() to restore. The membership row itself is never
    deleted on undo, even if this call is what created it: a project_entry
    proposal for the same (employee, project) may have since set the role/
    dates on that same row, and deleting it would destroy that unrelated,
    still-accepted change too. Reverting just the contribution field is
    what "remove the specific field it wrote" means for a shared row.
    """
    project_name = (value.get("project") or "").strip()
    contribution = (value.get("contribution") or "").strip()
    if not project_name or not contribution:
        raise ProposalNotActionable("proposed contribution is missing a project or text")
    project = _get_or_create_project(db, employee_id, project_name)
    membership = _get_or_create_membership(db, employee_id, project)
    previous = membership.contribution
    membership.contribution = contribution
    return {"kind": "contribution", "project_id": project.id, "previous": previous}


def _commit_project_entry(db: Session, employee_id: str, value: dict) -> dict:
    """Returns {"kind": "project_entry", "project_id": int, "previous":
    {"role", "start_date", "end_date"}} — the role/dates this write
    overwrote, for undo() to restore. Same "revert the field, never delete
    the shared row" reasoning as _commit_contribution above.
    """
    project_name = (value.get("project") or "").strip()
    if not project_name:
        raise ProposalNotActionable("proposed project entry has no project name")
    project = _get_or_create_project(db, employee_id, project_name)
    membership = _get_or_create_membership(db, employee_id, project)
    previous = {
        "role": membership.role,
        "start_date": membership.start_date.isoformat() if membership.start_date else None,
        "end_date": membership.end_date.isoformat() if membership.end_date else None,
    }
    if value.get("role"):
        membership.role = str(value["role"])[:150]
    start = _parse_date(value.get("start_date"))
    if start is not None:
        membership.start_date = start
    end = _parse_date(value.get("end_date"))
    if end is not None:
        membership.end_date = end
    return {"kind": "project_entry", "project_id": project.id, "previous": previous}


def _commit(db: Session, employee_id: str, change_type: ChangeType, value: dict) -> dict:
    if change_type is ChangeType.skill:
        return _commit_skill(db, employee_id, value)
    elif change_type is ChangeType.contribution:
        return _commit_contribution(db, employee_id, value)
    else:
        return _commit_project_entry(db, employee_id, value)


def _reverse_commit(db: Session, employee_id: str, effect: dict) -> None:
    """Reverses exactly what _commit() recorded, from its own returned
    effect dict — see undo() for why this never re-derives what to undo
    from the proposal's current proposed_value instead.

    Never deletes an EmployeeProject row (see _commit_contribution's and
    _commit_project_entry's own docstrings for why a shared membership row
    can't be safely deleted just because THIS proposal happened to create
    it) — only an EmployeeSkill row, and only when this exact proposal is
    the one that created it.
    """
    kind = effect.get("kind")
    if kind == "skill":
        if effect.get("created"):
            row = (
                db.query(EmployeeSkill)
                .filter(EmployeeSkill.employee_id == employee_id, EmployeeSkill.skill_id == effect["skill_id"])
                .first()
            )
            if row is not None:
                db.delete(row)
        return  # created=False: this proposal never wrote a row, nothing to reverse
    if kind == "contribution":
        membership = (
            db.query(EmployeeProject)
            .filter(EmployeeProject.employee_id == employee_id, EmployeeProject.project_id == effect["project_id"])
            .first()
        )
        if membership is not None:
            membership.contribution = effect.get("previous")
        return
    if kind == "project_entry":
        membership = (
            db.query(EmployeeProject)
            .filter(EmployeeProject.employee_id == employee_id, EmployeeProject.project_id == effect["project_id"])
            .first()
        )
        if membership is not None:
            previous = effect.get("previous") or {}
            membership.role = previous.get("role") or membership.role
            membership.start_date = _parse_date(previous.get("start_date"))
            membership.end_date = _parse_date(previous.get("end_date"))
        return
    # Deliberately not a silent no-op — same "raise on an obligation shape
    # you don't recognize" discipline app/search_client.py's
    # _apply_required_filter uses: an undo_state kind this function doesn't
    # know how to reverse should fail loudly, not pretend to have undone it.
    raise ProposalNotActionable(f"don't know how to undo effect kind {kind!r}")


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/reassign — re-point one row to a different
# employee, independent of (and possibly overriding) its subject_match.
# ---------------------------------------------------------------------------

def reassign(
    db: Session, caller: AuthenticatedUser, proposal_id: int, employee_id: str,
    view_mode: ViewMode,
) -> ProposedChange:
    """Fine-grained override: reassigns THIS field-level proposal, whether
    or not its doc_subject_match has been resolved yet, and independent of
    whatever that subject resolves to. "For resumes/docs where the wrong or
    no employee was matched" per spec — the subject-level resolve is the
    common path, this is the escape hatch for a single row that needs to
    point somewhere else.

    Stays pending: reassigning says who this is about, not that the claim
    is true. Accepting/editing is a separate, deliberate second action.
    """
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    employee = db.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise ProposalNotActionable(f"no active employee {employee_id}")

    proposal.employee_id = employee_id
    proposal.original_value = _compute_original_value(
        db, employee_id, proposal.change_type, json.loads(proposal.proposed_value)
    )
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "reassign_proposed_change", f"proposed_change_id={proposal.id}", ["employee_id"])
    return proposal


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/correct — back through the function-calling
# loop (an alternative to /edit for a harder case: ask the model to
# re-extract with a hint, rather than typing the corrected value directly).
# ---------------------------------------------------------------------------

def correct(
    db: Session, caller: AuthenticatedUser, proposal_id: int, instruction: str,
    view_mode: ViewMode,
) -> ProposedChange:
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    # correct_call re-reads doc.extracted_text to re-run extraction with the
    # reviewer's hint attached — once finalize_document has scrubbed it,
    # that text is gone (by design), so a re-extract has nothing left to
    # work from. Checked here rather than left to silently ask the model to
    # correct an empty document: /edit is still available for exactly this
    # case, since it commits the reviewer's own typed value directly.
    doc = db.get(UploadedDoc, proposal.source_doc_id)
    if doc is not None and doc.content_scrubbed_at is not None:
        raise ProposalNotActionable(
            f"document {doc.id} was finalized at {doc.content_scrubbed_at} and its content was cleared — "
            f"use /edit to set a value directly instead of re-extracting"
        )

    from app.doc_extraction import correct_call

    corrected = correct_call(db, proposal, instruction)
    proposal.proposed_value = json.dumps(corrected)
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "correct_proposed_change", f"proposed_change_id={proposal.id}",
           sorted(corrected.keys()))
    return proposal


# ---------------------------------------------------------------------------
# POST /proposed_changes/{id}/reject — discard, no change to the real profile.
# ---------------------------------------------------------------------------

def reject(
    db: Session, caller: AuthenticatedUser, proposal_id: int, view_mode: ViewMode
) -> ProposedChange:
    """Reject a proposal. Kept, not deleted — a rejected proposal is the
    most informative row in the table when someone asks why extraction
    quality is poor. IT's fallback is the manual edit endpoints
    (PATCH /employees/{id}, PUT /projects/{id}/description)."""
    _authorize(caller, view_mode)
    proposal = _load_pending(db, proposal_id)

    proposal.status = ProposedChangeStatus.rejected
    proposal.reviewed_by = caller.id
    proposal.reviewed_at = datetime.now()
    db.commit()
    db.refresh(proposal)

    _audit(db, caller, "reject_proposed_change", f"proposed_change_id={proposal.id}", [])
    return proposal


# ---------------------------------------------------------------------------
# Bulk accept/reject — a loop over the exact same per-row functions above.
# No separate commit logic exists anywhere for the bulk case; a batch of
# one is indistinguishable from calling accept()/reject() directly N times.
# ---------------------------------------------------------------------------

def _bulk_targets(
    db: Session, ids: list[int] | None, doc_id: int | None, employee_id: str | None,
) -> list[int]:
    if ids:
        return list(ids)
    query = db.query(ProposedChange.id).filter(ProposedChange.status == ProposedChangeStatus.pending)
    if doc_id is not None:
        query = query.filter(ProposedChange.source_doc_id == doc_id)
    if employee_id is not None:
        query = query.filter(ProposedChange.employee_id == employee_id)
    if doc_id is None and employee_id is None and not ids:
        raise ValueError("bulk action requires ids, doc_id, or employee_id")
    return [row[0] for row in query.all()]


def bulk_accept(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    ids: list[int] | None = None, doc_id: int | None = None, employee_id: str | None = None,
) -> list[dict]:
    targets = _bulk_targets(db, ids, doc_id, employee_id)
    results: list[dict] = []
    for proposal_id in targets:
        try:
            proposal = accept(db, caller, proposal_id, view_mode)
            results.append({"id": proposal_id, "ok": True, "status": proposal.status.value})
        except (ProposalNotFound, ProposalNotActionable, ReviewDenied) as exc:
            results.append({"id": proposal_id, "ok": False, "error": str(exc)})
    return results


def bulk_reject(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    ids: list[int] | None = None, doc_id: int | None = None, employee_id: str | None = None,
) -> list[dict]:
    targets = _bulk_targets(db, ids, doc_id, employee_id)
    results: list[dict] = []
    for proposal_id in targets:
        try:
            proposal = reject(db, caller, proposal_id, view_mode)
            results.append({"id": proposal_id, "ok": True, "status": proposal.status.value})
        except (ProposalNotFound, ProposalNotActionable, ReviewDenied) as exc:
            results.append({"id": proposal_id, "ok": False, "error": str(exc)})
    return results


# ---------------------------------------------------------------------------
# GET /docs — every uploaded document, with the live counts a review screen
# needs to tell "still awaiting a decision" apart from "finalized" without a
# second round trip per document.
# ---------------------------------------------------------------------------

def _serialize_document(db: Session, doc: UploadedDoc) -> dict:
    pending_count = (
        db.query(ProposedChange)
        .filter(
            ProposedChange.source_doc_id == doc.id,
            ProposedChange.status == ProposedChangeStatus.pending,
            ProposedChange.employee_id.isnot(None),  # only what's actually actionable right now
        )
        .count()
    )
    unresolved_subject_count = (
        db.query(DocSubjectMatch)
        .filter(
            DocSubjectMatch.source_doc_id == doc.id,
            DocSubjectMatch.resolution_status == ResolutionStatus.unresolved,
        )
        .count()
    )
    return {
        "id": doc.id,
        "filename": doc.filename,
        "uploaded_by": doc.uploaded_by,
        "uploaded_at": doc.uploaded_at,
        "content_scrubbed_at": doc.content_scrubbed_at,
        "pending_count": pending_count,
        "unresolved_subject_count": unresolved_subject_count,
    }


def list_documents(db: Session, caller: AuthenticatedUser, view_mode: ViewMode) -> list[dict]:
    _authorize(caller, view_mode)
    docs = db.query(UploadedDoc).order_by(UploadedDoc.uploaded_at.desc()).all()
    return [_serialize_document(db, doc) for doc in docs]


def _load_document(db: Session, doc_id: int) -> UploadedDoc:
    doc = db.get(UploadedDoc, doc_id)
    if doc is None:
        raise DocumentNotFound(str(doc_id))
    return doc


# ---------------------------------------------------------------------------
# POST /docs/{id}/finalize — the "Update" action. One decisive pass over
# everything currently actionable for this document, then the document's
# own content is gone.
# ---------------------------------------------------------------------------

def finalize_document(
    db: Session, caller: AuthenticatedUser, doc_id: int, view_mode: ViewMode,
    accept_ids: list[int],
) -> dict:
    """Accept every row in accept_ids; reject every OTHER still-pending,
    employee-resolved row for this document — an unchecked suggestion is a
    rejected one, not a skipped one, same "shopping cart" contract the
    frontend presents. A subject that's still unresolved (no employee_id
    yet) is untouched either way: its proposed_changes rows stay pending
    and remain decidable later, independently of what happens to the
    document's content here — resolving it later still works, since
    accept()/edit()/reject() never read source_doc.extracted_text (only
    /correct does, and /correct on a scrubbed document is refused, see
    app.doc_extraction.correct_call).

    Then the document's extracted_text is wiped and content_scrubbed_at is
    stamped — once. Calling this again on an already-finalized document
    raises DocumentAlreadyFinalized rather than quietly doing nothing.

    Per-row failures are collected exactly like bulk_accept/bulk_reject
    already do (a stale row — e.g. an employee who went inactive mid-
    review — is reported, not raised) and never block the scrub: a single
    bad row shouldn't leave a document's content stuck un-scrubbable
    forever. An id in accept_ids that isn't actually one of this
    document's pending, employee-resolved rows (wrong doc, already
    decided, subject still unresolved) is silently ignored — the only ids
    a well-behaved reviewer UI can ever send are exactly the ones this
    function would already process as targets.
    """
    _authorize(caller, view_mode)
    doc = _load_document(db, doc_id)
    if doc.content_scrubbed_at is not None:
        raise DocumentAlreadyFinalized(f"document {doc_id} was already finalized at {doc.content_scrubbed_at}")

    accept_set = set(accept_ids)
    target_ids = [
        row[0] for row in
        db.query(ProposedChange.id)
        .filter(
            ProposedChange.source_doc_id == doc_id,
            ProposedChange.status == ProposedChangeStatus.pending,
            ProposedChange.employee_id.isnot(None),
        )
        .all()
    ]

    results: list[dict] = []
    for proposal_id in target_ids:
        try:
            if proposal_id in accept_set:
                proposal = accept(db, caller, proposal_id, view_mode)
            else:
                proposal = reject(db, caller, proposal_id, view_mode)
            results.append({"id": proposal_id, "ok": True, "status": proposal.status.value})
        except (ProposalNotFound, ProposalNotActionable, ReviewDenied) as exc:
            results.append({"id": proposal_id, "ok": False, "error": str(exc)})

    doc.extracted_text = ""
    doc.content_scrubbed_at = datetime.now()
    db.commit()
    db.refresh(doc)

    _audit(db, caller, "finalize_document", f"doc_id={doc_id}", ["content_scrubbed_at"])
    return {"doc_id": doc_id, "results": results, "content_scrubbed_at": doc.content_scrubbed_at}
