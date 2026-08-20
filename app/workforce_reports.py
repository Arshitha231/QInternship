"""Workforce Intelligence — a natural-language question answered as a
structured, evidence-backed report.

    user query
      -> plan_analyses()      which analyses this question needs
      -> gather()             permission-filtered retrieval, via app/analytics.py
      -> _build_sections()    deterministic findings, with evidence and severity
      -> _narrate()           model prose over those facts, numeral-checked
      -> WorkforceReport

WHAT THE MODEL DOES, AND WHAT IT IS STRUCTURALLY UNABLE TO DO
-------------------------------------------------------------
It does two things. It picks which of four analyses a question needs (a
classification over a fixed enum), and it writes the executive summary over
facts that have already been computed.

It does not query anything: there is no database session, no ORM object and
no SQL anywhere in its path, and the only functions it can cause to run are
the four in ANALYSIS_RUNNERS, each of which takes the authenticated caller
and resolves its own scope.

It does not decide permissions. Scope is resolved exactly once, by
app/analytics.py's resolve_scope, from the CALLER — HR in work mode gets the
organization or a chosen unit, everyone else gets their own reporting
subtree and the requested scope is discarded rather than validated. A
planner that hallucinated `org_unit: "Engineering"` for a manager therefore
widens nothing: ReportPlan has no scope field for it to land in, and
gather() takes no scope parameter to pass one through.

It does not see people. Not even the caller: a manager's scope is labelled
"<their name>'s team" on screen and de-identified before it reaches a prompt
(app/grounding.neutral_scope_label). The payload carries skills, org
units, projects and counts — the same class of aggregate data
app/insight_narrative.py already shows it. Employee names travel from the
retrieval layer to the UI as structured evidence and never through the
prose, which is why "do not invent employees" is a property of the wiring
rather than an instruction in a prompt.

It does not produce numbers. Every numeral in the generated summary is
checked against the numerals in the facts (app/grounding.py, shared with the
dashboard narrative). A summed, averaged, estimated or invented figure fails
and the whole text is discarded for the deterministic version.

MODULAR BY ANALYSIS TYPE
------------------------
Adding a fifth analysis is: a member on AnalysisType, an entry in
ANALYSIS_RUNNERS, a _findings_* function, and a keyword row in _KEYWORDS.
Nothing else in this module, and nothing at all in the route or the UI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.analytics import (
    DashboardForbidden, project_coverage as project_coverage_service,
    resolve_scope, scope_for, skill_supply_demand as skill_supply_demand_service,
    training_analytics as training_analytics_service, training_roster as training_roster_service,
)
from app.auth import AuthenticatedUser
from app.grounding import is_grounded, neutral_scope_label, numerals
from app.models import AuditLog
from app.permissions import ViewMode
from app.schemas import (
    AnalysisType, DashboardScope, ProjectCoverage, ReportEvidence, ReportFinding, ReportSection,
    SkillSupplyDemand, TrainingAnalytics, TrainingRoster, WorkforceReport,
)

# Every analysis this build can serve. The planner may return only these;
# anything else it asks for is reported to the reader as unsupported rather
# than silently dropped, so "the training section is empty" never has to be
# guessed at.
SUPPORTED: tuple[AnalysisType, ...] = ("skill_gap", "skill_scarcity", "training", "project_coverage")

# How many rows of each kind reach a section. A report is a briefing, not a
# database dump: past this the reader stops reading and the model starts
# having more facts than it can connect.
MAX_ROWS = 6
MAX_SUMMARY_CHARS = 900

# Deterministic router, tried before the model, same shape as
# app/tool_calling.py's _deterministic_resolve. Most real questions name
# their analysis outright, and a keyword match is faster, free, and cannot
# hallucinate. The model is the fallback for the ones that don't.
_KEYWORDS: list[tuple[AnalysisType, re.Pattern[str]]] = [
    ("skill_gap", re.compile(
        r"\bskill gaps?\b|\bgaps? in\b|\bmissing skills?\b|\black(ing)?\b|\bshort of\b"
        r"|\bcapabilit(y|ies)\b|\bcompare\b|\bstrengths?\b", re.I)),
    ("skill_scarcity", re.compile(
        r"\bscarc\w*|\bdepend\w* on (a )?(small|few|handful)|\bsingle point\b|\bbus factor\b"
        r"|\bonly (a few|one|1|2|3) (person|people)\b|\bconcentrat\w*|\bkey person\b"
        r"|\brisk\w*\b|\bexperts?\b", re.I)),
    ("training", re.compile(
        r"\btraining\b|\bcourses?\b|\boverdue\b|\bcomplianc\w*|\bcertif\w*|\bcompletion\b"
        r"|\bdue soon\b|\blearning path\b|\bupskill\w*|\breminder", re.I)),
    ("project_coverage", re.compile(
        r"\bprojects?\b|\bstaffing\b|\bengagement\w*|\bcoverage\b|\bdeliver\w*|\bassigned\b", re.I)),
]

# Questions that name a skill: "consider for Terraform training", "gaps in
# Kubernetes". Captured so the skill sections can lead with what was asked
# about rather than with whatever happens to rank first.
_QUOTED = re.compile(r"[\"“']([^\"”']{2,40})[\"”']")


class ReportUnavailable(Exception):
    """The caller has no dashboard scope, so there is nothing to report on."""


# ---------------------------------------------------------------------------
# 1. Planner
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReportPlan:
    """Which analyses to run, and what the question was about.

    Deliberately carries no scope. Scope comes from the caller, and a plan
    that could name one would be a plan that could widen one -- see this
    module's docstring.
    """

    analyses: tuple[AnalysisType, ...]
    #: Skill names lifted from the question, used to order and filter the
    #: skill sections. Never trusted as data: an unrecognised name simply
    #: matches nothing.
    focus_skills: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    source: str = "keyword"


def _deterministic_plan(query: str) -> ReportPlan | None:
    """Keyword routing. Returns None when nothing matches, so the model gets
    its turn; returns a plan the moment anything does, because a question
    that says "training" wants the training analysis whatever else it says."""
    hits = tuple(kind for kind, pattern in _KEYWORDS if pattern.search(query))
    if not hits:
        return None
    return ReportPlan(analyses=_with_skill_context(hits, _focus_skills(query)),
                      focus_skills=_focus_skills(query), source="keyword")


def _with_skill_context(analyses: tuple[AnalysisType, ...],
                        focus: tuple[str, ...]) -> tuple[AnalysisType, ...]:
    """A question that NAMES a skill wants that skill's numbers, whichever
    analysis its verb routed to.

    "Which employees should we consider for Terraform training?" routes to
    `training` on the word training, and answering it needs the Terraform
    Expert/Working/Learning split — otherwise the report talks about course
    compliance and never mentions the skill the question was about.
    """
    if focus and "skill_gap" not in analyses:
        return (*analyses, "skill_gap")
    return analyses


def _focus_skills(query: str) -> tuple[str, ...]:
    """Skill names the question names. Quoted phrases first, then
    capitalised words that are not sentence-initial -- a cheap heuristic
    that costs nothing when it is wrong, since an unmatched name just
    filters nothing."""
    out: list[str] = [m.group(1).strip() for m in _QUOTED.finditer(query)]
    words = query.split()
    for i, word in enumerate(words):
        token = word.strip(" ,.?!:;\"'")
        if i > 0 and token[:1].isupper() and len(token) > 2:
            out.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return tuple(unique[:6])


_PLANNER_TOOL = {
    "type": "function",
    "function": {
        "name": "plan_workforce_report",
        "description": (
            "Choose which workforce analyses answer the user's question. "
            "Pick every analysis the question genuinely needs and no others."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "analyses": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(SUPPORTED)},
                    "description": (
                        "skill_gap: what capabilities are thin or missing, comparisons between "
                        "groups. skill_scarcity: dependence on very few people, key-person risk. "
                        "training: course completion, overdue, compliance. "
                        "project_coverage: whether active projects have the skills they need."
                    ),
                },
                "focus_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill names the question names, if any. Copy them verbatim; do not invent any.",
                },
            },
            "required": ["analyses"],
        },
    },
}

_PLANNER_SYSTEM = """You route a workforce question to the analyses that answer it.

