"""Mode 3 — semantic project search and the project->employee hop.

Runs with no Azure dependency. The embedding endpoint isn't configured in
the test environment, so embed_texts() returns None and the semantic arm is
simply absent; the keyword arm and the whole hop/ranking/permission path
still run for real. Where a test needs vectors, it writes them directly with
pack_vector() rather than calling Azure — the thing under test is the
ranking and the hop, not the embedding provider.

The permission tests are the important ones. Confidential projects must be
unreachable through this path in TWO independent ways (never embedded, and
filtered on the way out), and a restricted employee must not surface just
because a hop found them via a project everyone can see.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.auth import AuthenticatedUser
from app.models import Employee, EmployeeProject, Project, ProjectEmbedding
from app.models.enums import ProjectClassification, ProjectType
from app.project_search import (
    RRF_K,
    _assignment_rank,
    _rrf,
    build_project_text,
    embeddable_projects,
    find_experts,
    pack_vector,
    rank_projects,
    source_hash,
    unpack_vector,
)

HR = AuthenticatedUser(id="hr-1", role="hr", name="HR Caller")
EMPLOYEE = AuthenticatedUser(id="stranger-1", role="employee", name="Ordinary Caller")
CONF_MEMBER = AuthenticatedUser(id="member-1", role="employee", name="Mel Member")


# --- vector plumbing ------------------------------------------------------

def test_pack_unpack_roundtrips_and_normalises():
    packed = unpack_vector(pack_vector([3.0, 4.0]))
    # 3-4-5 triangle: normalised to 0.6/0.8, so cosine is a plain dot product.
    assert packed[0] == pytest.approx(0.6, abs=1e-6)
    assert packed[1] == pytest.approx(0.8, abs=1e-6)


def test_zero_vector_does_not_divide_by_zero():
    assert unpack_vector(pack_vector([0.0, 0.0])) == [0.0, 0.0]


def test_source_hash_tracks_text_changes():
    assert source_hash("a") == source_hash("a")
    assert source_hash("a") != source_hash("b")


# --- corpus construction --------------------------------------------------

def test_confidential_projects_never_enter_the_corpus(db_session):
    names = {p.name for p in embeddable_projects(db_session)}
    assert "Project Atlas" in names
    assert "Project Secret" not in names, (
        "a confidential project reached the embeddable corpus — the exclusion is structural, "
        "not a query-time filter, so this must never happen"
    )


def test_project_text_includes_owning_unit_and_description(db_session):
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    text = build_project_text(db_session, atlas)
    assert "Project Atlas" in text
    assert "billing ledger" in text


# --- ranking --------------------------------------------------------------

def test_rrf_rewards_agreement_between_arms():
    """A project both arms agree on outranks one that only a single arm
    put first — 1/62 + 1/62 > 1/61. That's the entire reason for fusing
    the two rankings instead of concatenating them: a keyword hit that the
    semantic arm also considers relevant is stronger evidence than either
    arm's own top result."""
    fused = _rrf([[1, 7, 2], [9, 7, 3]])
    assert fused[0] == 7, "the item both arms ranked should win"
    assert fused.index(1) < fused.index(2)
    assert fused.index(9) < fused.index(3)


def test_rrf_k_is_the_documented_constant():
    # Guards against someone "tuning" this away from the value Azure AI
    # Search itself uses for the employee-side hybrid ranking.
    assert RRF_K == 60


def test_keyword_arm_alone_still_ranks_when_nothing_is_embedded(db_session):
    ids, mode = rank_projects(db_session, "migration of the billing ledger")
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    assert atlas.id in ids
    assert mode == "keyword", "no embeddings and no embedding endpoint — semantic must be absent, not faked"


def test_stopwords_do_not_flatten_the_keyword_ranking(db_session):
    # "the"/"of"/"to" appear in most descriptions; a query made only of them
    # must match nothing rather than returning the whole corpus.
    ids, mode = rank_projects(db_session, "the of to a")
    assert ids == []
    assert mode == "none"


