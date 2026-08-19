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
to `dev`: every request needs an `X-Dev-Role: employee|manager|hr|it` header (plus
optional `X-Dev-User-Id`, `X-Dev-Name`), enforced by the same `get_current_user`
dependency the real Entra JWT-validation path uses — so nothing downstream
changes when `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` show up later.

Landing on `dev` mode by simply forgetting the two Entra vars is a full auth
bypass in a real deployment, so the app refuses to start that way unless it
was reached on purpose — `.env.example` sets `ALLOW_DEV_AUTH=1` for you, so
local dev keeps working out of the box, but a real deploy must set
`ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` (never `ALLOW_DEV_AUTH`) or it will
crash-loop at startup by design. See `assert_dev_auth_is_intentional` in
`app/auth.py`.

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
| `POST /notifications/date-milestones` | hr-only, optional `?on=YYYY-MM-DD`. Sweeps for birthdays and milestone service anniversaries and notifies HR. Idempotent per date — what a daily cron would call, since nothing in the database changes on someone's birthday |
| `GET /search` | **the unified search+ask surface.** Classifies `q` deterministically (trailing `?` or an interrogative opener) into `direct` (plain filtered results) or `assisted` (also runs the tool-calling layer and returns an `overview` with a prose answer + citations + reasoning trace) |
| `POST /ask` | the older direct entry point to the tool-calling layer; `/search` is what the frontend actually uses now, this is kept as a lower-level API |

## Project structure

```
app/
  main.py             FastAPI app, routes above; also serves the built
                       frontend (frontend/dist) from the same origin in prod
  auth.py             pluggable get_current_user: dev header vs. real Entra JWT validation
  db.py               SQLAlchemy engine/session, reads DATABASE_URL
  config.py           settings: ENABLE_TRAINING_API_SYNC, NOTIFY_LEVELS_UP, HR_ORG_UNIT_NAME
  permissions.py      field/record visibility rules, applied between retrieval and response
  notifications.py    all four triggers. Course status: employee reminder + full manager
                       chain, permission-checked, explicitly ordered employee-first. Date
                       driven: birthdays and milestone anniversaries to HR, a sweep rather
                       than an event, idempotent per occurrence via event_key
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
seed.py               synthetic data generator + constraint verification summary.
                       Starts by DELETING every employee/project/skill/org unit —
                       builds a directory from nothing, never run against one that
                       already exists (see seed_training.py)
seed_training.py      adds only the training-course tables to a database that
                       already has a directory, touching nothing else. This is what
                       to run against the deployed database
seed_people_data.py   backfills salary and date of birth onto a database that
                       already has people, same non-destructive contract
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
Database migrations run at **app startup**, not as a pipeline step — the App
Service's startup command is `alembic upgrade head && uvicorn ...` (set in
`terraform/main.tf`). The only SQL firewall rule is `AllowAzureServices`,
which is what lets the web app reach the database at all; a GitHub-hosted
runner isn't dependably covered by it, so running alembic from CI would
depend on which IP the runner happened to get. Chained with `&&` on purpose:
a failed migration stops the app, which fails the deploy's `/health` poll,
rather than serving a green deploy that 500s on every profile page.

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
| `HR_ORG_UNIT_NAME` | `HR Operations` | Which org unit's people receive birthday and work-anniversary notifications — everyone in it or beneath it. Resolved from the org tree because there is no role column and a scheduled sweep carries no role claim. Set to `People & Culture` for the whole division |
| `NOTIFY_LEVELS_UP` | `-1` (unlimited) | How far up the reporting chain a status resolution is reported. Full chain is the confirmed requirement today; it's a setting so narrowing it later (e.g. `1` for direct manager only, `0` to disable management notifications) needs no edit to notification logic. Still bounded by `org_chart.MAX_DEPTH`, which is the cycle guard, not a policy. Governs who gets *told*, never who may *look* — profile visibility stays the full chain regardless |
| `TRAINING_API_*` | unset | base URL, key, timeout. Shape only; nothing reads them while sync is off |

### Seeded shape

Five fake courses, scoped so **every profile shows one or two** and none shows
zero: `SEC-101` is company-wide, and the other four are keyed to divisions
that don't overlap, so nothing stacks a third onto anyone. Between them the
four narrow rows cover one scoping clause each — division alone, division +
job title, division + employment type — so the resolver is exercised end to
end. Real compliance training doesn't partition this tidily; this is demo
data shaped to keep the Training card short and readable.

About 15% of expected (employee, course) pairs are left with **no status row
at all**, which is the one case where `not_started` is legitimately inferred
rather than reported — and the case a provider outage must never be confused
with.

**Deployed vs. local.** Migration `6886efd9b63d` seeds the course *catalogue*
(the five courses and the five rules for who takes them) as reference data, so
the App Service's startup `alembic upgrade head` gives every deployed profile
its one or two courses with no manual step. It deliberately stops there:
per-employee statuses are ~900 rows about specific people that go stale as
soon as anyone joins, and schema history is the wrong home for them. The
consequence is that straight after a deploy everything reads **"Not
completed"** — correct, since a course with no status row genuinely means not
started. Getting the realistic mix is the manual step below.

### Seeding training data on the deployed app

Only needed for the completed/in-progress/failed spread; the catalogue arrives
on its own via the migration above. Run it from the **App Service's SSH
console** (Azure portal → App Service → SSH): the only SQL firewall rule is
`AllowAzureServices`, which is what lets the web app reach the database at
all, and a GitHub runner or a laptop isn't dependably inside it.

**1. Find the app directory.** It is *not* `/home/site/wwwroot` — that holds
only `hostingstart.html`, `output.tar.zst` and `requirements.txt`. Oryx
extracts and runs the app from `/tmp/<hash>`, and **that hash changes on every
deploy**, so discover it rather than reusing a path from last time:

```bash
APP=$(dirname "$(ls -t /tmp/*/seed.py 2>/dev/null | head -1)")
echo "app dir: $APP"; ls -1 "$APP"/seed*.py; cd "$APP"
```

**2. Confirm you're pointed at Azure SQL, not the sqlite fallback.** If the
`DATABASE_URL` app setting isn't visible in the shell, `app/db.py` quietly
falls back to `sqlite:///directory.db` — the seed would then report success
while writing to a throwaway file:

