# ARCHITECTURE_2 — smart search

Written 2026-08-18, from the working tree, not from memory. Every number below was
measured or read out of the code on that date.

This document exists because 24 source and test files cite it and it was never committed. Their
docstrings reference it by section (`ARCHITECTURE_2.md §8`, `§11/RC2`, `§15 item 7`),
so the numbering here is fixed by those citations, not chosen freshly. If you
renumber a section, grep for it first.

---

## §1 Invariants

These hold everywhere. A change that breaks one is a bug regardless of what it buys.

1. **The model never touches the database.** It emits a typed function call; a service
   function executes it. There is no model-generated SQL anywhere, and §16 records why
   that was rejected rather than deferred.
2. **Policy is testable without a model call.** Every permission decision is a pure
   function of `(plan, caller, view_mode)`. The whole 847-test suite runs in ~5s with no
   network.
3. **Permission is checked in the service function, not only at the route.** Routes may
   check again for HTTP-status shape, but a service function is never allowed to assume
   its caller was already vetted.
4. **Redact, don't reject; deny by default.** A caller who may not see a field gets a
   response without it, not an error naming it.
5. **Degrade, don't error.** An unavailable Azure resource falls back to a narrower path
   and says so; it does not 500.

---

## §2 The measured failures

The Aug 13 review (`SEARCH_ARCHITECTURE_REVIEW.md`, kept as history) measured six root
causes. Status as of this document:

| | Failure | Status |
|---|---|---|
| RC1 | Frontier model at default reasoning effort spending ~1150 tokens to pick one of nine function names — 20.7s vs 2.1s for an identical decision | Fixed. `reasoning_effort="minimal"`, §6 |
| RC2 | `get_org_chain` required a UUID the model never had, so multi-hop org questions returned one hop | Fixed. Name resolver, §13 |
| RC3 | A relevance cap of 5 applied to unranked structured filters — "Compliance Team + GDPR" returned 5 of 12 | Fixed. §11 |
| RC4 | Model-emitted filter values never checked against the real vocabulary — `org_unit="Cloud Infrastructure"` hard-emptied | Fixed. §9 |
| RC5 | `is_question()` — a trailing `?` — decided an 800× cost difference *and* which capabilities the query got | Fixed. §6 |
| RC6 | recall@3 against ground-truth sets of size 9–15: max achievable score 0.21 | Fixed. §14 |

---

## §3 Decisions already made

Recorded so they are not relitigated by accident.

1. **Azure AI Search stays**, for fuzzy and semantic *ranking* only. It never sees the
   caller and never makes a visibility decision.
2. **The LLM emits typed function calls, never SQL.** This is what makes the permission
   property provable. §16 has the full argument.
3. **The deterministic router uses a strict confidence threshold.** Every branch is an
   exact pattern match against a known intent shape. Ambiguous phrasing returns `None`
   and defers to the model — that is what `None` is *for*. Widening a regex to catch a
   near-miss is the wrong fix, and §6 documents what went wrong the last time the router
   claimed something it could not parse.
4. **Permission filtering happens in Python between retrieve and respond**, never inside
   the model's reach.
5. **One Azure AI Search index** is allocated to this group (`employees-index`), and it
   tracks the *deployed* database. See §16 for what that costs locally.

---

## §4 The pipeline

```
GET /search  →  unified_search()
                  │
                  ├─ _wants_assistant(db, text)?          §6
                  │
   no ────────────┴──────────► find_people()              ~5ms
                                 snap arguments           §9
                                 exact identifier → SQL
                                 structured filters → SQL
                                 free text → Azure Search
   yes ─► resolve_intent(text, db)                        §6
            ├─ deterministic router (regex, ~4ms)
            └─ gpt-5, reasoning_effort=minimal (~2s)
                 emits ONE of 9 typed tool calls
                      │
                      ├─ needs_followup? → execute_chain()   ≤3 steps
                      └─ else            → execute_with_retry()  ≤2 retries
                                 │
                                 ▼
                   enforce() → compile_query() → SQL        §8, §11
                                 │
                                 ▼
                   _phrase() prose + per-step trace
```

`POST /ask` is the same machinery without the search-result shaping. The frontend only
calls `/search`.