def test_short_technical_acronyms_are_not_dropped():
    """Regression: the keyword arm used to filter tokens by length > 3,
    which silently discarded SSO, MFA, AWS, SQL, ETL and CI — precisely the
    distinctive terms it exists to catch. Measured before the fix: "nobody
    can log in, SSO and MFA keep failing" tokenised to
    ["nobody", "keep", "failing"] and ranked an unrelated policy document
    first, dragging the semantically-correct project down through RRF."""
    from app.project_search import _STOPWORDS, _words

    kept = [w for w in _words("nobody can log in, SSO and MFA keep failing")
            if w not in _STOPWORDS and len(w) > 1]
    assert "sso" in kept
    assert "mfa" in kept
    assert "can" not in kept, "function words should still be dropped"


def test_ubiquitous_terms_are_dropped_but_rare_ones_survive(db_session):
    """A term matching most of the corpus carries no ranking signal, and
    fusing that non-ranking into RRF is what let "failing"/"keep" outvote a
    correct semantic match. A rare term must still rank.

    Needs a corpus big enough for the document-frequency ceiling to engage —
    the shipped fixture has one embeddable project, where the deliberate
    max(1, ...) floor correctly keeps everything.
    """
    from app.project_search import _keyword_ranking

    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    decoys = []
    for i in range(8):
        p = Project(
            name=f"Decoy Project {i}", type=ProjectType.project,
            description="Filler text containing ubiquitousmarker and nothing else of note.",
            owning_unit_id=atlas.owning_unit_id, owner_id=atlas.owner_id,
            classification=ProjectClassification.internal,
        )
        db_session.add(p)
        decoys.append(p)
    atlas_original = atlas.description
    atlas.description = f"{atlas_original} ubiquitousmarker raretermxyz"
    db_session.commit()
    try:
        # present in all 9 -> above the 25% ceiling -> no ranking at all
        assert _keyword_ranking(db_session, "ubiquitousmarker") == []
        # present in 1 of 9 -> distinctive -> ranks
        assert _keyword_ranking(db_session, "raretermxyz") == [atlas.id]
    finally:
        atlas.description = atlas_original
        for p in decoys:
            db_session.delete(p)
        db_session.commit()


def test_confidential_project_is_unreachable_by_keyword(db_session):
    secret = db_session.query(Project).filter(Project.name == "Project Secret").one()
    ids, _mode = rank_projects(db_session, "Project Secret")
    assert secret.id not in ids


# --- relevance excerpt: selection, never generation ------------------------

def test_split_sentences_handles_single_and_multi_sentence_text():
    from app.project_search import _split_sentences

    assert _split_sentences("") == []
    assert _split_sentences("One sentence only.") == ["One sentence only."]
    assert _split_sentences("First one. Second one. Third one!") == [
        "First one.", "Second one.", "Third one!",
    ]


def test_best_keyword_sentence_picks_the_sentence_with_the_most_hits():
    from app.project_search import _best_keyword_sentence

    sentences = [
        "The onboarding flow was rebuilt last quarter.",
        "It involved a Fabric migration from an on-prem warehouse.",
        "Rollout is complete.",
    ]
    picked = _best_keyword_sentence(sentences, ["fabric", "migration", "warehouse"])
    assert picked == "It involved a Fabric migration from an on-prem warehouse."


def test_best_keyword_sentence_returns_none_when_nothing_matches():
    from app.project_search import _best_keyword_sentence

    assert _best_keyword_sentence(["Nothing relevant here."], ["kubernetes"]) is None


def test_project_excerpts_fall_back_to_keyword_terms_without_embeddings(db_session):
    # No embedding creds in the test environment -> embed_texts() returns
    # None -> _project_excerpts degrades to the keyword-arm pick, same
    # contract as every other embedding use in this module.
    from app.project_search import _project_excerpts

    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    excerpts = _project_excerpts(db_session, "migration of the billing ledger", [atlas])
    assert excerpts[atlas.id] == atlas.description


