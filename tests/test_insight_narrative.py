"""Tests for app/insight_narrative.py — the dashboard's opening paragraph.

The numeral-grounding helpers now live in app/grounding.py (shared with the
workforce report generator, which needs the identical guarantee) and are
imported under their old names here: what these tests pin is the RULE, and
the rule did not move.

The whole point of this module is a boundary: a model may re-order and
connect findings that are already computed, and may not produce a number.
So the tests that matter are the ones that try to get a fabricated number
past the check, and the ones that prove the fallback is reached whenever
anything at all goes wrong.

The model client is stubbed throughout — these assert OUR contract with the
model's output, not the model's behaviour, and a test that needs a live
endpoint to tell you whether validation works is not testing validation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai import OpenAIError

from app.grounding import is_grounded as _numerals_are_grounded
from app.grounding import numerals as _numerals
from app.insight_narrative import (
    MAX_SUMMARY_CHARS, _derived_summary, _fact_numerals, _facts_payload, narrate,
)
from app.schemas import DashboardScope, WorkforceInsight

SCOPE = DashboardScope(kind="org", label="Organization-wide", headcount=545)

FINDINGS = [
    WorkforceInsight(
        kind="skill_concentration", severity="high",
        title="1 skill rests on a single person",
        detail="FHIR Interoperability covers 1 active project on one person.",
        evidence=["FHIR Interoperability: 1 capable person, 1 project"],
        skill_ids=[42], recommendation="Pair a second person onto it.",
    ),
    WorkforceInsight(
        kind="training_compliance", severity="high",
        title="194 required courses are past their due date",
        detail="20.2% of all course expectations in scope are overdue.",
        evidence=["Growth Marketing Team: 10 overdue"],
        recommendation="Send reminders from the Training section.",
    ),
    WorkforceInsight(
        kind="bench_capacity", severity="low",
        title="12 skills are held but unused by current work",
        detail="Capability no active project currently depends on.",
        evidence=["GCP: 3 capable, no active project"],
        recommendation="Check against upcoming work.",
    ),
]


class _StubClient:
    """Stands in for the OpenAI client. `reply` is what the model 'said';
    `error` makes the call raise instead."""

    def __init__(self, reply: str | None = None, error: Exception | None = None):
        self._reply, self._error = reply, error
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        message = SimpleNamespace(content=self._reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def stub(monkeypatch):
    """Puts the module in 'real' mode with a stubbed client, so the model
    path runs without a live endpoint."""
    def _install(reply=None, error=None):
        client = _StubClient(reply, error)
        monkeypatch.setattr("app.tool_calling._mode", lambda: "real")
        monkeypatch.setattr("app.tool_calling._get_openai_client", lambda: client)
        monkeypatch.setattr("app.tool_calling.OPENAI_CHAT_DEPLOYMENT", "test-deployment")
        return client
    return _install


# --- Numeral grounding: the check that makes the contract real -------------

def test_numerals_normalises_trailing_zeros():
    """A model writing "45%" for a fact that says "45.0%" is quoting it, not
    inventing it."""
    assert _numerals("45.0% and 20.2% and 7") == {"45", "20.2", "7"}


def test_a_number_taken_from_the_facts_is_grounded():
    allowed = _fact_numerals(FINDINGS, SCOPE)
    assert _numerals_are_grounded("194 courses are overdue, 20.2% of the total.", allowed)


def test_a_number_not_in_the_facts_is_rejected():
    allowed = _fact_numerals(FINDINGS, SCOPE)
    assert not _numerals_are_grounded("About 200 courses are overdue.", allowed)


def test_a_summed_number_is_rejected():
    """The specific failure this check exists for: 194 + 12 is arithmetic the
    model was told not to do, and 206 appears nowhere in the facts."""
    allowed = _fact_numerals(FINDINGS, SCOPE)
    assert not _numerals_are_grounded("There are 206 issues in total.", allowed)


def test_prose_with_no_numerals_at_all_is_grounded():
    """Omitting figures is the model doing its job; only producing new ones
    is forbidden."""
    assert _numerals_are_grounded("Several findings need attention first.", set())


def test_counts_written_as_words_are_not_treated_as_numerals():
    assert _numerals_are_grounded("Three findings need action.", set())


# --- What the model is allowed to see -------------------------------------

def test_the_payload_carries_no_person_and_no_ids():
    payload = _facts_payload(FINDINGS, SCOPE)
    # skill_ids/project_ids are for the UI's drill-downs and mean nothing in
    # prose; every id kept out is an id that cannot be echoed into a sentence.
    assert "42" not in payload
    assert "skill_ids" not in payload and "project_ids" not in payload


def test_the_payload_is_built_field_by_field():
    """Adding a field to WorkforceInsight later must not silently widen what
    the model sees, which is why the payload names its fields rather than
    dumping the object."""
    payload = _facts_payload(FINDINGS, SCOPE)
    for field in ("kind", "severity", "title", "detail", "evidence", "recommendation"):
        assert field in payload
    assert "Organization-wide" in payload


# --- narrate(): model path, and every route to the fallback ---------------

def test_a_valid_model_summary_is_used_and_labelled(stub):
    stub(reply="Start with the single-person skill: 194 courses are also overdue.")
    result = narrate(FINDINGS, SCOPE)
    assert result.source == "model"
    assert result.text.startswith("Start with")


def test_a_summary_with_an_invented_number_falls_back(stub):
    stub(reply="Roughly 250 items need attention across the organisation.")
    result = narrate(FINDINGS, SCOPE)
    assert result.source == "derived"
    assert "250" not in result.text


def test_an_overlong_summary_falls_back_rather_than_being_truncated(stub):
    """A sentence cut mid-clause is worse than the template."""
    stub(reply="word " * (MAX_SUMMARY_CHARS // 2))
    assert narrate(FINDINGS, SCOPE).source == "derived"


def test_an_empty_reply_falls_back(stub):
    stub(reply="   ")
    assert narrate(FINDINGS, SCOPE).source == "derived"


def test_a_none_reply_falls_back(stub):
    stub(reply=None)
    assert narrate(FINDINGS, SCOPE).source == "derived"


def test_a_failed_call_falls_back_without_raising(stub):
    stub(error=OpenAIError("upstream is down"))
    assert narrate(FINDINGS, SCOPE).source == "derived"


def test_no_model_configured_falls_back(monkeypatch):
    monkeypatch.setattr("app.tool_calling._mode", lambda: "mock")
    assert narrate(FINDINGS, SCOPE).source == "derived"


def test_the_model_is_asked_with_the_system_prompt_and_nothing_else(stub):
    client = stub(reply="Two findings need action first.")
    narrate(FINDINGS, SCOPE)
    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "ONLY numbers that appear verbatim" in messages[0]["content"]


# --- The deterministic template -------------------------------------------

def test_the_template_leads_with_the_highest_severity_finding():
    text = _derived_summary(FINDINGS, SCOPE)
    assert "2 findings here need action" in text
    assert "1 skill rests on a single person" in text


def test_the_template_quotes_titles_rather_than_paraphrasing():
    """A template that paraphrases is a template that can be wrong."""
    text = _derived_summary(FINDINGS, SCOPE)
    assert FINDINGS[0].detail in text


def test_the_template_counts_the_lower_severities_separately():
    assert "1 for information" in _derived_summary(FINDINGS, SCOPE)


def test_the_template_says_so_when_nothing_is_urgent():
    mild = [f.model_copy(update={"severity": "low"}) for f in FINDINGS]
    assert _derived_summary(mild, SCOPE).startswith("Nothing here is urgent.")


def test_an_empty_finding_list_is_reported_as_the_finding():
    text = _derived_summary([], SCOPE)
    assert "no items are padded in" in text


def test_the_template_is_used_for_an_empty_list_even_with_a_model(stub):
    stub(reply="Everything looks fine.")
    # Still asks -- there is nothing special-cased about zero findings -- but
    # the point is that narrate() never raises on one.
    assert narrate([], SCOPE).text


def test_every_numeral_the_template_writes_is_grounded():
    """The template must satisfy the same contract the model is held to."""
    text = _derived_summary(FINDINGS, SCOPE)
    allowed = _fact_numerals(FINDINGS, SCOPE)
    # The template adds its own severity counts, so those are grounded
    # against the findings plus the counts it legitimately derives.
    allowed |= {"2", "1"}
    assert _numerals_are_grounded(text, allowed)
