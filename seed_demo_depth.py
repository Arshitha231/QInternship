"""Broaden the synthetic dataset so three features look real in a demo.

Operator tool, same family as seed_continuity.py / seed_it_division.py:
non-destructive, additive, idempotent, run against an already-seeded
directory. seed.py deletes every employee on each run, so nothing here is
baked into the main reseed.

Three independent sections, each safe to re-run:

  1. CLIENT PORTFOLIO   Consulting-style engagements across Engineering and
                        Finance, staffed from real existing employees.
                        seed_continuity.py already creates three (Meridian
                        Health, Solstice Retail, Arcadia Logistics) sized
                        for its four exposure demo cases; three is not a
                        portfolio. This adds five more with real, specific
                        descriptions -- which also widens mode 3's corpus
                        (app/project_search.py), since every one of them
                        gets embedded.

  2. WORK AUTHORIZATION Currently 12 records over FOUR employees, all built
                        for continuity's demo cases. Four people out of 530
                        does not demonstrate a work-authorization feature;
                        it demonstrates four rows. This gives a realistic
                        slice of the workforce a real authorization history
                        -- citizens and permanent residents included, not
                        only visa holders, because a table containing
                        nothing but CPT/OPT/H-1B rows is its own kind of
                        unrealistic.

  3. LINKEDIN URLS      Every active employee gets a plausible profile URL
                        derived from their name. Requires migration
                        d5b81c4e77a3 (employees.linkedin_profile).

RUN ORDER: after seed.py / seed_it_division.py / seed_continuity.py, and
BEFORE build_project_embeddings.py -- section 1 adds projects whose
descriptions need embedding, and build_project_embeddings.py only embeds
what exists when it runs.

Every value here is invented. No real person, client or LinkedIn account is
referenced: client names are fictional, and the URL slugs point at
linkedin.com paths that are synthetic by construction (see _linkedin_url).
"""
from __future__ import annotations

import random
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from sqlalchemy import select

load_dotenv()

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Employee, EmployeeProject, OrgUnit, Project, WorkAuthorizationRecord,
)
from app.models.enums import (  # noqa: E402
    ProjectClassification, ProjectType, VerificationStatus, WorkAuthorizationType,
)

TODAY = date.today()

# Deterministic: re-running picks the same people and the same dates, so a
# second run is a genuine no-op rather than a second, different dataset.
RNG = random.Random(20260816)


# ---------------------------------------------------------------------------
# 1. Client portfolio
# ---------------------------------------------------------------------------
# (name, owning department, description). Descriptions are specific for the
# same reason migration 9f31c0d7ae64 rewrote eight existing ones: generic
# text embeds to nothing useful, so a vague client engagement would be dead
# weight in mode 3's corpus.

CLIENT_ENGAGEMENTS: list[tuple[str, str, str]] = [
    (
        "Northwind Bank — Core Ledger Migration",
        "Finance Operations",
        "Moving a retail bank off a mainframe general ledger onto a distributed core, with "
        "dual-running reconciliation for the whole transition so every posting is proved "
        "against both systems before cutover. The hard part is intraday balance drift: the "
        "legacy system settles in overnight batch and the new one posts in real time, so "
        "'correct' means different things until end of day. A rounding difference in "
        "multi-currency conversion took three weeks to trace and forced a restatement of "
        "the pilot month's reconciliation."
    ),
    (
        "Northwind Bank — Regulatory Reporting Automation",
        "Finance Operations",
        "Automating the quarterly regulatory return pack that finance analysts previously "
        "assembled by hand from six source systems, after a filing deadline was missed when "
        "one spreadsheet owner was on leave. Every figure now traces to a query with a named "
        "owner and a freshness check. Reviewers pushed back hard on the first version because "
        "it produced numbers nobody could explain without opening the code."
    ),
    (
        "Helio Energy — Grid Telemetry Platform",
        "Data & AI",
        "Ingesting high-frequency sensor telemetry from substations and solar sites into a "
        "streaming platform that flags anomalies before they become outages. Late and "
        "out-of-order events are the whole problem: readings arrive minutes apart from sites "
        "with intermittent connectivity, and naive windowing silently dropped them. Watermarking "
        "and a replayable event log fixed it, at the cost of a much larger storage footprint "
        "than the original estimate."
    ),
    (
        "Cobalt Manufacturing — Warehouse Systems Integration",
        "Platform Engineering",
        "Connecting a manufacturer's warehouse management system to their ERP and shipping "
        "carriers over a single integration layer, replacing nightly CSV drops that made stock "
        "counts a day stale. Idempotent message handling matters more than throughput here: a "
        "replayed shipment event must not double-decrement inventory. An early release did "
        "exactly that during a carrier outage retry storm, and the stock correction took a "
        "weekend."
    ),
    (
        "Vantage Insurance — Claims Intake Modernization",
        "Quality Engineering",
        "Replacing a paper-and-email claims intake process with a validated digital workflow, "
        "including document capture and automated routing to the right adjuster. Test coverage "
        "was the sticking point: the legacy process had no specification, so behaviour had to "
        "be characterised from historical claims before anything could be safely replaced. "
        "Several 'bugs' found during testing turned out to be deliberate undocumented "
        "exceptions the business still relied on."
    ),
]

