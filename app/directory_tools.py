"""find_project_owner, find_mentor, skill_gap, skill_scarcity — the four
tool functions step 9 adds on top of find_people/get_person/get_org_chain.

Same rules as everywhere else: permission filtering happens in Python
after retrieval, restricted records are simply absent (never a 403), and
every call writes an audit_log row regardless of outcome. None of these
rank people by anything except the stated mechanical criteria — no
function here scores "best candidate," since that depends on performance
and ambition, which aren't in the directory.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, EmployeeProject, EmployeeSkill, Project, Skill
from app.models.enums import AvailabilityStatus, ProjectClassification, SkillLevel
from rapidfuzz import fuzz, process

from app.org_chart import FUZZY_MATCH_THRESHOLD, MAX_DEPTH
from app.org_chart import _traverse as org_traverse
from app.people import MAX_SEARCH_RESULTS, resolve_skill
from app.permissions import can_see_confidential_project, is_record_visible
from app.schemas import AmbiguousProjectMatch, MentorCandidate, ProjectOwnerResult, SkillGapItem, SkillScarcityItem


def _tenure_days(emp: Employee) -> int:
    return (date.today() - emp.hire_date).days


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, query_text: str,
                  result_count: int, fields_returned: set[str]) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(sorted(fields_returned)), timestamp=datetime.now(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# find_project_owner(name) — covers project | system | function | policy
# ---------------------------------------------------------------------------

# Separators that carry no meaning in a project name but break literal
# substring matching. Every client engagement is named "Client — Project",
# and nobody types the em dash: "who owns the Northwind Bank core ledger
# migration" was a total miss against "Northwind Bank — Core Ledger
# Migration" purely because of one character.
_NAME_NOISE = re.compile(r"[—–‒\-_/:,.()\[\]'\"]+")

# Words too common across this directory's project names to carry signal.
# "the Migration project" should not resolve to whichever of the 16
# projects containing "Migration" happens to sort first. Same reasoning as
# app.project_search's document-frequency ceiling, hardcoded rather than
# computed because the vocabulary is small and stable.
_NAME_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "to", "our", "project", "programme",
    "program", "system", "initiative", "cycle", "team",
})


def _normalise_project_name(text: str) -> str:
    """Fold separators to spaces and collapse whitespace, so formatting
    differences stop mattering. Case is handled by the caller lowering."""
    return " ".join(_NAME_NOISE.sub(" ", text).lower().split())


def _name_tokens(text: str) -> set[str]:
    return {w for w in _normalise_project_name(text).split() if w not in _NAME_STOPWORDS}


def resolve_project_name(db: Session, name: str) -> list[Project]:
    """Every project a name query plausibly refers to, best first.

    Three tiers, same shape as app.org_chart.resolve_person_name and
    app.vocabulary._snap_one -- not a fourth matching tolerance invented
    independently:

      1. normalised exact, or normalised substring either way round
      2. token overlap: every meaningful query word appears in the name
      3. rapidfuzz over normalised names, above the shared threshold

    Returns a LIST, deliberately. The previous implementation took
    .first() off an ILIKE and answered confidently; the caller now decides
    what to do when a query genuinely names several projects.
    """
    projects = db.query(Project).order_by(Project.name).all()
    if not projects:
        return []

    query_norm = _normalise_project_name(name)
    if not query_norm:
        return []
    by_norm = {p: _normalise_project_name(p.name) for p in projects}

    exact = [p for p, norm in by_norm.items() if norm == query_norm]
    if exact:
        return exact

    # Substring in either direction: the query may be shorter than the full
    # title ("northwind bank core ledger migration") or longer, if the user
    # pasted a sentence fragment around it.
    substring = [p for p, norm in by_norm.items() if query_norm in norm or norm in query_norm]
    if substring:
        return substring

    query_tokens = _name_tokens(name)
    if query_tokens:
        overlap = [p for p in projects if query_tokens <= _name_tokens(p.name)]
        if overlap:
            return overlap

    match = process.extractOne(
        query_norm, list(by_norm.values()), scorer=fuzz.WRatio, score_cutoff=FUZZY_MATCH_THRESHOLD)
    if match:
        matched_norm = match[0]
        return [p for p, norm in by_norm.items() if norm == matched_norm]
    return []


def find_project_owner(
    db: Session, caller: AuthenticatedUser, name: str,
) -> ProjectOwnerResult | AmbiguousProjectMatch | None:
    result: ProjectOwnerResult | AmbiguousProjectMatch | None = None
    try:
        candidates = resolve_project_name(db, name)

        # Confidential projects are filtered BEFORE the ambiguity check, not
        # after: otherwise "how many matched" would itself leak their
        # existence — two matches collapsing to one is observable, and an
        # ambiguity message listing a confidential project name would be a
        # direct disclosure. Same reason app.project_search never embeds them.
        visible = [
            p for p in candidates
            if p.classification != ProjectClassification.confidential
            or can_see_confidential_project(db, caller, p.id)
        ]

        if len(visible) > 1:
            result = AmbiguousProjectMatch(query=name, matches=[p.name for p in visible[:MAX_SEARCH_RESULTS]])
            return result

        project = visible[0] if visible else None
        if project is None:
            return None

        # Redundant with the `visible` filter above, kept as defence in
        # depth -- that filter is the guarantee, this is what catches a
        # future edit to it. Confidential projects return no result, never
        # an access-denied message, same rule as any restricted record.
        if (project.classification == ProjectClassification.confidential
                and not can_see_confidential_project(db, caller, project.id)):
            return None

        owner = db.get(Employee, project.owner_id)
        if owner is None or not owner.is_active or not is_record_visible(caller, owner):
            return None

        result = ProjectOwnerResult(
            project_name=project.name, project_type=project.type.value,
            classification=project.classification.value, owner_id=owner.id, owner_name=owner.full_name,
        )
        return result
    finally:
        if isinstance(result, AmbiguousProjectMatch):
            # result_count is how many projects matched, not 1 -- an
            # ambiguous answer that audits as a single hit would be
            # indistinguishable from a resolved one afterwards.
            _write_audit(db, caller, "find_project_owner", f"name={name}",
                         len(result.matches), {"query", "matches"})
        else:
            fields = ({"project_name", "project_type", "classification", "owner_id", "owner_name"}
                      if result else set())
            _write_audit(db, caller, "find_project_owner", f"name={name}", 1 if result else 0, fields)


# ---------------------------------------------------------------------------
# find_mentor(skill, caller_id)
#
# Ranking (fixed priority order): Expert level -> available -> caller's own
# reporting chain ranked LOWER (never excluded) -> shared (non-confidential)
# project history -> tenure band as tiebreaker. Every result carries why it
# ranked where it did.
# ---------------------------------------------------------------------------

def find_mentor(db: Session, caller: AuthenticatedUser, skill: str, caller_id: str) -> list[MentorCandidate]:
    results: list[MentorCandidate] = []
    try:
        resolved = resolve_skill(db, skill)
        if resolved is None:
            return []

        rows = (
            db.query(EmployeeSkill, Employee)
            .join(Employee, EmployeeSkill.employee_id == Employee.id)
            .filter(
                EmployeeSkill.skill_id == resolved.id,
                EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
                # `.is_(True)` renders as `IS 1` on SQL Server, which
                # T-SQL rejects (IS only works with NULL) -- `== True`
                # renders as `= 1` everywhere, including here.
                Employee.is_active == True,
                Employee.id != caller_id,
            )
            .all()
        )
        candidates = [(es, e) for es, e in rows if is_record_visible(caller, e)]
        if not candidates:
            return []

        # Caller's reporting chain, both directions — de-prioritized, not excluded.
        chain_ids = {i for i, _d in org_traverse(db, caller_id, "up", MAX_DEPTH)}
        chain_ids |= {i for i, _d in org_traverse(db, caller_id, "down", MAX_DEPTH)}

        caller_project_ids = {
            pid for (pid,) in db.query(EmployeeProject.project_id)
            .join(Project, EmployeeProject.project_id == Project.id)
            .filter(EmployeeProject.employee_id == caller_id,
                    Project.classification != ProjectClassification.confidential)
        }

        def shared_project_count(emp_id: str) -> int:
            their_ids = {
                pid for (pid,) in db.query(EmployeeProject.project_id)
                .join(Project, EmployeeProject.project_id == Project.id)
                .filter(EmployeeProject.employee_id == emp_id,
                        Project.classification != ProjectClassification.confidential)
            }
            return len(caller_project_ids & their_ids)

        scored = []
        for es, e in candidates:
            is_expert = es.level == SkillLevel.expert
            is_available = e.availability_status == AvailabilityStatus.available
            in_chain = e.id in chain_ids
            shared = shared_project_count(e.id)
            tenure = _tenure_days(e)

            sort_key = (0 if is_expert else 1, 0 if is_available else 1, 1 if in_chain else 0, -shared, -tenure)

            reasons = [f"{resolved.name} at {es.level.value} level"]
            reasons.append("available" if is_available else f"currently {e.availability_status.value}")
            if in_chain:
                reasons.append("in your reporting chain")
            if shared:
                reasons.append(f"{shared} shared project{'s' if shared != 1 else ''}")

            scored.append((sort_key, MentorCandidate(
                id=e.id, full_name=e.full_name, job_title=e.job_title,
                level=es.level.value, reason=", ".join(reasons),
            )))

        scored.sort(key=lambda pair: pair[0])
        results = [candidate for _key, candidate in scored][:MAX_SEARCH_RESULTS]
        return results
    finally:
        fields = {"id", "full_name", "job_title", "level", "reason"} if results else set()
        _write_audit(db, caller, "find_mentor", f"skill={skill};caller_id={caller_id}", len(results), fields)


# ---------------------------------------------------------------------------
# skill_gap(required_skills[]) — coverage for a specific named list, e.g.
# "do we have what a project needs".
# ---------------------------------------------------------------------------

def skill_gap(db: Session, caller: AuthenticatedUser, required_skills: list[str]) -> list[SkillGapItem]:
    items: list[SkillGapItem] = []
    try:
        for name in required_skills:
            resolved = resolve_skill(db, name)
            if resolved is None:
                items.append(SkillGapItem(skill=name, recognized=False, expert_count=0,
                                          working_count=0, learning_count=0, gap=True))
                continue
            visible_rows = _visible_skill_holders(db, caller, resolved.id)
            expert = sum(1 for es, _e in visible_rows if es.level == SkillLevel.expert)
            working = sum(1 for es, _e in visible_rows if es.level == SkillLevel.working)
            learning = sum(1 for es, _e in visible_rows if es.level == SkillLevel.learning)
            items.append(SkillGapItem(
                skill=resolved.name, recognized=True, expert_count=expert,
                working_count=working, learning_count=learning, gap=(expert + working) == 0,
            ))
        return items
    finally:
        fields = {"skill", "recognized", "expert_count", "working_count", "learning_count", "gap"}
        _write_audit(db, caller, "skill_gap", json.dumps(required_skills), len(items), fields)


# ---------------------------------------------------------------------------
# skill_scarcity(skill?) — one named skill's coverage, or (no argument) the
# org's scarcest skills overall.
# ---------------------------------------------------------------------------

def skill_scarcity(db: Session, caller: AuthenticatedUser, skill: str | None = None) -> list[SkillScarcityItem]:
    items: list[SkillScarcityItem] = []
    try:
        if skill:
            resolved = resolve_skill(db, skill)
            skills_to_check = [resolved] if resolved else []
        else:
            # Org-wide scan: canonical skills only — alias rows (SRE, K8s,
            # ...) are never assigned to anyone, so they'd always show as
            # 100% scarce, which isn't a real signal.
            skills_to_check = db.query(Skill).filter(Skill.canonical_id.is_(None)).all()

        for sk in skills_to_check:
            visible_rows = _visible_skill_holders(db, caller, sk.id)
            expert = sum(1 for es, _e in visible_rows if es.level == SkillLevel.expert)
            working = sum(1 for es, _e in visible_rows if es.level == SkillLevel.working)
            learning = sum(1 for es, _e in visible_rows if es.level == SkillLevel.learning)
            items.append(SkillScarcityItem(skill=sk.name, expert_count=expert, working_count=working,
                                           learning_count=learning, capable_count=expert + working))

        if not skill:
            items.sort(key=lambda i: i.capable_count)
            items = items[:MAX_SEARCH_RESULTS * 2]  # a short "here's where we're thin" list, not the whole catalog
        return items
    finally:
        fields = {"skill", "expert_count", "working_count", "learning_count", "capable_count"}
        _write_audit(db, caller, "skill_scarcity", skill or "(org-wide scan)", len(items), fields)


def _visible_skill_holders(db: Session, caller: AuthenticatedUser, skill_id: int) -> list[tuple[EmployeeSkill, Employee]]:
    rows = (
        db.query(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .filter(EmployeeSkill.skill_id == skill_id, Employee.is_active == True)
        .all()
    )
    return [(es, e) for es, e in rows if is_record_visible(caller, e)]
