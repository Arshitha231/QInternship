"""A faithful-enough fake Azure AI Search backend for hermetic step-8 tests.

Real Azure behavior isn't reimplemented in full — but filter evaluation
matches exactly the clause shapes app.search_client._build_filter()
produces (both sides are ours to control), keyword/prefix/fuzzy matching
uses real prefix + Levenshtein-distance-1 logic against recovered query
tokens, and "vector similarity" uses a small hand-built concept embedding
plus real cosine similarity. It's not a real semantic model, but it's
enough to prove find_people() genuinely uses the vector channel to surface
matches with zero keyword overlap — not a canned answer.
"""
from __future__ import annotations

import re

# Hand-built "concepts" standing in for real embedding dimensions. A query
# or document's fake vector is 1.0 on every concept whose keywords appear
# in its lowercased text, so "good with dashboards" and "Power BI expert"
# land in the same concept despite sharing no literal words — the same
# relationship the real embedding model demonstrably captured in step 7/8's
# live verification.
#
# NOTE: keywords here must never collide with a test fixture's actual
# surname (e.g. "Ledger", "Cloud") — a name doubling as a concept trigger
# makes that person's own name-search spuriously "semantically match"
# every other document in that concept, which isn't a real embedding
# model's behavior and produces flaky, confusing test failures. Matching
# is word-boundary-aware (see fake_embed) specifically so short keywords
# like "go" can't also match as a bare substring of unrelated words like
# "good" — that bit us once already.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "data_reporting": ("power bi", "dashboard", "dashboards", "visualiz", "data analyst", "metrics", "numbers"),
    "backend_eng": ("backend", "software engineer", "python", "go", "node"),
    "cloud_infra": ("terraform", "infrastructure", "aws", "azure"),
    "finance": ("financial", "finance", "accounting"),
}
_CONCEPT_PATTERNS = {
    name: [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in kws]
    for name, kws in _CONCEPTS.items()
}


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def fake_embed(text: str) -> list[float]:
    # Deliberately no catch-all "generic" dimension: two texts that both
    # match zero concepts would otherwise tie at a false cosine of 1.0
    # (both being all-zero-except-generic), making two totally unrelated
    # people spuriously "semantically match". An all-zero vector's cosine
    # similarity is 0 by construction below (na=0 -> returns 0.0), which is
    # the correct "no relationship modeled" answer.
    text_l = text.lower()
    return [
        1.0 if any(p.search(text_l) for p in patterns) else 0.0
        for patterns in _CONCEPT_PATTERNS.values()
    ]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def recover_name_tokens(lucene_query: str) -> list[str]:
    """Undo app.search_client._name_query()'s "(t1* t2*) OR (t1~1 t2~1)" shape."""
    if lucene_query == "*":
        return []
    prefix_group = lucene_query.split(") OR (")[0].lstrip("(")
    return [t.rstrip("*") for t in prefix_group.split()]


_FILTER_CLAUSE_RE = {
    "is_active": re.compile(r"^is_active eq true$"),
    "org_unit": re.compile(r"^org_unit eq '(.*)'$"),
    "office_or_city": re.compile(r"^\(office eq '(.*)' or office_city eq '(.*)'\)$"),
    "available": re.compile(r"^availability_status eq 'available'$"),
    "skill": re.compile(r"^skills/any\(s: s/name eq '([^']*)'(?: and s/level eq '([^']*)')?\)$"),
    "language": re.compile(r"^skills/any\(s: s/category eq 'language' and s/name eq '([^']*)'\)$"),
}