```bash
python -c "from app.db import DATABASE_URL; print(DATABASE_URL[:30])"   # expect mssql+pymssql://
```

**3. Seed.**

```bash
python seed_training.py
```

`seed_training.py` exists because **`seed.py` would be a disaster here**: it
opens by deleting every employee, project, skill and org unit before
regenerating them, which against the deployed database means ~500 different
people with different ids and every bookmarked profile URL broken. The
training script only ever touches the four training tables.

**4. Verify it actually committed.** Observed once: a run printed a correct
summary (`applicable pairs: 914`, a normal status breakdown) and committed no
status rows at all, silently. An identical re-run worked. Root cause never
established — most likely a stale `/tmp/<hash>` from an earlier deploy — so
check rather than trust the summary:

```bash
python -c "
from app.db import SessionLocal
from app.models import EmployeeCourseStatus
from sqlalchemy import select, func
print('status rows:', SessionLocal().execute(select(func.count()).select_from(EmployeeCourseStatus)).scalar_one())
"
```

Expect several hundred. If it says 0, re-run step 3 from a freshly discovered
`$APP`.

**Ordering note:** `seed_training.py` deletes the `notifications` table — it
has to, since notifications carry a foreign key to the courses it rebuilds. So
seed *first*, then fire any demo notifications. Doing it the other way round
silently wipes them.

### Firing a notification on the deployed app

The training system isn't connected, so nothing generates a status change on
its own. `POST /people/{id}/training/{course_code}` is the stand-in (hr-only,
and it 409s once `ENABLE_TRAINING_API_SYNC` is on):

```bash
curl -X POST -H "X-Dev-Role: hr" -H "Content-Type: application/json" \
  -d '{"status":"failed"}' \
  "https://tempest34.azurewebsites.net/people/<employee-id>/training/SECDEV-210"
```

It responds with `notifications_sent`. A `failed` on someone with two levels of
management above them sends 3: the employee's own reminder plus one per level
of the chain. Read them back at `GET /me/notifications` as each recipient.

Pick an employee whose chain is also in the identity picker — that way both
halves of the trigger are visible from the UI: the employee is told they
didn't *pass* and must *retake*, while everyone above them is told only *did
not complete*. `notifications_sent: 0` means the status was already that value;
an unchanged status deliberately re-notifies nobody.

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

## People data: salary, date of birth, and date-driven notifications

### Fields

`salary`, `salary_currency` and `date_of_birth` are visible to **HR and the
person themselves, and to nobody else — not even their manager**. That is
deliberately narrower than `personal_mobile`, which is own-profile *or* direct
manager: a line manager holding your mobile number is ordinary, a line manager
reading your salary off the directory is not. Managers get neither field at
any level of the chain, unlike `training_status`, which the chain can see
precisely because the chain is already notified about it.

