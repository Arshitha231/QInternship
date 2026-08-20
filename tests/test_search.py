"""Step 8: find_people's hybrid-search retrieve path.

Hermetic — never talks to real Azure. app.search_client's SEARCH_ENDPOINT/
KEY are monkeypatched to non-empty fake values, httpx.post is redirected
to tests/fake_search.py's FakeSearchIndex (real prefix/fuzzy/cosine
matching against real filter clauses, not canned answers — see that
module's docstring), and _embed_query is replaced with the fake's concept
embedding so vector behavior is deterministic and inspectable.

These tests hold find_people to the exact same guarantees test_visibility.py
already proved for the plain SQL path — this is the same pipeline with a
different retrieval source, so nothing about permissions should differ.
"""
from types import SimpleNamespace

import pytest

from app.models import Employee, EmployeeSkill, Office, OrgUnit, Skill
from app.people import MAX_SEARCH_RESULTS
from app.search_index import build_profile_text
from tests.conftest import auth_headers
from tests.fake_search import FakeSearchIndex, fake_embed


@pytest.fixture
def search_index(monkeypatch, db_session) -> FakeSearchIndex:
    from app import search_client

    fake = FakeSearchIndex()
    employees = db_session.query(Employee).filter(Employee.is_active.is_(True)).all()
    for emp in employees:
        org_unit = db_session.get(OrgUnit, emp.org_unit_id) if emp.org_unit_id else None
        office = db_session.get(Office, emp.office_id) if emp.office_id else None
        skill_rows = (
            db_session.query(EmployeeSkill, Skill)
            .join(Skill, EmployeeSkill.skill_id == Skill.id)
            .filter(EmployeeSkill.employee_id == emp.id)
            .all()
        )
        fake.add({
            "id": emp.id,
            "full_name": emp.full_name,
            "job_title": emp.job_title,
            "org_unit": org_unit.name if org_unit else None,
            "office": office.name if office else None,
            "office_city": office.city if office else None,
            "availability_status": emp.availability_status.value,
            "is_active": emp.is_active,
            "skills": [{"name": sk.name, "level": es.level.value, "category": sk.category.value}
                      for es, sk in skill_rows],
            "profile_text": build_profile_text(db_session, emp),
        })

    monkeypatch.setattr(search_client, "SEARCH_ENDPOINT", "https://fake-search.test")
    monkeypatch.setattr(search_client, "SEARCH_KEY", "fake-key")

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    def fake_post(url, headers=None, json=None, timeout=None):
        return FakeResponse(fake.search(json))

    monkeypatch.setattr(search_client.httpx, "post", fake_post)
    monkeypatch.setattr(search_client, "_embed_query", lambda text: fake_embed(text))

    return fake


# ---------------------------------------------------------------------------
# 1. Misspelled name still finds the right person (fuzzy matching).
# ---------------------------------------------------------------------------