---

## §5 The field registry

`app/registry.py`. One source of truth for every field this API can select, filter, or
sort on. 29 fields, 10 filterable. `app/query_plan.py` builds its `Field` type as
`Literal[tuple(REGISTRY.keys())]`, so a plan **cannot name a field that doesn't exist** —
the type rejects it before any policy check runs.

Two populated sensitivity tiers, a direct relabeling of `app/permissions.py`'s existing
lists rather than a fresh judgment:

| Tier | Count | Meaning |
|---|---|---|
| `INTERNAL` | 23 | visible to every role |
| `HR_ONLY` | 5 | salary, salary_currency, date_of_birth, hire_date, cost_centre |
| `None` | 1 | `personal_mobile` — registered so the schema-coverage assertion passes, but statically denied for select/filter/order_by. It reaches a response only through the post-retrieval ABAC grant, entirely outside this system. |

There is no "public" tier. `id` and `full_name` go through the same `is_visible()` check
as everything else.

`assert_registry_covers_schema()` runs at app startup, so a column added to the model
without a registry entry fails the boot rather than silently becoming unfilterable.

---

## §6 Routing

**Deterministic first, model as fallback.** An exact pattern match is ~4ms, free, and
unit-testable; a model call for the identical decision is ~2s.

`_wants_assistant(db, text)` decides whether to take the assisted path at all, cheapest
question first:

1. **Is the whole query one person's identifier?** Then it is a lookup. Settled first, so
   a surname like "Report" can never be read as a relationship question.
2. **Can the deterministic router answer it, and does the person it named exist?** Then
   the assisted path is free, and punctuation should not gate something free.
3. **Otherwise:** question shape, a described problem, or a coordination between
   alternatives (`or`/`either` — the one request shape `find_people` cannot express,
   since its parameters take one value each).

RC5 was that step 3 used to be the *whole* test. `"anyone in Bangalore or Singapore who
knows Kubernetes"` returned 0 results; the identical text with a `?` returned 7.

### Two guards on the deterministic router

The router's extractors key on a single keyword with a **greedy** name group. Greedy is
deliberate — a non-greedy group breaks on a name that contains a keyword-shaped word
("Riley Report" loses its surname). The cost is that on input the extractor cannot
really parse, it captures too much:

- **`names_a_real_person()`** — a route naming a person is trusted only if that person
  resolves. `"who does the owner of the Billing API report to"` became a chain lookup
  for an employee called *"the owner of the Billing API"*.
- **`_is_clean_subject()`** — the captured subject must *look* like a name: at most five
  words, no interrogative or structural tokens. The existence check alone cannot catch
  `"who reports to Priya Nair"`, because the real name is inside the captured string and
  fuzzy-matches. What is wrong there is the shape, not the existence.

The blocklist is interrogative/structural tokens only — never relationship words.
"Report" is a real surname in this directory.

Both guards fail *toward the model*, never toward an empty answer.

---

## §7 The modes

Split by **retrieval strategy**, not by topic. Strategy determines cost, latency, and
failure shape; topic determines none of them.

| # | Mode | Answers | Retrieval | Latency |
|---|---|---|---|---|
| 1 | Structured lookup | "Who works in Payroll?" | `PeopleQuery` → policy → SQL | ~10ms |
| 2 | Rule-ranked | "Who can teach me Terraform?" | `find_mentor` → SQL, multi-factor sort | ~10ms |
| 3 | Semantic | "I'm stuck on X, who can help?" | project vectors → cosine → project→employee hop | ~1.5s |
| 4 | Org traversal | "Everyone above X" | name resolver → recursive CTE | ~10ms |
| 5 | Continuity | "Which engagements are exposed?" | deterministic, HR-only | ~50ms |

Nine tools sit on top of these five modes. The mapping is not 1:1 and does not need to
be — `find_people` and `search_people` are both mode 1 with different argument shapes,
and the four `directory_tools` functions are all mode 2.

Misroutes degrade **softly**: "who can help with Terraform" landing in mode 1 returns
people with the Terraform skill, which is a decent answer.

---

## §8 The policy engine

`app/policy.py`. `enforce(plan, caller, view_mode) -> PolicyDecision`. Not a boolean — a
decision carrying:

- **`dropped_fields`** — redaction, applied to `select`
- **`required_filters`** — obligations appended unconditionally; the caller and the model
  never see them and cannot negotiate them
- **`max_rows`** — the row cap; the model may hint lower, never higher

```python
q.select  = [f for f in q.select  if is_visible(f, role, view_mode)]
q.filters = [f for f in q.filters if is_visible(f, role, view_mode)]
```

That second line is the subtle one: `WHERE cost_centre = 'X'` reveals membership through
*which rows come back*, without ever projecting the column. Filtering on a field is a
read of it.

**A denial must not reveal that the field it denied is real.** A policy-denied plan
raises `ValueError("that request can't be answered as asked")` (`app/people.py`), and the
retry prompt built from it never echoes `decision.reason` — otherwise a rejected filter
confirms `cost_centre` is a recognised, restricted field rather than something the model
invented. Structural errors from `validate()` are safe to surface; policy denials are
not, and the two are deliberately given different treatment.

Both retrieval paths apply obligations **natively, before any row comes back**: the SQL
branch via `apply_filter`, the Search branch by compiling the same obligations into an
OData filter. Search still never learns who the caller is.

### The two-visibility-system split

`app/policy.py` governs `find_people` / `get_person` / `get_org_chain`.
`app/permissions.py`'s `is_record_visible` / `visible_fields` still govern
`app/directory_tools.py`, `app/notifications.py`, and `app/project_search.py`.

This is deliberate and deferred, not an accident — but it is the single most confusing
thing in the codebase, and it should eventually collapse into one.

---

## §9 Validation, snapping, and retry

`app/vocabulary.py`. Two jobs, deliberately not merged:

- **`validate()` rejects on STRUCTURE** — unknown field, illegal operator, wrong value
  type, or a registered-but-unlabelled field. Structural problems are never guessed at.
- **`snap()` corrects on VALUES** — a legal field whose value doesn't match the database.
  `"Cloud Infrastructure"` → `"Infrastructure"` (RC4). Exact → case-insensitive → fuzzy
  above 80 **and clearly ahead of the runner-up**. Unresolvable values are reported,
  never silently dropped.

  The scorer is `fuzz.ratio`, **not** `WRatio`. WRatio's partial-ratio pass scores a
  shared substring as though it were the whole string — right for a person name
  ("Anderson" should match "Shaun Anderson", and §13 keeps WRatio for that) and wrong
  for a vocabulary whose entries share a structural suffix. 60 of this directory's 75
  org units end in "Team", and WRatio scored every invented `"<x> Team"` at exactly 86:
  "Search Team", "Payments Team" and "Security Team" all snapped to "Machine Learning
  Team", and "Billing API Team" to "Product Management Team A". `fuzz.ratio` scores those
  34–50 while still scoring every genuine near-miss 82–95.

`snap_tool_arguments()` applies the same correction to `find_people`'s **named
arguments**, which for a long time only `search_people` got — even though the router
picks `find_people` far more often.

Two fields are deliberately not snapped:

- **`language`** — `find_related_language_speakers` already handles a miss, and better:
  it offers a linguistically related language and says so. Snapping would replace an
  honest fallback with a silent guess.
- **`level`** — a fixed enum. A bad level is a rejection.

An unresolvable value is left as typed, so `"do we have anyone who knows Quantum
Computing"` stays answerable with "no".

**Bounded retry.** A call that raises `TypeError`/`ValueError`/`KeyError` is re-prompted
with what went wrong, at most twice. A structural error is safe to surface to the model;
a policy denial is not (§8).

---

## §10 Mode 5 — staffing continuity

`app/continuity.py`, HR-only, no model call anywhere. Facts (overlap days, redundancy
counts) and severity classification are both code, from versioned config
(`config/continuity_thresholds.yml`).

The unit classified is always the **engagement**, never the employee: "Project Apollo —
High continuity exposure", never "\<person\> — HIGH RISK".

---

## §11 Mode 1 — the query compiler

`app/query_compiler.py`. The one place a `PeopleQuery` becomes SQL: approved columns
only, every value parameterized, a hard row cap from the policy decision. It only ever
returns a SELECT.