`salary` is `Numeric(12,2)`, not a float — money in binary floating point
accumulates rounding error, and that's painful to walk back once exports
depend on it. It's serialized as a **string** for the same reason: JSON
numbers are IEEE 754 doubles in most clients. `salary_currency` exists because
the dataset spans five countries and a bare number would be actively
misleading; 95,000 means very different things in USD and INR.

Nulls are meaningful. Contractors have no salary on file because they're paid
through an agency, so the company genuinely doesn't hold one — that's an
absent field, not missing data. A few employees have no date of birth, which
the notification sweep skips silently rather than guessing at.

### Birthday and work-anniversary notifications

HR is notified of birthdays, and of **milestone service anniversaries — year
1, then every fifth year**, unbounded (`is_milestone_year`). A 45-year
anniversary is rarer and more worth marking, not less, so it's a rule rather
than a list that silently stops.

These are structurally different from the course triggers. Those fire from a
state change: something happened, so something is sent. **Nothing changes in
the database on someone's birthday**, so these have to be a sweep — a caller
asks "what falls on this date", and the answer is computed rather than
observed. Two consequences:

- **Something external has to run it.** There is no scheduler in this project.
  `POST /notifications/date-milestones` (hr-only, optional `?on=YYYY-MM-DD`)
  is what a daily cron or Azure timer would call; the logic in
  `app/notifications.py` doesn't care what invoked it.
- **It must be safe to run twice**, because anything that runs daily
  eventually runs twice — a retried cron, a restarted container, someone
  checking it works. Each occurrence gets an `event_key` like
  `birthday:2026-08-13:<employee id>`, so a second sweep is a no-op rather
  than a second birthday message.

Who counts as "HR" is resolved from the org tree, since there's no role column
and a scheduled sweep has no request to read a role claim from.
`HR_ORG_UNIT_NAME` (default `HR Operations`) names the unit; everyone in it or
beneath it is a recipient. It defaults to the *department*, not the People &
Culture division above it — the division also contains Talent Acquisition, and
recruiters aren't the audience for a 10-year anniversary.

Two details worth knowing:

- **The messages name the person and nothing else.** A birthday reminder
  carries no date of birth and no age — HR is being told to mark the occasion,
  not handed a field that sits behind a stricter permission than the
  notification does.
- **29 February is observed on the 28th** in non-leap years, for both
  birthdays and anniversaries. Otherwise those people would come round once
  every four years.

An HR person isn't told about their own birthday; their colleagues still are,
so the day isn't missed.

### Adding the IT division to an existing database

`seed_it_division.py` is the only one of these scripts that **hires** rather
than backfills: it creates the IT division, the IT Operations department, its
teams, and ~30 people, and never modifies, reparents or deletes an existing
employee or org unit. Idempotent — if an org unit named `IT` exists it does
nothing.

```bash
python seed_it_division.py
python build_search_index.py    # REQUIRED, see below
```

Two traps it handles, and one it can't:

- **`seed.next_name()` drains a forced queue first**, and that queue is a
  *search fixture*, not a name pool — two exact "Priya Sharma"s so an
  exact-name lookup is genuinely ambiguous, plus near-duplicate pairs for
  fuzzy matching. A fresh process refills it, so hiring re-injects them:
  observed four Priya Sharmas, which quietly destroys the ambiguity the golden
  eval tests for. The script clears it.
- **Email and Slack uniqueness** dedupe against sets that start empty in a new
  process, so a new hire could be handed an address that already belongs to
  someone. The script reserves every existing identifier first.
- **The search index is shared.** `find_people` retrieves through Azure AI
  Search whenever it's configured, and the index is a snapshot — new hires
  have profiles that load fine by id but return nothing for a name or
  `org_unit` filter until `build_search_index.py` runs. Both local development
  and the deployed app point at the same Search resource and the same
  `employees-index`, so **rebuilding from a laptop publishes local-only
  employee ids into the index the deployed app queries**, producing search
  results that 404 when opened. Seed the deployed database, then rebuild from
  the deployed side.

### Seeding these onto an existing database

Same problem and same shape as `seed_training.py` — `seed.py` would delete
every employee first. `seed_people_data.py` fills in salary and date of birth
for people who don't have them, changes no name, id or reporting line, and
skips already-populated rows so it's safe to re-run:

```bash
python seed_people_data.py
```

