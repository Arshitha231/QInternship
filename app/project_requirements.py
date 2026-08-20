"""Requirements captured for a project: skill requirements live in
app/project_skills.py (ProjectSkillRequirement, unchanged by this module);
this one owns the qualitative half (ProjectRequirementNote) plus the reads
that combine both for the PRD assistant and its picker page.

Not added to app/project_skills.py itself -- that module's own docstring
frames its HR-or-owner write gate as the smallest version of a new
pattern, "not something to casually extend"; this is a second module that
reuses its visible_project confidentiality check rather than growing it.

Notes get a STRICTER read gate than skills. get_required_skills (existing,
untouched) stays open to anyone who can see the project -- structured org
facts, no different from any other project attribute. Requirement notes
are sentences lifted verbatim from a planning document, so reading them
uses the same HR-or-owner gate the write path does; leaving that open
would make every other HR gate in this feature control convenience rather
than access, since any employee could read the same notes by calling this
route directly.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.directory_tools import resolve_project_name
from app.models import Project, ProjectRequirementNote, ProjectSkillRequirement, UploadedDoc
from app.permissions import ViewMode, effective_role
from app.project_skills import get_required_skills, visible_project
from app.schemas import (
    AmbiguousProjectMatch,
    ProjectListItem,
    ProjectRequirementsOut,
    RequirementNoteIn,
    RequirementNoteOut,
)


class RequirementNotesNotAccessible(Exception):
    """Raised when the caller can see the project (visible_project didn't
    return None) but is neither its owner nor hr -- distinct from
    project_skills.ProjectNotWritable so a read-path denial doesn't read as
    a write-path exception in a stack trace or an except clause."""


def _owner_or_hr(caller: AuthenticatedUser, project: Project, view_mode: ViewMode) -> bool:
    return effective_role(caller.role, view_mode) == "hr" or caller.id == project.owner_id


def get_requirement_notes(
    db: Session, caller: AuthenticatedUser, project_id: int, view_mode: ViewMode = "work",
) -> list[RequirementNoteOut] | None:
    project = visible_project(db, caller, project_id, view_mode)
    if project is None:
        return None
    if not _owner_or_hr(caller, project, view_mode):
        raise RequirementNotesNotAccessible("Only the project's owner or HR can read its requirement notes")
    rows = (
        db.query(ProjectRequirementNote)
        .filter(ProjectRequirementNote.project_id == project_id)
        .order_by(ProjectRequirementNote.created_at)
        .all()
    )
    return [RequirementNoteOut(note=row.note, source_doc_id=row.source_doc_id) for row in rows]


def add_requirement_notes(
    db: Session, caller: AuthenticatedUser, project_id: int,
    notes: list[RequirementNoteIn], view_mode: ViewMode = "work",
) -> list[RequirementNoteOut] | None:
    """APPEND, not replace -- unlike set_required_skills, a second PRD
    upload confirming new notes must not erase one recorded months ago.

    Also scrubs each referenced source document: confirming a note is the
    moment its extraction has been reviewed and committed, so the PRD's
    full text no longer needs to sit in the database. UploadedDoc.
    content_scrubbed_at already exists for this (previously only ever set
    by app.proposals.finalize_document, a pipeline this flow bypasses
    entirely) -- restricted to docs that carry a project_id, so an
    ordinary POST /docs/upload row (status report, resume) is never
    touched by this path.
    """
    project = visible_project(db, caller, project_id, view_mode)
    if project is None:
        return None
    if not _owner_or_hr(caller, project, view_mode):
        raise RequirementNotesNotAccessible("Only the project's owner or HR can add requirement notes")

    source_doc_ids = set()
    for note in notes:
        db.add(ProjectRequirementNote(
            project_id=project_id, note=note.note, source_doc_id=note.source_doc_id, created_by=caller.id))
        if note.source_doc_id is not None:
            source_doc_ids.add(note.source_doc_id)

    if source_doc_ids:
        docs = db.query(UploadedDoc).filter(
            UploadedDoc.id.in_(source_doc_ids), UploadedDoc.project_id.isnot(None)).all()
        for doc in docs:
            doc.extracted_text = ""
            doc.content_scrubbed_at = datetime.now()

    db.commit()
    return get_requirement_notes(db, caller, project_id, view_mode)


def list_projects_for_picker(db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work") -> list[ProjectListItem]:
    """The PRD page's project picker. Trusts the caller already passed the
    route's own HR + work-mode gate (same convention POST /docs/upload's
    inline check trusts store_document not to re-check) -- this function
    does not re-derive that decision, it assumes HR is already true, which
    is also why it never filters by confidentiality: an hr caller's own
    visible_project exemption already covers every project.
    """
    projects = db.query(Project).order_by(Project.name).all()
    skill_project_ids = {
        row[0] for row in db.query(ProjectSkillRequirement.project_id).distinct().all()
    }
    note_project_ids = {
        row[0] for row in db.query(ProjectRequirementNote.project_id).distinct().all()
    }
    has_requirements_ids = skill_project_ids | note_project_ids
    return [
        ProjectListItem(
            id=p.id, name=p.name, type=p.type.value,
            is_client_engagement=p.is_client_engagement,
            has_requirements=p.id in has_requirements_ids,
        )
        for p in projects
    ]


def get_project_requirements_by_name(
    db: Session, caller: AuthenticatedUser, name: str, view_mode: ViewMode = "work",
) -> ProjectRequirementsOut | AmbiguousProjectMatch | None:
    """The PRD assistant's own read tool. Hard HR-only, checked first and
    before any query runs -- not HR-or-owner like the rest of this module.
    This is a feature-level restriction the assistant surface adds on top
    of data that isn't otherwise sensitive at the REST-route level
    (GET .../required-skills stays open to anyone who can see the
    project); deliberate, not an oversight, per this feature's own scope
    decision that the PRD assistant is HR-only, full stop.
    """
    if effective_role(caller.role, view_mode) != "hr":
        return None

    candidates = resolve_project_name(db, name)
    if len(candidates) > 1:
        return AmbiguousProjectMatch(query=name, matches=[p.name for p in candidates])
    if not candidates:
        return None
    project = candidates[0]

    skills = get_required_skills(db, caller, project.id, view_mode) or []
    notes = get_requirement_notes(db, caller, project.id, view_mode) or []
    return ProjectRequirementsOut(
        project_name=project.name, description=project.description, skills=skills, notes=notes)
