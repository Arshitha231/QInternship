"""Declare required skills for active projects, so coverage can be judged.

Operator tool, same family as seed_continuity.py / seed_demo_depth.py /
seed_training.py: non-destructive, additive, idempotent, run against an
already-seeded directory. seed.py deletes every employee on each run, so
nothing here is baked into the main reseed.

WHY THIS EXISTS
---------------
project_skill_requirements had three rows — the ones seed_continuity.py
files for its own demo cases. Three rows across 118 active projects means
the dashboards' project-coverage section can judge three projects and has
to answer "not recorded" for the other 115, and it means skill DEMAND is
almost entirely inferred (read off what a project's current members happen
to know) rather than declared.

Both of those are correct behaviour on the data as it stood — app/analytics.py
deliberately refuses to invent a coverage verdict from inferred
requirements, because checking whether a project's members hold the skills
inferred from its members' skills is circular and reports 100% everywhere.
The fix is more declared data, not looser analysis.

HOW THE REQUIREMENTS ARE DERIVED
--------------------------------
Most of each project's requirements come from what its CURRENT members can
do, and a minority from a skill only PAST members held.

The mix is the point. Deriving purely from current members would leave
every project fully covered and the coverage section would be a wall of
green telling you nothing. Deriving purely from all-time membership — the
first thing this script did — went the other way: on a portfolio where most
projects have one or two current members and a long history of past ones,
70% of projects came out at 0% coverage, which reads as a broken metric
rather than as a finding.

So: two or three skills the current team demonstrably holds, plus, on every
third project that has one available, a skill whose holders have all rolled
off. That last one is a genuine staffing gap with a real cause — the person
who covered it left the project — and it lands on roughly a third of the
portfolio, which is a believable rate for a real one.

Minimum level follows the same evidence: Expert if a member holds the skill
at Expert, Working otherwise.

Every requirement written here is synthetic and derived. It is not a
statement by a project owner about what their project needs — which is what
the table is FOR (see app/project_skills.py, where real owners record real
requirements). This is demo scaffolding, and the dashboards label demand
sourced from it "declared" because that is what the row says; if that
matters for a given demo, run this against a copy.

    python seed_project_requirements.py

Idempotent: projects that already have declared requirements are left
exactly as they are, so seed_continuity.py's hand-written rows survive a run
and a second run of this script changes nothing.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    EmployeeProject, EmployeeSkill, Project, ProjectSkillRequirement, Skill,
)
from app.models.enums import SkillLevel

# How many skills to declare per project. Three or four is what a real
# required-skills list looks like — a list of fifteen is a description of
# the team, not a requirement, and would make every project show gaps.
MIN_SKILLS = 2
MAX_SKILLS = 4

# A lapsed skill has to have been held by at least this many all-time
# members to count as something the project depended on. One person knowing
# something once is not evidence the work required it.
MIN_HOLDERS = 2

# Current-team skills need a lower bar: plenty of active projects have one
# or two people on them, and requiring two holders would leave those
# projects undeclared for a reason that is about team size, not evidence.
MIN_CURRENT_HOLDERS = 1

# One project in this many gets a lapsed skill added, giving the portfolio
# a believable share of genuinely uncovered requirements rather than all or
# none.
GAP_EVERY = 3


def main() -> int:
    session = SessionLocal()
    try:
        # Projects that are active (somebody currently assigned) and have no
        # declared requirements yet. The second half is what makes this
        # idempotent and what protects seed_continuity.py's rows.
        active_ids = {
            row.project_id for row in session.execute(
                select(EmployeeProject.project_id).where(EmployeeProject.end_date.is_(None))
            ).all()
        }
        already_declared = {
            row.project_id for row in session.execute(
                select(ProjectSkillRequirement.project_id).distinct()
            ).all()
        }
        targets = sorted(active_ids - already_declared)
        if not targets:
            print("Every active project already has declared required skills. Nothing to do.")
            return 0

        projects = {
            p.id: p for p in session.execute(
                select(Project).where(Project.id.in_(targets))
            ).scalars().all()
        }

        # Two membership views per project. Current membership is
        # end_date IS NULL, the same definition of "current" used
        # everywhere else in this codebase; all-time is every row.
        all_time: dict[int, set[str]] = defaultdict(set)
        current: dict[int, set[str]] = defaultdict(set)
        for row in session.execute(
            select(EmployeeProject.project_id, EmployeeProject.employee_id, EmployeeProject.end_date)
            .where(EmployeeProject.project_id.in_(targets))
        ).all():
            all_time[row.project_id].add(row.employee_id)
            if row.end_date is None:
                current[row.project_id].add(row.employee_id)

        everyone = {e for ids in all_time.values() for e in ids}
        # Technical and domain skills only. A project requiring "English"
        # because most of its members speak it is noise, and it would swamp
        # the top-N cut on every project in the company.
        levels: dict[str, dict[int, SkillLevel]] = defaultdict(dict)
        if everyone:
            for row in session.execute(
                select(EmployeeSkill.employee_id, EmployeeSkill.skill_id, EmployeeSkill.level, Skill.category)
                .join(Skill, EmployeeSkill.skill_id == Skill.id)
                .where(
                    EmployeeSkill.employee_id.in_(everyone),
                    EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
                    Skill.category != "language",
                )
            ).all():
                levels[row.employee_id][row.skill_id] = row.level

        def _counts(member_ids: set[str]) -> tuple[Counter[int], set[int]]:
            counts: Counter[int] = Counter()
            experts: set[int] = set()
            for member in member_ids:
                for skill_id, level in levels.get(member, {}).items():
                    counts[skill_id] += 1
                    if level is SkillLevel.expert:
                        experts.add(skill_id)
            return counts, experts

        written = 0
        skipped_thin = 0
        gapped = 0
        for project_id in targets:
            current_counts, current_experts = _counts(current[project_id])
            all_counts, _all_experts = _counts(all_time[project_id])

            # Skills nobody currently assigned holds — the rolled-off
            # capability that becomes a real, explicable gap.
            lapsed = [
                sid for sid, n in all_counts.most_common()
                if n >= MIN_HOLDERS and sid not in current_counts
            ]

            chosen = [sid for sid, n in current_counts.most_common(MAX_SKILLS - 1)
                      if n >= MIN_CURRENT_HOLDERS]
            # Deterministic on the project id rather than random, so two
            # runs against the same database produce the same portfolio and
            # a demo is reproducible.
            if lapsed and project_id % GAP_EVERY == 0:
                chosen.append(lapsed[0])
                gapped += 1

            if len(chosen) < MIN_SKILLS:
                # Not enough evidence to say what this project depends on.
                # Leaving it undeclared is the honest outcome — the
                # dashboard renders "no required skills recorded", which is
                # true, rather than a requirement nothing supports.
                skipped_thin += 1
                continue

            for skill_id in chosen:
                session.add(ProjectSkillRequirement(
                    project_id=project_id, skill_id=skill_id,
                    # Expert only where a CURRENT member actually holds it
                    # at Expert. Promoting on all-time evidence set a bar the
                    # present team could not clear on a skill it does hold —
                    # which reads as a staffing gap when the truth is that
                    # the person who was Expert at it has moved on. Lapsed
                    # skills are how this script expresses that, deliberately
                    # and countably; the minimum level should not do it a
                    # second time by accident.
                    minimum_level=(SkillLevel.expert if skill_id in current_experts
                                   else SkillLevel.working),
                ))
                written += 1

        session.commit()
        print(f"Declared {written} required skills across "
              f"{len(targets) - skipped_thin} active projects.")
        print(f"{gapped} of them include a skill whose holders have all rolled off — "
              f"a real coverage gap with a real cause.")
        if skipped_thin:
            print(f"{skipped_thin} projects left undeclared — too little skill history "
                  f"among their members to derive a requirement honestly.")
        print(f"{len(already_declared)} projects already had requirements and were untouched.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
