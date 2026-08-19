"""The seven canonical roles every employee's community graph answers for.

    manager           who I report to
    mentor            who helps me grow / get started
    hr_rep            who handles employee and company logistics
    it_rep            who fixes my technology problems
    security_rep      who handles security problems
    technical_expert  who can help me solve a technical problem
    project_contact   who knows about this project

Each is resolved per employee at READ time. Nothing here is stored: a
resolved role is a view of org data, HR's confirmed official links, skills
and project membership as they are right now, so a reassignment, a transfer
or a new project shows up on the next read instead of leaving a stale row
behind. That is the same reasoning app/community_links.py already applies to
the manager entry, applied to the other six.

HR's curation always wins where it exists. hr_rep and security_rep prefer a
confirmed official CommunityLink and only derive a contact when there isn't
one, so confirming a suggestion is never overridden by a guess.

Roles that belong to a PLACE (hr_rep, it_rep, security_rep) fall back
geographically: your own office first, then the nearest office that has
someone, measured as great-circle distance between cities. A Bangalore
employee with no security contact in Bangalore gets Singapore (~3,100 km),
not London (~8,000 km) and not whoever happens to sort first by id.

Roles that belong to YOU (mentor, technical_expert, project_contact) don't
use distance at all — the right mentor is on your team, not in the nearest
building — so they widen by org unit instead, and only tie-break by office.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

from sqlalchemy.orm import Session

from app.models import CommunityLink, Employee, Office, OrgUnit, Project
from app.models.employee_project import EmployeeProject
from app.models.employee_skill import EmployeeSkill
from app.models.enums import CommunityLinkSource, SkillLevel

# The canonical seven, in the order the graph presents them. Display text
# and emoji live in the frontend (frontend/src/community.ts) — this module
# decides WHO, never how it's captioned.
# Order matters twice over: it's the order the graph presents them in, and
# it's the order they claim people in. A role resolved earlier takes its
# contact off the table for later ones (see _prefer_unused), so the roles
# most likely to have an HR-confirmed answer come first — an Identity &
# Access Analyst who matches both the security keywords and the IT division
# is claimed by security, and IT then finds somebody else, instead of one
# person filling both slots.
CANONICAL_ROLES: tuple[str, ...] = (
    "manager", "mentor", "hr_rep", "security_rep", "it_rep",
    "technical_expert", "project_contact",
)

# The roles that belong to a place, and so are the only ones where "not in
# your office" means the search widened to find anybody at all. A technical
# expert on your own team who happens to sit in Seattle is not a fallback —
# they're the best answer, and calling that a fallback would tell the owner
# their directory came up short when it didn't.
PLACE_BASED_ROLES: frozenset[str] = frozenset({"hr_rep", "it_rep", "security_rep"})

# Sentinel ids for resolved roles with no community_links row behind them.
# Distinct negative values, never real primary keys (those autoincrement
# from 1), so a client can't mistake one for a row to PATCH/DELETE —
# db.get(CommunityLink, -3) simply finds nothing and those actions 404 like
# any other nonexistent id. -1 stays the manager's, unchanged from when it
# was the only synthesized entry.
SYNTHETIC_LINK_IDS: dict[str, int] = {
    role: -(index + 1) for index, role in enumerate(CANONICAL_ROLES)
}

# City coordinates for office proximity.
#
# Deliberately a code table rather than latitude/longitude columns on
# offices: this has to work against the already-seeded Azure SQL database
# without a migration and a deploy, and the office list is a fixed seven.
# An office whose city isn't here doesn't break — it just can't be measured,
# and _rank_by_proximity falls back to same-country-first, then any office,
# so a new city degrades to a sensible answer instead of no answer.
CITY_COORDINATES: dict[str, tuple[float, float]] = {
    "Seattle": (47.6062, -122.3321),
    "Austin": (30.2672, -97.7431),
    "New York": (40.7128, -74.0060),
    "London": (51.5074, -0.1278),
    "Bangalore": (12.9716, 77.5946),
    "Singapore": (1.3521, 103.8198),
    "Sydney": (-33.8688, 151.2093),
}

EARTH_RADIUS_KM = 6371.0

# Job-title keywords for the security role. Same substring approach, and the
# same caveat, as app/community_links.py's _TITLE_KEYWORD_ROLES: there is no
# role column on Employee, so job_title is the only per-person signal there
# is. "identity" is what catches the Identity & Access Analyst titles who
# actually own access requests.
SECURITY_TITLE_KEYWORDS: tuple[str, ...] = (
    "security", "compliance", "infosec", "information security", "identity",
)

# Stored official link labels that now answer as one HR representative. The
# spec folded payroll, benefits, leave, facilities and general HR questions
# into a single contact, but the confirmed rows behind them still exist and
# are still HR's own choices — so they're reused in preference order rather
# than discarded.
HR_OFFICIAL_LABELS: tuple[str, ...] = (
    "hr_contact", "benefits_admin", "payroll", "facilities_admin",
)
SECURITY_OFFICIAL_LABELS: tuple[str, ...] = ("security_compliance",)


def _distance_km(a: Office | None, b: Office | None) -> float | None:
    """Great-circle distance between two offices' cities, or None if either
    city has no coordinates on file."""
    if a is None or b is None:
        return None
    start, end = CITY_COORDINATES.get(a.city), CITY_COORDINATES.get(b.city)
    if start is None or end is None:
        return None
    lat1, lon1 = radians(start[0]), radians(start[1])
    lat2, lon2 = radians(end[0]), radians(end[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def _rank_by_proximity(
    db: Session, candidates: list[Employee], home_office_id: int | None,
) -> tuple[Employee, float | None] | None:
    """The nearest candidate to home_office_id, and how far away they are.

    Same office ranks first at distance 0. Beyond that, measured distance
    wins; then same-country (the degraded answer when a city has no
    coordinates, which still beats an arbitrary pick); then everyone else.
    Ties break on employee id so the same directory always answers the same
    way rather than shuffling between reads.
    """
    if not candidates:
        return None

    home = db.get(Office, home_office_id) if home_office_id is not None else None
    offices: dict[int, Office | None] = {}

    def office_of(employee: Employee) -> Office | None:
        if employee.office_id is None:
            return None
        if employee.office_id not in offices:
            offices[employee.office_id] = db.get(Office, employee.office_id)
        return offices[employee.office_id]

    def sort_key(employee: Employee) -> tuple[int, float, str]:
        if home_office_id is not None and employee.office_id == home_office_id:
            return (0, 0.0, employee.id)
        distance = _distance_km(home, office_of(employee))
        if distance is not None:
            return (1, distance, employee.id)
        candidate_office = office_of(employee)
        same_country = (
            home is not None and candidate_office is not None
            and home.country == candidate_office.country
        )
        return (2 if same_country else 3, 0.0, employee.id)

    best = min(candidates, key=sort_key)
    if home_office_id is not None and best.office_id == home_office_id:
        return best, 0.0
    return best, _distance_km(home, office_of(best))


def _org_subtree_ids(db: Session, unit_name: str) -> set[int]:
    """Every org unit id at or below the named one. Employees are filed
    under their most specific unit, so naming a division has to reach the
    teams beneath it. Empty set when the name matches nothing, which makes
    every caller below fail closed."""
    root = db.query(OrgUnit).filter(OrgUnit.name.ilike(unit_name)).first()
    if root is None:
        return set()
    ids = {root.id}
    frontier = {root.id}
    hops = 0
    # The tree is company -> division -> department -> team; 10 hops is
    # headroom, and also the cycle guard.
    while frontier and hops < 10:
        children = db.query(OrgUnit.id).filter(OrgUnit.parent_id.in_(frontier)).all()
        frontier = {c[0] for c in children if c[0] not in ids}
        ids |= frontier
        hops += 1
    return ids


def _active_in_units(db: Session, unit_ids: set[int], exclude_id: str) -> list[Employee]:
    if not unit_ids:
        return []
    return (
        db.query(Employee)
        .filter(
            Employee.is_active == True,  # noqa: E712
            Employee.org_unit_id.in_(unit_ids),
            Employee.id != exclude_id,
        )
        .all()
    )


def _prefer_unused(candidates: list[Employee], already: set[str]) -> list[Employee]:
    """Drop people already answering another role, unless that would leave
    nothing.

    An Identity & Access Analyst matches both the IT division and the
    security title keywords, so without this the same person fills both
    slots and the graph draws two nodes for one human — which looks like a
    bug and, worse, hides the fact that there is a separate security contact
    to know about. Falling back to the full list when everyone is taken is
    deliberate: a genuinely small office where one person really is both is
    better served by naming them twice than by an empty role.
    """
    remaining = [e for e in candidates if e.id not in already]
    return remaining or candidates


def _confirmed_official_contact(
    db: Session, owner_id: str, labels: tuple[str, ...],
) -> CommunityLink | None:
    """HR's own confirmed choice for this owner, taking the first label that
    matches in preference order — so an hr_contact beats a payroll row for
    the same person rather than depending on insertion order."""
    rows = (
        db.query(CommunityLink)
        .filter(
            CommunityLink.owner_employee_id == owner_id,
            CommunityLink.role_label.in_(labels),
        )
        .all()
    )
    if not rows:
        return None
    by_label = {row.role_label: row for row in rows}
    for label in labels:
        if label in by_label:
            return by_label[label]
    return None


# ---------------------------------------------------------------------------
# One resolver per role. Each returns (contact, distance_km, link_id) or None.
#
# distance_km is 0.0 for someone in your own office, a positive number for a
# geographic fallback, and None when distance isn't the axis that role widens
# along (mentor, technical expert, project contact) or can't be measured.
#
# link_id is the id of the stored CommunityLink this answer came from, when
# one exists — a mentor pairing, or an official link HR confirmed. It stays
# on the response so the node still points at a real row: an expired mentor
# link becomes the owner's to edit and delete by that id, and substituting a
# synthetic one would leave the client holding an id for a row that isn't
# there. None for the derived roles, which have no row behind them.
# ---------------------------------------------------------------------------

def _resolve_manager(db: Session, owner: Employee):
    """Straight from Employee.manager_id — already real org data, never
    duplicated into a stored row that could drift when someone's manager
    changes."""
    if owner.manager_id is None:
        return None
    manager = db.get(Employee, owner.manager_id)
    if manager is None or not manager.is_active:
        return None
    return manager, None, None


def _resolve_mentor(db: Session, owner: Employee):
    """The mentor pairing HR's sweep made, while it is still official.

    is_mentor_link stays True for the life of the row; expiration is the
    flip of `source` to personal, applied by the caller before this runs.
    Matching on the flag alone would keep an expired pairing pinned in the
    official mentor slot forever — which would both outlast the mentoring
    period and hide the fact that the row is now the owner's own, editable
    and deletable. An expired one belongs in the personal list, and falls
    through to it by being absent here.
    """
    row = (
        db.query(CommunityLink)
        .filter(
            CommunityLink.owner_employee_id == owner.id,
            CommunityLink.is_mentor_link == True,  # noqa: E712
            CommunityLink.source == CommunityLinkSource.official,
        )
        .order_by(CommunityLink.id.desc())
        .first()
    )
    if row is None:
        return None
    mentor = db.get(Employee, row.contact_employee_id)
    if mentor is None or not mentor.is_active:
        return None
    return mentor, None, row.id


def _resolve_office_role(
    db: Session, owner: Employee, candidates: list[Employee],
):
    """The shared shape for the three place-based roles: your office first,
    then the nearest office that has somebody."""
    ranked = _rank_by_proximity(db, candidates, owner.office_id)
    if ranked is None:
        return None
    contact, distance = ranked
    return contact, distance, None


def _from_confirmed_link(db: Session, owner: Employee, confirmed: CommunityLink):
    """A contact HR confirmed, with the distance to them measured for
    display — never for ranking. HR chose this person; how far away they
    are is information, not a reason to overrule it."""
    contact = db.get(Employee, confirmed.contact_employee_id)
    if contact is None or not contact.is_active:
        return None
    distance = _distance_km(
        db.get(Office, owner.office_id) if owner.office_id else None,
        db.get(Office, contact.office_id) if contact.office_id else None,
    )
    return contact, (0.0 if contact.office_id == owner.office_id else distance), confirmed.id


def _resolve_hr_rep(db: Session, owner: Employee, already: set[str]):
    from app.config import hr_org_unit_name

    confirmed = _confirmed_official_contact(db, owner.id, HR_OFFICIAL_LABELS)
    if confirmed is not None:
        resolved = _from_confirmed_link(db, owner, confirmed)
        if resolved is not None:
            return resolved
    return _resolve_office_role(db, owner, _prefer_unused(
        _active_in_units(db, _org_subtree_ids(db, hr_org_unit_name()), owner.id), already))


def _resolve_it_rep(db: Session, owner: Employee, already: set[str]):
    # Same unit name the demo login derives its `it` role from, so "who the
    # app treats as IT" has one answer, not two that can disagree.
    from app.demo_auth import it_org_unit_name

    return _resolve_office_role(db, owner, _prefer_unused(
        _active_in_units(db, _org_subtree_ids(db, it_org_unit_name()), owner.id), already))


def _resolve_security_rep(db: Session, owner: Employee, already: set[str]):
    confirmed = _confirmed_official_contact(db, owner.id, SECURITY_OFFICIAL_LABELS)
    if confirmed is not None:
        resolved = _from_confirmed_link(db, owner, confirmed)
        if resolved is not None:
            return resolved

    candidates = [
        e for e in db.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
        if e.id != owner.id
        and any(keyword in (e.job_title or "").lower() for keyword in SECURITY_TITLE_KEYWORDS)
    ]
    return _resolve_office_role(db, owner, _prefer_unused(candidates, already))


def _resolve_technical_expert(db: Session, owner: Employee, exclude: set[str]):
    """Someone at expert level in something the owner is still working on or
    learning.

    Widens by org unit, not by distance: the person who can unblock you on a
    skill is the one who works on what you work on, and a video call doesn't
    care which building they're in. Office proximity is only the tie-break
    among equally relevant people.

    Excludes the manager and mentor — they're already on the graph, and a
    third node pointing at the same person teaches the owner nothing.
    """
    growing_levels = (SkillLevel.learning, SkillLevel.working)
    owner_skill_ids = {
        row[0] for row in
        db.query(EmployeeSkill.skill_id)
        .filter(EmployeeSkill.employee_id == owner.id, EmployeeSkill.level.in_(growing_levels))
        .all()
    }
    if not owner_skill_ids:
        return None

    expert_rows = (
        db.query(EmployeeSkill.employee_id)
        .filter(
            EmployeeSkill.skill_id.in_(owner_skill_ids),
            EmployeeSkill.level == SkillLevel.expert,
            EmployeeSkill.employee_id != owner.id,
        )
        .all()
    )
    expert_ids = {row[0] for row in expert_rows} - exclude
    if not expert_ids:
        return None

    candidates = (
        db.query(Employee)
        .filter(Employee.is_active == True, Employee.id.in_(expert_ids))  # noqa: E712
        .all()
    )
    if not candidates:
        return None

    same_unit = [e for e in candidates if e.org_unit_id == owner.org_unit_id]
    ranked = _rank_by_proximity(db, same_unit or candidates, owner.office_id)
    if ranked is None:
        return None
    # Distance is reported but never ranked on for this role, so a same-team
    # expert in another city still wins — and the graph still says where
    # they are.
    contact, distance = ranked
    return contact, distance, None


def _resolve_project_contact(db: Session, owner: Employee, already: set[str]):
    """The owner of a project this person actually works on — current work
    first, then the most recently started. Someone who owns the only project
    they're on gets nothing here rather than a link to themselves."""
    assignments = (
        db.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == owner.id)
        .all()
    )
    if not assignments:
        return None

    # Current assignments (no end date) first, then most recent start.
    assignments.sort(key=lambda a: (a.end_date is not None, -a.start_date.toordinal()))
    # Two passes: prefer a project whose owner isn't already on the graph,
    # then accept one who is rather than leaving the role unanswered.
    for skip_already in (True, False):
        for assignment in assignments:
            project = db.get(Project, assignment.project_id)
            if project is None or project.owner_id == owner.id:
                continue
            if skip_already and project.owner_id in already:
                continue
            contact = db.get(Employee, project.owner_id)
            if contact is None or not contact.is_active:
                continue
            distance = _distance_km(
                db.get(Office, owner.office_id) if owner.office_id else None,
                db.get(Office, contact.office_id) if contact.office_id else None,
            )
            return contact, (0.0 if contact.office_id == owner.office_id else distance), None
    return None