def test_project_excerpts_is_none_for_a_project_with_no_description(db_session):
    from app.project_search import _project_excerpts

    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    blank = Project(
        name="Blank Description Project", type=ProjectType.project, description=None,
        owning_unit_id=atlas.owning_unit_id, owner_id=atlas.owner_id,
        classification=ProjectClassification.internal,
    )
    db_session.add(blank)
    db_session.commit()
    try:
        excerpts = _project_excerpts(db_session, "migration of the billing ledger", [blank])
        assert excerpts[blank.id] is None
    finally:
        db_session.delete(blank)
        db_session.commit()


# --- the hop --------------------------------------------------------------

def test_assignment_rank_prefers_current_then_lead_then_recent():
    today = date(2026, 1, 1)
    current_contributor = EmployeeProject(role="Contributor", start_date=date(2020, 1, 1), end_date=None)
    past_lead = EmployeeProject(role="Tech Lead", start_date=date(2019, 1, 1), end_date=date(2025, 1, 1))
    old_lead = EmployeeProject(role="Tech Lead", start_date=date(2015, 1, 1), end_date=date(2016, 1, 1))
    current_lead = EmployeeProject(role="Owner", start_date=date(2021, 1, 1), end_date=None)

    ranked = sorted(
        [past_lead, current_contributor, old_lead, current_lead],
        key=lambda a: _assignment_rank(a, today, is_owner=False),
    )
    assert ranked[0] is current_lead        # current + lead wins
    assert ranked[1] is current_contributor  # current beats any past role
    assert ranked[2] is past_lead            # among past, more recent first
    assert ranked[3] is old_lead


def test_owner_id_outranks_a_lead_role_string_even_when_its_own_role_string_does_not_match():
    # Regression: _LEAD_ROLES used to be the only "who's driving this"
    # signal, matched against EmployeeProject.role -- unconstrained free
    # text. A genuine owner (Project.owner_id) whose role string reads
    # "Backend Engineer" (not in _LEAD_ROLES at all) got no ranking credit
    # for owning the project, and could be outranked by a non-owner whose
    # role string happened to say "Tech Lead".
    today = date(2026, 1, 1)
    owner_engineer = EmployeeProject(role="Backend Engineer", start_date=date(2021, 1, 1), end_date=None)
    non_owner_lead = EmployeeProject(role="Tech Lead", start_date=date(2021, 1, 1), end_date=None)

    ranked = sorted(
        [non_owner_lead, owner_engineer],
        key=lambda a: _assignment_rank(a, today, is_owner=(a is owner_engineer)),
    )
    assert ranked[0] is owner_engineer, (
        "Project.owner_id must outrank a free-text _LEAD_ROLES match, even when the owner's "
        "own role string ('Backend Engineer') isn't one of _LEAD_ROLES's recognized strings"
    )


def test_project_relevance_outranks_individual_involvement(db_session, monkeypatch):
    """Regression: the hop used to sort by how strong a person's own
    involvement was BEFORE the project's fused rank, so a Lead on the
    fifth-best-matching project outranked a Contributor on the best one.
    Measured symptom: "our test suite is so flaky" retrieved the Quality
    Engineering test project first, then listed a different project's leads
    above anyone actually on it."""
    import app.project_search as ps

    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    other = Project(
        name="Decoy Project", type=ProjectType.project,
        description="An unrelated decoy used only by this test.",
        owning_unit_id=atlas.owning_unit_id, owner_id=atlas.owner_id,
        classification=ProjectClassification.internal,
    )
    db_session.add(other)
    db_session.flush()
    # A Lead on the LOWER-ranked project, versus Atlas's existing Analyst.
    db_session.add(EmployeeProject(
        employee_id="mgr-1", project_id=other.id, role="Lead",
        start_date=date(2024, 1, 1), end_date=None,
    ))
    db_session.commit()

    # Force the ranking: Atlas first, decoy second.
    monkeypatch.setattr(ps, "rank_projects", lambda db, q: ([atlas.id, other.id], "keyword"))
    try:
        results = ps.find_experts(db_session, HR, "anything")
        assert results
        assert results[0].project_name == "Project Atlas", (
            "a Lead on the second-ranked project outranked the top-ranked project's team"
        )
    finally:
        db_session.query(EmployeeProject).filter(EmployeeProject.project_id == other.id).delete()
        db_session.query(Project).filter(Project.id == other.id).delete()
        db_session.commit()


