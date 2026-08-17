"""find_people and get_person — the only two ways employee data leaves this
service in step 4+. Both follow the same fixed pipeline:

    retrieve -> filter records -> filter fields -> department check
             -> cap results -> write audit_log -> respond

The model (step 9) will call these same two functions and nothing else — it
never touches the database directly, and permission logic never runs inside
the model's reach.

Step 8: find_people's retrieve step now tries Azure AI Search (hybrid
keyword+fuzzy+vector via native RRF) first when there's a name/description
query to rank on, falling back to the plain SQL query when Search isn't
configured, has no query text to work with, or errors.

"Filter records" is no longer a post-retrieval Python step. app.policy.enforce()
is called once per request, and BOTH retrieval paths apply its
required_filters natively before any row comes back — the SQL branch via
app.query_compiler.apply_filter on its own Select, the Search branch by
translating the same obligations into an OData filter (app/search_client.py's
_build_filter). Search still never sees the caller's identity or role and
never makes its own visibility decision — it only compiles whatever decision
enforce() already made into its own filter syntax, same division of
responsibility as before, just enforced earlier. is_record_visible/
visible_fields (app/permissions.py) are no longer called anywhere in this
file; app.policy.excluded_by_obligations/compute_visible_fields replace them,
driven by the same enforce() decision. See this repo's migration notes for
why app/permissions.py itself is untouched — those two functions remain the
live gate for app/directory_tools.py and app/notifications.py, a separate,
deferred scope.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth import AuthenticatedUser
from app.certifications import employee_training_status
from app.models import (
    AuditLog,
    Employee,
    EmployeeProject,
    EmployeeSkill,
    Office,
    OrgUnit,
    Project,
    Skill,
)
from app.models.enums import AvailabilityStatus, SkillLevel
from app.permissions import ViewMode, can_see_confidential_project
from app.policy import can_see_direct_reports, compute_visible_fields, enforce, excluded_by_obligations
from app.query_compiler import apply_filter, compile_query, enforced_person_ref
from app.query_plan import PeopleQuery
from app.schemas import OfficeOut, PersonDetail, PersonRef, PersonSummary, ProjectHistoryItem, SkillOut
from app.search_reindex import reindex_employee
from app.search_client import search_people

# Query-level restriction: one colleague's email is a lookup, every
# employee's email in one response is bulk extraction. This is the ceiling
# for plain filter/browse queries (no relevance ranking to truncate on).
MAX_RESULTS = 50

# Hybrid search results are relevance-ranked, not just filtered — past the
# first handful, matches stop being meaningfully related to the query (see
# the "Priya Shrama" case: real matches at #1-#2, noise from #15 on). A
# much tighter cap belongs here than on plain browsing, which has no
# ranking signal to justify keeping only a few. Only applied when Search
# actually produced the results (never the SQL fallback, which has no
# ranking to trust a tiny cutoff on).
MAX_SEARCH_RESULTS = 5

# The fixed, always-visible field set PersonSummary is built from — see the
# comment in find_people() for why no per-record field filtering is needed.
SUMMARY_FIELDS = {"id", "full_name", "preferred_name", "job_title", "org_unit", "office", "availability_status"}

# Mirrors app.org_chart.MAX_DEPTH (org_units is only company -> division ->
# department -> team, 4 levels, so this is a generous bound) — not imported
# from there directly, since org_chart already imports MAX_RESULTS from this
# module and importing back would be circular.
ORG_UNIT_MAX_DEPTH = 10


def _tenure_band(hire_date: date) -> str:
    years = (date.today() - hire_date).days // 365
    if years < 1:
        return "<1 year"
    return f"{years} year" if years == 1 else f"{years} years"


def _month(d: date | None) -> str | None:
    return d.strftime("%Y-%m") if d else None


def resolve_skill(db: Session, name: str) -> Skill | None:
    """Case-insensitive lookup that resolves a synonym straight to its
    canonical skill, so a caller never needs to know which name is the
    alias — "SRE" and "Site Reliability Engineering" find the same people.
    """
    skill = db.query(Skill).filter(Skill.name.ilike(name)).first()
    if skill is None:
        return None
    if skill.canonical_id is not None:
        return db.get(Skill, skill.canonical_id)
    return skill


def _parse_level(level: str) -> SkillLevel | None:
    return next((m for m in SkillLevel if m.value.lower() == level.lower()), None)


# Curated, hand-picked real language-family groupings -- never ML-guessed or
# embedding-similarity-derived. Used only as a fallback when a requested
# language has zero direct matches (unresolvable, like "Telugu" below, or
# resolvable but genuinely nobody has it), to suggest people who speak a
# linguistically related language instead. The result is always presented
# as related, never substituted in as if it matched the actual request --
# that distinction is what keeps this different from the exact failure mode
# diagnosed for the free-text/semantic path (confident nearest-neighbor
# results with no connection to what was actually asked). Covers every
# language in seed.py's LANGUAGE_SKILLS plus a handful of common unseeded
# ones (Telugu, Malayalam, ...) so an unseeded request still has somewhere
# sensible to land.
LANGUAGE_FAMILIES: dict[str, str] = {
    "tamil": "dravidian", "kannada": "dravidian", "telugu": "dravidian", "malayalam": "dravidian",
    "hindi": "indo-aryan", "marathi": "indo-aryan", "bengali": "indo-aryan",
    "punjabi": "indo-aryan", "gujarati": "indo-aryan", "urdu": "indo-aryan",
    "spanish": "romance", "french": "romance", "portuguese": "romance", "italian": "romance", "romanian": "romance",
    "german": "germanic", "english": "germanic", "dutch": "germanic",
    "mandarin": "sino-tibetan", "cantonese": "sino-tibetan",
    "japanese": "japonic",
}


def find_related_language_speakers(
    db: Session, caller: AuthenticatedUser, language: str,
) -> tuple[str | None, list[PersonSummary]]:
    """Called only after a language search for `language` has already come
    back empty. Looks up its language family and returns speakers of any
    other seeded language in that family (deduped, capped at
    MAX_SEARCH_RESULTS same as any ranked lookup). Returns (family, [])
    with family set but no results when the family is known but nobody
    speaks any related language either; (None, []) when `language` isn't in
    the curated table at all.
    """
    family = LANGUAGE_FAMILIES.get(language.strip().lower())
    if family is None:
        return None, []
    related_names = [n for n, f in LANGUAGE_FAMILIES.items() if f == family and n != language.strip().lower()]
    seen: dict[str, PersonSummary] = {}
    for related_name in related_names:
        for person in find_people(db, caller, language=related_name):
            seen.setdefault(person.id, person)
    return family, list(seen.values())[:MAX_SEARCH_RESULTS]


def _org_unit_and_descendant_ids(db: Session, name: str) -> list[int] | None:
    """Resolve an org_unit filter value to every unit id in its subtree.

    Employees only ever belong to their single most-specific unit, so a flat
    exact-name match against a division/department name like "Infrastructure"
    always returned zero rows — everyone in it is actually filed under one of
    its teams ("Cloud Operations Team", "Networking Team", ...). This walks
    org_units.parent_id downward from the matched unit, same recursive-CTE
    shape as org_chart._traverse's walk over employees.manager_id, so a
    parent-level filter value now includes every descendant team's people.
    Returns None if the name doesn't match any unit at all.
    """
    root = db.query(OrgUnit).filter(OrgUnit.name.ilike(name)).first()
    if root is None:
        return None

    anchor = (
        select(OrgUnit.id, OrgUnit.parent_id, literal(0).label("depth"))
        .where(OrgUnit.id == root.id)
        .cte(name="org_unit_tree", recursive=True)
    )
    child = aliased(OrgUnit)
    recursive_term = (
        select(child.id, child.parent_id, (anchor.c.depth + 1).label("depth"))
        .join(anchor, child.parent_id == anchor.c.id)
        .where(anchor.c.depth < ORG_UNIT_MAX_DEPTH)
    )
    tree = anchor.union_all(recursive_term)
    rows = db.execute(select(tree.c.id)).all()
    return [r.id for r in rows]


def _office_out(office: Office | None) -> OfficeOut | None:
    if office is None:
        return None
    return OfficeOut(id=office.id, name=office.name, city=office.city, country=office.country)


def _resolve_office_name(db: Session, office: str) -> str:
    """Case-insensitive resolution of a user-typed office/city string to a
    canonical Office.name — exact match first, substring second (so a
    partial city like "Bang" still finds "Bangalore"), mirroring the
    tolerance the SQL fallback path already has via its own ilike. Needed
    because Search's OData filter (app.search_client) is an exact,
    case-sensitive `eq` — unlike the free-text UI input feeding it
    (Filters.tsx) — and, unlike org_unit (_org_unit_and_descendant_ids
    above), office never got this treatment. Returns the original input
    unchanged when nothing matches at all: Search then correctly finds
    nothing for a genuinely nonexistent office, same outcome as today —
    this only fixes the case where a real office exists but the raw
    input's casing or partiality didn't land on it exactly.
    """
    match = (
        db.query(Office).filter(or_(Office.name.ilike(office), Office.city.ilike(office))).first()
        or db.query(Office).filter(or_(Office.name.ilike(f"%{office}%"), Office.city.ilike(f"%{office}%"))).first()
    )
    return match.name if match else office


def _write_audit(db: Session, caller: AuthenticatedUser, action: str, query_text: str,
                  result_count: int, fields_returned: set[str]) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(sorted(fields_returned)), timestamp=datetime.now(),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# find_people(name?, query?, skill?, level?, org_unit?, office?, language?, available?)
#
# `name` and `query` both feed the same hybrid-search text — `query` is the
# explicit "description of a person" entry point (e.g. "who knows Power BI
# in Bangalore"), `name` stays for an exact/partial/misspelled literal name.
# CLAUDE.md's tool signature only lists `name`; `query` is an additive,
# more self-documenting alias so the free-text/semantic capability isn't
# hidden behind a parameter that reads as "literal name only" — same
# underlying search_people() call either way. If both are given, `query`
# wins (it's the more explicit signal of intent).
# ---------------------------------------------------------------------------

def find_people(
    db: Session,
    caller: AuthenticatedUser,
    *,
    name: str | None = None,
    query: str | None = None,
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    view_mode: ViewMode = "work",
) -> list[PersonSummary]:
    effective_query = query or name

    filters_used = {k: v for k, v in dict(
        name=name, query=query, skill=skill, level=level, org_unit=org_unit,
        office=office, language=language, available=available,
    ).items() if v is not None}

    # Resolve skill/language synonyms once, up front — both the Search
    # filter and the SQL fallback need the same canonical name/id, and an
    # unknown skill/language or invalid level means no matches either way
    # (not an error).
    resolved_skill = None
    parsed_level = None
    if skill:
        resolved_skill = resolve_skill(db, skill)
        if resolved_skill is None:
            _write_audit(db, caller, "find_people", json.dumps(filters_used), 0, SUMMARY_FIELDS)
            return []
        if level:
            parsed_level = _parse_level(level)
            if parsed_level is None:
                _write_audit(db, caller, "find_people", json.dumps(filters_used), 0, SUMMARY_FIELDS)
                return []
    resolved_lang = None
    if language:
        resolved_lang = resolve_skill(db, language)
        if resolved_lang is None:
            _write_audit(db, caller, "find_people", json.dumps(filters_used), 0, SUMMARY_FIELDS)
            return []

    # org_unit resolves once, up front, to every unit id (and name) in its
    # subtree — both the Search filter and the SQL fallback need the same
    # hierarchy-expanded set, and an org_unit that matches nothing means no
    # matches either way (not an error).
    org_unit_ids: list[int] | None = None
    org_unit_names: list[str] | None = None
    if org_unit:
        org_unit_ids = _org_unit_and_descendant_ids(db, org_unit)
        if not org_unit_ids:
            _write_audit(db, caller, "find_people", json.dumps(filters_used), 0, SUMMARY_FIELDS)
            return []
        org_unit_names = db.execute(select(OrgUnit.name).where(OrgUnit.id.in_(org_unit_ids))).scalars().all()

    # office resolves once, up front, to its canonical Office.name — see
    # _resolve_office_name for why (Search's OData `eq` needs an exact,
    # correctly-cased value; the SQL fallback below stays on the raw input
    # since its own ilike is already case/substring-tolerant).
    resolved_office = _resolve_office_name(db, office) if office else None

    # Exact-match short-circuit: a query that exactly matches a unique
    # identifier (work email, Slack handle) or a full/preferred name
    # shouldn't be diluted by fuzzy/semantic neighbors at all. Verified
    # live against the real Search index: RRF scores decay too smoothly to
    # separate "the right answer" from "four other Wilsons" by a
    # relevance-score cutoff (an exact full-name match only scores ~5%
    # above its nearest neighbor) — and email/Slack aren't indexed for
    # Search in the first place, so a Slack-handle query rides entirely on
    # weak vector similarity to *anyone* with a similar-shaped name. An
    # exact match on a field that's actually unique per person is a
    # stronger, cheaper signal than any ranking could produce, so it skips
    # Search/fuzzy matching entirely. A genuine duplicate exact name (two
    # "Priya Sharma"s) still correctly returns both — this narrows to
    # exact matches, it doesn't force uniqueness that isn't really there.
    exact_match_ids: set[str] | None = None
    if effective_query and effective_query.strip():
        q_lower = effective_query.strip().lower()
        q_handle_variants = {q_lower, q_lower.lstrip("@"), f"@{q_lower.lstrip('@')}"}
        found_ids = set(db.execute(
            select(Employee.id).where(
                # `.is_(True)` renders as `IS 1` on SQL Server, which
                # T-SQL rejects (IS only works with NULL) -- `== True`
                # renders as `= 1` everywhere, including here.
                Employee.is_active == True,
                or_(
                    func.lower(Employee.full_name) == q_lower,
                    func.lower(Employee.preferred_name) == q_lower,
                    func.lower(Employee.work_email) == q_lower,
                    func.lower(Employee.slack_handle).in_(q_handle_variants),
                ),
            )
        ).scalars().all())
        if found_ids:
            exact_match_ids = found_ids

    # 1. retrieve — Search is for ranking (a name/description to score
    # against) and semantic/fuzzy work only, per ARCHITECTURE_2.md §11 and
    # §16's non-goal ("moving structured filters into Search... reverted by
    # mode 1"). A plain filter combo ("Terraform" + "Cloud Operations Team")
    # has no text to rank, so it skips Search's OData filter entirely and
    # goes straight to the SQL branch below — no embedding round-trip, no
    # network call to a resource that isn't doing any ranking for it
    # anyway. This used to route filter-only queries through Search too
    # (superseded reasoning: "Search already knows the hierarchy-expanded
    # org_unit set... reusing it beats re-deriving the same filter in
    # SQL") — but org_unit_ids/resolved_skill/resolved_office/resolved_lang
    # are already fully resolved right above, before either branch runs, so
    # the SQL branch below needs no re-derivation either way; there was
    # nothing left for routing through Search to actually save.
    #
    # Whether there's actual free text for Search to rank against — decides
    # both whether Search is even called at all now, and later which result
    # cap applies. A pure filter combo has no ranking to trust a *tight*
    # cap on regardless of retrieval path — see the cap comment below.
    is_ranked_query = bool(effective_query and effective_query.strip())

    # A result is only relevance-ranked — and therefore only trustworthy
    # under a tight cutoff — when there was actual free text for Search to
    # rank against (`effective_query`), or an exact-identifier short-circuit
    # (a stronger signal than ranking, not a weaker one). A pure filter
    # combo no longer touches Search at all (see the retrieve comment
    # above), so it always lands on the wide SQL-fallback cap below via
    # this same used_search=False path — RC3 (ARCHITECTURE_2.md §2) is now
    # structurally impossible for this case, not just capped correctly.
    # Computed before retrieval now (not after) so both branches can apply
    # it directly at the query level instead of over-fetching and slicing.
    used_search = is_ranked_query or exact_match_ids is not None
    effective_cap = MAX_SEARCH_RESULTS if used_search else MAX_RESULTS

    # Row-level policy decision, computed once. Only `required_filters` is
    # used here -- enforce()'s own cap logic (app/policy.py's _max_rows) has
    # no concept of "this is a relevance-ranked query" and would always
    # resolve to the wide DEFAULT_LIMIT, silently discarding the tight
    # MAX_SEARCH_RESULTS cutoff above for ranked results. `select=["id"]` is
    # arbitrary -- PersonSummary's SUMMARY_FIELDS need no per-record
    # redaction (see the comment below), so dropped_fields is never
    # consulted here, only required_filters.
    decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)

    candidates: list[Employee] | None = None
    if exact_match_ids is None and is_ranked_query:
        ranked_ids = search_people(
            name=effective_query, skill=resolved_skill.name if resolved_skill else None,
            level=parsed_level.value if parsed_level else None,
            org_unit=org_unit_names, office=resolved_office,
            language=resolved_lang.name if resolved_lang else None, available=available,
            required_filters=decision.required_filters,
            # No overfetch margin needed anymore -- the restricted-employee
            # obligation is now excluded at the index-filter level (above),
            # not post-filtered in Python, so there's nothing left for a
            # tighter-than-requested result to need headroom against.
            top=MAX_SEARCH_RESULTS,
        )
        if ranked_ids is not None:
            rows = db.execute(select(Employee).where(Employee.id.in_(ranked_ids))).scalars().all()
            by_id = {e.id: e for e in rows}
            candidates = [by_id[i] for i in ranked_ids if i in by_id]  # preserve Search's relevance order

    if candidates is None:
        stmt = select(Employee).where(Employee.is_active == True)
        if exact_match_ids is not None:
            stmt = stmt.where(Employee.id.in_(exact_match_ids))
        elif effective_query:
            # SQL fallback only ever does a literal substring match — a
            # description-style query legitimately returns nothing here,
            # per spec ("fall back to direct database queries for name
            # lookup" — not semantic lookup, which needs Search).
            pattern = f"%{effective_query}%"
            stmt = stmt.where((Employee.full_name.ilike(pattern)) | (Employee.preferred_name.ilike(pattern)))
        if org_unit_ids:
            stmt = stmt.where(Employee.org_unit_id.in_(org_unit_ids))
        if office:
            stmt = stmt.join(Office, Employee.office_id == Office.id).where(
                (Office.name.ilike(f"%{office}%")) | (Office.city.ilike(f"%{office}%"))
            )
        if available:
            # Only the positive filter is exposed. "Show me everyone who's away"
            # is exactly the restricted aggregate-absence query the spec calls
            # out, so `available=False` is a silent no-op, never a bulk listing.
            stmt = stmt.where(Employee.availability_status == AvailabilityStatus.available)
        if resolved_skill:
            skill_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved_skill.id)
            if parsed_level:
                skill_subq = skill_subq.where(EmployeeSkill.level == parsed_level)
            stmt = stmt.where(Employee.id.in_(skill_subq))
        if resolved_lang:
            lang_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved_lang.id)
            stmt = stmt.where(Employee.id.in_(lang_subq))
        for f in decision.required_filters:
            stmt = apply_filter(db, stmt, f)

        stmt = stmt.order_by(Employee.full_name).limit(effective_cap)
        candidates = db.execute(stmt).scalars().all()

    # 2. filter records — no longer a Python post-filter: both branches
    # above already exclude restricted employees at the query level (the
    # OData filter for Search, the WHERE clause for SQL), so `candidates`
    # is already policy-filtered. is_record_visible is not called anywhere
    # in this function anymore.
    #
    # 3. filter fields / 4. department check — PersonSummary only ever
    # carries SUMMARY_FIELDS, which is a subset of BASE_FIELDS visible to
    # every role with no ABAC/department gating. Per-record field filtering
    # is therefore provably a no-op for this shape; full RBAC/ABAC/
    # department filtering happens in get_person's detail view instead.
    #
    # 5. cap results — tight relevance cutoff for ranked Search results, the
    # wider bulk-extraction ceiling for plain filter/browse queries. Also
    # already applied at the query level above (top=/`.limit()`); this slice
    # is now just a defensive backstop, never expected to actually trim
    # anything.
    capped = candidates[:effective_cap]

    # A `name` search that resolves unambiguously to exactly one EXACT
    # full/preferred-name match can answer a relationship question ("who
    # does X report to", "list X's direct reports", "who's covering for X")
    # in this same call, without a second get_person/get_org_chain round
    # trip. Deliberately keyed on an exact match, not "the whole result list
    # has one entry" — hybrid search legitimately surfaces fuzzy neighbors
    # (Ethan Wilson, Sean Ryan, ...) alongside an exact "Sean Wilson" match,
    # and that breadth is the misspelling-tolerance feature working as
    # intended, not ambiguity about who "Sean Wilson" unambiguously refers
    # to. Two people who share the exact same full name (the seeded "Priya
    # Sharma" duplicate) correctly produce zero exact matches here — genuine
    # ambiguity, not enriched. Never enriched from a `query`-only search —
    # that's the description-style lookup, with no literal name to match
    # exactly against.
    single = None
    if name:
        exact = [
            e for e in capped
            if e.full_name.lower() == name.strip().lower()
            or (e.preferred_name and e.preferred_name.lower() == name.strip().lower())
        ]
        if len(exact) == 1:
            single = exact[0]
    fields_returned = set(SUMMARY_FIELDS)

    results: list[PersonSummary] = []
    for e in capped:
        kwargs: dict = dict(
            id=e.id, full_name=e.full_name, preferred_name=e.preferred_name,
            job_title=e.job_title, org_unit=_org_unit_name(db, e.org_unit_id),
            office=_office_out(db.get(Office, e.office_id) if e.office_id else None),
            availability_status=e.availability_status.value,
        )
        if e is single:
            # manager/delegate: policy-gated via enforce()+compile_query(),
            # not a raw db.get() -- ARCHITECTURE_2.md §15 item 6 / Phase 3
            # Round 2 (app/query_compiler.py's enforced_person_ref). Both
            # are INTERNAL in the registry (visible to every role), so this
            # only changes behavior for the one case a raw lookup missed:
            # the referenced manager/delegate is themself a restricted
            # record and the caller isn't hr.
            manager_ref = enforced_person_ref(db, caller, e.manager_id, view_mode) if e.manager_id else None
            if manager_ref:
                kwargs["manager"] = manager_ref
                fields_returned.add("manager")
            delegate_ref = enforced_person_ref(db, caller, e.delegate_id, view_mode) if e.delegate_id else None
            if delegate_ref:
                kwargs["delegate"] = delegate_ref
                fields_returned.add("delegate")
            # direct_reports: downward chain, manager/hr only — same shared
            # gate as get_org_chain's "down" direction (app.policy's
            # can_see_direct_reports, consolidated from two independently
            # -written inline checks that had already drifted). The
            # restricted-employee obligation is pushed into this one-hop
            # query directly via apply_filter, same as the main retrieval
            # above, instead of a post-hoc is_record_visible Python filter.
            if can_see_direct_reports(caller.role, view_mode):
                reports_stmt = select(Employee).where(Employee.manager_id == e.id, Employee.is_active == True)
                for f in decision.required_filters:
                    reports_stmt = apply_filter(db, reports_stmt, f)
                reports = db.execute(reports_stmt).scalars().all()
                kwargs["direct_reports"] = [PersonRef(id=r.id, full_name=r.full_name) for r in reports]
                fields_returned.add("direct_reports")
        results.append(PersonSummary(**kwargs))

    # 6. write to audit_log
    _write_audit(db, caller, "find_people", json.dumps(filters_used), len(results), fields_returned)

    # 7. respond
    return results


def _org_unit_name(db: Session, org_unit_id: int) -> str:
    unit = db.get(OrgUnit, org_unit_id)
    return unit.name if unit else ""


def search_people_by_plan(
    db: Session, caller: AuthenticatedUser, plan: PeopleQuery, view_mode: ViewMode = "work",
) -> list[PersonSummary]:
    """The model-emitted-PeopleQuery retrieval path (Piece 2) -- find_people's
    named-parameter tool stays exactly as it is for the deterministic router
    and the common cases; this is what the LLM fallback reaches for on a
    request find_people's fixed parameters can't express ("Bangalore or
    Singapore", "title contains Architect", any filter combination nobody
    anticipated in advance). validate() -> snap() -> enforce() -> compile ->
    respond, same pipeline shape as every other retrieval in this file, just
    driven by a caller-constructed plan instead of named kwargs.

    SQL only, deliberately -- app.query_plan.PeopleQuery's own docstring
    already says it: "Deliberately cannot express free-text ranking...
    reverted by mode 1". There's no query-text field anywhere on this
    shape, so there is never anything for Azure Search to rank against
    here, and find_people's own established rule is that a pure-filter
    request with nothing to rank skips Search entirely regardless. A
    Search-side compiler for this would have been dead code -- exactly the
    non-goal ARCHITECTURE_2.md §16 already names.

    validate()/enforce() failures raise ValueError, deliberately, so
    execute_with_retry's existing bounded retry loop (app/tool_calling.py)
    catches them for free -- no new retry mechanism needed. The two are
    NOT given the same message treatment. validate()'s structural errors
    (unknown field, illegal op, wrong value type) are safe to surface
    verbatim -- they don't reveal anything about data sensitivity, only
    about the plan's own legal shape. enforce()'s denial is different:
    ARCHITECTURE_2.md §8's own rule is that "a denial must not reveal that
    a restricted capability exists" (decision.reason is for the audit log,
    never the caller), and _retry_after_execution_failure() feeds this
    exception's message straight back to the model as part of the retry
    prompt -- so a denial raises a generic message instead of
    decision.reason, never naming the field it actually tripped on.

    snap()'s unresolvable values (SnapNote.resolved is None) are NOT
    treated as an error to retry -- retrying won't fix a value that
    genuinely isn't in the database, same reasoning find_people's own
    unresolvable skill/org_unit filters already follow (an empty result,
    not an exception -- see tests/test_query_compiler.py's
    test_unresolvable_*_returns_nothing_not_an_error). The unresolved value
    is passed through as-is (snap() already does this substitution
    internally), so compile_query() naturally returns zero rows for it.
    The notes are folded into the audit query_text so they stay traceable;
    a richer "did you mean X?" surfaced back to the caller is a real future
    enhancement, not built in this pass.
    """
    # Local import: app.vocabulary imports app.org_chart (for
    # FUZZY_MATCH_THRESHOLD), and app.org_chart imports app.people (for
    # MAX_RESULTS) -- a module-level import here would be a real circular
    # import. Same fix app.permissions.training_extra_fields already uses
    # for the identical shape of cycle.
    from app.vocabulary import snap, validate

    validation = validate(plan)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    snapped_plan, notes = snap(db, plan)

    decision = enforce(snapped_plan, caller, view_mode)
    if not decision.allow:
        raise ValueError("that request can't be answered as asked")

    stmt = compile_query(db, snapped_plan, decision)
    ids = db.execute(stmt).scalars().all()
    rows = db.execute(select(Employee).where(Employee.id.in_(ids))).scalars().all()
    by_id = {e.id: e for e in rows}
    ordered = [by_id[i] for i in ids if i in by_id]

    results: list[PersonSummary] = [
        PersonSummary(
            id=e.id, full_name=e.full_name, preferred_name=e.preferred_name,
            job_title=e.job_title, org_unit=_org_unit_name(db, e.org_unit_id),
            office=_office_out(db.get(Office, e.office_id) if e.office_id else None),
            availability_status=e.availability_status.value,
        )
        for e in ordered
    ]

    query_text = plan.model_dump_json()
    unresolved = "; ".join(f"{n.field}={n.original!r} unresolved" for n in notes if n.resolved is None)
    if unresolved:
        query_text = f"{query_text} [{unresolved}]"
    _write_audit(db, caller, "search_people_by_plan", query_text, len(results), SUMMARY_FIELDS)

    return results


# ---------------------------------------------------------------------------
# get_person(person_id)
# ---------------------------------------------------------------------------

def get_person(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode = "work"
) -> PersonDetail | None:
    fields_returned: set[str] = set()
    found = False
    try:
        # 1. retrieve
        target = db.get(Employee, person_id)
        if target is None or not target.is_active:
            return None

        # 2. filter records — identical response (None -> 404) whether the
        # id doesn't exist or the record is record-level restricted.
        # required_filters doesn't depend on `select`, so this lightweight
        # decision and compute_visible_fields' own (differently-shaped)
        # internal one always agree on required_filters -- two enforce()
        # calls, but a pure, cheap function, not a second DB round trip.
        decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)
        if excluded_by_obligations(decision, target):
            return None

        # 3. filter fields (RBAC + ABAC) / 4. department check
        fields = compute_visible_fields(db, caller, target, view_mode)
        fields_returned = fields
        found = True

        # 5. cap results — not applicable to a single-record lookup.
        return _build_detail(db, caller, target, fields, view_mode)
    finally:
        # 6. write to audit_log — logged either way; the audit trail is
        # allowed to know more than the caller's response reveals.
        _write_audit(db, caller, "get_person", f"person_id={person_id}",
                     1 if found else 0, fields_returned)
    # 7. respond (via the return above)


# ---------------------------------------------------------------------------
# update_own_bio(person_id, bio) — self-service only. Ownership (person_id
# == caller.id) is enforced by the route, not here; this is a straight
# persistence op with none of the ABAC/RBAC field logic get_person has.
# ---------------------------------------------------------------------------

def update_own_bio(db: Session, caller: AuthenticatedUser, person_id: str, bio: str) -> PersonDetail | None:
    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        return None
    target.bio = bio
    db.commit()
    db.refresh(target)
    # Rule 6: bio is in build_profile_text, so this write has to re-index.
    # It didn't until the hook existed — an edited About was invisible to
    # search until the next manual full rebuild.
    reindex_employee(db, target)
    fields = compute_visible_fields(db, caller, target)
    result = _build_detail(db, caller, target, fields)
    _write_audit(db, caller, "update_own_bio", f"person_id={person_id}", 1, fields)
    return result


# ---------------------------------------------------------------------------
# update_own_name_pronunciation(person_id, name_pronunciation) — self-service
# only, same shape as update_own_bio above. Not reindexed: unlike bio,
# name_pronunciation doesn't feed build_profile_text -- it's a "how do I say
# this" aid, not searchable profile content.
# ---------------------------------------------------------------------------

def update_own_name_pronunciation(
    db: Session, caller: AuthenticatedUser, person_id: str, name_pronunciation: str,
) -> PersonDetail | None:
    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        return None
    target.name_pronunciation = name_pronunciation
    db.commit()
    db.refresh(target)
    fields = compute_visible_fields(db, caller, target)
    result = _build_detail(db, caller, target, fields)
    _write_audit(db, caller, "update_own_name_pronunciation", f"person_id={person_id}", 1, fields)
    return result


def _build_detail(
    db: Session, caller: AuthenticatedUser, target: Employee, fields: set[str], view_mode: ViewMode = "work",
) -> PersonDetail:
    # view_mode defaults to "work" -- update_own_bio (below) has no view_mode
    # concept of its own to give this, same deferred-scope reasoning as
    # get_org_chain never gaining one; get_person passes its real value.
    kwargs: dict = {"id": target.id, "full_name": target.full_name}

    if "preferred_name" in fields:
        kwargs["preferred_name"] = target.preferred_name
    if "name_pronunciation" in fields:
        kwargs["name_pronunciation"] = target.name_pronunciation
    if "job_title" in fields:
        kwargs["job_title"] = target.job_title
    if "org_unit" in fields:
        kwargs["org_unit"] = _org_unit_name(db, target.org_unit_id)
    if "work_email" in fields:
        kwargs["work_email"] = target.work_email
    if "work_phone" in fields:
        kwargs["work_phone"] = target.work_phone
    if "slack_handle" in fields:
        kwargs["slack_handle"] = target.slack_handle

    office = db.get(Office, target.office_id) if target.office_id else None
    if "effective_timezone" in fields:
        kwargs["effective_timezone"] = target.timezone or (office.timezone if office else None)
    if "office" in fields:
        kwargs["office"] = _office_out(office)

    if "employment_type" in fields:
        kwargs["employment_type"] = target.employment_type.value
    if "photo_url" in fields:
        kwargs["photo_url"] = target.photo_url
    if "linkedin_profile" in fields:
        kwargs["linkedin_profile"] = target.linkedin_profile

    if "manager" in fields and target.manager_id:
        manager_ref = enforced_person_ref(db, caller, target.manager_id, view_mode)
        if manager_ref:
            kwargs["manager"] = manager_ref
    if "delegate" in fields and target.delegate_id:
        delegate_ref = enforced_person_ref(db, caller, target.delegate_id, view_mode)
        if delegate_ref:
            kwargs["delegate"] = delegate_ref

    if "availability_status" in fields:
        kwargs["availability_status"] = target.availability_status.value
    if "away_until_month" in fields:
        kwargs["away_until_month"] = _month(target.away_until)
    if "tenure_band" in fields:
        kwargs["tenure_band"] = _tenure_band(target.hire_date)
    if "bio" in fields:
        kwargs["bio"] = target.bio

    if "skills" in fields or "languages" in fields:
        rows = (
            db.query(EmployeeSkill, Skill)
            .join(Skill, EmployeeSkill.skill_id == Skill.id)
            .filter(EmployeeSkill.employee_id == target.id)
            .all()
        )
        skills_out: list[SkillOut] = []
        langs_out: list[SkillOut] = []
        for es, sk in rows:
            item = SkillOut(name=sk.name, category=sk.category.value, level=es.level.value, source=es.source.value)
            (langs_out if sk.category.value == "language" else skills_out).append(item)
        if "skills" in fields:
            kwargs["skills"] = skills_out
        if "languages" in fields:
            kwargs["languages"] = langs_out

    if "project_history" in fields:
        # project_desc rides on the same project_history list rather than
        # being a top-level field: it describes a project, and the list is
        # already the one place project data reaches the response.
        # Unconditional now — project_desc is visible to every caller who
        # can see project_history at all (see permissions.PROJECT_DESC_FIELDS),
        # so there's no separate field-table decision left to make here.
        kwargs["project_history"] = _project_history(db, caller, target)

    if "training_status" in fields:
        # Routed through the provider factory, never at a provider directly
        # — with ENABLE_TRAINING_API_SYNC off this reads seeded synthetic
        # data, and with it on the same call reaches the training team's
        # API, with nothing here changing. None means the provider couldn't
        # answer; leaving the key unset makes it genuinely absent from the
        # response rather than an empty list that reads as "no courses".
        items = employee_training_status(db, target)
        if items is not None:
            kwargs["training_status"] = items

    if "hire_date" in fields:
        kwargs["hire_date"] = target.hire_date
    if "cost_centre" in fields:
        kwargs["cost_centre"] = target.cost_centre
    if "personal_mobile" in fields:
        kwargs["personal_mobile"] = target.personal_mobile
    # str() rather than float() — see the schema comment. Set even when empty,
    # so a contractor with no salary on file comes back as null rather than
    # absent: absent has to keep meaning "you may not see this", or HR can't
    # tell "no salary held" from "hidden from me". Same rule away_until_month
    # follows.
    if "salary" in fields:
        kwargs["salary"] = str(target.salary) if target.salary is not None else None
    if "salary_currency" in fields:
        kwargs["salary_currency"] = target.salary_currency
    if "date_of_birth" in fields:
        kwargs["date_of_birth"] = target.date_of_birth

    return PersonDetail(**kwargs)


def _project_history(
    db: Session, caller: AuthenticatedUser, target: Employee
) -> list[ProjectHistoryItem]:
    rows = (
        db.query(EmployeeProject, Project)
        .join(Project, EmployeeProject.project_id == Project.id)
        .filter(EmployeeProject.employee_id == target.id)
        .all()
    )
    items: list[ProjectHistoryItem] = []
    for ep, proj in rows:
        # Confidential projects: visible to members only. Members stay
        # fully visible in the directory — only this membership edge hides.
        if proj.classification.value == "confidential" and not can_see_confidential_project(db, caller, proj.id):
            continue
        item_kwargs: dict = dict(
            project_name=proj.name, project_type=proj.type.value, role=ep.role,
            start_month=_month(ep.start_date), end_month=_month(ep.end_date),
            current=ep.end_date is None,
        )
        # Set even when empty, so a project with no description on file
        # reads as null rather than absent — absent would mean "you may not
        # see this", which is no longer true for anyone; the only remaining
        # question is whether there's anything on file, and null answers it.
        item_kwargs["project_desc"] = proj.description
        items.append(ProjectHistoryItem(**item_kwargs))
    return items
