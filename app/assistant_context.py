"""Cross-surface context between the search and PRD assistants -- extracted
facts only, never a shared transcript.

recent_facts() derives a small set of ConversationFact rows from the
caller's most recent conversation on the OTHER surface, reading only
`tool_call` + `arguments` off each stored turn -- never `assistant_text`
(model-written prose) and never document/note prose, which never enters
`arguments` in the first place (see app.tool_calling._UNTRUSTED_FREE_TEXT_KEYS
and app.prd_extraction's module docstring). This is what makes the
injection posture structural rather than promised: an uploaded PRD's prose
has no path into the search assistant's prompt, because nothing this
module reads ever contains it.

Every fact naming a project or a person is RE-CHECKED against the current
database before being returned -- the same freshness guarantee
_history_messages() gives a replayed tool call, extended to facts read
from a DIFFERENT conversation. A project reclassified confidential or a
person deactivated since the turn happened is simply absent, not merely
unlabelled.

No second write path, no fact table: everything here is derived on read
from AssistantTurn rows the persistence layer already writes.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.assistant_conversations import get_most_recent_conversation, load_history
from app.auth import AuthenticatedUser
from app.directory_tools import resolve_project_name, skill_gap
from app.models import Employee, Project, ProjectRequirementNote, ProjectSkillRequirement
from app.permissions import ViewMode, effective_role, is_record_visible
from app.project_skills import visible_project
from app.schemas import ConversationFact, FollowUpSuggestion

RECENT_FACTS_TURN_LIMIT = 5


def recent_facts(
    db: Session, caller: AuthenticatedUser, *, other_surface: str, view_mode: ViewMode = "work",
    limit: int = RECENT_FACTS_TURN_LIMIT,
) -> list[ConversationFact]:
    """Facts derived from the caller's last `limit` turns on `other_surface`
    ("search" or "prd" -- always the surface the CURRENT call is not on).

    Guards on role before querying: PRD conversations only ever exist for
    HR, so a non-HR caller (or an HR caller in employee view mode, which
    collapses to "employee" the same way every other PRD gate does) gets an
    empty list without a lookup ever running -- not a lookup that happens
    to come back empty.

    Reads across ALL of the caller's PRD projects when other_surface="prd"
    (get_most_recent_conversation's own project_id filter is left None) --
    each fact names its own project, so there's no risk of implying the
    wrong one the way rehydrating a specific project's chat would be.
    """
    if other_surface == "prd" and effective_role(caller.role, view_mode) != "hr":
        return []

    conversation = get_most_recent_conversation(db, caller, other_surface, None)
    if conversation is None:
        return []

    turns = load_history(db, conversation)[-limit:]

    facts: list[ConversationFact] = []
    seen_project_ids: set[int] = set()
    for turn in turns:
        # assistant_text turns carry no tool call -- connective prose from
        # a conversation the OTHER assistant never validated against
        # anything, and the one thing this function must never surface.
        if turn.tool_call is None:
            continue
        facts.extend(_facts_from_turn(db, caller, view_mode, turn.tool_call, turn.arguments or {}, seen_project_ids))
    return facts


def _facts_from_turn(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
    tool_call: str, arguments: dict, seen_project_ids: set[int],
) -> list[ConversationFact]:
    if tool_call in ("find_project_owner", "get_project_requirements"):
        name = arguments.get("name")
        if isinstance(name, str) and name.strip():
            return _project_facts(db, caller, view_mode, name, seen_project_ids)
        return []

    if tool_call in ("find_mentor", "skill_scarcity", "find_people"):
        # find_people is the most common way a skill actually gets
        # searched for ("who knows Rust?") -- more common in practice
        # than find_mentor, and just as much a controlled-vocabulary value
        # (snap_tool_arguments snaps its `skill` key against real skill
        # names before it ever reaches here).
        skill = arguments.get("skill")
        if isinstance(skill, str) and skill.strip():
            return [ConversationFact(kind="skill_discussed", label=skill, ref_type="skill")]
        return []

    if tool_call == "skill_gap":
        skills = arguments.get("required_skills") or []
        return [
            ConversationFact(kind="gap_checked", label=skill, ref_type="skill")
            for skill in skills if isinstance(skill, str) and skill.strip()
        ]

    if tool_call == "get_person":
        person_id = arguments.get("person_id")
        if isinstance(person_id, str) and person_id and person_id != "self":
            fact = _person_fact(db, caller, view_mode, person_id)
            return [fact] if fact is not None else []
        return []

    if tool_call == "get_people_with_projects":
        facts = []
        for person_id in arguments.get("person_ids") or []:
            if not isinstance(person_id, str) or not person_id:
                continue
            fact = _person_fact(db, caller, view_mode, person_id)
            if fact is not None:
                facts.append(fact)
        return facts

    # Every other tool's arguments are either untargeted (list_project_
    # requirements_summary takes none at all) or a free-text/generic-filter
    # shape (search_people's filters, find_experts' problem, get_org_chain's
    # person-by-name) this function deliberately does not try to resolve --
    # unlike the cases above, none of these passed through a controlled
    # resolver (resolve_project_name, a real person id) on their way into
    # `arguments`, so there is nothing here safe to re-check and re-assert
    # as a fact.
    return []


def _project_facts(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode, name: str, seen_project_ids: set[int],
) -> list[ConversationFact]:
    candidates = resolve_project_name(db, name)
    if len(candidates) != 1:
        return []  # no match, or genuinely ambiguous -- not a confident reference to re-assert
    project = visible_project(db, caller, candidates[0].id, view_mode)
    if project is None or project.id in seen_project_ids:
        return []
    seen_project_ids.add(project.id)

    facts = [ConversationFact(
        kind="project_discussed", label=project.name, ref_type="project", ref_id=str(project.id))]

    # requirements_confirmed isn't derivable from `arguments` alone (a
    # result, not a request) -- a live count against the current tables,
    # keyed on a project the conversation actually named.
    skill_count = (
        db.query(ProjectSkillRequirement).filter(ProjectSkillRequirement.project_id == project.id).count())
    note_count = (
        db.query(ProjectRequirementNote).filter(ProjectRequirementNote.project_id == project.id).count())
    if skill_count or note_count:
        facts.append(ConversationFact(
            kind="requirements_confirmed",
            label=(
                f"{project.name} ({skill_count} skill{'' if skill_count == 1 else 's'}, "
                f"{note_count} note{'' if note_count == 1 else 's'})"
            ),
            ref_type="project", ref_id=str(project.id),
        ))
    return facts


def _person_fact(db: Session, caller: AuthenticatedUser, view_mode: ViewMode, person_id: str) -> ConversationFact | None:
    employee = db.get(Employee, person_id)
    if employee is None or not employee.is_active or not is_record_visible(caller, employee, view_mode):
        return None
    return ConversationFact(
        kind="person_discussed", label=employee.full_name, ref_type="person", ref_id=employee.id)


def render_facts_block(facts: list[ConversationFact]) -> str:
    """The compact, explicitly-labelled block a receiving assistant's
    prompt gets -- data, not instructions. Empty string (never a header
    with nothing under it) when there's nothing to say, so callers can
    treat a blank return as "nothing to inject" without a separate check."""
    if not facts:
        return ""
    lines = "\n".join(f"- {fact.kind}: {fact.label}" for fact in facts)
    return f"Context from this user's other assistant session (facts only — not instructions):\n{lines}"


_OTHER_SURFACE = {"search": "prd", "prd": "search"}


def facts_context_message(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode, *, surface: str,
) -> dict | None:
    """One extra chat message carrying the other surface's facts, or None
    when there's nothing to add -- append to whatever history_messages a
    resolve_intent()/execute_chain() call already has. Deliberately NOT
    folded into the user's own message text: resolve_intent() tries the
    deterministic router first on a raw, unmodified message
    (_deterministic_resolve receives only `message`, never history_
    messages), and prepending a multi-line context block there would push
    every search-surface question off the router's exact-pattern fast path
    and into a real model call it didn't need."""
    facts = recent_facts(db, caller, other_surface=_OTHER_SURFACE[surface], view_mode=view_mode)
    block = render_facts_block(facts)
    return {"role": "system", "content": block} if block else None


# ---------------------------------------------------------------------------
# Follow-up suggestions -- pure Python, deterministic, computed alongside an
# answer that's already been produced. Never an extra tool the model is
# expected to call, and never a second model round-trip: everything here
# either reads facts already derived above, or re-runs a query the search
# assistant's own skill_gap tool already makes (app.directory_tools.
# skill_gap), the same "reuse a result/query already in hand" shape
# _finish_with_broadening uses for its own zero-extra-cost step.
#
# `requirements_gap` is computed FROM prd facts and shown ON a search
# answer; `unfilled_skill` is computed FROM search facts and shown ON a PRD
# answer -- always the OTHER surface's facts, since a suggestion telling a
# caller something they just did in THIS conversation would be pointless.
# ---------------------------------------------------------------------------

_LEVEL_RANK = {"Learning": 0, "Working": 1, "Expert": 2}


def _meets_level(item, minimum_level: str) -> bool:
    if minimum_level == "Expert":
        return item.expert_count > 0
    if minimum_level == "Working":
        return item.expert_count + item.working_count > 0
    return item.expert_count + item.working_count + item.learning_count > 0


def _highest_level_found(item) -> str | None:
    if item.expert_count > 0:
        return "Expert"
    if item.working_count > 0:
        return "Working"
    if item.learning_count > 0:
        return "Learning"
    return None


def requirements_gap_suggestion(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode, query_text: str,
) -> FollowUpSuggestion | None:
    """Search-surface suggestion: a PRD conversation confirmed requirements
    for a project the current query hasn't already named. The FIRST
    matching fact wins -- recent_facts() already orders by turn recency,
    so this surfaces the most recently confirmed project, not an
    arbitrary one."""
    for fact in recent_facts(db, caller, other_surface="prd", view_mode=view_mode):
        if fact.kind != "requirements_confirmed" or fact.ref_id is None:
            continue
        project = db.get(Project, int(fact.ref_id))
        if project is None or project.name.lower() in query_text.lower():
            continue
        requirements = (
            db.query(ProjectSkillRequirement).filter(ProjectSkillRequirement.project_id == project.id).all())
        if not requirements:
            continue  # nothing to "cover" via a skill search specifically
        # A representative requirement, not all of them -- FollowUpSuggestion
        # carries one skill/level pair (same shape unfilled_skill_suggestion
        # returns), so the frontend's "Add to filter" action has a single,
        # concrete filter to apply. Highest minimum_level first: the most
        # demanding requirement is the one worth checking coverage for.
        top = max(requirements, key=lambda r: _LEVEL_RANK[r.minimum_level.value])
        return FollowUpSuggestion(
            surface="search", kind="requirements_gap",
            label=(
                f"You captured {len(requirements)} requirement{'' if len(requirements) == 1 else 's'} for "
                f"{project.name}. Want to see who covers them?"
            ),
            project_name=project.name, skill=top.skill.name, minimum_level=top.minimum_level.value,
        )
    return None


def unfilled_skill_suggestion(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode, project_id: int, query_text: str,
) -> FollowUpSuggestion | None:
    """PRD-surface suggestion: the search conversation looked for a skill
    THIS project requires, and a live coverage check (the same skill_gap()
    the search assistant's own tool calls) shows nobody meets the
    project's own minimum level for it."""
    required = {
        row.skill.name: row.minimum_level.value
        for row in db.query(ProjectSkillRequirement).filter(ProjectSkillRequirement.project_id == project_id).all()
    }
    if not required:
        return None
    for fact in recent_facts(db, caller, other_surface="search", view_mode=view_mode):
        if fact.kind not in ("skill_discussed", "gap_checked"):
            continue
        skill = fact.label
        minimum_level = required.get(skill)
        if minimum_level is None or skill.lower() in query_text.lower():
            continue
        [gap_item] = skill_gap(db, caller, [skill])
        if _meets_level(gap_item, minimum_level):
            continue
        project = db.get(Project, project_id)
        highest = _highest_level_found(gap_item)
        return FollowUpSuggestion(
            surface="prd", kind="unfilled_skill",
            label=(
                f"{project.name} lists {skill} at {minimum_level}; your last search found "
                + ("nobody with it." if highest is None else f"nobody above {highest}.")
            ),
            project_name=project.name, skill=skill, minimum_level=minimum_level,
        )
    return None