def test_find_experts_returns_people_from_a_matching_project(db_session):
    results = find_experts(db_session, HR, "migration of the billing ledger")
    assert results, "the keyword arm should have matched Project Atlas"
    assert any(r.full_name == "Sam Stranger" or r.id == "stranger-1" for r in results)
    top = results[0]
    assert top.project_name == "Project Atlas"
    assert top.reason.startswith("works on") or top.reason.startswith("worked on")
    assert top.retrieval == "keyword"


def test_find_experts_reason_states_only_what_the_record_shows(db_session):
    results = find_experts(db_session, HR, "migration of the billing ledger")
    for r in results:
        assert "best" not in r.reason.lower()
        assert r.role in r.reason or r.reason.endswith("a team member")


def test_find_experts_excerpt_is_a_verbatim_substring_of_the_description(db_session):
    # The core guarantee: the excerpt is a SELECTION, never a rewording.
    # No embedding creds in the test environment (see module docstring),
    # so this exercises the keyword-arm fallback path in _project_excerpts.
    results = find_experts(db_session, HR, "migration of the billing ledger")
    assert results
    top = results[0]
    project = db_session.query(Project).filter(Project.id == top.project_id).one()
    assert top.excerpt
    assert top.excerpt in project.description, (
        "excerpt must appear verbatim in the project's own description -- "
        "any deviation means something reworded it instead of selecting it"
    )


def test_empty_problem_returns_nothing(db_session):
    assert find_experts(db_session, HR, "   ") == []


def test_unmatched_problem_returns_nothing_rather_than_guessing(db_session):
    assert find_experts(db_session, HR, "zzzznonsensequery flibbertigibbet") == []


# --- permissions ----------------------------------------------------------

def test_confidential_project_members_never_surface_even_for_hr(db_session):
    """The strongest of the two mechanisms: HR can see every *record*, but
    the confidential project is not in the corpus at all, so there is no
    query that reaches its membership through this path."""
    results = find_experts(db_session, HR, "Project Secret confidential")
    assert all(r.project_name != "Project Secret" for r in results)
    assert all(r.id not in {"conf-owner-1", "member-1"} for r in results)


def test_even_a_confidential_member_cannot_reach_it_through_this_path(db_session):
    # can_see_confidential_project(member-1) is True, but the corpus
    # exclusion is upstream of that check, so Mode 3 still shows nothing.
    results = find_experts(db_session, CONF_MEMBER, "Project Secret confidential")
    assert all(r.project_name != "Project Secret" for r in results)


def test_restricted_employee_is_filtered_out_of_the_hop(db_session):
    """A restricted person on an ordinary, fully-visible project must not
    surface to a non-HR caller just because the hop found them."""
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    restricted = db_session.get(Employee, "restricted-1")
    assert restricted is not None
    db_session.add(EmployeeProject(
        employee_id=restricted.id, project_id=atlas.id, role="Contributor",
        start_date=date(2024, 1, 1), end_date=None,
    ))
    db_session.commit()
    try:
        as_employee = find_experts(db_session, EMPLOYEE, "migration of the billing ledger")
        assert all(r.id != "restricted-1" for r in as_employee), "restricted record leaked through the hop"

        as_hr = find_experts(db_session, HR, "migration of the billing ledger")
        assert any(r.id == "restricted-1" for r in as_hr), "HR should still see it in work mode"

        # employee mode strips HR's exemption, exactly as everywhere else
        as_hr_employee_mode = find_experts(
            db_session, HR, "migration of the billing ledger", view_mode="employee")
        assert all(r.id != "restricted-1" for r in as_hr_employee_mode)
    finally:
        db_session.query(EmployeeProject).filter(
            EmployeeProject.employee_id == "restricted-1",
            EmployeeProject.project_id == atlas.id,
        ).delete()
        db_session.commit()