async def test_misspelled_name_finds_right_person(client, search_index):
    resp = await client.get("/people", params={"name": "Shaun Wilsen"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    names = [p["full_name"] for p in resp.json()]
    assert "Sean Wilson" in names


# ---------------------------------------------------------------------------
# 2. Semantic query surfaces Power BI people with zero keyword overlap.
# ---------------------------------------------------------------------------

async def test_semantic_query_finds_power_bi_without_keyword_overlap(client, search_index):
    query = "good with dashboards"
    # Confirm this test is actually isolating the vector channel: the query
    # shares no word with Dana's indexed text at all.
    dana_doc = search_index.docs["search-dana"]
    assert not (set(query.lower().split()) & set(dana_doc["profile_text"].lower().split()))

    resp = await client.get("/people", params={"name": query}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    names = [p["full_name"] for p in resp.json()]
    assert "Dana Powers" in names


# ---------------------------------------------------------------------------
# 3. Filters narrow results correctly: org unit, skill+level, location, language.
# ---------------------------------------------------------------------------

async def test_org_unit_filter_narrows(client, search_index):
    resp = await client.get("/people", params={"name": "Cloud", "org_unit": "Platform Engineering"},
                            headers=auth_headers("employee"))
    assert "Taylor Cloud" in [p["full_name"] for p in resp.json()]

    resp = await client.get("/people", params={"name": "Ledger", "org_unit": "Platform Engineering"},
                            headers=auth_headers("employee"))
    assert resp.json() == []  # Jordan Ledger is in Finance Operations, not Platform Engineering


async def test_skill_and_level_filter_narrows(client, search_index):
    resp = await client.get("/people", params={"name": "Cloud", "skill": "Terraform", "level": "Expert"},
                            headers=auth_headers("employee"))
    assert "Taylor Cloud" in [p["full_name"] for p in resp.json()]

    resp = await client.get("/people", params={"name": "Ledger", "skill": "Terraform", "level": "Expert"},
                            headers=auth_headers("employee"))
    assert resp.json() == []  # Jordan Ledger only has Terraform at Learning, not Expert


async def test_location_filter_narrows(client, search_index):
    resp = await client.get("/people", params={"name": "Ledger", "office": "Satellite City"},
                            headers=auth_headers("employee"))
    assert "Jordan Ledger" in [p["full_name"] for p in resp.json()]

    resp = await client.get("/people", params={"name": "Ledger", "office": "Testville"},
                            headers=auth_headers("employee"))
    assert resp.json() == []  # Jordan Ledger is in Satellite City, not Testville


async def test_language_filter_narrows(client, search_index):
    resp = await client.get("/people", params={"name": "Ledger", "language": "French"},
                            headers=auth_headers("employee"))
    assert "Jordan Ledger" in [p["full_name"] for p in resp.json()]

    resp = await client.get("/people", params={"name": "Cloud", "language": "French"},
                            headers=auth_headers("employee"))
    assert resp.json() == []  # Taylor Cloud doesn't speak French


# ---------------------------------------------------------------------------
# 4. Restricted record: empty for non-HR, present for HR — through Search.
# ---------------------------------------------------------------------------

async def test_restricted_person_absent_for_non_hr_via_search(client, search_index):
    resp = await client.get("/people", params={"name": "Rory Restricted"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_restricted_person_present_for_hr_via_search(client, search_index):
    resp = await client.get("/people", params={"name": "Rory Restricted"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert "Rory Restricted" in [p["full_name"] for p in resp.json()]


# ---------------------------------------------------------------------------
# 5. A field the caller can't see is absent (not null), for a person
# discovered via the hybrid-search path — same standard as
# test_visibility.py::test_absent_field_is_truly_missing_not_null.
# ---------------------------------------------------------------------------

async def test_field_absent_not_null_for_person_found_via_search(client, search_index):
    found = await client.get("/people", params={"name": "Riley Report"}, headers=auth_headers("employee"))
    resp_id = next(p["id"] for p in found.json() if p["full_name"] == "Riley Report")

    detail = await client.get(f"/people/{resp_id}", headers=auth_headers("employee", "some-unrelated-caller"))
    body = detail.json()
    assert "personal_mobile" not in body
    assert "hire_date" not in body
    assert "cost_centre" not in body
    # Same control as the SQL-path test: a legitimately empty but visible
    # field still comes through as null, proving fields aren't just all
    # being dropped wholesale.
    assert "away_until_month" in body
    assert body["away_until_month"] is None


# ---------------------------------------------------------------------------
# 6. Result cap on a broad semantic query — the tight relevance cap
# (MAX_SEARCH_RESULTS), not the wider browse/filter ceiling. Hybrid search
# results are relevance-ranked; past the first handful, matches stop being
# meaningfully related to the query, so a much smaller cap belongs here
# than on plain filter browsing (see test_visibility.py's
# test_result_cap_holds_on_broad_query, which covers the no-query browse
# case and still expects MAX_RESULTS).
# ---------------------------------------------------------------------------

async def test_result_cap_holds_on_broad_query_via_search(client, search_index):
    resp = await client.get("/people", params={"name": "Person"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    body = resp.json()
    # 60 "Bulk Person N" fixtures match this query — comfortably more than
    # the cap, so this proves truncation actually happened, not a
    # coincidence of exactly 5 matches existing.
    assert len(body) == MAX_SEARCH_RESULTS


# ---------------------------------------------------------------------------
# 8. Phase 3 Round 2 (ARCHITECTURE_2.md §11/§16): a pure filter combo, no
# name/query text, must skip Search entirely and go straight to the SQL
# path — even with Search fully configured (search_index fixture), unlike
# every filter test above, which always pairs a filter with `name` and so
# never actually exercises this. Search is for ranking/semantic work only.
# ---------------------------------------------------------------------------

async def test_pure_filter_query_never_calls_search(client, search_index, monkeypatch):
    from app import people as people_module

    real_search_people = people_module.search_people
    calls: list[dict] = []

    def counting_search_people(**kwargs):
        calls.append(kwargs)
        return real_search_people(**kwargs)

    monkeypatch.setattr(people_module, "search_people", counting_search_people)

    resp = await client.get(
        "/people", params={"org_unit": "Platform Engineering", "skill": "Terraform"},
        headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    assert calls == []  # Search must never be invoked for a pure filter combo
    names = [p["full_name"] for p in resp.json()]
    assert "Taylor Cloud" in names  # still finds the right person, via the SQL fallback


async def test_query_with_free_text_still_calls_search(client, search_index, monkeypatch):
    # Control for the test above: confirm the counting wrapper itself would
    # actually catch a call, by proving Search IS still invoked the moment
    # there's real text to rank.
    from app import people as people_module

    real_search_people = people_module.search_people
    calls: list[dict] = []

    def counting_search_people(**kwargs):
        calls.append(kwargs)
        return real_search_people(**kwargs)

    monkeypatch.setattr(people_module, "search_people", counting_search_people)

    resp = await client.get("/people", params={"name": "Cloud"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 7. Embedding service failure degrades to keyword/fuzzy, never errors.
# ---------------------------------------------------------------------------

async def test_embedding_failure_falls_back_to_keyword_fuzzy(client, search_index, monkeypatch):
    from app import search_client

    # Simulate exactly what _embed_query does on a real OpenAI failure —
    # it catches the error internally and returns None, never raises.
    monkeypatch.setattr(search_client, "_embed_query", lambda text: None)

    resp = await client.get("/people", params={"name": "Sean Wilson"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    names = [p["full_name"] for p in resp.json()]
    assert "Sean Wilson" in names

    # The vector-only match should now genuinely be unreachable: with no
    # embedding, "good with dashboards" has no keyword/fuzzy overlap with
    # Dana Powers' name at all, so she can't be found this way anymore —
    # proving the fallback really did drop the vector channel rather than
    # silently keeping it.
    resp2 = await client.get("/people", params={"name": "good with dashboards"}, headers=auth_headers("employee"))
    assert resp2.status_code == 200
    assert "Dana Powers" not in [p["full_name"] for p in resp2.json()]


# ---------------------------------------------------------------------------
# Embedding cache (app/search_client.py). The expensive caller is not the
# query: project_search's excerpt picker embeds the problem PLUS every
# sentence of every matched project on every request.
# ---------------------------------------------------------------------------

class _CountingEmbedClient:
    def __init__(self):
        self.batches = []

    @property
    def embeddings(self):
        return self

    def create(self, model, input):
        self.batches.append(list(input))
        return SimpleNamespace(data=[
            SimpleNamespace(index=i, embedding=[float(len(t)), 0.0, 1.0])
            for i, t in enumerate(input)
        ])


def test_embed_texts_only_sends_texts_it_has_not_seen(monkeypatch):
    import app.search_client as sc
    sc.clear_vector_cache()
    client = _CountingEmbedClient()
    monkeypatch.setattr(sc, "_get_openai_client", lambda: client)

    first = sc.embed_texts(["alpha", "beta"])
    second = sc.embed_texts(["beta", "gamma"])

    assert client.batches == [["alpha", "beta"], ["gamma"]]  # beta reused
    assert second[0] == first[1]                             # and identical
    sc.clear_vector_cache()


def test_embed_texts_keeps_input_order_when_partly_cached(monkeypatch):
    """rebuild_embeddings zips results back onto its pending rows, so order
    is load-bearing -- and a partial cache hit is where it would break."""
    import app.search_client as sc
    sc.clear_vector_cache()
    client = _CountingEmbedClient()
    monkeypatch.setattr(sc, "_get_openai_client", lambda: client)

    sc.embed_texts(["bb"])
    out = sc.embed_texts(["a", "bb", "cccc"])
    assert [v[0] for v in out] == [1.0, 2.0, 4.0]  # length-derived, in order
    sc.clear_vector_cache()


def test_a_failed_embedding_call_is_not_cached_as_a_failure(monkeypatch):
    import app.search_client as sc
    from openai import OpenAIError
    sc.clear_vector_cache()

    class _Flaky:
        tries = 0

        @property
        def embeddings(self): return self

        def create(self, model, input):
            type(self).tries += 1
            if type(self).tries == 1:
                raise OpenAIError("transient")
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0])])

    monkeypatch.setattr(sc, "_get_openai_client", lambda: _Flaky())
    assert sc.embed_texts(["x"]) is None      # degrades, as documented
    assert sc.embed_texts(["x"]) == [[1.0]]   # and recovers on the next call
    sc.clear_vector_cache()
