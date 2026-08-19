"""Self-service skills and languages: the person themselves adding,
re-levelling and removing their own entries.

WHY THIS ISN'T IN app/writes.py
-------------------------------
Everything in writes.py is gated by app.permissions' EDITABLE table, which
is keyed on (role, view_mode) and answers "may this ROLE change this field
on somebody's record". That is the wrong question here. Every role other
than hr/it is pinned to employee mode (resolve_view_mode), and EDITABLE
grants nothing at all in employee mode by design — so expressing "everyone
edits their own skills" through that table would mean punching a hole in
the one invariant it exists to state.

The right precedent is already in this codebase: update_own_bio and
update_own_name_pronunciation in app/people.py. Both bypass EDITABLE
entirely and are gated on identity — person_id == caller.id — because the
record's own subject is the authority on that field, whatever role they
hold. A skill somebody claims about themselves is the same kind of fact as
the bio they write about themselves, and `source` (below) is the axis that
already says so out loud.

Kept in its own module rather than piled onto people.py because this is a
write surface with real domain rules of its own (skill resolution, category
agreement, the source/verified_at re-stamp), not the two-line persistence
ops bio and pronunciation are. Same reasoning app/project_skills.py already
applies to the project-side skill writes.

SKILLS AND LANGUAGES ARE ONE TABLE
----------------------------------
There is no separate languages table — a language is a `skills` row with
category=language, split apart only at render time (app/people.py's
_build_detail, app/search_index.py's build_profile_text). So this module
serves both, and `category` on the add request is what decides which card a
brand-new skill lands under.

SOURCE IS THE HONESTY AXIS
--------------------------
The directory records level (how good) and source (how much to trust the
claim) independently. Anything written here is `self` sourced, and
re-levelling an existing holding RE-STAMPS it to `self` with verified_at
cleared, even if it arrived as `confirmed` or `certified`. That is the
point: the moment the subject changes the number, the person asserting it
is them, and the profile has to say so rather than let a self-claim inherit
somebody else's attestation. Claiming Expert is allowed — `self` is exactly
how a reader tells that apart from a verified Expert.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, EmployeeSkill, Skill
from app.models.enums import SkillCategory, SkillLevel, SkillSource
from app.people import _build_detail, resolve_skill
from app.policy import compute_visible_fields
from app.schemas import PersonDetail
from app.search_reindex import reindex_employee


class SkillAlreadyHeld(Exception):
    """add_own_skill on a skill the person already has.

    Refused rather than treated as a re-level, for the same reason
    app.writes.add_project_history refuses an existing membership: "add"
    that silently overwrites is how a mistaken duplicate becomes data loss
    on the entry that was already there. PATCH is the way to change a level.
    """


class SkillNotHeld(Exception):
    """update_own_skill / remove_own_skill on a skill the person doesn't hold."""


class SkillCategoryMismatch(Exception):
    """The named skill exists, but on the other side of the Skills /
    Languages split — "Python" submitted from the Languages card, or
    "Tamil" from the Skills card.

    Refused rather than silently filed under the category already on the
    Skill row. Category belongs to the skill, not to one person's holding
    of it, so honouring the request would mean re-categorising it for
    everybody; ignoring it would mean the entry vanishing from the card the
    person just typed it into and appearing under the other one. A plain
    error naming the real category is the only outcome that isn't a
    surprise.

    Only the LANGUAGE boundary is checked — see _crosses_language_boundary.
    """

    def __init__(self, name: str, actual: SkillCategory, requested: SkillCategory):
        self.name = name
        self.actual = actual
        self.requested = requested
        super().__init__(
            f"'{name}' is a {actual.value} skill, not {requested.value} — "
            f"add it under {'Languages' if actual is SkillCategory.language else 'Skills'} instead"
        )


def _crosses_language_boundary(actual: SkillCategory, requested: SkillCategory) -> bool:
    """Whether a requested category disagrees with an existing skill's in a
    way the profile can actually show.

    technical and domain are the same card — the profile renders "skills"
    as everything that isn't a language, and never displays the category
    itself — so a domain skill added from the Skills card (which sends
    "technical", since that's the default for a name nobody has used yet)
    must NOT be refused: the person typed it into the right box and it
    would land exactly where they expect. The language split is the only
    one they can see, so it's the only one worth enforcing.

    Either way the existing category wins for a name that already exists.
    This decides refuse-vs-accept, never re-categorise.
    """
    return (actual is SkillCategory.language) != (requested is SkillCategory.language)


def _audit(db: Session, caller: AuthenticatedUser, action: str, query_text: str) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text, result_count=1,
        fields_returned=json.dumps(["languages", "skills"]), timestamp=datetime.now(),
    ))
    db.commit()