You do not answer the question and you do not have the data. You choose from \
a fixed set of analyses, and nothing else. If the question is not about the \
workforce at all, return an empty analyses list."""


def _model_plan(query: str) -> ReportPlan | None:
    """The model's turn, only for questions the keywords did not recognise.

    Constrained to one tool with an enum'd argument, the same
    prompt-injection defence app/tool_calling.py's TOOLS relies on: text
    that is trying to do something else cannot produce a valid call, so it
    produces nothing.
    """
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    if _mode() != "real":
        return None
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": query},
            ],
            tools=[_PLANNER_TOOL],
            tool_choice="auto",
            # Picking names from a fixed enum is classification, not
            # deliberation -- the same measured call app/tool_calling.py's
            # routing makes for the same reason.
            reasoning_effort="minimal",
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            return None
        args = json.loads(calls[0].function.arguments or "{}")
    except (OpenAIError, json.JSONDecodeError, AttributeError, IndexError):
        return None

    raw = args.get("analyses") or []
    if not isinstance(raw, list):
        return None
    chosen = tuple(a for a in raw if a in SUPPORTED)
    # An analysis the model asked for that this build does not have is
    # surfaced, not swallowed: the reader is owed the difference between
    # "nothing found" and "not implemented".
    unsupported = tuple(str(a) for a in raw if a not in SUPPORTED)
    skills = args.get("focus_skills") or []
    focus = tuple(str(s)[:40] for s in skills if isinstance(s, str))[:6] if isinstance(skills, list) else ()
    if not chosen:
        return ReportPlan(analyses=(), unsupported=unsupported, source="model")
    return ReportPlan(analyses=chosen, focus_skills=focus or _focus_skills(query),
                      unsupported=unsupported, source="model")


def plan_analyses(query: str) -> ReportPlan:
    """Keywords first, model second, and a documented default third.

    The default matters: a question nobody could route ("how are we doing?")
    should produce a general briefing rather than an error, because the
    analyses are cheap and a reader who asked something vague is better
    served by the overview than by a complaint.
    """
    plan = _deterministic_plan(query)
    if plan is not None:
        return plan
    plan = _model_plan(query)
    if plan is not None and plan.analyses:
        return plan
    unsupported = plan.unsupported if plan is not None else ()
    return ReportPlan(analyses=SUPPORTED, focus_skills=_focus_skills(query),
                      unsupported=unsupported, source="default")


# ---------------------------------------------------------------------------
# 2. Retrieval — permission-filtered, through the existing analytics layer
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """Everything the report drew on. Populated only by the runners below,
    each of which goes through app/analytics.py and therefore through
    resolve_scope."""

    scope: DashboardScope
    skills: list[SkillSupplyDemand] = field(default_factory=list)
    training: TrainingAnalytics | None = None
    roster: TrainingRoster | None = None
    projects: list[ProjectCoverage] = field(default_factory=list)
    insight_titles: list[str] = field(default_factory=list)


def _run_skill_gap(db: Session, caller: AuthenticatedUser, mode: ViewMode, ev: Evidence) -> None:
    if not ev.skills:
        ev.skills = skill_supply_demand_service(db, caller, mode, limit=120)


def _run_skill_scarcity(db: Session, caller: AuthenticatedUser, mode: ViewMode, ev: Evidence) -> None:
    _run_skill_gap(db, caller, mode, ev)


def _run_training(db: Session, caller: AuthenticatedUser, mode: ViewMode, ev: Evidence) -> None:
    ev.training = training_analytics_service(db, caller, mode)
    # The named people behind the overdue bucket -- carried as structured
    # evidence for the UI's "send reminders" hand-off, and deliberately not
    # shown to the model. See this module's docstring.
    ev.roster = training_roster_service(db, caller, mode, bucket="overdue", limit=50)


def _run_project_coverage(db: Session, caller: AuthenticatedUser, mode: ViewMode, ev: Evidence) -> None:
    ev.projects = project_coverage_service(db, caller, mode)


ANALYSIS_RUNNERS = {
    "skill_gap": _run_skill_gap,
    "skill_scarcity": _run_skill_scarcity,
    "training": _run_training,
    "project_coverage": _run_project_coverage,
}


def gather(db: Session, caller: AuthenticatedUser, mode: ViewMode, plan: ReportPlan) -> Evidence:
    """Run the planned analyses. Every one resolves its own scope from the
    caller, so this function has no scope parameter to get wrong."""
    ev = Evidence(scope=scope_for(db, caller, mode))
    for analysis in plan.analyses:
        runner = ANALYSIS_RUNNERS.get(analysis)
        if runner is not None:
            runner(db, caller, mode, ev)
    return ev


# ---------------------------------------------------------------------------
# 3. Deterministic findings — severity and evidence, never the model's
# ---------------------------------------------------------------------------

def _skill_ev(row: SkillSupplyDemand) -> ReportEvidence:
    return ReportEvidence(
        kind="skill", skill_id=row.skill_id,
        label=(f"{row.skill}: {row.expert_count} Expert, {row.working_count} Working, "
               f"{row.learning_count} Learning · {row.demand_project_count} active "
               f"project{'s' if row.demand_project_count != 1 else ''} need it"),
    )


def _ordered(rows: list[SkillSupplyDemand], focus: tuple[str, ...]) -> list[SkillSupplyDemand]:
    """Skills the question named come first. An unmatched name costs
    nothing -- it simply promotes nobody."""
    if not focus:
        return rows
    wanted = {f.lower() for f in focus}
    named = [r for r in rows if r.skill.lower() in wanted]
    rest = [r for r in rows if r.skill.lower() not in wanted]
    return named + rest


def _named_skill_finding(r: SkillSupplyDemand) -> ReportFinding:
    """A skill the question asked about by name, whatever its verdict.

    Without this, "which employees should we consider for Terraform
    training?" answered about whichever skill happened to rank worst,
    because the gap section only lists understaffed rows and the skill
    somebody asked about need not be one. A named skill is the question; it
    leads.

    The reading is the one the brief calls for: Expert coverage against the
    Learning pipeline decides whether this is a training opportunity or a
    hiring gap, and the sentence says which.
    """
    pipeline = r.learning_count
    if r.capable_count == 0:
        severity, verdict = "high", (
            f"Nobody is at Working or above. {pipeline} at Learning"
            if pipeline else "Nobody holds it at any level")
    elif pipeline >= max(1, r.expert_count) and r.expert_count <= 2:
        severity, verdict = "medium", (
            f"Limited Expert coverage ({r.expert_count}) against {pipeline} at Learning — "
            "a training opportunity rather than an immediate hiring gap")
    elif r.single_point_of_failure:
        severity, verdict = "high", "One capable holder — a key-person risk"
    elif r.verdict == "understaffed":
        severity, verdict = "high", (
            f"{r.capable_count} capable against {r.demand_project_count} active "
            f"project{'s' if r.demand_project_count != 1 else ''}")
    else:
        severity, verdict = "info", f"{r.maturity_label} depth, {r.verdict} against current demand"
    return ReportFinding(
        title=f"{r.skill} — {r.expert_count} Expert, {r.working_count} Working, {r.learning_count} Learning",
        detail=(f"{verdict}. {r.holder_count} {'person holds' if r.holder_count == 1 else 'people hold'} it "
                f"across the scope; {r.demand_project_count} active "
                f"project{'s' if r.demand_project_count != 1 else ''} depend on it."),
        severity=severity,
        evidence=[_skill_ev(r)],
    )


def _findings_skill_gap(ev: Evidence, focus: tuple[str, ...]) -> tuple[ReportSection, ReportSection]:
    """Returns (strengths, gaps). Both come off the same table, because
    "where are we strong" and "where are we thin" are the two ends of one
    ordering and answering only one of them makes a report feel evasive."""
    rows = _ordered(ev.skills, focus)
    wanted = {f.lower() for f in focus}
    named = [r for r in rows if r.skill.lower() in wanted]
    understaffed = [r for r in rows if r.verdict == "understaffed" and r.skill.lower() not in wanted]
    # Languages are excluded from strengths. "505 people speak English" is
    # true, useless, and crowds out every real capability -- the same reason
    # app/skill_routes.py refuses to bridge on them.
    deep = [r for r in rows
            if r.category != "language"
            and r.verdict in ("healthy", "overrepresented") and r.expert_count > 0]

    gaps = ReportSection(heading="Skill gaps", findings=[
        *[_named_skill_finding(r) for r in named],
        *[ReportFinding(
            title=f"{r.skill} — {r.capable_count} capable against {r.demand_project_count} active "
                  f"project{'s' if r.demand_project_count != 1 else ''}",
            detail=(
                f"{r.expert_count} at Expert, {r.working_count} at Working, {r.learning_count} at Learning. "
                + (f"{r.learning_count} people are already learning it, so this reads as a training "
                   "opportunity rather than a hiring gap."
                   if r.learning_count >= max(1, r.capable_count) else
                   "The Learning pipeline is thin, so this is unlikely to close on its own.")
            ),
            severity="high" if r.capable_count == 0 or r.single_point_of_failure else "medium",
            evidence=[_skill_ev(r)],
        ) for r in understaffed[:MAX_ROWS]],
    ])

    strengths = ReportSection(heading="Strengths", findings=[
        ReportFinding(
            title=f"{r.skill} — {r.expert_count} Expert, {r.capable_count} capable",
            detail=f"{r.maturity_label} depth ({r.maturity_pct}% maturity) across "
                   f"{r.holder_count} {'person' if r.holder_count == 1 else 'people'}.",
            severity="info",
            evidence=[_skill_ev(r)],
        )
        for r in sorted(deep, key=lambda r: (-r.expert_count, -r.capable_count))[:MAX_ROWS]
    ])
    return strengths, gaps


def _findings_scarcity(ev: Evidence, focus: tuple[str, ...]) -> ReportSection:
    rows = _ordered(ev.skills, focus)
    spof = [r for r in rows if r.single_point_of_failure]
    thin = [r for r in rows if not r.single_point_of_failure and 0 < r.capable_count <= 2
            and r.demand_project_count > 0]
    return ReportSection(heading="Workforce risks", findings=[
        *[ReportFinding(
            title=f"{r.skill} rests on one person",
            detail=f"One capable holder against {r.demand_project_count} active "
                   f"project{'s' if r.demand_project_count != 1 else ''}. "
                   f"{r.learning_count} at Learning behind them.",
            severity="high",
            evidence=[_skill_ev(r)],
        ) for r in spof[:MAX_ROWS]],
        *[ReportFinding(
            title=f"{r.skill} — only {r.capable_count} capable people",
            detail=f"Covering {r.demand_project_count} active "
                   f"project{'s' if r.demand_project_count != 1 else ''}.",
            severity="medium",
            evidence=[_skill_ev(r)],
        ) for r in thin[:max(0, MAX_ROWS - len(spof))]],
    ])


def _findings_training(ev: Evidence) -> ReportSection:
    t = ev.training
    if t is None:
        return ReportSection(heading="Training insights")
    b = t.buckets
    findings = [ReportFinding(
        title=f"{b.compliance_pct}% of required courses are complete",
        detail=(f"{b.completed} of {b.expected} expectations across {t.employee_count} people. "
                f"{b.overdue} overdue, {b.due_soon} due within {t.due_soon_days} days."),
        severity="high" if b.overdue > 0 else "info",
        evidence=[ReportEvidence(kind="training", label=(
            f"Completed {b.completed} · overdue {b.overdue} · due soon {b.due_soon} · "
            f"outstanding {b.outstanding}"))],
    )]
    for course in t.by_course[:MAX_ROWS]:
        if course.buckets.incomplete == 0:
            continue
        findings.append(ReportFinding(
            title=f"{course.label} — {course.buckets.incomplete} outstanding",
            detail=(f"{course.buckets.completed} of {course.buckets.expected} complete "
                    f"({course.buckets.compliance_pct}%), {course.buckets.overdue} overdue."),
            severity="high" if course.buckets.overdue > 0 else "medium",
            evidence=[ReportEvidence(kind="training", course_code=course.key,
                                     label=f"{course.label}: {course.buckets.overdue} overdue")],
        ))
    worst = [u for u in t.by_unit if u.buckets.overdue > 0][:3]
    if worst:
        findings.append(ReportFinding(
            title="Where the overdue work sits",
            detail=", ".join(f"{u.label} ({u.buckets.overdue} overdue)" for u in worst),
            severity="medium",
            evidence=[ReportEvidence(kind="department", org_unit_id=int(u.key) if u.key.isdigit() else None,
                                     label=f"{u.label}: {u.buckets.overdue} overdue") for u in worst],
        ))
    return ReportSection(heading="Training insights", findings=findings)


def _findings_projects(ev: Evidence) -> ReportSection:
    judged = [p for p in ev.projects if p.requirements_recorded]
    gapped = [p for p in judged if p.gap_skills]
    findings = [ReportFinding(
        title=f"{len(gapped)} of {len(judged)} active projects are missing a required skill",
        detail=("Coverage is judged only for projects with recorded skill requirements; "
                f"{len(ev.projects) - len(judged)} more have none recorded and cannot be judged."),
        severity="high" if gapped else "info",
        # Always evidenced, including the good news. A headline with nothing
        # behind it is the one claim in the report a reader cannot check,
        # and "0 of 12 are missing anything" is exactly the sentence that
        # most deserves a source.
        evidence=([ReportEvidence(kind="project", project_id=p.project_id,
                                  label=f"{p.project_name}: missing {', '.join(p.gap_skills[:3])}")
                   for p in gapped[:MAX_ROWS]]
                  or [ReportEvidence(kind="project",
                                     label=f"{len(judged)} active project"
                                           f"{'s' if len(judged) != 1 else ''} with recorded "
                                           "requirements, all covered")]),
    )] if judged else []
    for p in gapped[:MAX_ROWS]:
        findings.append(ReportFinding(
            title=f"{p.project_name} — {len(p.gap_skills)} of {p.required_skill_count} requirements uncovered",
            detail=f"Missing: {', '.join(p.gap_skills)}. {p.member_count} "
                   f"{'person' if p.member_count == 1 else 'people'} currently assigned.",
            severity="high",
            evidence=[ReportEvidence(kind="project", project_id=p.project_id,
                                     label=f"{p.project_name}: {p.coverage_pct}% covered")],
        ))
    return ReportSection(heading="Project coverage", findings=findings)


def _findings_recommendations(ev: Evidence, plan: ReportPlan) -> ReportSection:
    """Actions, each tied to the finding that motivates it. Deterministic:
    a recommendation the model invented would be the one part of the report
    with no evidence behind it."""
    out: list[ReportFinding] = []
    trainable = [r for r in ev.skills
                 if r.verdict == "understaffed" and r.learning_count > 0][:3]
    for r in trainable:
        out.append(ReportFinding(
            title=f"Train up on {r.skill} rather than hire",
            detail=(f"{r.learning_count} {'person is' if r.learning_count == 1 else 'people are'} "
                    f"already at Learning against {r.capable_count} capable — the pipeline exists, "
                    "it just has not converted."),
            severity="medium",
            evidence=[_skill_ev(r)],
        ))
    for r in [x for x in ev.skills if x.single_point_of_failure][:2]:
        out.append(ReportFinding(
            title=f"Pair a second person onto {r.skill}",
            detail=f"One capable holder covers {r.demand_project_count} active "
                   f"project{'s' if r.demand_project_count != 1 else ''}.",
            severity="high",
            evidence=[_skill_ev(r)],
        ))
    if ev.training and ev.training.buckets.overdue > 0:
        n = ev.training.buckets.overdue
        people = len({row.employee_id for row in ev.roster.rows}) if ev.roster else 0
        out.append(ReportFinding(
            title=f"Send reminders for {n} overdue course record{'s' if n != 1 else ''}",
            detail=(f"{people} {'person is' if people == 1 else 'people are'} behind on a required "
                    "course. The Training section's overdue list is already filtered to exactly them."),
            severity="high",
            evidence=[ReportEvidence(kind="training", label=f"{n} overdue records")],
        ))
    return ReportSection(heading="Recommendations", findings=out)


# ---------------------------------------------------------------------------
# 4. The executive summary
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """You write the executive summary of a workforce report.