It recovers each person's org level from their depth in the management chain,
since the level map `seed.py` uses lives only in the process that ran it. It
also plants a few birthdays and milestone anniversaries on today's date —
without that the sweep is undemoable, since with ~500 people roughly one
birthday falls on any given day and the next 5-year anniversary might be weeks
out, so "run it and see" would usually show an empty result indistinguishable
from a broken sweep.

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
   Implemented by `app/search_reindex.py`, which `build_search_index.py` now
   shares its document-building and upload code with. It no-ops when Search
   is unconfigured (tests, most local dev) and never raises into its caller —
   a committed row must not be reported as failed because Azure was briefly
   unreachable.
7. Roles are per request, never a column. `employee` / `manager` / `hr` / `it`
   arrive from a dev header or an Entra app-role claim. The org tree
   (`config.hr_org_unit_name`) is the fallback signal only where there is no
   request to read a claim from — a scheduled sweep.
8. Privilege is a table, not a ladder. `it` may edit project descriptions and
   review AI-extracted changes; it may not read salaries. `hr` is the reverse.
   Neither is a superset of the other, and `app/permissions.py`'s ALLOWED /
   EDITABLE tables are where that is decided.
9. The model's output is never a database write. Extraction emits typed
   `propose_project_update` calls that land in `proposed_changes` as
   `pending`; only an IT reviewer's explicit accept moves content into
   `EmployeeProject` / `EmployeeSkill`, and only then is it searchable.

## Roles and view modes

Four roles, two lenses. `view_mode` is a parameter on the directory/profile
read endpoints (`GET /people`, `GET /people/{id}`, `GET /search`, `POST /ask`)
and on every write:

| | `employee` mode | `work` mode |
|---|---|---|
| `employee` / `manager` | base fields | *unreachable — pinned to employee mode* |
| `hr` | base fields | \+ salary, DOB, hire_date, cost_centre, training, project_desc |
| `it` | base fields | \+ project_desc (**no** salary/DOB) |

Three things are worth knowing before changing any of this:

- **`resolve_view_mode` is the only place the client's parameter is read.**
  Anything other than `hr`/`it` is answered in employee mode however it asks;
  an unrecognised value narrows rather than 400s. `hr`/`it` default to work
  mode when they don't ask, which is what they got before view modes existed.
- **Employee-mode output is identical whoever is looking.** Enforced in three
  places, not one — the field table, `is_record_visible`, and
  `department_filter` — because each is a separate pipeline stage and any one
  left role-aware leaks the caller's privilege back into a view that is
  supposed to be anonymous. The sharp edge: **HR loses its restricted-record
  exemption in employee mode**, so `restricted-1` 404s for them there too.
- **ABAC survives employee mode, deliberately.** Own-profile and
  direct-manager grants (personal_mobile, own salary/DOB, training status up
  the chain) key on the caller's *identity*, never their role, so they return
  the same answer for a given pair of people whoever asks — which is exactly
  what the identity guarantee requires. An employee can still see their own
  salary; they still cannot edit it.

## Document extraction and review

`POST /docs/upload` (IT, work mode) parses a .docx/.pdf, stores the extracted
text in `uploaded_docs`, and queues what it says in `proposed_changes` as
`pending`. Name resolution runs through the same `find_people` fuzzy search
the directory uses, and **returns nothing on ambiguity** — the dataset
contains two people called Priya Sharma on purpose, and `employee_id` is
nullable precisely so "I don't know who this is" is a reviewable outcome
rather than a coin flip.

Review is IT-only, work mode: `GET /proposed_changes?doc_id=` (grouped by
employee, unresolved first), then `accept` (commits + re-indexes + audits with
`source=ai_extraction`), `reassign` (re-points it, stays pending), `correct`
(back through the function-calling loop, stays pending), or `DELETE` (rejects;
the row is kept, not deleted — a rejected proposal is the most useful row in
the table when extraction quality is next reviewed). IT's fallback for
anything it won't accept is the manual edit endpoints.

Accepted skills land as `Learning` / `self`-sourced, never higher: a document
saying somebody used Terraform is evidence they touched it, not that they are
an expert `find_mentor` should be recommending.

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
- [x] 16. Fourth role (`it`) + view modes: visibility re-keyed by
      `(role, view_mode)`, employee-mode output identical for every role,
      HR/IT write endpoints enforced server-side (see Roles and view modes)
- [x] 17. Rule 6 actually implemented — `app/search_reindex.py`, shared with
      `build_search_index.py` and wired into every write path including the
      pre-existing `update_own_bio`, which never re-indexed
- [x] 18. Document upload → typed-call extraction → IT review workflow
      (`uploaded_docs`, `proposed_changes`; see Document extraction and review)