# How many people to staff on each engagement, and the role mix. Small teams
# on purpose -- mode 3 hops from a matched project to its whole membership,
# so a 40-person client project would flood every result it matched.
ENGAGEMENT_ROLES = ["Lead", "Engineer", "Engineer", "Analyst", "Reviewer"]


def _dept_unit(session, name: str) -> OrgUnit | None:
    return session.execute(select(OrgUnit).where(OrgUnit.name == name)).scalars().first()


def _unit_subtree_ids(session, root: OrgUnit) -> list[int]:
    """Every unit id at or below `root`. Employees sit on leaf teams, so a
    department-level unit has no direct members of its own."""
    ids = [root.id]
    frontier = [root.id]
    while frontier:
        children = session.execute(
            select(OrgUnit.id).where(OrgUnit.parent_id.in_(frontier))
        ).scalars().all()
        children = [c for c in children if c not in ids]
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


def _staff_pool(session, dept_name: str, exclude: set[str]) -> list[Employee]:
    unit = _dept_unit(session, dept_name)
    if unit is None:
        return []
    unit_ids = _unit_subtree_ids(session, unit)
    people = session.execute(
        select(Employee).where(
            Employee.org_unit_id.in_(unit_ids),
            # `== True` not `.is_(True)`: the latter renders as `IS 1`, which
            # Azure SQL rejects. tests/test_sql_portability.py enforces this.
            Employee.is_active == True,  # noqa: E712
        ).order_by(Employee.id)
    ).scalars().all()
    return [p for p in people if p.id not in exclude]


def seed_client_portfolio(session) -> dict[str, int]:
    created, staffed, skipped = 0, 0, 0
    used: set[str] = set()

    for name, dept_name, description in CLIENT_ENGAGEMENTS:
        existing = session.execute(select(Project).where(Project.name == name)).scalars().first()
        if existing is not None:
            skipped += 1
            continue

        pool = _staff_pool(session, dept_name, used)
        if len(pool) < len(ENGAGEMENT_ROLES):
            print(f"  ! skipping {name!r}: {dept_name} has too few people ({len(pool)})")
            continue

        team = RNG.sample(pool, len(ENGAGEMENT_ROLES))
        owner = team[0]
        unit = _dept_unit(session, dept_name)

        project = Project(
            name=name, type=ProjectType.project, description=description,
            owning_unit_id=unit.id, owner_id=owner.id,
            classification=ProjectClassification.internal, is_client_engagement=True,
        )
        session.add(project)
        session.flush()
        created += 1

        for person, role in zip(team, ENGAGEMENT_ROLES):
            # Staggered starts, all open-ended: these are live engagements,
            # which is what makes them count as current continuity exposure.
            session.add(EmployeeProject(
                employee_id=person.id, project_id=project.id, role=role,
                start_date=TODAY - timedelta(days=RNG.randint(90, 540)), end_date=None,
            ))
            used.add(person.id)
            staffed += 1

    session.commit()
    return {"created": created, "staffed": staffed, "skipped": skipped}


# ---------------------------------------------------------------------------
# 2. Work authorization
# ---------------------------------------------------------------------------
# A realistic mix, not an all-visa table. Weights are illustrative for a
# consulting firm with a large India delivery presence and a US client base;
# they are invented, not sourced from real workforce statistics.

AUTH_MIX: list[tuple[WorkAuthorizationType, int]] = [
    (WorkAuthorizationType.citizen, 46),
    (WorkAuthorizationType.permanent_resident, 14),
    (WorkAuthorizationType.h1b, 16),
    (WorkAuthorizationType.opt, 8),
    (WorkAuthorizationType.stem_opt, 7),
    (WorkAuthorizationType.cpt, 4),
    (WorkAuthorizationType.l1, 4),
    (WorkAuthorizationType.other, 1),
]

# Statuses that expire and therefore carry an HR review date. Citizens and
# permanent residents get neither an expiry nor a review -- giving them one
# would make the review queue meaningless.
TIME_LIMITED = {
    WorkAuthorizationType.h1b, WorkAuthorizationType.opt, WorkAuthorizationType.stem_opt,
    WorkAuthorizationType.cpt, WorkAuthorizationType.l1, WorkAuthorizationType.other,
}

# Share of the active workforce given a record. Not everyone: a directory
# where every single person has a filed authorization record is as
# unrealistic as one where four do.
COVERAGE = 0.35