def _own_active_target(db: Session, person_id: str) -> Employee | None:
    """The route has already established person_id == caller.id; this is the
    remaining "does that record still exist and count" half, matching
    update_own_bio's own None-on-missing-or-inactive contract."""
    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        return None
    return target


def _detail(db: Session, caller: AuthenticatedUser, target: Employee) -> PersonDetail:
    """Every write here answers with the caller's own profile as they can
    see it, rather than the row that changed.

    Two reasons. The category rules above mean an add doesn't always land
    where the form that submitted it lives, so the response has to show
    where it actually went. And the caller is by definition looking at their
    own profile page, so the full detail is what the screen needs anyway —
    the same reason update_own_bio returns PersonDetail instead of a bio.
    """
    return _build_detail(db, caller, target, compute_visible_fields(db, caller, target))


def _held(db: Session, employee_id: str, skill_id: int) -> EmployeeSkill | None:
    return (
        db.query(EmployeeSkill)
        .filter(EmployeeSkill.employee_id == employee_id, EmployeeSkill.skill_id == skill_id)
        .first()
    )


def add_own_skill(
    db: Session, caller: AuthenticatedUser, person_id: str,
    name: str, category: SkillCategory, level: SkillLevel,
) -> PersonDetail | None:
    """Adds a skill the caller doesn't already hold.

    An unrecognised name creates the `skills` row — refusing unknown names
    would limit people to the seeded vocabulary, and the review pipeline
    (app.proposals._commit_skill) and IT's project writes
    (app.writes.get_or_create_project) both already create by name. The
    guard against vocabulary sprawl is resolve_skill, not refusal: it
    matches case-insensitively AND follows an alias to its canonical row, so
    "sre" attaches to the existing Site Reliability Engineering rather than
    minting a third spelling of it.
    """
    target = _own_active_target(db, person_id)
    if target is None:
        return None

    skill = resolve_skill(db, name)
    if skill is None:
        skill = Skill(name=name, category=category, canonical_id=None)
        db.add(skill)
        db.flush()
    elif _crosses_language_boundary(skill.category, category):
        raise SkillCategoryMismatch(skill.name, skill.category, category)

    if _held(db, person_id, skill.id) is not None:
        raise SkillAlreadyHeld(skill.name)

    db.add(EmployeeSkill(
        employee_id=person_id, skill_id=skill.id,
        level=level, source=SkillSource.self_reported, verified_at=None,
    ))
    db.commit()

    # Rule 6: skills and languages both feed build_profile_text, so a skill
    # added here is invisible to search until this runs.
    reindex_employee(db, target)
    _audit(db, caller, "add_own_skill", f"person_id={person_id} skill={skill.name}")
    return _detail(db, caller, target)


def update_own_skill(
    db: Session, caller: AuthenticatedUser, person_id: str, name: str, level: SkillLevel,
) -> PersonDetail | None:
    """Re-levels a skill the caller already holds.

    Level is the only thing editable: a skill's NAME identifies which row
    this is, and its category belongs to the skill rather than to this
    person's holding of it. Correcting a name is remove-then-add, which is
    the honest shape — it's a different skill.
    """
    target = _own_active_target(db, person_id)
    if target is None:
        return None

    skill = resolve_skill(db, name)
    held = _held(db, person_id, skill.id) if skill is not None else None
    if held is None:
        raise SkillNotHeld(name)

    held.level = level
    # See the module docstring: the subject setting their own level makes
    # them the source of it, whatever it was before. Dropping verified_at
    # with it, so a stale verification date can't sit next to a number
    # nobody verified.
    held.source = SkillSource.self_reported
    held.verified_at = None
    db.commit()

    reindex_employee(db, target)
    _audit(db, caller, "update_own_skill", f"person_id={person_id} skill={skill.name}")
    return _detail(db, caller, target)


def remove_own_skill(
    db: Session, caller: AuthenticatedUser, person_id: str, name: str,
) -> PersonDetail | None:
    """Drops the caller's holding of a skill.

    Only the EmployeeSkill row goes; the `skills` row itself stays, since
    other people hold it and project requirements point at it.

    A `confirmed` or `certified` holding is removable like any other. The
    alternative — pinning somebody to an attestation they say is wrong —
    makes a mis-attributed skill permanent, and continuity's bus-factor
    numbers are better served by a profile its subject is willing to keep
    accurate than by one nobody can correct. The audit row names the skill,
    so the removal is on the record either way.
    """
    target = _own_active_target(db, person_id)
    if target is None:
        return None

    skill = resolve_skill(db, name)
    held = _held(db, person_id, skill.id) if skill is not None else None
    if held is None:
        raise SkillNotHeld(name)

    db.delete(held)
    db.commit()

    reindex_employee(db, target)
    _audit(db, caller, "remove_own_skill", f"person_id={person_id} skill={skill.name}")
    return _detail(db, caller, target)
