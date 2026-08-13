# Employee Directory with Smart Search

Internship project at Quadrant Technologies — Project 4 of 11 in the AI internship
programme. Deploy and present by **20 August 2026**.

An internal employee directory with natural-language search: find people by name,
skill, team, or a plain-English description ("who knows Power BI in Bangalore"),
or ask a direct question ("who could mentor me in Terraform?") from the same
search bar — the backend decides which mode a query needs, not the frontend.
Search runs on Azure AI Search hybrid retrieval; a language model turns messy
queries into typed function calls but never touches the database directly —
permission filtering happens in Python, between retrieval and the model.

**Live:** [tempest34.azurewebsites.net](https://tempest34.azurewebsites.net) ·
[API docs](https://tempest34.azurewebsites.net/docs)

## Team

| Area | Owner |
|---|---|
| Backend & AI layer + Team Lead (this repo) | Arshitha |
| Features | Shreyas |
| Embeddings & indexing | Aarya |
| Search quality | Nikhil |
| Infrastructure & Terraform | Abhinav |
| Security & QA | Deeptha |
| Frontend | Sathwik |

## Hard constraints

- **No real Quadrant employee data.** Everything runs on a generated synthetic
  dataset (`seed.py`, ~500 records). Microsoft Graph is an interface spec only —
  never connected. There is no live directory sync.
- **Runs without Azure OpenAI credentials.** Semantic search degrades to keyword +
  fuzzy matching, and the app still starts, when `EMBEDDING_ENDPOINT` / `EMBEDDING_KEY`
  are unset. Chat/tool-calling degrades to the mock resolver the same way when
  `CHAT_ENDPOINT` / `CHAT_KEY` are unset — configured independently via separate
  env vars, though in Quadrant's deployment chat and embeddings happen to be two
  model deployments on the same underlying Azure AI Foundry resource
  ("sharedfoundry"), sharing one endpoint/key; Search is a genuinely separate
  resource. Chat's deployment name is the model catalog id directly (`gpt-5`),
  not a custom alias — confirmed the hard way after every guessed alias 404'd.
- **SQLite locally, Azure SQL in deployment** — switching is a one-line
  `DATABASE_URL` change. Steps 1–6 of the build need no Azure resources at all.

## Stack

Python 3.14, FastAPI, SQLAlchemy 2.x, Alembic · SQLite (local) / Azure SQL
(deployed) · Azure AI Search · Azure OpenAI · Microsoft Entra ID · Azure App
Service.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # DATABASE_URL defaults to sqlite:///directory.db

alembic upgrade head          # create the schema
python seed.py                # generate ~500 synthetic employees + verification report

uvicorn app.main:app --reload --port 8000   # http://127.0.0.1:8000/docs
```

Auth is pluggable (`app/auth.py`). With no Entra config set, `AUTH_MODE` defaults
to `dev`: every request needs an `X-Dev-Role: employee|manager|hr` header (plus
optional `X-Dev-User-Id`, `X-Dev-Name`), enforced by the same `get_current_user`
dependency the real Entra JWT-validation path uses — so nothing downstream
changes when `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` show up later.

```bash
curl http://127.0.0.1:8000/health
curl -H "X-Dev-Role: manager" http://127.0.0.1:8000/auth/whoami

pytest   # runs against a throwaway temp SQLite db, never directory.db
```

`.python-version` pins 3.14.6 — Azure App Service's newest Linux runtime
(`PYTHON|3.14`, confirmed via `az webapp list-runtimes`).

### Frontend

Vite + React + TypeScript, in `frontend/`. Talks to the backend above over
CORS at `http://127.0.0.1:8000`; dev-mode auth is sent via the same
`X-Dev-Role` / `X-Dev-User-Id` / `X-Dev-Name` headers, switchable from an
identity picker in the top bar (no real login yet).

```bash
cd frontend
npm install
npm run dev -- --port 5173 --strictPort   # http://localhost:5173, talks to the local backend

npm run dev:live                          # same UI, talks to the deployed Azure backend instead
```

## API

| Route | Purpose |
|---|---|
| `GET /health` | liveness check, used by the deploy pipeline |
| `GET /auth/whoami` | resolves the caller's identity/role from the active auth mode |
| `GET /people` | filtered directory listing, permission-filtered per caller |
| `GET /people/{id}` | one person's detail, restricted fields genuinely absent (not null) for callers without access |
| `PATCH /people/{id}/bio` | self-service edit of your own "About" text |
| `GET /people/{id}/org-chart` | manager chain + direct reports, both directions |
| `GET /me/notifications` | your own notifications, newest first — no person-id parameter exists, so no role can read anyone else's |
| `POST /people/{id}/training/{course_code}` | hr-only. Records a course status change and fires both notification triggers; stands in for the training system pushing us an event (409 once `ENABLE_TRAINING_API_SYNC` is on) |
| `GET /search` | **the unified search+ask surface.** Classifies `q` deterministically (trailing `?` or an interrogative opener) into `direct` (plain filtered results) or `assisted` (also runs the tool-calling layer and returns an `overview` with a prose answer + citations + reasoning trace) |
| `POST /ask` | the older direct entry point to the tool-calling layer; `/search` is what the frontend actually uses now, this is kept as a lower-level API |

## Project structure

```
app/
  main.py             FastAPI app, routes above; also serves the built
                       frontend (frontend/dist) from the same origin in prod
  auth.py             pluggable get_current_user: dev header vs. real Entra JWT validation
  db.py               SQLAlchemy engine/session, reads DATABASE_URL
  config.py           certification-tracking settings (ENABLE_TRAINING_API_SYNC, NOTIFY_LEVELS_UP)
  permissions.py      field/record visibility rules, applied between retrieval and response
  notifications.py    the two course-status triggers: employee reminder + full manager chain,
                       permission-checked, explicitly ordered employee-first
  certifications/     course status behind an internal interface — see Certification tracking below
    base.py             CertificationProvider (Protocol) + CertStatus (DTO) + the error types
    synthetic.py        SyntheticCertProvider: seeded fake data, powers the demo
    training_api.py     TrainingApiProvider: SHAPE ONLY, never wired, open questions at the top
    factory.py          get_provider(): gated on ENABLE_TRAINING_API_SYNC
    requirements.py     which courses are expected of whom (our side, not theirs)
    service.py          joins expectations to reported status; records status changes
  people.py           find_people / get_person + the full filter pipeline, language-family
                       and skill-miss fallbacks for "no exact match" queries
  org_chart.py        recursive org chart (both directions), cycle-guarded
  directory_tools.py  the 7-function tool-calling allowlist (find_people, get_person,
                       get_org_chain, find_project_owner, find_mentor, skill_gap, skill_scarcity)
  tool_calling.py      resolves a natural-language message to one of the 7 tools and runs it
                       (mock resolver with no credentials, real Azure OpenAI tool-calling with them)
  unified_search.py    GET /search: deterministic direct-vs-assisted classification, builds
                       the {mode, results, overview} response, permission-safe by construction
  search_client.py     Azure AI Search hybrid retrieval (keyword + prefix + fuzzy + vector) +
                       the embedding client (plain OpenAI client — sharedfoundry is a v1-API
                       Azure AI Foundry endpoint, not a classic per-resource AzureOpenAI one)
  search_index.py      builds/refreshes the Azure AI Search index from the database
  schemas.py           Pydantic response models (PersonSummary, PersonDetail, OrgChainNode, …)
  models/               Employee, OrgUnit, Office, Skill, EmployeeSkill, Project,
                        EmployeeProject, EmployeeCertification, AuditLog,
                        TrainingCourse, EmployeeCourseStatus, CourseRequirement, Notification
alembic/              migrations (SQLite locally, Azure SQL in deployment — same DDL)
seed.py               synthetic data generator + constraint verification summary
build_search_index.py CLI wrapper around search_index.py, run after seeding/migrating
eval/                 golden evaluation set (55 questions) + scorer, run in CI when
                       AI/search-relevant files change (eval/run_golden_eval.py)

frontend/src/
  App.tsx                     top-level state: search query, ?q= URL sync, profile
                               navigation stack (back/breadcrumb), graphs vs. profile mode
  api.ts, types.ts             typed fetch wrappers + response shapes for /search etc.
  components/
    TopBar.tsx                 search bar + identity picker (dev-mode role switch) + notification bell
    NotificationBell.tsx        bell with unread count and a dropdown over GET /me/notifications;
                                read state is per-identity in localStorage (no read/unread column
                                server-side), click-through to the subject's profile
    UnifiedResults.tsx          renders GET /search's direct/assisted response — the
                                "pure renderer," no query classification lives here
    AIOverview.tsx              the AI-answer panel for assisted mode: prose + citation
                                links + collapsed-by-default reasoning trace
    PersonCard.tsx, ProfilePage.tsx   result cards; full profile page (not a slide-over),
                                URL-routed at /profile/:id
    GraphPage.tsx               tab switcher for the three graph views below
    graphs/
      DepartmentGraph.tsx        org hierarchy: manager above, direct reports below,
                                  expand/collapse per branch, recenter on click
      TeamGraph.tsx               same hierarchical-tree pattern for a person's team;
                                  clicking a teammate recenters AND opens their profile
      SkillsGraph.tsx             skill-based relationship view
      treeShared.tsx               shared tree rendering: NodeBox, useTreeConnectors
                                  (measures real DOM positions for the SVG elbow connectors)

tests/                pytest suite — permission/visibility, org chart, search,
                       unified search (incl. a zero-model-call proof for direct mode)
terraform/            Azure infra as code — see Deployment below
.github/workflows/    CI/CD — see Deployment below
```

## Deployment

Pushing to `main` runs `.github/workflows/ci-cd.yml`, three jobs in sequence:

1. **test** — `pytest`, always. Also runs the 55-question golden evaluation
   set against the real Azure resources (`eval/run_golden_eval.py`), but only
   when AI/search-relevant files changed, and it never blocks the rest of the
   pipeline — a regression there is a signal to look at, not a hard gate.
2. **terraform** — `terraform/main.tf` provisions the App Service (plan +
   web app), Azure SQL (server + database + firewall rule), and the storage
   account backing Terraform's own remote state. Azure AI Search
   (`internaisearch`) and Azure AI Foundry (`sharedfoundry`, chat +
   embeddings) are **not** provisioned here — they're Quadrant's own
   centrally-managed shared resources; this repo only ever holds their
   endpoint/key as secrets, wired into the web app's `app_settings` block so
   a future infra recreation (e.g. a region move) can't silently drop them
   again, same as it did once already.
3. **deploy** — builds the frontend, zips it with the backend, and deploys
   via `az webapp deploy` (OneDeploy, `--clean true`). Ends with a health-check
   poll against `/health` and `/` so a "successful" deploy that's actually
   crash-looping fails the workflow instead of leaving a silent 503.

Required GitHub repo secrets: `ARM_CLIENT_ID` / `ARM_CLIENT_SECRET` /
`ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID` (deployment service principal),
`DB_PASSWORD`, and three independent endpoint/key pairs for Quadrant's AI
resources — `GROUP3_4OPENAI*` (chat), `GROUP3_4_TEXT_EMBEDDING_3_SMALL_*`
(embeddings), `AISEARCH_*` (search).

## Certification tracking

Course completion is shown on profiles and drives two notification triggers.
The training courses themselves belong to another team's system, which isn't
ready — so nothing here talks to it. Everything runs against an internal
interface with a synthetic implementation behind it, and going live is a
config flip, not a refactor.

### What's real and what's stubbed

| | Status |
|---|---|
| Data model + migration (`36c145414911`) | **real** — 4 tables, additive, no existing table touched |
| `CertificationProvider` interface + `CertStatus` | **real** — everything codes against this, nothing against an implementation |
| `SyntheticCertProvider` | **real**, and what powers the demo — reads the seeded `employee_course_statuses` table |
| `TrainingApiProvider` | **stub: shape only.** Method signatures, the URL/auth sketch, and the open questions. Every body raises `NotImplementedError` |
| Requirements (who's expected to take what) | **real** — ours to own, not the training team's |
| Profile display | **real**, permission-filtered like every other field |
| Notification triggers, ordering, chain walk | **real** |
| Notification *delivery* | **stub** — the row in `notifications` is the delivery; `_deliver()` in `app/notifications.py` is the single seam a mailer/Slack transport plugs into |
| Recipient role derivation | **assumption** — no role column exists, so `_role_for()` infers manager-vs-employee from having direct reports. Should read the Entra app-role claim once that's live |

### Configuration

| Setting | Default | Effect |
|---|---|---|
| `ENABLE_TRAINING_API_SYNC` | `false` | `false` selects `SyntheticCertProvider`. `TrainingApiProvider` is not merely error-handled when off — `app/certifications/factory.py` imports the module *inside* the enabled branch, so it is never imported, constructed, or called. Flipping this is the whole go-live change on our side |
| `NOTIFY_LEVELS_UP` | `-1` (unlimited) | How far up the reporting chain a status resolution is reported. Full chain is the confirmed requirement today; it's a setting so narrowing it later (e.g. `1` for direct manager only, `0` to disable management notifications) needs no edit to notification logic. Still bounded by `org_chart.MAX_DEPTH`, which is the cycle guard, not a policy. Governs who gets *told*, never who may *look* — profile visibility stays the full chain regardless |
| `TRAINING_API_*` | unset | base URL, key, timeout. Shape only; nothing reads them while sync is off |

### Status, and the two notifications

The stored status is the four-value enum `not_started | in_progress | failed
| completed`. User-facing copy collapses it to `completed` / `not completed`
— but the four values survive into the database, because the reminder
wording depends on the distinction the label throws away. The underlying
status never appears in any profile response, for any role.

- **Employee reminder** — fires whenever `display_status` becomes
  `not_completed`. Wording depends on the underlying status: *"you haven't
  started X yet"* (not_started), *"you didn't pass X, you'll need to retake
  it"* (failed).
- **Management report** — fires on a status *resolution* (completed, or not
  completed after an actual attempt), and walks the **full** reporting
  chain. Reads *"completed"* or *"did not complete"*; pass/fail is never
  exposed upward, in the body or in the stored columns.

**Ordering is explicit, not incidental.** The employee's notification is
created first with `sequence` 0, the chain follows at 1..n, and both are
written in one transaction — so the employee is told *before or at the same
instant as* their management, never after. This matters most in the failed
case. There is no dispatcher and no subscriber list precisely so the order
can't depend on registration order, and `sequence` is persisted rather than
inferred from `created_at`, whose millisecond resolution would leave ties
unresolved.

Both routes go through `_may_receive()`, which asks the same
`app.permissions` functions the profile API asks. Nothing calls a mailer
directly.

### Open questions for the training-courses team

1. **Join key — employee id or email?** We hold both. Email is probably what
   their sign-in uses, but our employee ids survive a name change and email
   doesn't. Preference: they store our `employees.id` as an external id.
   Only `_employee_key()` changes either way.
2. **Push or pull — webhook or poll?** Pull is simplest but puts their API
   on our page-load path and gives us no event to hang notifications off; a
   transition is what the triggers need. Preference: push for notifications,
   pull for backfill. The pipeline is already written against a
   `(previous, current)` transition, so a webhook handler is a thin route
   over the existing service function.
3. **Timeout/error semantics.** Settled on our side, needs stating to
   theirs: a timeout or 5xx raises `CertProviderUnavailable` and **never**
   degrades to `not_started`. Defaulting there would mail a real employee
   "you haven't started X yet" — and tell their whole management chain —
   because someone else's service blipped. Still open: whether a 404 on an
   employee/course pair means "no record, genuinely not started" or "unknown
   employee, our join key is wrong". Those want opposite handling and the
   status code can't distinguish them. Same conversation: their status
   vocabulary vs. our four values, where an unrecognised value must raise
   rather than default.

Smaller assumption to confirm, flagged in code: `in_progress` also maps to
"not completed", so the employee trigger fires for it, but only the
not_started and failed variants were specified. It currently gets *"you've
started X but haven't finished it yet"*.

## Architecture rules (non-negotiable)

1. The language model never touches the database — it only emits typed function
   calls (`find_people`, `get_person`, `get_org_chain`, …).
2. Permission filtering happens in Python: retrieve → filter records → filter
   fields → department check → cap results → audit → respond.
3. Restricted fields are **absent** from the response body, not hidden client-side.
4. Redact, never reject — a caller without access gets an empty result set, not
   a 403 or an "access denied" message.
5. Deny by default — a field not listed in the visibility config is hidden.
6. Every write to an indexed field (skills, bio, projects, title) re-indexes.

## Build order

- [x] 1. Schema + migrations (SQLite)
- [x] 2. Seed data + verification summary
- [x] 3. FastAPI skeleton, Entra auth dependency, `/docs`, health endpoint
- [x] 4. `find_people` / `get_person` with the full filter pipeline + audit log
- [x] 5. Field-visibility tests (assert restricted keys absent from response bodies)
- [x] 6. Recursive org chart endpoint (both directions, cycle-guard test)
- [x] 7. Azure AI Search index + `build_profile_text()` + batch embedding
- [x] 8. Hybrid search wired into `find_people`
- [x] 9. Tool-calling layer with few-shot examples (mock first)
- [x] 10. Golden evaluation set, scored per tier
- [x] 11. Frontend: search, results, three graph views (Department, Team,
      Skills), profile page, AI assistant panel
- [x] 12. Merged Search + Ask into one surface: backend classifies `q` and
      returns direct results or an assisted-mode AI Overview from the same
      `GET /search` endpoint; frontend is a pure renderer
- [x] 13. Azure infra (Terraform) + CI/CD pipeline, deployed to
      `tempest34.azurewebsites.net`
- [x] 14. Wired Quadrant's real Search/embedding/chat resources into both CI
      and the deployed app itself (previously credentials only reached CI's
      golden-eval step, so production silently ran on the mock resolver)
- [x] 15. Certification tracking + notifications behind a provider interface —
      synthetic data now, one config flip to the training team's API later
      (see Certification tracking above)