**Search is for ranking, not filtering.** A query reaches Azure AI Search only when
there is text to rank (`effective_query`) or an exact-identifier short-circuit. A pure
filter combination — "Terraform" + "Cloud Operations Team" — has nothing to rank, so it
skips Search entirely: no embedding round-trip, no network call to a service doing no
ranking for it.

That is also the structural fix for RC3. The tight cap (`MAX_SEARCH_RESULTS = 5`) is
correct for a ranked list and wrong for an enumerable filter result, and the two are now
different code paths rather than the same path with a flag. Filter results cap at
`MAX_RESULTS = 50`.

---

## §12 Mode 3 — semantic project search

`app/project_search.py`. Answers a described *problem*, which modes 1, 2 and 4
structurally cannot: mode 1 needs a field and value, mode 2 a named skill, mode 4 a
person.

The unit retrieved is the **project**; people are reached by hopping from it, because
the evidence that someone can help is usually "they worked on a project that hit this",
not "this noun appears on their profile".

Hybrid, fused with reciprocal rank fusion (`RRF_K = 60`, the Cormack et al. value Azure
AI Search itself uses):

- **semantic** — cosine over stored project embeddings
- **keyword** — LIKE over name + description, which catches the proper noun embeddings blur

Vectors live in a local `project_embeddings` table, not an index: 129 projects × 1536
dims × 4 bytes ≈ 790 KB, brute-force cosine in sub-millisecond numpy. This consumes none
of the group's single index allocation (§3.5) and behaves identically on SQLite and
Azure SQL. Revisit at ~10k projects; the retrieval interface stays, only the
implementation moves.

**Confidential projects are never embedded.** Excluding at index time makes "who's
connected to Project Nightingale" *impossible* rather than merely handled. Two projects.

Caps: `MAX_PROJECTS = 5` before the hop, `MAX_EXPERTS = 8` after.

**Phrasing carries availability.** "Who can help?" is the one question whose useful
answer is not a name — it is who hit the same thing and whether they can be asked. The
answer leads with someone reachable when there is one, keeps the closest match named
first so availability never silently reshuffles the ranking, and says so plainly when
nobody is free. A missing excerpt is reported as a looser match: it means nothing in
that project's write-up overlapped the problem.

---

## §13 Mode 4 — org traversal and name resolution

`app/org_chart.py`. `MAX_DEPTH = 10` is the cycle guard, not a policy setting: a
malformed `manager_id` cycle stops adding rows after ten hops rather than hanging.

`resolve_person()` returns **three** outcomes, not two:

| Outcome | Meaning |
|---|---|
| resolved | exactly one active employee |
| ambiguous | several matched; candidates named, none chosen |
| unknown | nothing matched |

Collapsing the last two into `None` produced one message — *"Nobody found above them in
the org chart"* — for a duplicated name, a typo, and a genuine top-of-chain. Two of those
three are confidently wrong.

Three tiers, each over **both** `full_name` and `preferred_name`: exact →
case-insensitive → fuzzy above `FUZZY_MATCH_THRESHOLD = 80`. Nine employees here have a
real nickname and none were reachable by it before.

`AMBIGUITY_MARGIN = 5` on the fuzzy tier. `WRatio`'s partial pass scores a bare surname
*identically* against every namesake — "Anderson" ties across all five — so without a
margin the resolver silently answered about whichever one rapidfuzz ranked first. A real
typo still has one clear winner, so "Shaun Andersen" resolves.

A tier that matches several distinct people stops there rather than falling through to a
looser tier, which could only add candidates.

**The answer is the headline record.** "Who does Sean Wilson report to?" returns Min-jun
Sanchez's card, not Sean Wilson's.

---

## §14 Evaluation

`eval/`. 64 questions against a **pinned** `eval/fixture.db`, never live `directory.db` —
a reseed reshuffles names, not just ids, and a golden set anchored to names cannot
survive that.

`score()` uses **recall@k where k = |relevant|**, not a fixed top-3. That was RC6: a
fixed cap made any question with more than three correct answers structurally incapable
of scoring above `3/|relevant|`, and t1-04 returned all 14 correct direct reports and
scored 0.21.

