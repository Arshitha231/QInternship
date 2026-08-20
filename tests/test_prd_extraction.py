"""Tests for app/prd_extraction.py -- the mock regex fallback, and the
real/model round-loop path faked the same way tests/test_proposed_changes.py
fakes app.doc_extraction._real_extract_project_doc: no model API touched,
the client is faked at app.tool_calling._get_openai_client (where
prd_extraction imports it from, same as doc_extraction does).
"""
from __future__ import annotations

import json

from openai import OpenAIError

from app.prd_extraction import (
    ExtractionResult,
    _mock_extract_requirement_notes,
    _mock_extract_requirements,
    _mock_extract_skill_requirements,
    _real_extract_requirements,
    extract_requirements,
)

# ---------------------------------------------------------------------------
# Mock extractor
# ---------------------------------------------------------------------------

def test_mock_extracts_a_required_skill():
    calls = _mock_extract_skill_requirements("This engagement requires Terraform at Expert level.")
    assert [(c.skill, c.minimum_level) for c in calls] == [("Terraform", "Expert")]


def test_mock_defaults_to_working_when_no_level_stated():
    calls = _mock_extract_skill_requirements("The team needs Kubernetes for this rollout.")
    assert [(c.skill, c.minimum_level) for c in calls] == [("Kubernetes", "Working")]


def test_mock_dedupes_a_repeated_skill():
    calls = _mock_extract_skill_requirements(
        "This project requires Terraform.\nThe client also requires Terraform at Expert level.")
    assert len(calls) == 1


def test_mock_extracts_a_qualitative_note():
    notes = _mock_extract_requirement_notes("- The client is sensitive about timeline slippage.")
    assert [n.note for n in notes] == ["The client is sensitive about timeline slippage."]


def test_mock_does_not_double_record_a_skill_line_as_a_note():
    # "requires ... Expert level" matches the skill pattern; it shouldn't
    # also show up as a free-text note just because it's a line of text.
    notes = _mock_extract_requirement_notes("This engagement requires Terraform at Expert level.")
    assert notes == []


def test_mock_ignores_lines_with_no_requirement_signal():
    notes = _mock_extract_requirement_notes("This is the project overview section.")
    assert notes == []


def test_mock_combined_extracts_both_kinds(monkeypatch):
    text = (
        "This engagement requires Terraform at Expert level.\n"
        "The client is sensitive about timeline slippage.\n"
    )
    result = _mock_extract_requirements(text)
    assert isinstance(result, ExtractionResult)
    assert [s.skill for s in result.skills] == ["Terraform"]
    assert len(result.notes) == 1


def test_extract_requirements_uses_mock_when_not_real(monkeypatch):
    from app import prd_extraction

    monkeypatch.setattr(prd_extraction, "_mode", lambda: "mock")
    result = extract_requirements("This engagement requires Terraform.")
    assert [s.skill for s in result.skills] == ["Terraform"]


# ---------------------------------------------------------------------------
# Real/model round loop -- faked client, same shape as
# tests/test_proposed_changes.py's _FakeClient family.
# ---------------------------------------------------------------------------

class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls):
        self.content = "" if tool_calls else "DONE"
        self.tool_calls = tool_calls or None