def _split_top_level_and(filter_str: str) -> list[str]:
    """Split on ' and ' at paren-depth 0 — skills/any(s: ... and ...) has
    its own internal ' and ' that must NOT be split on."""
    parts, buf, depth, i = [], "", 0, 0
    while i < len(filter_str):
        ch = filter_str[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and filter_str[i:i + 5] == " and ":
            parts.append(buf)
            buf = ""
            i += 5
            continue
        buf += ch
        i += 1
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def doc_matches_filter(doc: dict, filter_str: str) -> bool:
    for clause in _split_top_level_and(filter_str):
        if _FILTER_CLAUSE_RE["is_active"].match(clause):
            if not doc["is_active"]:
                return False
            continue
        m = _FILTER_CLAUSE_RE["org_unit"].match(clause)
        if m:
            if doc["org_unit"] != m.group(1):
                return False
            continue
        m = _FILTER_CLAUSE_RE["office_or_city"].match(clause)
        if m:
            if doc["office"] != m.group(1) and doc["office_city"] != m.group(2):
                return False
            continue
        if _FILTER_CLAUSE_RE["available"].match(clause):
            if doc["availability_status"] != "available":
                return False
            continue
        m = _FILTER_CLAUSE_RE["language"].match(clause)
        if m:
            wanted = m.group(1)
            if not any(s["category"] == "language" and s["name"] == wanted for s in doc["skills"]):
                return False
            continue
        m = _FILTER_CLAUSE_RE["skill"].match(clause)
        if m:
            wanted_name, wanted_level = m.group(1), m.group(2)
            ok = any(
                s["name"] == wanted_name and (wanted_level is None or s["level"] == wanted_level)
                for s in doc["skills"]
            )
            if not ok:
                return False
            continue
        raise AssertionError(f"fake Search backend doesn't understand filter clause: {clause!r}")
    return True


class FakeSearchIndex:
    """Holds fake documents (built from real build_profile_text() output on
    the seeded test employees) and answers /docs/search-shaped request
    bodies the way find_people() actually sends them.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.doc_order: list[str] = []

    def add(self, doc: dict) -> None:
        self.doc_order.append(doc["id"])
        # Real Azure also default-searches profile_text (observed in live
        # verification), not just name fields — but every test here relies
        # only on properties that hold either way, so the fake keeps
        # keyword/fuzzy matching scoped to names for tractability.
        self.docs[doc["id"]] = {**doc, "_vector": fake_embed(doc["profile_text"])}

    def search(self, body: dict) -> dict:
        candidates = [
            self.docs[i] for i in self.doc_order
            if doc_matches_filter(self.docs[i], body.get("filter", ""))
        ]

        tokens = recover_name_tokens(body["search"])
        keyword_ranked = []
        for doc in candidates:
            name_tokens = [t.lower() for t in doc["full_name"].split()]
            if not tokens or any(
                nt.startswith(qt.lower()) or levenshtein(nt, qt.lower()) <= 1
                for qt in tokens for nt in name_tokens
            ):
                keyword_ranked.append(doc)

        vector_queries = body.get("vectorQueries")
        vector_ranked = []
        if vector_queries:
            qvec = vector_queries[0]["vector"]
            # Real dense embeddings return *some* similarity for nearly
            # everything, but this coarse concept vector is either an exact
            # concept match (cosine 1.0) or genuinely unrelated (cosine 0) —
            # there's no meaningful middle ground to rank, so a >0 floor is
            # the fake's equivalent of "actually semantically relevant"
            # rather than every filtered candidate becoming vector noise.
            scored = [(d, cosine(qvec, d["_vector"])) for d in candidates]
            vector_ranked = [d for d, score in sorted(scored, key=lambda x: x[1], reverse=True) if score > 0]

        # Simple RRF-style fusion (k=60, matching Azure's usual constant).
        scores: dict[str, float] = {}
        for rank, doc in enumerate(keyword_ranked):
            scores[doc["id"]] = scores.get(doc["id"], 0.0) + 1.0 / (60 + rank)
        for rank, doc in enumerate(vector_ranked):
            scores[doc["id"]] = scores.get(doc["id"], 0.0) + 1.0 / (60 + rank)

        ranked_ids = sorted(scores, key=lambda i: scores[i], reverse=True)
        top = body.get("top", len(ranked_ids))
        return {"value": [{"id": i} for i in ranked_ids[:top]]}
