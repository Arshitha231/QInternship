"""Project skill requirements: which skills (and minimum level) a project's
delivery actually needs, as recorded by whoever owns the project or HR.

This is what lets app/continuity.py's delivery-dependency calculation tell
a real requirement apart from a heuristic. Without a row here for a given
project, that calculation falls back to its previous behavior — treating
every Working/Expert skill an assigned employee happens to hold as a
candidate dependency — which overcounts: a skill on someone's profile that
this particular engagement never needed still showed up as a "dependency".
See app/schemas.py's DeliveryDependency.source for how that distinction
surfaces in the API.

Not sensitive data — unlike work-authorization records, there's no
isolation requirement here. Visibility follows the same confidentiality
rule as everywhere else in this app (confidential projects: members and hr
only). Write access is narrower: only the project's owner, or hr — there's
no existing "project owner can edit their project" pattern elsewhere in
this codebase, so this introduces the smallest version of one rather than
gating writes behind hr alone, since a project's actual skill requirements
are usually known by whoever runs it, not by HR.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import Project, ProjectSkillRequirement, Skill
from app.models.enums import ProjectClassification, SkillLevel
from app.people import resolve_skill
from app.permissions import ViewMode, can_see_confidential_project, effective_role
from app.schemas import ProjectSkillRequirementIn, ProjectSkillRequirementOut


class ProjectNotWritable(Exception):
    """Raised when the caller is neither the project's owner nor hr."""


class UnknownSkill(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Unrecognized skill: {name}")


def _visible_project(
    db: Session, caller: AuthenticatedUser, project_id: int, view_mode: ViewMode = "work",
) -> Project | None:
    """None whether the project doesn't exist or is confidential and the
    caller isn't a member/hr — same "restricted looks like absent" shape
    used throughout this app, not a distinguishable 403/404 split.

    HR's blanket exemption for confidential projects is a role privilege, so
    it collapses in employee mode like every other one (effective_role) — it
    used to read caller.role directly, which let an hr caller keep seeing a
    confidential project's requirements while claiming to be looking at the
    ordinary-colleague view. Membership is untouched by the mode: it's an
    ABAC grant keyed on identity, and those deliberately survive employee
    mode (README, "ABAC survives employee mode"), so somebody actually
    staffed on the project still sees it either way.
    """
    project = db.get(Project, project_id)
    if project is None:
        return None
    if (project.classification == ProjectClassification.confidential
            and effective_role(caller.role, view_mode) != "hr"
            and not can_see_confidential_project(db, caller, project_id)):
        return None
    return project


def get_required_skills(
    db: Session, caller: AuthenticatedUser, project_id: int, view_mode: ViewMode = "work",
) -> list[ProjectSkillRequirementOut] | None:
    if _visible_project(db, caller, project_id, view_mode) is None:
        return None
    rows = (
        db.query(ProjectSkillRequirement, Skill)
        .join(Skill, ProjectSkillRequirement.skill_id == Skill.id)
        .filter(ProjectSkillRequirement.project_id == project_id)
        .order_by(Skill.name)
        .all()
    )
    return [ProjectSkillRequirementOut(skill=skill.name, minimum_level=req.minimum_level.value) for req, skill in rows]


def set_required_skills(
    db: Session, caller: AuthenticatedUser, project_id: int, requirements: list[ProjectSkillRequirementIn],
    view_mode: ViewMode = "work",
) -> list[ProjectSkillRequirementOut] | None:
    """Replaces the full set for this project — not an incremental add, so
    re-recording requirements can't accidentally leave a stale one behind.
    Returns None if the project doesn't exist or isn't visible to the
    caller (confidential, non-member); raises ProjectNotWritable if it's
    visible but the caller isn't its owner or hr; raises UnknownSkill on
    the first name that doesn't resolve, before writing anything."""
    project = _visible_project(db, caller, project_id, view_mode)
    if project is None:
        return None
    # Ownership is identity, not role, so it survives employee mode; HR's
    # role privilege does not, matching every other write in the app (nothing
    # is editable in employee mode -- app.permissions.EDITABLE).
    if effective_role(caller.role, view_mode) != "hr" and caller.id != project.owner_id:
        raise ProjectNotWritable("Only the project's owner or HR can set its required skills")

    # Keyed by skill id, not appended to a list: two input rows naming the
    # same skill (including via a synonym that resolves to it) would
    # otherwise try to insert two rows for the same (project_id, skill_id)
    # and hit the unique constraint. Last one in the request wins.
    resolved: dict[int, tuple[Skill, SkillLevel]] = {}
    for req in requirements:
        skill = resolve_skill(db, req.skill)
        if skill is None:
            raise UnknownSkill(req.skill)
        resolved[skill.id] = (skill, SkillLevel(req.minimum_level))

    db.query(ProjectSkillRequirement).filter(ProjectSkillRequirement.project_id == project_id).delete()
    for skill, level in resolved.values():
        db.add(ProjectSkillRequirement(project_id=project_id, skill_id=skill.id, minimum_level=level))
    db.commit()

    # De-duplicated by skill (two input rows naming the same skill collapse
    # to whichever was resolved last) and re-sorted by name, same shape
    # get_required_skills returns, so a caller can't tell a fresh write
    # apart from a re-read by response shape alone.
    return get_required_skills(db, caller, project_id)
