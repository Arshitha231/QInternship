"""build_profile_text() and the Azure AI Search index schema.

No Azure connection here — this only defines what would be sent, not a
client that sends it. Step 8 wires an actual azure-search-documents
SearchIndexClient against INDEX_SCHEMA and calls build_profile_text() per
employee for batch embedding.

Two design decisions worth calling out:

1. Confidential projects are never mentioned in profile_text. This isn't a
   query-time redaction — the name simply never enters the corpus, so
   "search a confidential project name" is structurally guaranteed to
   return nothing, per spec, rather than relying on every future caller of
   the search API to remember to filter it out downstream.

2. Nothing else about the caller matters here. Record-level and field-level
   restrictions (hire_date, cost_centre, personal_mobile, restricted
   records, ...) are a step 4/6 concern applied when *retrieving* results,
   not when *indexing* them — the same way find_people's DB query pulls
   restricted employees first and filters them after. profile_text is
   intentionally built from the full record for every active employee,
   restricted or not; nothing HR-only or ABAC-gated is in the field list
   at all, so there's nothing to leak regardless.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.models import Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill
from app.models.enums import ProjectClassification, SkillCategory


def _month(d: date | None) -> str | None:
    return d.strftime("%b %Y") if d else None


def build_profile_text(db: Session, employee: Employee) -> str:
    """The text that gets embedded for one employee.

    Includes: name, preferred name, title, org unit, office, skills with
    levels, project history (non-confidential only), languages, bio. Every
    section is optional in the output — a record with only a name and title
    still produces valid, if short, text.
    """
    lines: list[str] = []

    # --- identity: name, preferred name, title, org unit ------------------
    identity = employee.full_name
    if employee.preferred_name and employee.preferred_name != employee.full_name:
        identity += f' (goes by "{employee.preferred_name}")'
    org_unit = db.get(OrgUnit, employee.org_unit_id) if employee.org_unit_id else None
    org_unit_name = org_unit.name if org_unit else "an unspecified team"
    lines.append(f"{identity} is a {employee.job_title} in {org_unit_name}.")

    # --- office -------------------------------------------------------------
    office = db.get(Office, employee.office_id) if employee.office_id else None
    if office:
        lines.append(f"Based in the {office.name} ({office.city}, {office.country}).")
    else:
        lines.append("Works remotely, with no fixed office.")

    # --- skills / languages (same table, split by category) ---------------
    skill_rows = (
        db.query(EmployeeSkill, Skill)
        .join(Skill, EmployeeSkill.skill_id == Skill.id)
        .filter(EmployeeSkill.employee_id == employee.id)
        .all()
    )
    non_language = [(es, sk) for es, sk in skill_rows if sk.category != SkillCategory.language]
    languages = [(es, sk) for es, sk in skill_rows if sk.category == SkillCategory.language]

    if non_language:
        phrases = [f"{sk.name} ({es.level.value})" for es, sk in non_language]
        lines.append("Skills: " + ", ".join(phrases) + ".")
    if languages:
        phrases = [f"{sk.name} ({es.level.value})" for es, sk in languages]
        lines.append("Languages: " + ", ".join(phrases) + ".")

    # --- project history: confidential projects are never included --------
    project_rows = (
        db.query(EmployeeProject, Project)
        .join(Project, EmployeeProject.project_id == Project.id)
        .filter(EmployeeProject.employee_id == employee.id)
        .all()
    )
    indexable_projects = [(ep, p) for ep, p in project_rows if p.classification != ProjectClassification.confidential]
    if indexable_projects:
        phrases = []
        for ep, p in indexable_projects:
            span = f"{_month(ep.start_date)}–{_month(ep.end_date) or 'present'}"
            phrases.append(f"{ep.role} on {p.name} ({span})")
        lines.append("Project history: " + "; ".join(phrases) + ".")

    # --- bio ------------------------------------------------------------
    if employee.bio:
        lines.append(employee.bio)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Azure AI Search index schema (REST index definition shape). Not created
# against Azure — this is the JSON document step 8 would PUT to
# https://{service}.search.windows.net/indexes/{name}?api-version=2024-07-01.
# ---------------------------------------------------------------------------

INDEX_SCHEMA: dict = {
    "name": "employees-index",
    "fields": [
        # Key. Employee.id is a UUID string — URL-safe, valid as a Search key.
        {"name": "id", "type": "Edm.String", "key": True,
         "searchable": False, "filterable": True, "retrievable": True},

        # --- Name search: exact/partial via keyword+prefix, misspelled via
        # fuzzy. Fuzzy (`~`) and simple prefix (`*`) are query-time Lucene
        # syntax features, not index-schema settings — nothing extra needed
        # here for those. The edge-ngram analyzer below is the one thing
        # that *is* an index-time choice: it makes prefix queries fast at
        # scale (matches from the first couple of characters typed) instead
        # of relying on a full wildcard scan every time.
        {"name": "full_name", "type": "Edm.String",
         "searchable": True, "filterable": False, "sortable": True, "retrievable": True,
         "indexAnalyzer": "name_prefix_analyzer", "searchAnalyzer": "standard.lucene"},
        {"name": "preferred_name", "type": "Edm.String",
         "searchable": True, "filterable": False, "retrievable": True,
         "indexAnalyzer": "name_prefix_analyzer", "searchAnalyzer": "standard.lucene"},

        {"name": "job_title", "type": "Edm.String",
         "searchable": True, "filterable": True, "facetable": True, "retrievable": True},

        # --- Structured filter dimensions (spec: "Filters: org unit, skill,
        # level, location, language"). Their content is also folded into
        # profile_text for semantic matching — these fields exist for exact
        # OData filtering (org_unit eq 'Platform Engineering'), not free text.
        {"name": "org_unit", "type": "Edm.String",
         "searchable": False, "filterable": True, "facetable": True, "retrievable": True},
        {"name": "office", "type": "Edm.String",
         "searchable": False, "filterable": True, "facetable": True, "retrievable": True},
        {"name": "office_city", "type": "Edm.String",
         "searchable": False, "filterable": True, "facetable": True, "retrievable": True},

        # skill + level + language filters, all via one complex collection
        # (skills and languages are the same underlying table, split only by
        # category) — enables OData filters like
        # skills/any(s: s/name eq 'Power BI' and s/level eq 'Expert') and
        # skills/any(s: s/category eq 'language' and s/name eq 'Hindi').
        {"name": "skills", "type": "Collection(Edm.ComplexType)",
         "fields": [
             {"name": "name", "type": "Edm.String",
              "searchable": True, "filterable": True, "facetable": True, "retrievable": True},
             {"name": "level", "type": "Edm.String",
              "filterable": True, "facetable": True, "retrievable": True},
             {"name": "category", "type": "Edm.String",
              "filterable": True, "facetable": True, "retrievable": True},
             {"name": "source", "type": "Edm.String", "filterable": True, "retrievable": True},
         ]},

        {"name": "availability_status", "type": "Edm.String",
         "searchable": False, "filterable": True, "facetable": True, "retrievable": True},

        # Excludes soft-deleted employees from query results without ever
        # deleting the document; not user-facing.
        {"name": "is_active", "type": "Edm.Boolean",
         "filterable": True, "retrievable": False},

        # --- The main semantic/BM25 text field. build_profile_text()'s
        # output goes here verbatim.
        {"name": "profile_text", "type": "Edm.String",
         "searchable": True, "filterable": False, "retrievable": True,
         "analyzer": "en.microsoft"},

        # --- Vector field for the "description of a person" search type.
        # 1536 = text-embedding-3-small's output size; adjust if a
        # different Azure OpenAI embedding model is used once credentials
        # exist. Not retrievable — never returned in results, only searched.
        {"name": "profile_vector", "type": "Collection(Edm.Single)",
         "searchable": True, "retrievable": False, "dimensions": 1536,
         "vectorSearchProfile": "profile-vector-profile"},
    ],

    # Native RRF hybrid search needs no special index config beyond having
    # both a searchable text field and a vector field present — RRF fusion
    # of the keyword and vector rankings happens at query time, when a
    # request submits both `search=` and `vectorQueries=[...]` together.
    # This section only configures *how* the vector half is indexed/searched.
    "vectorSearch": {
        "algorithms": [
            {"name": "hnsw-config", "kind": "hnsw",
             "hnswParameters": {"metric": "cosine", "m": 4, "efConstruction": 400, "efSearch": 500}},
        ],
        "profiles": [
            {"name": "profile-vector-profile", "algorithm": "hnsw-config"},
        ],
    },

    # Semantic reranking ("native RRF, then semantic reranking") — applied
    # as a second pass over the RRF-fused results at query time.
    "semantic": {
        "configurations": [
            {"name": "employee-semantic-config",
             "prioritizedFields": {
                 "titleField": {"fieldName": "full_name"},
                 "prioritizedContentFields": [{"fieldName": "profile_text"}],
                 "prioritizedKeywordsFields": [{"fieldName": "job_title"}],
             }},
        ],
    },

    # Edge-ngram analyzer for fast name-prefix matching (see full_name
    # above). Index-time only — deliberately not the search-time analyzer,
    # since matching a query's ngrams against ngrams would over-match.
    "analyzers": [
        {"name": "name_prefix_analyzer", "@odata.type": "#Microsoft.Azure.Search.CustomAnalyzer",
         "tokenizer": "name_prefix_tokenizer", "tokenFilters": ["lowercase"]},
    ],
    "tokenizers": [
        {"name": "name_prefix_tokenizer", "@odata.type": "#Microsoft.Azure.Search.EdgeNGramTokenizer",
         "minGram": 2, "maxGram": 15, "tokenChars": ["letter", "digit"]},
    ],
}


if __name__ == "__main__":
    import json
    from pathlib import Path

    out = Path(__file__).resolve().parent.parent / "search_index_schema.json"
    out.write_text(json.dumps(INDEX_SCHEMA, indent=2) + "\n")
    print(f"Wrote {out}")