You are given the report's own findings, already computed from the \
organisation's records. Write three to five plain sentences for a busy \
reader: what the shape of this is, what deserves attention first, and what \
connects to what.

Rules, all absolute:
- Use ONLY numbers that appear verbatim in the findings. Never add, total, \
average, estimate or recompute anything.
- Never invent a finding, a skill, a project, a department or a consequence \
the findings do not state.
- Never name a person, and never guess at one.
- No markdown, no bullet points, no headings. Prose only.

If the findings are mild, say so plainly rather than manufacturing urgency."""


def _sections(report_parts: dict[str, ReportSection]) -> list[ReportSection]:
    return [s for s in report_parts.values() if s.findings]


def _fact_text(sections: list[ReportSection], scope: DashboardScope) -> str:
    parts = [neutral_scope_label(scope), str(scope.headcount)]
    for section in sections:
        for f in section.findings:
            parts += [f.title, f.detail, *[e.label for e in f.evidence]]
    return " ".join(parts)


def _derived_summary(sections: list[ReportSection], scope: DashboardScope) -> str:
    """The always-correct version. Quotes finding titles verbatim rather
    than paraphrasing them: a template that paraphrases is one that can be
    wrong, and the titles were written to be read on their own."""
    if not sections:
        return (f"Nothing in {scope.label} crosses a threshold worth reporting on for this "
                "question. That is the finding — nothing is padded in to fill the sections.")
    high = [f for s in sections for f in s.findings if f.severity == "high"]
    lead = high[0] if high else next(f for s in sections for f in s.findings)
    opening = (
        f"{len(high)} finding{'s' if len(high) != 1 else ''} in {scope.label} need attention."
        if high else f"Nothing urgent surfaced in {scope.label}."
    )
    covered = ", ".join(s.heading.lower() for s in sections)
    return f"{opening} Start with: {lead.title.rstrip('.')}. {lead.detail} This report covers {covered}."


def _model_summary(sections: list[ReportSection], scope: DashboardScope) -> str | None:
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    if _mode() != "real":
        return None
    payload = json.dumps({
        # De-identified: a manager's scope label is "<their name>'s team",
        # and the model has no use for whose team it is -- only how big.
        "scope": {"label": neutral_scope_label(scope), "headcount": scope.headcount},
        "sections": [
            {"heading": s.heading, "findings": [
                {"title": f.title, "detail": f.detail, "severity": f.severity,
                 "evidence": [e.label for e in f.evidence]}
                for f in s.findings
            ]}
            for s in sections
        ],
    })
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": payload},
            ],
            reasoning_effort="minimal",
        )
    except OpenAIError:
        return None
    text = (response.choices[0].message.content or "").strip()
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return None
    if not is_grounded(text, numerals(_fact_text(sections, scope))):
        return None
    return text


def _narrate(sections: list[ReportSection], scope: DashboardScope) -> tuple[str, str]:
    text = _model_summary(sections, scope)
    if text is not None:
        return text, "model"
    return _derived_summary(sections, scope), "derived"


# ---------------------------------------------------------------------------
# 5. Assembly
# ---------------------------------------------------------------------------

def _title(plan: ReportPlan, scope: DashboardScope) -> str:
    names = {
        "skill_gap": "Skill gaps", "skill_scarcity": "Key-person risk",
        "training": "Training compliance", "project_coverage": "Project coverage",
    }
    if len(plan.analyses) == 1:
        return f"{names[plan.analyses[0]]} — {scope.label}"
    if len(plan.analyses) >= len(SUPPORTED):
        return f"Workforce review — {scope.label}"
    return f"{' and '.join(names[a] for a in plan.analyses)} — {scope.label}"


def generate_report(
    db: Session, caller: AuthenticatedUser, query: str, view_mode: ViewMode = "work",
) -> WorkforceReport:
    """The whole flow. Raises ReportUnavailable for a caller with no scope."""
    try:
        # Resolved before anything else runs, so a caller who has no
        # dashboard never reaches the planner, the model, or any data.
        resolve_scope(db, caller, view_mode)
    except DashboardForbidden as e:
        raise ReportUnavailable(str(e)) from e

    plan = plan_analyses(query)
    ev = gather(db, caller, view_mode, plan)

    strengths = ReportSection(heading="Strengths")
    gaps = ReportSection(heading="Skill gaps")
    if "skill_gap" in plan.analyses:
        strengths, gaps = _findings_skill_gap(ev, plan.focus_skills)
    risks = _findings_scarcity(ev, plan.focus_skills) if "skill_scarcity" in plan.analyses \
        else ReportSection(heading="Workforce risks")
    training = _findings_training(ev) if "training" in plan.analyses \
        else ReportSection(heading="Training insights")
    projects = _findings_projects(ev) if "project_coverage" in plan.analyses \
        else ReportSection(heading="Project coverage")
    recommendations = _findings_recommendations(ev, plan)

    ordered = {
        "strengths": strengths, "skill_gaps": gaps, "risks": risks,
        "training_insights": training, "project_insights": projects,
        "recommendations": recommendations,
    }
    populated = _sections(ordered)
    summary, source = _narrate(populated, ev.scope)

    # One deduplicated index of everything the report drew on -- the
    # reader's audit trail and the UI's click-through map.
    seen: set[tuple] = set()
    evidence: list[ReportEvidence] = []
    for section in populated:
        for finding in section.findings:
            for e in finding.evidence:
                key = (e.kind, e.label)
                if key not in seen:
                    seen.add(key)
                    evidence.append(e)

    _audit(db, caller, query, plan, len(evidence))
    return WorkforceReport(
        title=_title(plan, ev.scope),
        query=query,
        scope=ev.scope,
        analyses=list(plan.analyses),
        unsupported=list(plan.unsupported),
        executive_summary=summary,
        narrative_source=source,
        strengths=strengths, skill_gaps=gaps, risks=risks,
        training_insights=training, project_insights=projects,
        recommendations=recommendations,
        evidence=evidence,
    )


def _audit(db: Session, caller: AuthenticatedUser, query: str, plan: ReportPlan, count: int) -> None:
    """The QUERY is recorded, not the report: what somebody asked is the
    reviewable act, and the findings are reproducible from it."""
    db.add(AuditLog(
        actor_id=caller.id, action="workforce_report",
        query_text=f"plan={'+'.join(plan.analyses) or 'none'};via={plan.source};q={query[:200]}",
        result_count=count,
        fields_returned=json.dumps(["title", "executive_summary", "findings", "evidence"]),
        timestamp=datetime.now(),
    ))
    db.commit()