**Preflight checks index/database parity.** It samples the index and compares against the
database being evaluated. The old preflight checked whether Search was *configured* and
never whether it was configured against the *right data* — the more dangerous state,
because it fails silently and looks exactly like poor retrieval. Below 50% overlap,
Search is removed from the run and ranking-dependent questions are reported
**UNMEASURABLE** rather than counted as zeros.

Baseline, 2026-08-18, SQL-only pipeline:

```
Tier 1   recall 1.000  precision 1.000   (n=21)
Tier 2   recall 0.830  precision 0.897   (n=18, 5 unmeasurable)
Tier 3   recall 0.800  precision 0.800   (n=15)
Out-of-scope  5/5 refused, exact wording
Chains   58 single-call, 1 chained
```

**Read that with a caveat:** `resolve_intent_strict` calls the model directly and
bypasses the deterministic router. Since the router is now primary, the eval measures the
*fallback* path, not what production runs. Closing that gap is open work (§16).

---

## §15 The fix list

Carried forward from the Aug 13 review's §5, with status, because module docstrings cite
these by number.

| # | Item | Status |
|---|---|---|
| 1 | `tests/test_agentic_loop.py` broke collection — whole suite ran 0 tests | Fixed |
| 2 | `_assisted()` dropped `clean_filters`, so UI filter chips were ignored on any question | Fixed |
| 3 | `MAX_SEARCH_RESULTS=5` applied to unranked filter-only queries | Fixed (§11) |
| 4 | Office/org_unit filters case- and exactness-sensitive behind free-text inputs | Fixed (§9) |
| 5 | `app/agent_pipeline.py` was broken dead code | Deleted |
| 6 | `manager`/`delegate` `PersonRef`s attached without a visibility check on the referenced person | Fixed — `enforced_person_ref` routes them through `enforce()` + `compile_query()` |
| 7 | `args.setdefault("depth", 10)` — a model omitting `depth` on an "up" call made `_phrase()` report the CEO as "your manager" | Fixed — defaults to 1, which undershoots rather than confidently answering with the wrong person |
| 8 | README claimed re-index-on-write and semantic reranking that don't exist | Fixed — the semantic-rerank claim is gone, and re-index on write is now real: `reindex_employee()` is called from seven write paths across `app/people.py` and `app/writes.py` |

---

## §16 Non-goals and known limitations

**Model-generated SQL with a rewriting policy layer — explicitly rejected.** It converts
a provable set-membership check into an unbounded parsing problem (aliases, expressions,
subqueries, CTEs, `UNION`), and aggregate/`HAVING`/`COUNT` inference attacks survive even
a correct row-and-column rewriter. It would also make latency worse and deployment
riskier given the documented SQLite/T-SQL divergence.

**Moving structured filters into Search — reverted by mode 1.** At 500 records the right
index for a structured filter is the database. At 50,000 you would want them back in
Search for pagination and faceting. Saying so beats pretending this scales unchanged.

**Cross-query inference is not addressed.** A user can ask several individually-permitted
questions and assemble something restricted from the combined answers. The chain
automates exactly that pattern at the cost of one prompt instead of two. It does not
exceed the bound — `enforce()` is a pure function with no memory across calls, so a later
step is exactly as authorized as a fresh request — but it makes the existing gap cheaper
to walk through.

**Local free-text employee search does not work.** The single allocated index tracks the
deployed database; local `directory.db` and `eval/fixture.db` share almost nothing with
it (11/559 and 0/559 ids). Decided 2026-08-18: leave the shared index alone rather than
break it for the group. The eval detects the mismatch instead of scoring it (§14).

**The eval measures the fallback router, not the primary one** (§14).

**Two visibility systems still coexist** (§8).

**Re-indexing degrades silently by design.** `reindex_employee()` never raises — a
failed re-index leaves the index stale rather than failing the write. That is the right
tradeoff for a derived cache, but it means the index can drift without anything
surfacing it. `find_people` compensates on the read side (a ranked id that no longer
resolves locally falls through to SQL rather than reporting "no such person"), and
`is_active` is re-checked after ranking so a deactivation whose re-index failed cannot
resurrect that person. There is no drift *alarm*, though — only compensation.