def _find_verifier(session) -> Employee | None:
    unit = _dept_unit(session, "HR Operations") or _dept_unit(session, "People")
    if unit is None:
        return None
    ids = _unit_subtree_ids(session, unit)
    return session.execute(
        select(Employee).where(Employee.org_unit_id.in_(ids), Employee.is_active == True)  # noqa: E712
        .order_by(Employee.id)
    ).scalars().first()


def seed_work_authorization(session) -> dict[str, int]:
    verifier = _find_verifier(session)
    already = {
        r for r in session.execute(select(WorkAuthorizationRecord.employee_id)).scalars().all()
    }
    everyone = session.execute(
        select(Employee).where(Employee.is_active == True).order_by(Employee.id)  # noqa: E712
    ).scalars().all()
    candidates = [e for e in everyone if e.id not in already]
    target = int(len(everyone) * COVERAGE) - len(already)
    if target <= 0:
        return {"created": 0, "skipped": len(already), "review_soon": 0}

    chosen = RNG.sample(candidates, min(target, len(candidates)))
    types = [t for t, w in AUTH_MIX for _ in range(w)]

    created, review_soon = 0, 0
    for person in chosen:
        auth = RNG.choice(types)
        effective_from = TODAY - timedelta(days=RNG.randint(200, 1400))

        if auth in TIME_LIMITED:
            # Spread expiries so the review queue has near-term entries to
            # show without every visa in the company expiring this quarter.
            effective_until = TODAY + timedelta(days=RNG.randint(20, 900))
            review = effective_until - timedelta(days=RNG.randint(30, 120))
            if review < TODAY:
                review = TODAY + timedelta(days=RNG.randint(5, 45))
            if (review - TODAY).days <= 90:
                review_soon += 1
        else:
            effective_until = None
            review = None

        session.add(WorkAuthorizationRecord(
            employee_id=person.id, authorization_type=auth,
            effective_from=effective_from, effective_until=effective_until,
            next_hr_review_date=review, verification_status=VerificationStatus.verified,
            verified_at=datetime.combine(effective_from, datetime.min.time()),
            verified_by=verifier.id if verifier else None, is_current=True,
        ))
        created += 1

    session.commit()
    return {"created": created, "skipped": len(already), "review_soon": review_soon}


# ---------------------------------------------------------------------------
# 3. LinkedIn URLs
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """ASCII slug from a display name. The dataset has names with accents and
    non-Latin characters; NFKD-folding them keeps the URL plausible instead
    of emitting percent-escapes."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug or "member"


def _linkedin_url(employee: Employee, taken: set[str]) -> str:
    """Synthetic by construction: every slug carries a numeric suffix derived
    from the employee's own uuid, so these cannot collide with a real
    linkedin.com/in/ vanity URL belonging to an actual person."""
    base = _slugify(employee.preferred_name or employee.full_name)
    suffix = employee.id.replace("-", "")[:6]
    slug = f"{base}-{suffix}"
    while slug in taken:
        suffix = f"{suffix}x"
        slug = f"{base}-{suffix}"
    taken.add(slug)
    return f"https://www.linkedin.com/in/{slug}"


def seed_linkedin(session) -> dict[str, int]:
    everyone = session.execute(
        select(Employee).where(Employee.is_active == True).order_by(Employee.id)  # noqa: E712
    ).scalars().all()
    taken: set[str] = set()
    created, skipped = 0, 0
    for person in everyone:
        if person.linkedin_profile:
            skipped += 1
            taken.add(person.linkedin_profile.rsplit("/", 1)[-1])
            continue
        person.linkedin_profile = _linkedin_url(person, taken)
        created += 1
    session.commit()
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------

def main() -> int:
    session = SessionLocal()
    try:
        if not hasattr(Employee, "linkedin_profile"):
            print("Employee has no linkedin_profile attribute — is this the right code version?")
            return 2

        print("1. client portfolio")
        stats = seed_client_portfolio(session)
        print(f"   engagements created: {stats['created']}  "
              f"(already present: {stats['skipped']})  people staffed: {stats['staffed']}")

        print("2. work authorization")
        stats = seed_work_authorization(session)
        print(f"   records created: {stats['created']}  "
              f"(employees already covered: {stats['skipped']})  "
              f"reviews due within 90 days: {stats['review_soon']}")

        print("3. linkedin urls")
        stats = seed_linkedin(session)
        print(f"   urls set: {stats['created']}  (already had one: {stats['skipped']})")

        total_ce = session.execute(
            select(Project).where(Project.is_client_engagement == True)  # noqa: E712
        ).scalars().all()
        covered = len(set(session.execute(select(WorkAuthorizationRecord.employee_id)).scalars().all()))
        active = session.query(Employee).filter(Employee.is_active == True).count()  # noqa: E712
        print(f"\nnow: {len(total_ce)} client engagements, "
              f"{covered}/{active} employees with a work-authorization record")
        print("\nRe-run build_project_embeddings.py — the new engagements are not embedded yet.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