class _FakeCompletions:
    """Replays a scripted list of rounds, one per create() call. Each round
    is a list of ("skill", name, level) / ("note", text) tuples, or an
    Exception to raise instead."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls_made = 0
        self.seen_messages = []

    def create(self, **kwargs):
        self.seen_messages.append(kwargs["messages"])
        self.calls_made += 1
        round_spec = self.rounds.pop(0) if self.rounds else []
        if isinstance(round_spec, Exception):
            raise round_spec

        tool_calls = []
        for i, item in enumerate(round_spec):
            if item[0] == "skill":
                _, skill, level = item
                tool_calls.append(_FakeToolCall(
                    f"call-{self.calls_made}-{i}", "propose_skill_requirement",
                    json.dumps({"skill": skill, "minimum_level": level, "confidence": 0.9}),
                ))
            else:
                _, note = item
                tool_calls.append(_FakeToolCall(
                    f"call-{self.calls_made}-{i}", "propose_requirement_note",
                    json.dumps({"note": note, "confidence": 0.8}),
                ))
        return type("R", (), {"choices": [type("C", (), {"message": _FakeMessage(tool_calls)})()]})()


class _FakeClient:
    def __init__(self, rounds):
        self.completions = _FakeCompletions(rounds)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _fake_model(monkeypatch, rounds):
    from app import tool_calling
    client = _FakeClient(rounds)
    monkeypatch.setattr(tool_calling, "_get_openai_client", lambda: client)
    monkeypatch.setattr(tool_calling, "OPENAI_CHAT_DEPLOYMENT", "fake-deployment")
    return client


def test_real_extraction_collects_across_rounds(monkeypatch):
    client = _fake_model(monkeypatch, [
        [("skill", "Terraform", "Expert")],
        [("note", "Client wants weekly status updates.")],
        [],  # model answers in prose -- document finished
    ])
    result = _real_extract_requirements("...document text...")

    assert [(s.skill, s.minimum_level) for s in result.skills] == [("Terraform", "Expert")]
    assert [n.note for n in result.notes] == ["Client wants weekly status updates."]
    assert client.completions.calls_made == 3


def test_real_extraction_handles_a_mixed_round(monkeypatch):
    """A single round may legitimately emit both kinds at once -- there is
    no classification step forcing one or the other."""
    client = _fake_model(monkeypatch, [
        [("skill", "Terraform", "Working"), ("note", "Prefers an on-site presence.")],
        [],
    ])
    result = _real_extract_requirements("...document text...")

    assert [s.skill for s in result.skills] == ["Terraform"]
    assert [n.note for n in result.notes] == ["Prefers an on-site presence."]


def test_real_extraction_feeds_prior_rounds_back_to_the_model(monkeypatch):
    client = _fake_model(monkeypatch, [
        [("skill", "Terraform", "Working")],
        [],
    ])
    _real_extract_requirements("...document text...")

    second_round = client.completions.seen_messages[1]
    assert [m["role"] for m in second_round] == ["system", "user", "assistant", "tool"]
    assert second_round[2]["tool_calls"][0]["function"]["name"] == "propose_skill_requirement"
    assert second_round[3]["tool_call_id"] == second_round[2]["tool_calls"][0]["id"]


def test_real_extraction_dedupes_a_re_emitted_skill(monkeypatch):
    client = _fake_model(monkeypatch, [
        [("skill", "Terraform", "Working")],
        [("skill", "terraform", "Working")],  # same skill, different case
        [("skill", "Kubernetes", "Working")],
    ])
    result = _real_extract_requirements("...document text...")

    assert [s.skill for s in result.skills] == ["Terraform"]
    # A round that added nobody new ends the loop -- Kubernetes' scripted
    # round is never reached.
    assert client.completions.calls_made == 2


def test_real_extraction_dedupes_a_re_emitted_note(monkeypatch):
    client = _fake_model(monkeypatch, [
        [("note", "Client wants weekly updates.")],
        [("note", "Client wants weekly updates.")],
    ])
    result = _real_extract_requirements("...document text...")

    assert len(result.notes) == 1
    assert client.completions.calls_made == 2


def test_real_extraction_is_bounded_by_max_rounds(monkeypatch):
    client = _fake_model(monkeypatch, [
        [("skill", f"Skill {i}", "Working")] for i in range(50)
    ])
    result = _real_extract_requirements("...document text...")

    from app.prd_extraction import MAX_EXTRACTION_ROUNDS
    assert client.completions.calls_made == MAX_EXTRACTION_ROUNDS
    assert len(result.skills) == MAX_EXTRACTION_ROUNDS


def test_real_extraction_invalid_level_defaults_to_working(monkeypatch):
    _fake_model(monkeypatch, [
        [("skill", "Terraform", "Not A Real Level")],
        [],
    ])
    result = _real_extract_requirements("...document text...")
    assert result.skills[0].minimum_level == "Working"


def test_real_extraction_degrades_to_mock_when_nothing_was_extracted(monkeypatch):
    _fake_model(monkeypatch, [OpenAIError("boom")])
    result = _real_extract_requirements("This engagement requires Terraform.")
    # Degraded to the mock extractor, not an empty result or a raised error.
    assert [s.skill for s in result.skills] == ["Terraform"]


def test_real_extraction_keeps_partial_results_on_a_later_round_failure(monkeypatch):
    """A failure in round two must not throw away what round one already
    extracted -- same reasoning doc_extraction's own version of this holds
    to: partial real results beat discarding them for a regex re-read."""
    _fake_model(monkeypatch, [
        [("skill", "Terraform", "Expert")],
        OpenAIError("boom"),
    ])
    result = _real_extract_requirements("...document text...")
    assert [s.skill for s in result.skills] == ["Terraform"]
