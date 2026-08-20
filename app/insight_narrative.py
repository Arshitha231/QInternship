"""A short "here's what to look at first" over the dashboard's own findings.

Deliberately a separate module from app/analytics.py. That file's opening
line is "Deterministic throughout. Every number here is counted or divided",
and that stays literally true — the model call lives here, on the other side
of a boundary you can see, and analytics.py never imports this.

WHAT THE MODEL IS AND IS NOT ALLOWED TO DO
------------------------------------------
It receives the already-computed list of WorkforceInsight objects and
nothing else. Not the employee rows, not the skill table, not the training
records — the facts, already counted, already scoped, already through
resolve_scope. Its job is ordering and connective prose: six cards in
severity order is a list, and a person opening a dashboard wants a sentence
telling them where to start.

Same trust boundary as app/tool_calling.py's phrase_answer, and for the same
reason stated there: the model is grounding a sentence in data the caller
can already read off the cards below it, not asserting anything of its own.

Three things enforce that rather than merely asking for it:

  1. The input carries no personal data. Insight evidence names skills, org
     units and projects — never a person. There is no code path from an
     employee row to this module.
  2. Every numeral in the output is checked against the numerals in the
     input (see _numerals_are_grounded). A number the model computed,
     estimated or invented fails the check.
  3. A failed check drops the model's text entirely and falls back to the
     deterministic template below. There is no partial acceptance and no
     "close enough".

WHY THERE IS A DETERMINISTIC FALLBACK AT ALL
--------------------------------------------
Because "no summary" and "a summary assembled from the same facts by a
format string" are not equally good, and the second is always correct. The
template is what runs with no model configured, when the call fails, and
when validation rejects the result — the same degrade-to-a-template shape
app/tool_calling.py's _build_assisted already uses. `source` on the response
says which one the caller got, so a demo screenshot can never pass a
template off as model-written or vice versa.
"""
from __future__ import annotations

import json

from app.grounding import is_grounded, neutral_scope_label, numerals
from app.schemas import DashboardScope, InsightNarrative, WorkforceInsight

# Hard cap on what the model is allowed to hand back. A narrative summary
# that runs longer than the findings it summarises has stopped being a
# summary; anything past this is treated as a failed call, not truncated,
# because a sentence cut mid-clause is worse than the template.
MAX_SUMMARY_CHARS = 420

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_SYSTEM_PROMPT = """You orient a reader arriving at a workforce dashboard.

You are given a JSON list of findings that have ALREADY been computed from \
the organisation's own records, plus the scope they were computed over.

Write two or three plain sentences saying what deserves attention first and \
why, connecting findings where they are related (a skill shortage and a \
project gap naming the same skill, say).

Rules, all absolute:
- Use ONLY numbers that appear verbatim in the JSON. Never add, total, \
average, estimate or recompute anything. If you want to state a figure that \
is not already in the JSON, leave it out instead.
- Never invent a finding, a cause, or a consequence that the JSON does not \
state.
- Never name a person. Never guess at one.
- No markdown, no bullet points, no headings. Prose only.
- Do not open with a restatement of the scope; the reader can see it.

Be direct and specific. If the findings are mild, say so plainly rather \
than manufacturing urgency."""


def _fact_numerals(insights: list[WorkforceInsight], scope: DashboardScope) -> set[str]:
    parts = [neutral_scope_label(scope), str(scope.headcount)]
    for insight in insights:
        parts += [insight.title, insight.detail, insight.recommendation, *insight.evidence]
    return numerals(" ".join(parts))


def _numerals_are_grounded(text: str, allowed: set[str]) -> bool:
    """Thin alias over app/grounding.is_grounded, kept as a name because the
    call sites below read better with it and because the report generator
    shares the same check -- see that module for the rule."""
    return is_grounded(text, allowed)


