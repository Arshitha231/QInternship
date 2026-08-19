"""Community Graph: each employee's private "who to contact for what" list.

Two edge sources share app.models.community_link.CommunityLink — official
(HR-bootstrapped, read-only to the employee) and personal (the employee's
own additions) — merged into one list per owner by list_community_links.
Visibility is unconditionally private to owner_employee_id: unlike every
other role-gated view in this codebase, no role — including hr/it — can
read another employee's graph, checked by construction (the query's WHERE
clause) rather than through app.permissions' (role, view_mode) tables,
because the spec's rule has no role exception to encode.

Mentor links are the one row shape that changes source over time: created
official, they flip to personal once
created_at + org_settings.mentor_link_duration_days has passed, computed at
READ time (never stored as a fixed expiry column — see
_resolve_mentor_expiration) so raising or lowering the org setting reshapes
future reads without silently rewriting rows that already expired under the
old value.

Official-link bootstrapping (generate_suggested_official_links) stages
suggestions for HR to review — nothing becomes a real CommunityLink until an
explicit confirm, same staging discipline as app/proposals.py.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AuditLog, CommunityLink, Employee, OrgSettings, OrgUnit, SuggestedOfficialLink
from app.models.enums import CommunityLinkSource, SuggestedLinkStatus
from app.models.org_settings import DEFAULT_MENTOR_LINK_DURATION_DAYS
from app.community_roles import SYNTHETIC_LINK_IDS, resolve_canonical_roles
from app.permissions import ViewMode
from app.permissions import effective_role
from app.policy import enforce, excluded_by_obligations
from app.query_plan import PeopleQuery


class LinkNotFound(Exception):
    pass


class LinkDenied(Exception):
    """Not the owner, or the link is official — edit/delete is personal-only."""


class SuggestionNotFound(Exception):
    pass


class SuggestionNotActionable(Exception):
    """Already reviewed, or its candidate is no longer an active employee."""


class SuggestionDenied(Exception):
    """An HR-only action: reviewing or generating official-link suggestions,
    or running the mentor auto-assignment sweep."""


def _audit(
    db: Session, caller: AuthenticatedUser, action: str, query_text: str, fields: list[str],
) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text,
        result_count=1, fields_returned=json.dumps(sorted(fields)),
        timestamp=datetime.now(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Mentor-link expiration — computed at read time, persisted only when it
# actually flips (a write side-effect of a read, same shape as
# app.certifications recomputing derived status on access).
# ---------------------------------------------------------------------------

def _mentor_link_duration_days(db: Session, office_id: int | None) -> int:
    """Office-specific override first, then the org-wide row, then a
    hardcoded fallback for a deployment that has configured neither."""
    if office_id is not None:
        office_row = db.query(OrgSettings).filter(OrgSettings.office_id == office_id).first()
        if office_row is not None:
            return office_row.mentor_link_duration_days
    org_wide = db.query(OrgSettings).filter(OrgSettings.office_id.is_(None)).first()
    if org_wide is not None:
        return org_wide.mentor_link_duration_days
    return DEFAULT_MENTOR_LINK_DURATION_DAYS


def _resolve_mentor_expiration(db: Session, link: CommunityLink) -> CommunityLink:
    """Flip an expired mentor link from official to personal, in place.

    Only ever reconsiders is_mentor_link rows — an ordinary official link
    (payroll, HR contact) has is_mentor_link=False and is never touched
    here, so a stale read can't accidentally make a non-mentor official
    link editable.
    """
    if not link.is_mentor_link or link.source is not CommunityLinkSource.official:
        return link
    duration = _mentor_link_duration_days(db, link.office_id)
    if datetime.now() >= link.created_at + timedelta(days=duration):
        link.source = CommunityLinkSource.personal
        db.commit()
        db.refresh(link)
    return link


def _serialize(link: CommunityLink) -> dict:
    return {
        "id": link.id,
        "owner_employee_id": link.owner_employee_id,
        "contact_employee_id": link.contact_employee_id,
        "role_label": link.role_label,
        "reason": link.reason,
        "source": link.source.value,
        "office_id": link.office_id,
        "department_id": link.department_id,
        "is_mentor_link": link.is_mentor_link,
        "created_at": link.created_at,
    }


def _contact_is_visible(db: Session, decision, contact_employee_id: str) -> bool:
    """Whether the person on the other end of a link can still be shown.

    A community_links row outlives the person it points at: nothing deletes
    links when somebody is deactivated or restricted, and nothing should —
    they come back intact if that person is reactivated or unrestricted, and
    the row is the owner's own note about who to ask for what.

    But the link is only a pointer. Rendering one whose contact has since
    become invisible means rendering a person the caller is not allowed to
    see, which is the same question every other read path answers with
    `is_active` plus the obligations from enforce(). Answering it the same
    way here keeps the community graph from being the one surface where a
    deactivated or restricted person still shows up.
    """
    contact = db.get(Employee, contact_employee_id)
    if contact is None or not contact.is_active:
        return False
    return not excluded_by_obligations(decision, contact)


# ---------------------------------------------------------------------------
# GET /community_links — the caller's own graph: the seven canonical roles
# resolved at read time (app/community_roles.py), plus their own personal
# additions.
#
# The stored official rows are no longer returned one-for-one. payroll,
# benefits_admin, facilities_admin and hr_contact were four separate nodes
# answering one question — "who handles employee and company logistics" —
# and the spec collapses them into a single HR representative. They are
# still HR's confirmed choices and still win over a derived contact; they
# are just presented as the one role they always were.
# ---------------------------------------------------------------------------

def _serialize_resolved_role(owner_id: str, resolved: dict) -> dict:
    """A resolved role in the same shape as a stored link, so the frontend
    draws one kind of node and not two."""
    contact = resolved["contact"]
    office = resolved["office"]
    distance_km = resolved["distance_km"]
    return {
        # The real row's id when one is behind this answer (a mentor
        # pairing, or an official link HR confirmed), so the node still
        # points at something that exists; the role's sentinel otherwise.
        "id": resolved["link_id"] or SYNTHETIC_LINK_IDS[resolved["role"]],
        "owner_employee_id": owner_id,
        "contact_employee_id": contact.id,
        "role_label": resolved["role"],
        "reason": None,
        "source": CommunityLinkSource.official.value,
        "office_id": office.id if office else None,
        "department_id": None,
        "is_mentor_link": resolved["role"] == "mentor",
        # Resolved fresh on every read — there is no stored "since when" to
        # report for a relationship derived from current org data.
        "created_at": datetime.now(),
        "role_key": resolved["role"],
        "contact_office_name": office.name if office else None,
        "contact_office_city": office.city if office else None,
        # Rounded to whole kilometres: the graph says "about 3,100 km away",
        # and a decimal place would imply a precision that city-level
        # coordinates don't have.
        "distance_km": round(distance_km) if distance_km else None,
        "is_remote_fallback": resolved["is_remote_fallback"],
    }


def _serialize_personal(link: CommunityLink) -> dict:
    """A personal link, widened with the same keys the resolved roles carry
    so one response model covers both. role_key is null — a personal link's
    label is whatever the owner typed, not one of the canonical seven."""
    return {
        **_serialize(link),
        "role_key": None,
        "contact_office_name": None,
        "contact_office_city": None,
        "distance_km": None,
        "is_remote_fallback": False,
    }


def list_community_links(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work",
) -> list[dict]:
    """The caller's own graph, with links to people they can no longer see
    omitted.

    view_mode matters here even though ownership is already private to the
    caller: the rows are filtered on whether the *contact* is visible, and
    an hr caller previewing employee mode loses their exemption for
    restricted records exactly as they do everywhere else. Without it this
    endpoint would hand back a contact that the profile lookup for the same
    person, in the same mode, refuses — which is precisely the mismatch that
    rendered a bare id where a name belonged.
    """
    decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)

    caller_employee = db.get(Employee, caller.id)
    if caller_employee is None:
        return []

    # Mentor expiration is applied before resolving, because whether a
    # mentor link has flipped to personal decides which of the two lists
    # below it belongs in.
    stored = (
        db.query(CommunityLink)
        .filter(CommunityLink.owner_employee_id == caller.id)
        .order_by(CommunityLink.id)
        .all()
    )
    for row in stored:
        _resolve_mentor_expiration(db, row)

    out = [
        _serialize_resolved_role(caller.id, resolved)
        for resolved in resolve_canonical_roles(db, caller_employee)
        if _contact_is_visible(db, decision, resolved["contact"].id)
    ]

    # The owner's own additions, which no resolver produces and nothing
    # collapses. An expired mentor link arrives here, as a personal one.
    out.extend(
        _serialize_personal(row)
        for row in stored
        if row.source == CommunityLinkSource.personal
        and _contact_is_visible(db, decision, row.contact_employee_id)
    )
    return out


# ---------------------------------------------------------------------------
# POST /community_links — add a personal link. owner is always the caller;
# there is no request field for it, so nobody can add to someone else's
# graph even by supplying an id.
# ---------------------------------------------------------------------------

def create_personal_link(
    db: Session, caller: AuthenticatedUser, contact_employee_id: str,
    role_label: str, reason: str,
) -> dict:
    if not reason or not reason.strip():
        raise ValueError("reason is required for a personal link")
    if not role_label or not role_label.strip():
        raise ValueError("role_label is required")
    if contact_employee_id == caller.id:
        raise ValueError("cannot link to yourself")

    contact = db.get(Employee, contact_employee_id)
    if contact is None or not contact.is_active:
        raise ValueError(f"no active employee {contact_employee_id}")

    link = CommunityLink(
        owner_employee_id=caller.id, contact_employee_id=contact_employee_id,
        role_label=role_label.strip(), reason=reason.strip(),
        source=CommunityLinkSource.personal, office_id=None, department_id=None,
        is_mentor_link=False, created_at=datetime.now(),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    _audit(db, caller, "create_community_link", f"link_id={link.id}", ["role_label", "reason"])
    return _serialize(link)


def _load_owned_personal(db: Session, caller: AuthenticatedUser, link_id: int) -> CommunityLink:
    """The single permission check the spec calls for: source == 'personal'
    AND owner_employee_id == requester.id. Applied here, once, so
    update/delete can't drift apart from each other — an official link is
    denied regardless of who owns it, and someone else's personal link is
    denied regardless of source."""
    link = db.get(CommunityLink, link_id)
    if link is None:
        raise LinkNotFound(str(link_id))
    if link.owner_employee_id != caller.id or link.source is not CommunityLinkSource.personal:
        raise LinkDenied(
            "only the owner may edit or delete their own personal links; "
            "official links are read-only"
        )
    return link


def update_personal_link(db: Session, caller: AuthenticatedUser, link_id: int, changes: dict) -> dict:
    link = _load_owned_personal(db, caller, link_id)
    if "role_label" in changes:
        value = (changes["role_label"] or "").strip()
        if not value:
            raise ValueError("role_label cannot be empty")
        link.role_label = value
    if "reason" in changes:
        value = (changes["reason"] or "").strip()
        if not value:
            raise ValueError("reason cannot be empty")
        link.reason = value
    db.commit()
    db.refresh(link)
    _audit(db, caller, "update_community_link", f"link_id={link.id}", list(changes.keys()))
    return _serialize(link)


def delete_personal_link(db: Session, caller: AuthenticatedUser, link_id: int) -> None:
    link = _load_owned_personal(db, caller, link_id)
    db.delete(link)
    db.commit()
    _audit(db, caller, "delete_community_link", f"link_id={link_id}", [])


# ---------------------------------------------------------------------------
# HR review queue for bootstrapped office/role suggestions.
# ---------------------------------------------------------------------------

def _authorize_hr(caller: AuthenticatedUser, view_mode: ViewMode = "work") -> None:
    """HR in work mode. Employee mode is "what an ordinary colleague sees",
    and an ordinary colleague has no bootstrapping queue to review — so the
    role collapses through effective_role here exactly as it does on every
    other privileged surface. This used to read caller.role alone."""
    if effective_role(caller.role, view_mode) != "hr":
        raise SuggestionDenied("reviewing official-link suggestions is an HR action, in work mode")


def _serialize_suggestion(row: SuggestedOfficialLink) -> dict:
    return {
        "id": row.id, "office_id": row.office_id, "role_label": row.role_label,
        "candidate_employee_id": row.candidate_employee_id, "status": row.status.value,
        "created_at": row.created_at, "reviewed_by": row.reviewed_by, "reviewed_at": row.reviewed_at,
    }


def list_suggested_official_links(
    db: Session, caller: AuthenticatedUser, office_id: int | None = None,
    view_mode: ViewMode = "work",
) -> list[dict]:
    _authorize_hr(caller, view_mode)
    query = db.query(SuggestedOfficialLink)
    if office_id is not None:
        query = query.filter(SuggestedOfficialLink.office_id == office_id)
    rows = query.order_by(SuggestedOfficialLink.id).all()
    return [_serialize_suggestion(r) for r in rows]


def _load_pending_suggestion(db: Session, suggestion_id: int) -> SuggestedOfficialLink:
    row = db.get(SuggestedOfficialLink, suggestion_id)
    if row is None:
        raise SuggestionNotFound(str(suggestion_id))
    if row.status is not SuggestedLinkStatus.pending:
        raise SuggestionNotActionable(f"suggestion {suggestion_id} is already {row.status.value}")
    return row


def confirm_suggested_official_link(
    db: Session, caller: AuthenticatedUser, suggestion_id: int, view_mode: ViewMode = "work",
) -> dict:
    """Fans the confirmed office/role mapping out to every active employee
    in the office: one official CommunityLink per employee, owner=that
    employee, contact=the confirmed candidate.

    Skips an employee who already holds an official link with this exact
    role_label — from an earlier confirm, or any other official source —
    so "never overwrite a confirmed one" holds per-owner, not just at the
    suggestion level, and re-running a confirm stays idempotent.
    """
    _authorize_hr(caller, view_mode)
    suggestion = _load_pending_suggestion(db, suggestion_id)

    candidate = db.get(Employee, suggestion.candidate_employee_id)
    if candidate is None or not candidate.is_active:
        raise SuggestionNotActionable(f"candidate {suggestion.candidate_employee_id} is not active")

    office_employees = (
        db.query(Employee)
        .filter(Employee.office_id == suggestion.office_id, Employee.is_active == True)  # noqa: E712
        .all()
    )
    for employee in office_employees:
        if employee.id == candidate.id:
            continue
        existing = (
            db.query(CommunityLink)
            .filter(
                CommunityLink.owner_employee_id == employee.id,
                CommunityLink.role_label == suggestion.role_label,
                CommunityLink.source == CommunityLinkSource.official,
            )
            .first()
        )
        if existing is not None:
            continue
        db.add(CommunityLink(
            owner_employee_id=employee.id, contact_employee_id=candidate.id,
            role_label=suggestion.role_label, reason=None,
            source=CommunityLinkSource.official, office_id=suggestion.office_id,
            department_id=None, is_mentor_link=False, created_at=datetime.now(),
        ))

    suggestion.status = SuggestedLinkStatus.confirmed
    suggestion.reviewed_by = caller.id
    suggestion.reviewed_at = datetime.now()
    db.commit()
    db.refresh(suggestion)
    _audit(db, caller, "confirm_suggested_official_link", f"suggestion_id={suggestion.id}", ["status"])
    return _serialize_suggestion(suggestion)


def reject_suggested_official_link(
    db: Session, caller: AuthenticatedUser, suggestion_id: int, view_mode: ViewMode = "work",
) -> dict:
    _authorize_hr(caller, view_mode)
    suggestion = _load_pending_suggestion(db, suggestion_id)
    suggestion.status = SuggestedLinkStatus.rejected
    suggestion.reviewed_by = caller.id
    suggestion.reviewed_at = datetime.now()
    db.commit()
    db.refresh(suggestion)
    _audit(db, caller, "reject_suggested_official_link", f"suggestion_id={suggestion.id}", ["status"])
    return _serialize_suggestion(suggestion)


# ---------------------------------------------------------------------------
# Suggestion generation — derives office/role candidates from existing
# office/job-title data. There is no dedicated role column anywhere in this
# schema (app/auth.py: role arrives per-request, never stored on Employee),
# so job_title is the only per-employee role signal that exists — the same
# gap app.models.course_requirement.job_title_keyword documents, solved the
# same way, by substring match. HR contact instead reuses
# app.config.hr_org_unit_name(), an existing, stronger signal than guessing
# at title text for that one role.
# ---------------------------------------------------------------------------

# role_label -> case-insensitive job_title substrings that count as a match.
#
# A role whose keywords match nobody simply never gets a suggestion staged —
# the scan degrades to silence for a role a company doesn't have ("if your
# org has one", per spec), with no separate "does this org have X" flag
# needed. That is the intended behaviour for a genuinely absent role, but it
# also means a keyword set that merely FAILS TO GUESS this directory's title
# vocabulary is indistinguishable from one whose role doesn't exist: both
# just produce nothing, silently. Every entry below is therefore checked
# against real job_title values rather than assumed
# (tests/test_community_links.py asserts each one still matches somebody, so
# a future title rename can't quietly turn a role back into a dead path).
#
# "identity" is what catches the Identity & Access Analyst / Identity &
# Endpoints Team Manager titles — the actual owners of access requests,
# which is the specific need the spec names for this role. Matching only
# "security"/"compliance" missed all ten of them.
_TITLE_KEYWORD_ROLES: dict[str, tuple[str, ...]] = {
    "payroll": ("payroll",),
    "facilities_admin": ("workplace", "office manager", "office admin", "facilities"),
    "benefits_admin": ("benefits", "leave admin", "leave administrator"),
    "security_compliance": ("security", "compliance", "infosec", "information security", "identity"),
}


def _org_unit_subtree_ids(db: Session, root_id: int) -> set[int]:
    """Every org unit at or below root_id. Bounded the same way
    app.permissions.division_of bounds its upward walk — the tree is at
    most 4 levels deep (company -> division -> department -> team), so 10
    hops is generous headroom, not a magic number chosen for this call site.
    """
    ids = {root_id}
    frontier = {root_id}
    hops = 0
    while frontier and hops < 10:
        children = db.query(OrgUnit.id).filter(OrgUnit.parent_id.in_(frontier)).all()
        frontier = {c[0] for c in children if c[0] not in ids}
        ids |= frontier
        hops += 1
    return ids


def generate_suggested_official_links(db: Session) -> list[dict]:
    """HR-triggered (not automatic): scans active employees for office/role
    signals and stages one pending suggestion per (office, role, candidate)
    combo that has no existing mapping yet. Never creates a CommunityLink
    directly — confirm_suggested_official_link is the only path that does.
    """
    from app.config import hr_org_unit_name

    created: list[SuggestedOfficialLink] = []

    def _already_mapped(office_id: int, role_label: str) -> bool:
        return (
            db.query(SuggestedOfficialLink)
            .filter(
                SuggestedOfficialLink.office_id == office_id,
                SuggestedOfficialLink.role_label == role_label,
                SuggestedOfficialLink.status == SuggestedLinkStatus.confirmed,
            )
            .first()
            is not None
        )

    def _already_pending(office_id: int, role_label: str, candidate_id: str) -> bool:
        return (
            db.query(SuggestedOfficialLink)
            .filter(
                SuggestedOfficialLink.office_id == office_id,
                SuggestedOfficialLink.role_label == role_label,
                SuggestedOfficialLink.candidate_employee_id == candidate_id,
                SuggestedOfficialLink.status == SuggestedLinkStatus.pending,
            )
            .first()
            is not None
        )

    def _stage(office_id: int, role_label: str, candidate_id: str) -> None:
        # Never suggest for an office/role combo already mapped, and never
        # re-stage a candidate that's already awaiting review for it — this
        # is where "only suggest for office/role combos with no existing
        # mapping" is actually enforced.
        if _already_mapped(office_id, role_label) or _already_pending(office_id, role_label, candidate_id):
            return
        row = SuggestedOfficialLink(
            office_id=office_id, role_label=role_label, candidate_employee_id=candidate_id,
            status=SuggestedLinkStatus.pending, created_at=datetime.now(),
        )
        db.add(row)
        created.append(row)

    employees = (
        db.query(Employee)
        .filter(Employee.is_active == True, Employee.office_id.isnot(None))  # noqa: E712
        .all()
    )

    for role_label, keywords in _TITLE_KEYWORD_ROLES.items():
        for employee in employees:
            title = (employee.job_title or "").lower()
            if any(keyword in title for keyword in keywords):
                _stage(employee.office_id, role_label, employee.id)

    hr_unit = db.query(OrgUnit).filter(OrgUnit.name == hr_org_unit_name()).first()
    if hr_unit is not None:
        hr_unit_ids = _org_unit_subtree_ids(db, hr_unit.id)
        for employee in employees:
            if employee.org_unit_id in hr_unit_ids:
                _stage(employee.office_id, "hr_contact", employee.id)

    db.commit()
    for row in created:
        db.refresh(row)
    return [_serialize_suggestion(r) for r in created]


# ---------------------------------------------------------------------------
# Mentor auto-assignment for new hires. Unlike every other official-link
# kind above, this never goes through suggested_official_links for HR to
# confirm first — a mentor pairing is inherently 1:1 per new hire, not an
# office-wide "everyone gets this contact" mapping, so the
# suggest/confirm/fan-out shape those other kinds use doesn't fit it here.
# Auto-assignment is the deliberate design for this one role, not a
# shortcut around review. Time-boxing is the same read-time expiration
# mechanism every mentor link already uses (is_mentor_link + org_settings —
# see _resolve_mentor_expiration); this function only ever creates the row,
# never the flip.
# ---------------------------------------------------------------------------

def _eligible_new_hires(db: Session, window_days: int) -> list[Employee]:
    """Active employees hired within the window who don't already have a
    mentor link — including one that has since expired and flipped to
    personal, which still counts as "already had one assigned" and is
    never topped up with a second."""
    cutoff = date.today() - timedelta(days=window_days)
    already_assigned = {
        row[0] for row in
        db.query(CommunityLink.owner_employee_id)
        .filter(CommunityLink.is_mentor_link == True)  # noqa: E712
        .all()
    }
    return [
        e for e in
        db.query(Employee)
        .filter(Employee.is_active == True, Employee.hire_date >= cutoff)  # noqa: E712
        .all()
        if e.id not in already_assigned
    ]


def _pick_mentor(db: Session, new_hire: Employee) -> Employee | None:
    """Same team (org unit) first, widening to the whole office if nobody
    there qualifies; never the new hire's own manager (that relationship is
    already surfaced separately, see _synthesize_manager_link) or the new
    hire themselves, and always someone hired earlier so a brand-new
    employee is never paired with an equally brand-new one.

    Among eligible candidates, the most RECENTLY hired one wins, not the
    most tenured: someone who went through onboarding themselves within the
    last year or two remembers what it's like far better than whoever has
    been here the longest, and is a lighter-weight ask than pairing a new
    hire with a director-level veteran. A real judgment call, not the only
    reasonable one — flagged here rather than left silent.
    """
    base = db.query(Employee).filter(
        Employee.is_active == True,  # noqa: E712
        Employee.id != new_hire.id,
        Employee.hire_date < new_hire.hire_date,
    )
    if new_hire.manager_id is not None:
        base = base.filter(Employee.id != new_hire.manager_id)

    same_team = (
        base.filter(Employee.org_unit_id == new_hire.org_unit_id)
        .order_by(Employee.hire_date.desc())
        .first()
    )
    if same_team is not None:
        return same_team
    if new_hire.office_id is None:
        return None
    return (
        base.filter(Employee.office_id == new_hire.office_id)
        .order_by(Employee.hire_date.desc())
        .first()
    )


def auto_assign_mentors(db: Session, caller: AuthenticatedUser, view_mode: ViewMode = "work") -> list[dict]:
    """Sweep for new hires (app.config.new_hire_mentor_window_days()) who
    don't yet have a mentor link, and pair each with an eligible colleague —
    see module docstring above for why this creates the CommunityLink
    directly rather than staging a suggestion.

    Idempotent and safe to run repeatedly (a cron, or this same HR-triggered
    route called by hand): _eligible_new_hires already excludes anyone who
    has one, so a second run only ever picks up genuinely new employees, or
    ones an earlier run had no eligible candidate for.
    """
    _authorize_hr(caller, view_mode)
    from app.config import new_hire_mentor_window_days

    window_days = new_hire_mentor_window_days()
    new_hires = _eligible_new_hires(db, window_days)

    created: list[CommunityLink] = []
    for employee in new_hires:
        mentor = _pick_mentor(db, employee)
        if mentor is None:
            continue
        link = CommunityLink(
            owner_employee_id=employee.id, contact_employee_id=mentor.id,
            role_label="mentor", reason=None, source=CommunityLinkSource.official,
            office_id=employee.office_id, department_id=employee.org_unit_id,
            is_mentor_link=True, created_at=datetime.now(),
        )
        db.add(link)
        created.append(link)

    db.commit()
    for link in created:
        db.refresh(link)
    _audit(db, caller, "auto_assign_mentors", f"window_days={window_days}", ["mentor"])
    return [_serialize(link) for link in created]