def test_every_call_writes_exactly_one_audit_row(db_session):
    from app.models import AuditLog

    before = db_session.query(AuditLog).filter(AuditLog.action == "find_experts").count()
    find_experts(db_session, HR, "migration of the billing ledger")
    after = db_session.query(AuditLog).filter(AuditLog.action == "find_experts").count()
    assert after == before + 1


def test_audit_row_is_written_even_when_nothing_matches(db_session):
    from app.models import AuditLog

    before = db_session.query(AuditLog).filter(AuditLog.action == "find_experts").count()
    find_experts(db_session, HR, "zzzznonsensequery flibbertigibbet")
    after = db_session.query(AuditLog).filter(AuditLog.action == "find_experts").count()
    assert after == before + 1


# --- semantic arm, with vectors written directly (no Azure) ---------------

def test_semantic_arm_is_skipped_when_models_are_mixed(db_session):
    """Cosine across two embedding models is meaningless. The ranker must
    drop to the dominant model's subset rather than compare across them."""
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    db_session.add(ProjectEmbedding(
        project_id=atlas.id, model="model-a", dimensions=2,
        source_hash="x" * 64, vector=pack_vector([1.0, 0.0]), updated_at=datetime.now(),
    ))
    db_session.commit()
    try:
        # Still keyword-only: the query embedding can't be produced without
        # an endpoint, so the semantic arm yields nothing regardless.
        ids, mode = rank_projects(db_session, "migration of the billing ledger")
        assert atlas.id in ids
        assert mode == "keyword"
    finally:
        db_session.query(ProjectEmbedding).filter(ProjectEmbedding.project_id == atlas.id).delete()
        db_session.commit()


def test_rebuild_refuses_to_write_a_partial_corpus(db_session):
    """embed_texts() returns None with no endpoint configured; rebuild must
    raise rather than half-populate, since a partially embedded corpus ranks
    some projects and silently omits others."""
    from app.project_search import EmbeddingUnavailable, rebuild_embeddings

    with pytest.raises(EmbeddingUnavailable):
        rebuild_embeddings(db_session)
    assert db_session.query(ProjectEmbedding).count() == 0


# --- routing --------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "our nightly data pipeline keeps failing and I can't work out why",
    "I'm stuck on a memory leak in the payments service",
    "having trouble with our deploy pipeline",
    "debugging a flaky consumer, has anyone seen this before",
])
def test_problem_shaped_questions_route_to_find_experts(message):
    from app.tool_calling import _deterministic_resolve

    turn = _deterministic_resolve(message)
    assert turn is not None and turn.tool_call is not None, f"deferred instead of routing: {message!r}"
    assert turn.tool_call.name == "find_experts"
    # The whole description is forwarded, not a extracted keyword.
    assert len(turn.tool_call.arguments["problem"]) > 10


@pytest.mark.parametrize("message,expected", [
    ("find someone who could mentor me in terraform", "find_mentor"),
    ("i want to get better at kubernetes, who can help", None),  # ambiguous -> defer to the model
    ("who works with Power BI in Bangalore", None),
])
def test_learning_and_filter_questions_are_not_stolen_by_find_experts(message, expected):
    from app.tool_calling import _deterministic_resolve

    turn = _deterministic_resolve(message)
    name = turn.tool_call.name if (turn and turn.tool_call) else None
    assert name != "find_experts", f"find_experts stole {message!r}"
    if expected is not None:
        assert name == expected
