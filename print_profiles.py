"""Print build_profile_text() output for 20 sample employees, so the search
index's actual content can be eyeballed before Azure credentials exist.

Deliberately includes a few hand-picked edge cases (no bio, no office, a
confidential-project member, a preferred name) ahead of a random
cross-section, rather than a plain LIMIT 20 — a straight limit might not
happen to land on any of the gaps that matter to inspect.
"""
import random

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Employee, EmployeeProject, Project
from app.models.enums import ProjectClassification
from app.search_index import build_profile_text

SAMPLE_SIZE = 20
SAMPLE_SEED = 7


def _pick_first(pool: list[Employee], picks: dict[str, Employee], predicate, label: str) -> None:
    for emp in pool:
        if emp.id not in picks and predicate(emp):
            picks[emp.id] = emp
            return
    print(f"(no employee matched edge case: {label})")


def pick_samples(db) -> list[Employee]:
    pool = db.execute(select(Employee).where(Employee.is_active.is_(True))).scalars().all()
    picks: dict[str, Employee] = {}

    _pick_first(pool, picks, lambda e: e.bio is None, "no bio")
    _pick_first(pool, picks, lambda e: e.office_id is None, "no office / remote")
    _pick_first(pool, picks, lambda e: e.preferred_name is not None, "has a preferred name")
    _pick_first(pool, picks, lambda e: e.bio is not None and e.office_id is not None, "fully complete profile")

    # A confidential-project member, to show the project name is genuinely
    # absent from their indexed text, not just filtered from a UI later.
    conf_member_id = db.execute(
        select(EmployeeProject.employee_id)
        .join(Project, EmployeeProject.project_id == Project.id)
        .where(Project.classification == ProjectClassification.confidential)
        .limit(1)
    ).scalar()
    by_id = {e.id: e for e in pool}
    if conf_member_id and conf_member_id not in picks and conf_member_id in by_id:
        picks[conf_member_id] = by_id[conf_member_id]

    rng = random.Random(SAMPLE_SEED)
    remaining = [e for e in pool if e.id not in picks]
    rng.shuffle(remaining)
    for emp in remaining:
        if len(picks) >= SAMPLE_SIZE:
            break
        picks[emp.id] = emp

    return list(picks.values())[:SAMPLE_SIZE]


def main() -> None:
    db = SessionLocal()
    try:
        samples = pick_samples(db)
        for i, emp in enumerate(samples, 1):
            print(f"\n{'=' * 78}")
            print(f"[{i}/{len(samples)}] {emp.full_name}  (id={emp.id})")
            print("=" * 78)
            print(build_profile_text(db, emp))
    finally:
        db.close()


if __name__ == "__main__":
    main()