def resolve_canonical_roles(db: Session, owner: Employee) -> list[dict]:
    """All seven, in CANONICAL_ROLES order, skipping any this directory
    genuinely can't answer — a company with no IT division, an employee with
    no manager or no projects. An unresolvable role is absent from the list
    rather than present-but-empty, and the frontend renders the gap.
    """
    resolved: list[dict] = []
    already: set[str] = {owner.id}

    for role in CANONICAL_ROLES:
        if role == "manager":
            outcome = _resolve_manager(db, owner)
        elif role == "mentor":
            outcome = _resolve_mentor(db, owner)
        elif role == "hr_rep":
            outcome = _resolve_hr_rep(db, owner, already)
        elif role == "it_rep":
            outcome = _resolve_it_rep(db, owner, already)
        elif role == "security_rep":
            outcome = _resolve_security_rep(db, owner, already)
        elif role == "technical_expert":
            outcome = _resolve_technical_expert(db, owner, already)
        else:
            outcome = _resolve_project_contact(db, owner, already)

        if outcome is None:
            continue
        contact, distance_km, link_id = outcome
        office = db.get(Office, contact.office_id) if contact.office_id else None
        resolved.append({
            "role": role,
            "contact": contact,
            "link_id": link_id,
            "office": office,
            # None when the role doesn't widen geographically or the cities
            # can't be measured; 0.0 means "your own office".
            "distance_km": distance_km,
            # Only a fallback for the roles that widen by distance — see
            # PLACE_BASED_ROLES.
            "is_remote_fallback": role in PLACE_BASED_ROLES and bool(distance_km),
        })
        already.add(contact.id)

    return resolved