def _facts_payload(insights: list[WorkforceInsight], scope: DashboardScope) -> str:
    """The model's entire view of the world.

    Built field by field rather than by dumping the objects, so adding a
    field to WorkforceInsight later cannot silently widen what the model
    sees. `skill_ids` and `project_ids` are deliberately excluded: they are
    for the UI's drill-downs and mean nothing in prose, and every id kept
    out is an id that cannot be echoed into a sentence.
    """
    return json.dumps({
        # De-identified: a manager's scope label names them, and the
        # model has no use for whose team it is (app/grounding.py).
        "scope": {"label": neutral_scope_label(scope), "headcount": scope.headcount},
        "findings": [
            {
                "kind": i.kind,
                "severity": i.severity,
                "title": i.title,
                "detail": i.detail,
                "evidence": i.evidence,
                "recommendation": i.recommendation,
            }
            for i in insights
        ],
    }, indent=None)


# ---------------------------------------------------------------------------
# The deterministic template
# ---------------------------------------------------------------------------

def _derived_summary(insights: list[WorkforceInsight], scope: DashboardScope) -> str:
    """A correct summary with no model involved.

    Quotes the highest-severity finding's own title verbatim rather than
    rephrasing it — a template that paraphrases is a template that can be
    wrong, and the title was already written to be read on its own.
    """
    if not insights:
        return (
            "Nothing in this scope crosses a threshold worth flagging. That is the finding — "
            "no items are padded in to fill the section."
        )

    ordered = sorted(insights, key=lambda i: _SEVERITY_ORDER.get(i.severity, 3))
    high = [i for i in ordered if i.severity == "high"]
    medium = [i for i in ordered if i.severity == "medium"]
    low = [i for i in ordered if i.severity == "low"]
    lead = ordered[0]

    if high:
        opening = (
            f"{len(high)} finding{'s' if len(high) != 1 else ''} here need"
            f"{'' if len(high) != 1 else 's'} action."
        )
    else:
        opening = "Nothing here is urgent."

    rest: list[str] = []
    if medium:
        rest.append(f"{len(medium)} to watch")
    if low:
        rest.append(f"{len(low)} for information")
    tail = f" Beyond that, {' and '.join(rest)}." if rest else ""

    return f"{opening} Start with: {lead.title.rstrip('.')}. {lead.detail}{tail}"


# ---------------------------------------------------------------------------
# The model call
# ---------------------------------------------------------------------------

def _model_summary(insights: list[WorkforceInsight], scope: DashboardScope) -> str | None:
    """None, never an exception, whenever there is nothing to ask — no real
    model configured, the call failed, or what came back did not survive
    validation. Same degrade-don't-error shape phrase_answer uses."""
    # Imported here rather than at module scope so importing this module
    # never drags in the OpenAI client for a caller that only wants the
    # template -- and so app/analytics.py's own import graph stays clear of
    # it entirely.
    from openai import OpenAIError

    from app.tool_calling import OPENAI_CHAT_DEPLOYMENT, _get_openai_client, _mode

    if _mode() != "real":
        return None

    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _facts_payload(insights, scope)},
            ],
            # Ordering a handful of pre-computed findings into two sentences
            # is not a deliberation-worthy task, same call phrase_answer
            # makes for the same reason.
            reasoning_effort="minimal",
        )
    except OpenAIError:
        return None

    text = (response.choices[0].message.content or "").strip()
    if not text or len(text) > MAX_SUMMARY_CHARS:
        return None
    if not _numerals_are_grounded(text, _fact_numerals(insights, scope)):
        return None
    return text


def narrate(insights: list[WorkforceInsight], scope: DashboardScope) -> InsightNarrative:
    """The dashboard's opening paragraph.

    Always returns a summary — the template guarantees that — and always
    says which kind it is. Callers render `text` and may show `source` as
    provenance; nothing downstream needs to branch on it.
    """
    text = _model_summary(insights, scope)
    if text is not None:
        return InsightNarrative(text=text, source="model")
    return InsightNarrative(text=_derived_summary(insights, scope), source="derived")
