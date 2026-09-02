# Employee Directory with Smart Search

Internship project at Quadrant Technologies — Project 4 of 11 in the AI internship
programme. Deployed and presented **20 August 2026**. The in-app name is **Mel**.

An internal employee directory with natural-language search: find people by name,
skill, team, or a plain-English description ("who knows Power BI in Bangalore"),
or ask a direct question ("who could mentor me in Terraform?") from the same
search bar — the backend decides which mode a query needs, not the frontend.
Search runs on Azure AI Search hybrid retrieval; a language model turns messy
queries into typed function calls but never touches the database directly —
permission filtering happens in Python, between retrieval and the model.

Around that core sit the org graphs, workforce dashboards, team building, the
document review queue and PRD requirement capture. Every question-shaped one of
them is served by the same bounded agent — see [The assistants](#the-assistants).

**Live:** [tempest34.azurewebsites.net](https://tempest34.azurewebsites.net) ·
[API docs](https://tempest34.azurewebsites.net/docs)

## Team

| Area | Owner |
|---|---|
| Team lead — backend & the AI layer (this repo) | Arshitha |
| AI layer, security & AI features | Aarya |
| AI features on top of the agent layer | Shreyas |
| Search quality & evaluation | Nikhil |
| Infrastructure & Terraform | Abhinav |
| Backend, testing & QA | Deeptha |
| Data & frontend | Sathwik |

## What it does

| Surface | What it answers |
|---|---|
| **Search and ask** | One box for both a name and a question. A name or a skill goes straight to retrieval — no model, no routing call; a real question is routed to one of the search assistant's eleven tools and answered with the people it came from |
| **Org graphs** | Four views of the same organisation — Department, Team, Skills, and each person's private Community graph. Clicking anyone recentres the whole graph on them |
| **Workforce dashboards** | Skill supply against project demand, training compliance, project coverage, concentration risk. HR sees the whole org and can narrow to a department; everyone else sees their own reporting line and nothing else |
| **Build Team** | A project brief in plain language becomes roles, a ranked candidate per role, a coverage percentage and the skill gaps behind it. The model reads the brief into roles and skills — it selects nobody and computes none of the numbers |
| **Find a Team** | The opposite question: rank the teams that already exist for a described problem, with Expert/Working/Learning counts and the manager's contact details |
| **Document review** | Uploaded documents are parsed into proposed changes; nothing reaches an employee record until a human accepts it, field by field |
| **PRD requirement capture** | HR uploads a project's requirements document; extracted skills and notes are previewed, edited and confirmed, then a second HR-only assistant answers questions about them |

## Hard constraints

- **No real Quadrant employee data.** Everything runs on a generated synthetic
  dataset (`seed.py`, 545 employees across 134 projects). Microsoft Graph is an interface spec only —
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

Python 3.14, FastAPI, SQLAlchemy 2.x, Alembic (31 migrations, 26 tables) ·
SQLite (local) / Azure SQL (deployed) · Azure AI Search · Azure OpenAI (`gpt-5`
for routing and phrasing, `text-embedding-3-small` for project vectors, which
live in the application database rather than a vector store) · Microsoft Entra
ID · Azure App Service, provisioned by Terraform · React + TypeScript + Vite,
served from the same origin as the API.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # DATABASE_URL defaults to sqlite:///directory.db

alembic upgrade head          # create the schema
python seed.py                # generate 545 synthetic employees + verification report

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

### Signing in

The UI has a login form; `POST /auth/login` turns an email and password into
the same `{id, role, name}` the dev headers carry, and the frontend sends
those headers for every call afterwards. **It is a demo shim, not
authentication** — there is no password column, no hashing, and no reset
flow (`app/demo_auth.py` says so at length). The route 404s as soon as
`ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` are set, because signing in is then
Entra's job and these credentials must not be a second way in.

**Any active employee can sign in**, with their work email and one shared
password: `orghub2026`, overridable with `DEMO_LOGIN_PASSWORD` in `.env`. So
"can you show me someone else's view?" is answerable with any of the ~545
seeded people, not just a shortlist. A deactivated employee cannot sign in.

These four are the ones worth demoing, one per role:

| Role | Email | What they get |
|---|---|---|
| `hr` | `naomi.lewis@example.com` | Salary fields, Dashboard, Continuity, Review, Admin, PRDs, work/employee toggle |
| `it` | `shaun.iyer@example.com` | Nothing an `employee` doesn't get — which is the point of demoing it (see [Roles and view modes](#roles-and-view-modes)) |
| `manager` | `sean.wilson@example.com` | Real direct reports, so the downward org chart renders |
| `employee` | `joshua.liu@example.com` | Plain IC — the restricted view |

`xiomara.mensah@example.com` and `minjun.sanchez@example.com` are worth
knowing too: Xiomara → Sean → Min-jun is one reporting chain, so a
training-status change on Xiomara notifies all three, which is what makes
the certification notifications demoable from the UI.

#### Where the role comes from

Role is not a column on `Employee` and deliberately never will be — it is a
per-request claim that production takes from an Entra app-role assignment
(`app/auth.py`). The shim has no claim to read, so it derives one from the
org tree, the same signal `config.hr_org_unit_name()` already uses to decide
who counts as HR for notification sweeps. In order:

| Rule | Role | Seeded count |
|---|---|---|
| In the `IT` division or below (`IT_ORG_UNIT_NAME`) | `it` | 30 |
| In `HR Operations` or below (`HR_ORG_UNIT_NAME`) | `hr` | 18 |
| Has at least one **active** direct report | `manager` | 70 |
| Everyone else | `employee` | 427 |

IT and HR outrank manager because directors in those units manage people
too, and being HR's director is the more specific fact about them. Reports
count only while active, so managing one person who has since been
deactivated doesn't leave someone holding a manager's view of an empty team.
Both unit names fail closed: a name matching no unit grants nothing.

The IT rule staying first is deliberate even now that `it` grants nothing an
`employee` doesn't. It has always meant an IT lead with reports resolves to
`it` and does not get a manager's `direct_reports` view — that was true when
`it` was the privileged role and is unchanged by its privileges moving to
`hr`. Reordering it below `manager` would be a separate decision about the
demo shim, not part of this one.

This is why signing in as a VP can yield `manager` rather than something
grander — the directory role is an access claim, not a rung on the org chart.

#### One credential list, both databases

Accounts are keyed by **work email, and the employee id is looked up at login
time** against whatever database the API is pointed at. `seed.py` draws names
from a fixed RNG seed but ids from `uuid4`, so local SQLite and deployed
Azure SQL hold the same people under different ids — one credential list
works against both, and re-seeding either needs no code change.

Wrong password, unknown email and deactivated employee all return the same
401 with the same message, on purpose.

**Free-text search returns nothing locally, and that's expected.** The
project has one Azure AI Search index and it belongs to the deployed app.
`seed.py` generates the same synthetic people every run but fresh UUIDs, so
your local database shares names with the indexed data and shares no ids —
ranked hits resolve to nobody, and `find_people` degrades to its SQL keyword
path (name/`preferred_name` substring). Structured filters (`?skill=`,
`?org_unit=`, `?office=`) are pure SQL and work locally exactly as deployed;
so do exact-name lookups, which short-circuit before Search is consulted.
Semantic and misspelling-tolerant matching need the deployed backend
(`npm run dev:live`). Do **not** rebuild the index from local data — it
breaks search for everyone using the deployed app.

`.python-version` pins 3.14.6 — Azure App Service's newest Linux runtime
(`PYTHON|3.14`, confirmed via `az webapp list-runtimes`).

### Frontend

Vite + React + TypeScript, in `frontend/`. Talks to the backend above over
CORS at `http://127.0.0.1:8000`; dev-mode auth is sent via the same
`X-Dev-Role` / `X-Dev-User-Id` / `X-Dev-Name` headers, obtained from the
login form (see [Signing in](#signing-in)) and held in `sessionStorage` for
the tab. Sign out from the account menu in the top bar.

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
| `GET /me/capabilities` | which team features this caller can actually use, answered by running the real gate (`analytics.resolve_scope`) rather than re-deriving the rule client-side. Advisory only — every endpoint still enforces for itself |
| `POST /auth/login` | dev-mode only. Any active employee's work email + the shared password → `{id, role, name}`, role derived from the org tree; 404s once Entra is configured (see [Signing in](#signing-in)) |
| `GET /people` | filtered directory listing, permission-filtered per caller |
| `GET /people/{id}` | one person's detail, restricted fields genuinely absent (not null) for callers without access |
| `PATCH /people/{id}/bio` | self-service edit of your own "About" text |
| `PATCH /people/{id}/pronunciation` | self-service edit of your own name pronunciation |
| `POST` \| `PATCH` \| `DELETE /people/{id}/skills` | self-service skills: add, re-level or remove your own, recorded `self`-sourced |
| `GET /people/{id}/skill-routes` | shortest chains from this person to somebody capable in a skill — yours to ask about yourself; HR in work mode may ask on anyone's behalf |
| `GET /people/{id}/skill-suggestions` | skills worth asking about, drawn from what this person's current projects require and what the directory is thin on, each with the reason it was suggested |
| `GET /people/{id}/org-chart` | manager chain + direct reports, both directions |
| `GET /me/notifications` | your own notifications, newest first — no person-id parameter exists, so no role can read anyone else's |
| `POST /people/{id}/training/{course_code}` | hr-only. Records a course status change and fires both notification triggers; stands in for the training system pushing us an event (409 once `ENABLE_TRAINING_API_SYNC` is on) |
| `POST /notifications/date-milestones` | hr-only, optional `?on=YYYY-MM-DD`. Sweeps for birthdays and milestone service anniversaries and notifies HR. Idempotent per date — what a daily cron would call, since nothing in the database changes on someone's birthday |
| `GET /search` | **the unified search+ask surface.** Classifies `q` deterministically (trailing `?` or an interrogative opener) into `direct` (plain filtered results) or `assisted` (also runs the tool-calling layer and returns an `overview` with a prose answer + citations + reasoning trace) |
| `POST /ask` | the older direct entry point to the tool-calling layer; `/search` is what the frontend actually uses now, this is kept as a lower-level API. Every call opens or continues the caller's `search` conversation and records the turn |
| `GET /conversations/{surface}` | the caller's most recent `search` or `prd` thread plus its turns — how a page reload rehydrates. `prd` is scoped per project and HR-only |

Dashboards, workforce intelligence and team building. Every one of these is
scoped by `analytics.resolve_scope`, which **discards** a requested scope rather
than validating it — see [The assistants](#the-assistants):

| Route | Purpose |
|---|---|
| `GET /analytics/overview` | headline dashboard counts for the caller's scope |
| `GET /analytics/org-units` | the departments this caller may narrow to (HR gets all; everyone else, their own line) |
| `GET /analytics/skills`, `GET /analytics/skills/{id}` | skill supply against project demand, and one skill's detail |
| `GET /analytics/training`, `/training/roster`, `POST /analytics/training/reminders` | course compliance, who is outstanding, and a reminder sweep |
| `GET /analytics/projects` | per-project skill coverage |
| `GET /analytics/insights` | the computed findings behind the dashboard's narrative |
| `POST /analytics/report` | a natural-language workforce question answered as a structured report — the model chooses the analyses and writes the summary, never the figures |
| `POST /team/build` | a project brief becomes roles, ranked candidates, a coverage percentage and the gaps behind it, drawn only from people the caller may already see. The model reads the brief into roles and skills; every number is computed in `app/team_builder.py` |
| `POST /team/find` | ranks existing teams and departments for a technical problem, with Expert/Working/Learning counts and the unit head's contact details. Creates nothing |

Projects and requirements:

| Route | Purpose |
|---|---|
| `GET /projects` | the project list behind the PRD and requirement pickers |
| `PUT` \| `DELETE /projects/{id}/description` | hr-only, work mode |
| `PUT` \| `DELETE /people/{id}/projects/{project_id}` | project-membership writes |
| `GET` \| `PUT /projects/{id}/required-skills` | the skills, at a minimum level, a project's delivery needs — also the confirm path for a PRD preview |
| `GET` \| `POST /projects/{id}/requirement-notes` | free-text requirements lifted from a PRD. Readable and writable by **HR or the project's owner only**, a narrower rule than the skill rows, which stay broadly readable as ordinary org facts |

Community links — each person's private "who to ask for what" graph:

| Route | Purpose |
|---|---|
| `GET` \| `POST` \| `PATCH` \| `DELETE /community_links` | your own links |
| `GET /suggested_official_links`, `POST .../generate`, `POST .../{id}/confirm` \| `/reject` | HR's queue for confirming official links |
| `POST /community_links/auto_assign_mentors` | HR's mentor sweep |

Employee lifecycle — all hr-only, work mode. See **HR employee lifecycle** below:

| Route | Purpose |
|---|---|
| `PATCH /employees/{id}` | hr-only internal-field edit (salary, title, `manager_id`, …). Refuses `availability_status: restricted` — that one transition is maker-checker, see below |
| `POST /employees` | **stages** a request to add an employee; creates nobody. Returns 202 with a pending request, not 201 with a person. Small required set (name, title, org unit, work email, employment type) plus an optional mentor; everything else is a follow-up PATCH |
| `POST /employees/{id}/restrict` | **stages** a request to hide a profile; restricts nothing until approved |
| `POST /employees/{id}/deactivate` | **stages** a request to soft-delete; 409s up front while the target still manages anyone active |
| `POST /employees/{id}/reactivate` | reverses a deactivation. Single-actor and immediate — the control is on the destructive direction, not the undo |
| `GET /employees/deactivated` | the only read path that surfaces `is_active=false` records at all; every other one treats them as nonexistent |
| `GET /employee_action_requests` | pending restrict/deactivate/create requests **this caller is the approver for**. Identity-scoped, not role-scoped |
| `POST /employee_action_requests/{id}/approve` \| `/reject` | only the request's own resolved approver may call these — 403 for anyone else, whatever role they hold |
| `GET /org_units`, `GET /offices` | flat lookups behind the create-employee pickers. Any authenticated caller: both are already `BASE_FIELDS` on every profile |

Document extraction and review — all hr-only, work mode. See **Document extraction and review** below:

| Route | Purpose |
|---|---|
| `POST /docs/upload` | parse a .docx/.pdf, stage what it says as `pending` proposed changes |
| `GET /uploaded_docs` | every uploaded document + live pending/unresolved counts, finalized ones included |
| `GET /doc_subject_matches`, `POST /doc_subject_matches/{id}/resolve` | who a document mentions, and confirming which employee that is |
| `GET /proposed_changes` | the review queue, grouped by employee |
| `POST /proposed_changes/{id}/accept` \| `edit` \| `reassign` \| `correct` \| `reject` | per-field review actions; only `accept`/`edit` ever write to a real table |
| `POST /proposed_changes/{id}/undo` | reverses an accept/edit, while the source document is still under review |
| `POST /proposed_changes/bulk_accept` \| `/bulk_reject` | the same per-field decisions over a whole selection |
| `POST /docs/{id}/finalize` | the "Update" action: accept the listed ids, dismiss the rest, then clear the document's own text for good |

PRD requirement capture and its assistant — hr-only, work mode. See
[PRD requirement capture](#prd-requirement-capture) below:

| Route | Purpose |
|---|---|
| `POST /projects/{id}/prd` | upload a project's requirements document and get back a **preview** of the skills and notes extracted from it. Saves nothing — the caller edits the preview and confirms it through the ordinary `required-skills` / `requirement-notes` write paths |
| `POST /prd/ask` | the PRD assistant: its own endpoint, profile, two tools and chain budget. `project_id` selects the thread when starting a new conversation |

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
  query_entities.py   types a free-text query into role / seniority / skill / office /
                       org_unit, using only vocabulary that exists in this database, so
                       "senior data engineer with react, java" is read as more than one
                       job_title-contains-word guess
  people_ranking.py   scores an already permission-filtered pool against that typed
                       interpretation, and explains each score from the same values that
                       produced it. Cannot admit an id that wasn't already in the pool
  writes.py           every write path plus the employee lifecycle: update_employee,
                       create_employee, the restrict/deactivate maker-checker
                       (request → _resolve_approver → approve/reject), reactivate,
                       and list_deactivated_employees — see HR employee lifecycle below
  proposals.py        the HR review workflow over extracted changes: resolve_subject,
                       accept / edit / reassign / correct / reject, undo, and
                       finalize_document (accept some, dismiss the rest, scrub the doc)
  doc_extraction.py   .docx/.pdf → classify → typed extraction calls → staged rows.
                       Python orchestrates the sequence; the model only answers one
                       question per call and never writes
  registry.py         one source of truth for every field the API can select, filter
                       or sort on, and how sensitive it is. Fails startup if a DB column
                       has no entry and no explicit ignore
  policy.py           the policy engine every PeopleQuery passes through. Returns a
                       PolicyDecision (approved fields + mandatory obligations), not a boolean
  query_plan.py       the typed shape a filter-based people request takes (PeopleQuery)
  query_compiler.py   PeopleQuery + PolicyDecision → a read-only, parameterized SQLAlchemy
                       query: approved columns only, values never interpolated
  vocabulary.py       validates a plan and snaps loose values onto real vocabulary
  project_search.py   Mode 3: semantic project search + the project→employee hop, hybrid
                       (embeddings + keyword) fused with RRF. Confidential projects are
                       never embedded, so no query can reach one
  project_skills.py   which skills, at what minimum level, a project's delivery needs
  project_requirements.py  requirement notes: HR or the project's owner only, through the
                       same visible_project check the write path uses
  prd_extraction.py   a PRD's text -> typed skill/note proposals for preview. Structurally
                       separate from doc_extraction: a PRD names no person to disambiguate,
                       only what a project needs, and it writes nothing
  analytics.py        the dashboards, and resolve_scope: the scope gate every workforce
                       surface starts from. A requested scope is DISCARDED, not validated
  workforce_reports.py  a workforce question as a structured report; the model picks the
                       analyses and writes the summary, never the figures
  insight_narrative.py  prose over already-computed dashboard findings
  team_builder.py     brief -> roles -> ranked candidates + coverage. Candidates come only
                       from resolve_scope's pool; every percentage is computed here
  team_finder.py      ranks the teams that already exist for a described problem
  skill_routes.py     shortest chains from one person to somebody capable in a skill
  own_skills.py       self-service skill add/re-level/remove, always `self`-sourced
  community_roles.py  who counts as a mentor/owner in the community graph
  grounding.py        every numeral in generated prose checked against the rows it came
                       from; an unsupported sentence is discarded for a computed one
  continuity.py       staffing continuity, hr + WORK mode only: work-authorization dates against
                       client engagements, severity from versioned config. No model calls
  community_links.py  each employee's private "who to contact for what" graph; official
                       links are HR-confirmed, personal ones are their own
  search_reindex.py   rule 6: every write to an indexed field re-indexes
  org_chart.py        recursive org chart (both directions), cycle-guarded
  directory_tools.py  the tool-calling allowlist and the Python behind each tool —
                       11 search tools + 2 PRD tools, see The assistants below
  tool_calling.py      resolves a natural-language message to ONE tool and runs it (mock
                       resolver with no credentials, real Azure OpenAI tool-calling with
                       them). Holds AssistantProfile + SEARCH_PROFILE / PRD_PROFILE — one
                       engine, two assistants — and _redact_for_phrasing, which strips
                       self-authored free text (bio, contribution, note) before the model
                       that writes the sentence ever sees it
  chain_budgets.py    the per-plan-class budget a multi-step chain runs under (steps,
                       distinct records, wall clock) and the absolute ceiling above it,
                       asserted at startup
  assistant_conversations.py  saved threads: a turn is stored as its PLAN (message, tool
                       name, arguments), never a result, so replay re-runs every call
                       through the permission gate. Someone else's id is a 404, not a 403
  assistant_context.py  the cross-surface layer: extracted facts only, re-checked against
                       the live database before use. Model prose and note text never cross
  text_filters.py     deterministic routing rules, tried before any model call
  unified_search.py    GET /search: deterministic direct-vs-assisted classification, builds
                       the {mode, results, overview} response, permission-safe by construction
  search_client.py     Azure AI Search hybrid retrieval (keyword + prefix + fuzzy + vector) +
                       the embedding client (plain OpenAI client — sharedfoundry is a v1-API
                       Azure AI Foundry endpoint, not a classic per-resource AzureOpenAI one)
  search_index.py      builds/refreshes the Azure AI Search index from the database
  schemas.py           Pydantic response models (PersonSummary, PersonDetail, OrgChainNode, …)
  models/               Employee, OrgUnit, Office, Skill, EmployeeSkill, Project,
                        EmployeeProject, EmployeeCertification, AuditLog,
                        TrainingCourse, EmployeeCourseStatus, CourseRequirement, Notification,
                        UploadedDoc, DocSubjectMatch, ProposedChange, CommunityLink,
                        WorkAuthorizationRecord, ProjectSkillRequirement, ProjectEmbedding,
                        EmployeeActionRequest (staged restrict/deactivate awaiting approval),
                        SuggestedOfficialLink, OrgSettings, ProjectRequirementNote,
                        AssistantConversation, AssistantTurn (26 tables in all)
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
eval/                 golden evaluation set + scorer, run in CI on manual
                       dispatch only (eval/run_golden_eval.py)

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
                                URL-routed at /profile/:id. Carries HR's per-person
                                lifecycle controls: Edit / Restrict / Deactivate, the
                                "pending approval from X" state, and the inline
                                reassignment picker when direct reports block a deactivation
    PendingApprovals.tsx        the banner for whoever has restrict/deactivate requests
                                waiting on them. Identity-scoped, deliberately NOT
                                role-gated: the approver is whoever the requester's
                                chain names, whatever role header they happen to carry
    PeopleAdminPage.tsx         the HR-only Admin tab: create an employee, and the
                                deactivated-employees list (the one place they can be
                                found and put back)
    ReviewPage.tsx              HR's document review: one card per uploaded document,
                                checkbox-selectable suggestions, one Update button that
                                applies the checked ones and clears the doc, an Undo on
                                anything already accepted, and a ✕ to discard a whole
                                wrong-file upload
    AskChat.tsx                 the follow-up thread under an answer; rehydrates from
                                GET /conversations/search on reload
    DashboardPage.tsx, MetricCards.tsx, charts/   the workforce dashboard: skill supply
                                vs. demand, training compliance, project coverage,
                                concentration risk, scoped to whatever resolve_scope
                                returned rather than to what the page asked for
    WorkforceIntelligence.tsx   the dashboard's ask box: a workforce question in, a
                                structured report out, every figure linked to its data
    TeamBuilder.tsx, graphs/ProposedTeamGraph.tsx   Build Team: the brief, the proposed
                                team, coverage and gaps, with the "technical
                                recommendation only" notice the feature ships with
    TeamFinder.tsx              Find a Team: ranked existing teams for a described problem
    PRDsPage.tsx, PRDChat.tsx   the HR-only PRDs tab: upload a requirements document,
                                edit the extracted preview, confirm it, then ask the PRD
                                assistant about it. Its own thread, per project
    SkillDetailModal.tsx        one skill's supply, demand and who holds it
    LoginPage.tsx               the demo sign-in form (see Signing in)
    ContinuityPage.tsx          staffing continuity views; hr in work mode only
    CommunityPage.tsx, CommunityGraphCanvas.tsx   the personal "who to ask" graph
    HelpMenu.tsx, HelpOverlay.tsx   the guided tour and click-to-learn overlay
    GraphPage.tsx               tab switcher for the graph views below, plus Build Team
                                and Find a Team
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

Pushing to `main` runs `.github/workflows/ci-cd.yml`. `test` and `frontend`
run on every pull request and push; `terraform` and `deploy` additionally run
on a push to `main`, and `deploy` needs all three of the others. Concurrency is
grouped per branch with **`cancel-in-progress: false`** on purpose: killing a
deploy mid-way can leave a half-extracted site or a half-applied migration, so
queueing costs minutes where a torn deploy costs an outage.

1. **test** — `pytest`, always. If the push or PR touched AI/search-relevant
   files it also annotates the run with a reminder that the golden eval is
   worth running, but it does not run it.

   The golden evaluation set (`eval/run_golden_eval.py`, 64 questions at the
   time of writing — `eval/golden_set.py` is the source of truth) is its
   own job and runs on **manual dispatch only** — the Actions tab's "Run
   workflow" button, or `gh workflow run ci-cd.yml`. It hits the real Azure
   OpenAI and Azure AI Search resources and paces itself deliberately
   slowly against their token quota, so it is too slow and too rate-limited
   to sit on every push; it also used to run twice per change (once for the
   PR, once for the merge) for a result that never gated anything. Run it
   when AI or search behaviour changed, and read it as a signal rather than
   a gate.
2. **frontend** — `tsc -b` then the production `npm run build`, the exact
   command the deploy job runs rather than a bare `tsc --noEmit`, so a type
   error stops the release instead of shipping.
3. **terraform** — `terraform/main.tf` provisions the App Service (plan +
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

4. **deploy** — builds the frontend, zips it with the backend, and deploys
   via `az webapp deploy` (OneDeploy, `--clean true`). A guard waits for any
   in-flight deployment rather than starting a second one on top of it. Ends
   with a health-check poll against `/health` and `/` so a "successful" deploy
   that's actually crash-looping fails the workflow instead of leaving a
   silent 503.

Required GitHub repo secrets: `ARM_CLIENT_ID` / `ARM_CLIENT_SECRET` /
`ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID` (deployment service principal),
`DB_PASSWORD`, and three independent endpoint/key pairs for Quadrant's AI
resources — `GROUP3_4OPENAI*` (chat), `GROUP3_4_TEXT_EMBEDDING_3_SMALL_*`
(embeddings), `AISEARCH_*` (search).

## The assistants

Two assistants — search and PRD — run on **one engine under different
profiles**. An `AssistantProfile` (`app/tool_calling.py`) carries a system
prompt, few-shot examples, a tool set and a chain budget, and is threaded
through every model call in a turn: the first resolve, a chain's re-prompt,
and a failed-call retry alike. Adding the second assistant was a profile plus
a budget-registry entry, not a change to the loop.

| Profile | Endpoint | Tools | Budget | Gated to |
|---|---|---|---|---|
| `SEARCH_PROFILE` | `GET /search`, `POST /ask` | 11 | `assistant_chain` | everyone |
| `PRD_PROFILE` | `POST /prd/ask` | 2 | `prd_chain` | hr, work mode |

### What the model may and may not do

The latitude is real: it picks the tool, fills the arguments itself, and plans
one call at a time — seeing each result before choosing the next, rather than
committing to a sequence up front. What it cannot do is the point:

- it **never queries the database** — it holds no connection and no credentials;
- it **never decides authorisation** — permissions are resolved before it is invoked;
- it **never supplies identity** — caller id and view mode come from the session
  and are overwritten server-side before any tool runs;
- it **never selects a person or computes a statistic** — ranking and arithmetic
  are Python (`app/people_ranking.py`, `app/team_builder.py`, `app/analytics.py`);
- it **cannot invoke a tool outside its own profile** — the function schema is
  what constrains the output, not an instruction asking it not to.

If it writes a numeral the source rows do not support, `app/grounding.py`
discards the sentence and substitutes a computed one.

### The thirteen tools

Eleven belong to the search assistant; the last two belong to the PRD
assistant and are never offered to the search surface.

| Tool | Answers |
|---|---|
| `find_people` / `search_people` | Who matches these filters or this description? |
| `get_person` | Full profile for one named individual |
| `get_org_chain` | Who is above or below this person? |
| `find_project_owner` | Who owns this system or policy? |
| `find_mentor` | Who could teach me this skill? |
| `skill_gap` / `skill_scarcity` | Where are we thin, and who is a single point of failure? |
| `find_experts` | I have this problem — who has solved it before? |
| `get_people_with_projects` | Who worked on what? |
| `compare_people` | How do two people differ? |
| `get_project_requirements` | What does this project need? *(PRD, hr-only)* |
| `list_project_requirements_summary` | Which projects have requirements on file? *(PRD, hr-only)* |

Before the model is consulted at all, deterministic pattern rules
(`app/text_filters.py`) attempt the routing themselves, and a direct search — a
name, a skill — never reaches a reasoning model. A test proves that by
patching the router to raise.

### Three gates, because there are three different questions

Using the wrong one is the most plausible way to introduce a security bug
here, so they are named explicitly:

| Gate | Question | Used by |
|---|---|---|
| `policy.enforce()` | Which rows and fields may this query return? | directory search, profile, org chart |
| `is_record_visible()` | May this person be discovered at all? | skill statistics, expert finding, Find a Team |
| `analytics.resolve_scope()` | Whose workforce is this caller responsible for? | dashboards, workforce reports, Build Team |

`resolve_scope` does not validate a requested scope — it **discards** it. HR in
work mode may choose any department; everyone else gets their own reporting
line whatever parameters they send, and `substituted` in the response records
that it happened. This is why it is safe to point a model at Build Team: the
plan it produces has no scope field for one to land in, and a test asserts
that absence directly.

The three gates pair with the agent's three answer paths, each guarded the way
its risk allows:

- **A composed query** (`find_people` / `search_people`) is the only path that
  becomes SQL, and its shape cannot be predicted — so the plan itself is
  inspected: validated against the field registry, snapped onto real
  vocabulary, stripped of fields the caller cannot read, and rejected outright
  if it smuggles in a filter or sort.
- **A named operation** runs a fixed method with open inputs, so each service
  function carries its own role check.
- **Semantic search** cannot be inspected at all — there is no field in a
  similarity score to reject — so its guard moves to the corpus: confidential
  projects are never embedded, and what was never indexed cannot be retrieved.

### Bounded chains

A question like "who on Priya's team knows Terraform and is free next month?"
needs the team resolved before it can be searched. The model declares that
itself by setting `needs_followup`, a typed boolean in every tool's schema —
"this needs another step" arrives as a validated argument, never as prose the
engine would have to interpret. Only then does a loop exist; an ordinary
request costs exactly one call.

The loop runs under a declared budget with three independent axes, and
whichever runs out first ends the chain (`app/chain_budgets.py`):

| Plan class | Budget | Used by |
|---|---|---|
| `assistant_chain` | 4 steps, 100 distinct records, 8 s | the search assistant |
| `prd_chain` | 3 steps, 60 distinct records, 8 s | the PRD assistant — a requirements conversation is about one project, so a wide fan-out is a symptom, not a use case |

Steps bound reasoning depth, distinct records bound how much a chain
accumulates across steps, wall-clock bounds the caller's wait. An absolute
`CEILING` (8 steps, 300 records, 20 s) caps every plan class however it is
declared, and `assert_chain_budgets_within_ceiling()` fails the application at
startup if a declared budget exceeds it — the same boot-time discipline
`assert_registry_covers_schema()` uses. The budget is checked after every step,
and when a limit ends a chain the response carries which axis tripped and the
answer text says it may be incomplete.

### Saved conversations store plans, never answers

`assistant_turns` persists each turn as the plan it resolved to — the message,
the tool name and its arguments — never the result. On a new turn the stored
calls are **re-executed through the same permission-gated dispatcher** as a
fresh request, so access revoked between one turn and the next is simply
absent on replay. Persisting a conversation adds no second permission path to
keep in sync.

Ownership is absolute: a conversation id that exists but belongs to someone
else returns **404, never 403**, so not even the id's existence is confirmed.
PRD conversations are additionally scoped per project, so working on project B
never rehydrates project A's thread.

### What crosses between the two assistants

Each assistant can read extracted facts from the caller's most recent
conversation on the other surface — which projects and skills were discussed —
and suggest a next step from them ("You captured 4 requirements for Meridian.
Want to see who covers them?"). Three rules keep that safe, all structural, in
`app/assistant_context.py`:

- **Only turns that carry a tool call.** Model-written prose (`assistant_text`)
  never crosses surfaces: inside its own conversation it is connective tissue,
  injected into a different assistant's prompt it would be an unverified claim
  laundered as context.
- **Only controlled vocabulary.** The tool name plus argument values that
  already passed vocabulary snapping, rendered into the receiving prompt as an
  explicitly-labelled data block — facts, not instructions.
- **Every fact re-checked against the live database before use.** A project
  reclassified confidential, or a person deactivated, since the turn was stored
  is dropped rather than surfaced.

### Untrusted free text is removed, not distrusted

Injection through *stored content* is handled separately from injection through
the question. Text that people author themselves — a bio, a project
contribution note, a PRD requirement note — is stripped from the payload the
phrasing model receives (`_UNTRUSTED_FREE_TEXT_KEYS` in `app/tool_calling.py`)
rather than merely surrounded by an instruction to ignore it. A prompt
instruction is exactly what adversarial input is written to defeat; absent data
cannot be leveraged. The text still reaches the human on screen — only the
model is denied it, and there is a test for each half.

The same idea is what protects semantic search: confidential projects are never
embedded, so no query can reach one.

### The cache key is a security decision

Both model calls are pure functions of what is handed to them, which is what
makes them cacheable at all: the router reads only the message text, and
`phrase_answer` reads only the already-redacted, already-permission-filtered
result. So the key is a hash of **the exact model input, never the question
alone** — two callers entitled to different rows produce different payloads,
therefore different keys, and neither can be served the other's phrasing. A
cache keyed on question text would have precisely that bug.

Bounded and in-process on purpose: a 512-entry LRU for the two model calls
(`app/tool_calling.py`) and a 2,048-entry one for embeddings
(`app/search_client.py`) — an LRU dict, not Redis, because there is one App
Service instance and a cold cache is only ever as slow as today. Retries and
multi-step chains skip the cache, since they carry state the message text does
not describe.

## PRD requirement capture

HR picks a project and uploads its requirements document (`.docx`/`.pdf`) to
`POST /projects/{id}/prd`. Extraction (`app/prd_extraction.py`) sends the
document text to the model — the only step that ever sees it, and the
strongest-authorised moment in the flow, since HR chose the file seconds
earlier. The model may only emit typed proposals: a skill with a minimum
level, or a free-text note. It has no write access and is instructed never to
answer in prose.

What comes back is a **preview**, not a save. HR corrects it on screen and
confirms through the ordinary write paths — `PUT /projects/{id}/required-skills`
and `POST /projects/{id}/requirement-notes` — the same routes a hand-authored
requirement already goes through, so there is no second write path to keep in
sync. Skills **replace** the project's requirement list; notes are
**appended**, because a second PRD must never silently erase a note recorded
months ago.

On confirm the source document's extracted text is erased and
`content_scrubbed_at` stamped, exactly as document review does it — the upload
row survives for provenance, its content does not. The scrub touches only PRD
uploads (rows carrying a project id), so ordinary document-review uploads are
never affected.

**Access rules**

- The PRDs page, `POST /projects/{id}/prd`, `POST /prd/ask` and the `prd`
  conversation surface are all gated to **hr in work mode**.
- **Requirement notes** are readable and writable by **HR or the project's
  owner**, nobody else — they are sentences lifted verbatim out of a planning
  document, a different category from a skill-and-level row, which stays
  broadly readable as an ordinary org fact. The asymmetry is deliberate.
- The service function behind every PRD tool re-checks the caller's role
  before any query runs. The route gate and the separate tool set are defence
  in depth; **the service check is the enforcement boundary**.
- Note text is in the redaction set above, so it never reaches the model call
  that writes the final sentence. An uploaded document can be authored by
  anyone, including someone outside the company, and a sentence inside it could
  be an instruction aimed at the model.

**Why a separate assistant rather than a filtered tool list.** The PRD
assistant has no people-search tools, so even a wholly successful injection has
nothing to pivot to — the defence is structural, not textual. The search
assistant is byte-identical to its pre-PRD behaviour, and a coverage test
drives a full PRD chain end to end, asserting that the PRD tool set is what
reaches the model at every step.

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
   calls (`find_people`, `get_person`, `get_org_chain`, …), one at a time, from
   the tool set its own profile offers. See [The assistants](#the-assistants).
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
8. Privilege is a table, not a ladder. `hr` reads internal information, edits
   project descriptions and reviews AI-extracted changes; `it` reads and
   writes exactly what `employee` does. `it` used to hold the last two of
   those and never salaries — proof the tables are the decision and not a
   ranking, since moving them to `hr` was an edit to `app/permissions.py`'s
   ALLOWED / EDITABLE tables and the two role gates that name a role
   directly (`proposals._authorize`, the upload route), not a restructure.
9. The model's output is never a database write. Extraction emits typed
   `propose_project_update` calls that land in `proposed_changes` as
   `pending`; only an HR reviewer's explicit accept moves content into
   `EmployeeProject` / `EmployeeSkill`, and only then is it searchable.
10. Nothing is deleted, only marked. Employees deactivate (`is_active`),
    documents are scrubbed but keep their row, rejected proposals are kept.
    Every table here is referenced by id from somewhere that has to outlive
    it — audit rows, past project membership, notifications.
11. Changing who exists takes two people. Restricting a profile,
    deactivating an employee and **adding** one all stage an
    `EmployeeActionRequest` for the **requester's own** manager to approve;
    the requester cannot approve their own. Approval is gated by identity
    (`caller.id == approver_id`), never by role — see HR employee lifecycle.
    The reversals (unrestrict, reactivate) stay single-actor: the control
    belongs on the direction that changes the roster, and a reactivation can
    only restore somebody this same control already approved removing.
12. A staged request is text, never a half-built row. A pending create keeps
    its proposed employee in `employee_action_requests.payload` as JSON
    rather than an `employees` row with `is_active=false`, so no query that
    forgets to exclude it can surface a person nobody approved.
13. Untrusted free text is removed from a prompt, never argued with. Bios,
    project contributions and PRD requirement notes are stripped before the
    phrasing call (`_UNTRUSTED_FREE_TEXT_KEYS`); an instruction to ignore
    them is exactly what adversarial input is written to defeat.
14. A scope is discarded, not validated. `analytics.resolve_scope` throws the
    caller's requested scope away and derives one from who they are, so no
    phrasing of a brief or a question can widen a workforce view.
15. A conversation stores its plan, never its answer. Replay re-runs every
    stored call through the same permission gate as a fresh request, so a
    saved thread adds no second permission path to keep in sync.
16. Every multi-step chain runs under a declared budget, and no declared
    budget may exceed the absolute ceiling — asserted at startup, alongside
    the registry/schema check.

## Roles and view modes

Four roles, two lenses. `view_mode` is a parameter on the directory/profile
read endpoints (`GET /people`, `GET /people/{id}`, `GET /search`, `POST /ask`)
and on every write:

| | `employee` mode | `work` mode |
|---|---|---|
| `employee` / `manager` / `it` | base fields | *unreachable — pinned to employee mode* |
| `hr` | base fields | \+ salary, DOB, hire_date, cost_centre, training |

`it` used to sit on its own row here, with a work mode that granted
`project_desc` editing and the document review queue but never salary. That
split is gone: administering the system that holds people's records is not
the same as owning the records, so **`it` now reads and writes exactly what
`employee` does**, and both of its former privileges — project-description
CRUD and the whole review pipeline — belong to `hr`. `hr` is the only role
with a reachable work mode. (`project_desc` is absent from the table because
it is readable by everyone now, in either mode; only *editing* it is gated,
and that is `hr`/work.)

Three things are worth knowing before changing any of this:

- **`resolve_view_mode` is the only place the client's parameter is read.**
  Anything other than `hr` is answered in employee mode however it asks; an
  unrecognised value narrows rather than 400s. `hr` defaults to work mode
  when it doesn't ask, which is what it got before view modes existed.
- **Employee-mode output is identical whoever is looking.** Enforced in three
  places, not one — the field table, `is_record_visible`, and
  `department_filter` — because each is a separate pipeline stage and any one
  left role-aware leaks the caller's privilege back into a view that is
  supposed to be anonymous. The sharp edge: **HR loses its restricted-record
  exemption in employee mode**, so `restricted-1` 404s for them there too.
- **Whole surfaces disappear in employee mode, not just fields.** Continuity
  (HR), Review (HR), Admin (HR), PRDs (HR) and the official-link/mentor-sweep
  panels (HR) are work-mode surfaces: an ordinary colleague has no work-authorization
  review dates, no document review queue, no create-employee form and no
  bootstrapping queue, so neither does anyone previewing that lens. Several
  had to be retrofitted — their gates read `caller.role` alone, so an HR
  caller kept full access while claiming to be looking at the ordinary view.
  They route through `effective_role` now, in the service functions and again
  at the route layer, so hiding a tab is the cosmetic half of a check that
  exists on the server. Also retrofitted: the org chart's downward direction
  (`GET /people/{id}/org-chart` had no `view_mode` parameter *at all*), and
  HR's blanket exemption for confidential projects in
  `project_skills._visible_project`.
- **`manager` has no work mode, and that has a consequence worth stating.**
  `resolve_view_mode` pins every role outside `WORK_MODE_ROLES` (`hr`) to
  employee mode however it asks, and `effective_role` collapses every role
  there — so a manager sees `direct_reports` through **neither** `find_people`
  nor the org chart. `find_people` was always like this; the org chart only
  differed because it had no `view_mode` to pass, and the two disagreed
  outright for a manager until that was fixed.

  The tempting fix is a carve-out — "employee mode takes away what work mode
  granted, and a manager never had a work mode, so it takes away nothing".
  That was written, and it fails
  `test_employee_mode_list_identical_across_roles` immediately:
  `direct_reports` present for a manager and absent for an employee is the
  caller's role leaking back into the view that exists to be anonymous. The
  identity guarantee wins. Giving managers their team back means giving them
  a **work mode** (adding `manager` to `WORK_MODE_ROLES`), which is a
  deliberate product change rather than an exception inside one predicate.

  Note this is only about *role predicates*. A manager's real extra reach is
  ABAC, which keys on identity and survives employee mode by design — and the
  field table grants them nothing extra anyway (`ALLOWED[("manager", "work")]`
  is identical to `ALLOWED[("employee", "work")]`).
- **ABAC survives employee mode, deliberately.** Own-profile and
  direct-manager grants (personal_mobile, own salary/DOB, training status up
  the chain) key on the caller's *identity*, never their role, so they return
  the same answer for a given pair of people whoever asks — which is exactly
  what the identity guarantee requires. An employee can still see their own
  salary; they still cannot edit it.

## HR employee lifecycle

Creating people, removing them, and hiding one person's profile from
everyone. All hr-only, work mode, gated by the same `app/permissions.py`
EDITABLE table every other write goes through.

### Hiding a profile was already enforced — nothing could set it

`AvailabilityStatus` has had a third value, `restricted`, since the original
schema, and `is_record_visible` has always honoured it: a restricted person is
absent from search, from the org chart, from project-membership results, from
AI answer citations, and from **their own manager's** view. Only `hr` in work
mode is exempt, and even HR loses that exemption while previewing employee
mode. What was missing was any way to turn it on — `availability_status`
wasn't in EDITABLE and wasn't in the PATCH schema, so the only restricted
records that ever existed were seeded that way.

That gap is now closed, but not through the generic PATCH: `POST
/employees/{id}/restrict` stages an approval request instead (below).
`PATCH /employees/{id}` explicitly **refuses** `availability_status:
restricted` and says to use that route. `available`/`away` stay ordinary
PATCHable values — only the transition *into* restricted is controlled.

### Deleting is deactivating

`employees.is_active` has always been documented as the only intended delete
("Soft delete only. Records are never hard-deleted") and nothing ever wrote it.
`POST /employees/{id}/deactivate` does, alongside a new `deactivated_at`.

Hard deletion isn't offered: `audit_log`, `employee_projects`,
`proposed_changes`, `notifications` and anyone's `manager_id` all reference
employees by id with no cascade, so a real `DELETE` would either fail or
silently orphan history.

**Deactivation is blocked while the target still manages anyone active.** HR
reassigns those reports first — which is why `manager_id` is now editable, it
wasn't before — rather than leaving the org chart pointing at someone who's
gone. The 409 names exactly who's blocking it, so the UI can offer a picker per
person instead of making HR go find them. Delegate references are different:
`delegate` means "who's covering while I'm away", so those are cleared
automatically rather than blocking — a cleanup, not a decision.

`POST /employees/{id}/reactivate` reverses it, single-actor and immediate. It
deliberately does **not** restore the delegate references deactivation
cleared: those pointed at the target being available to cover for someone
else, not at their own employment, and silently recreating a relationship
nobody asked for would be guessing.

### Two people, not one

Restricting, deactivating and creating are **maker-checker**. The POST stages
an `EmployeeActionRequest` and notifies an approver; nothing is applied until
that approver acts. The requester cannot approve their own request.

The approver is resolved from the **requester's own reporting chain** — not
the target's — because the control is "one person shouldn't be able to do this
alone", which is about the actor, not about who they're acting on:

1. their manager, if active and not `away`;
2. if that manager is `away`, their `delegate` — the field already means
   "who's covering for me", so this is exactly its use;
3. if the delegate is also away or inactive, keep walking up.

Bounded by `org_chart.MAX_DEPTH`, the same cycle guard every other chain walk
uses. If the whole chain is exhausted with nobody reachable, the request is
**refused (422)** rather than staged with a null approver nobody could ever
action.

The approver is stamped on the row **once, at request time**, and never
re-derived when the approval arrives — otherwise a reorg mid-approval would
silently move who was authorised to decide.

Approval is gated by **identity, not role**: `caller.id == request.approver_id`.
Whoever the chain names is who decides, whatever role header they're carrying —
so `GET /employee_action_requests` has no role gate either, and a
`manager`-role identity who happens to manage an HR person sees their requests
normally. A different HR identity gets a 403.

Preconditions are re-checked at approval time, not just at request time: an
approval can sit pending while the org moves under it. Someone can acquire
direct reports in that window; a proposed hire's email address can be taken by
somebody else, or their chosen manager or mentor can leave.

### Finding someone after you've deactivated them

`is_active=false` is a stronger gate than `restricted` — `get_person` returns
nothing for **every** caller, HR included. That made `reactivate` correct but
unreachable: nothing could find the id to call it with.

`GET /employees/deactivated` is the one deliberate carve-out, gated by the
same `deactivate_employee` capability deactivating took — the people who can
put someone back are exactly the people who could have taken them out. It's
narrow on purpose (identity and placement, nothing else): the question it
answers is "who did we deactivate, when, and should they come back", not "show
me their record". Newest departure first, with `deactivated_at IS NULL` rows
last — anyone seeded inactive, or deactivated before that column existed, is
still a deactivated employee, and this is the only view that can see them.

Capped rather than paginated: the set grows for the life of the company while
the thing it serves is short-horizon (undo a mistake, reinstate a recent
leaver). A genuine "search every former employee" need would want a query
parameter, not a bigger cap.

### Creating

`POST /employees` takes a deliberately small required set — full name, job
title, org unit, work email, employment type — plus optional placement
(office, manager, preferred name, work phone, hire date, defaulting to today)
and an optional mentor. Everything `update_employee` already covers (salary,
DOB, cost centre, LinkedIn) is a follow-up PATCH rather than a duplicated
field list here: a new hire's identity and where they sit is what onboarding
needs on day one, and the rest fills in as it becomes known.

**Creating is maker-checker too**, on the same machinery as restrict and
deactivate — the route returns **202 with a pending request**, not 201 with a
person. Adding someone is what mints a real identity in the directory, and a
fabricated one isn't cheaply undone: deactivating it later leaves the record,
its audit trail, and anything already linked to it in place. (`reactivate`
stays single-actor, and genuinely is the low-risk direction — it can only
restore somebody the two-person control already approved removing.)

`create` is the one action type with **no target employee**, since the person
doesn't exist until the approval lands. That shapes three things:

- `employee_action_requests.target_employee_id` is nullable, and the proposed
  fields sit in a new `payload` column as JSON. Deliberately inert text, not a
  half-built `employees` row with `is_active=false`: a real row would be
  reachable by every query that forgets to exclude it — search indexing, the
  org chart, the deactivated-employees list, the `work_email` uniqueness
  check. Keeping it as text means "proposed" and "employed" cannot be confused
  by any code path that doesn't deliberately open the column.
- Every "who is this about" surface reads through `request_subject_name`,
  which falls back to the payload's name. The approval queue can therefore
  name a person who has no id yet.
- On approval the request's `target_employee_id` is backfilled with the id it
  just created, so the audit trail can get from "who approved this" to "who
  exists because of it".

Validation (`work_email` uniqueness; the org unit, office, manager and mentor
existing and being active) runs **twice** — once when the request is staged,
so HR gets immediate feedback and an approver is never handed something that
cannot apply, and again at approval, because the world moves while a request
sits pending. Someone else can take the email address; the chosen manager or
mentor can leave. Either way the whole action is refused (409), never applied
half-way.

#### The mentor question

The create form asks whether the new hire has a mentor. `mentor_id` is the one
field that isn't an `employees` column at all — "who shows this person the
ropes" is a relationship, not an attribute of them — so on approval it becomes
an **official `community_links` row owned by the new hire**, byte-identical to
what `auto_assign_mentors` would have created: `role_label: "mentor"`,
`is_mentor_link: true`, office and department stamped.

Matching that shape exactly is load-bearing, not tidiness. `_eligible_new_hires`
excludes anyone who already has a mentor link, so an HR-chosen mentor
automatically suppresses the sweep for that person — HR's real decision
overrides the algorithm's guess instead of being topped up with a second
mentor beside it. And because it's `official` rather than `personal`, the new
hire can't delete it, and it ages into a personal link on the same
`mentor_link_duration_days` clock as every other mentor link.

Leaving the field blank keeps today's behaviour: the sweep picks somebody
during their first few weeks.

### A link outlives the person it points at

Nothing deletes `community_links` rows when somebody is deactivated or
restricted, and nothing should — the row is the owner's own note about who to
ask for what, and it should come back intact if that person returns.

But the link is only a pointer, and the Community graph was the one surface
that rendered a contact who had since become invisible. `GET /community_links`
returned every stored row; the client's per-contact profile lookup then 404'd
for the ones it wasn't allowed to see, and the card fell back to printing the
**raw contact id** with a green "available" dot — for someone who was neither
available nor visible. The synthesized manager entry had a narrower version of
the same hole: it checked `is_active` but not `restricted`.

Both now filter on the same `is_active` + obligations pair every other read
path uses, at read time rather than by deleting anything. That's why the route
takes `view_mode`: the check has to agree with the one `GET /people/{id}`
applies in the same mode, or the list hands back a contact the profile lookup
refuses — which is exactly the disagreement that produced the bare id. So hr in
work mode still sees a restricted contact here, and loses it in employee mode,
consistent with everywhere else.

The frontend stopped being able to express the bug rather than just avoiding
it: `ContactNode.person` is no longer nullable, and `ContactCard` takes the
person as a required prop, so a card cannot be constructed for a contact
nobody can name.

## Document extraction and review

`POST /docs/upload` (HR, work mode) parses a .docx/.pdf, stores the extracted
text in `uploaded_docs`, and queues what it says in `proposed_changes` as
`pending`. Name resolution runs through the same `find_people` fuzzy search
the directory uses, and **returns nothing on ambiguity** — the dataset
contains two people called Priya Sharma on purpose, and `employee_id` is
nullable precisely so "I don't know who this is" is a reviewable outcome
rather than a coin flip.

Review is HR-only, work mode: `GET /proposed_changes?doc_id=` (grouped by
employee, unresolved first), then `accept` (commits + re-indexes + audits with
`source=ai_extraction`), `reassign` (re-points it, stays pending), `correct`
(back through the function-calling loop, stays pending), or `reject` (the row
is kept, not deleted — a rejected proposal is the most useful row in the table
when extraction quality is next reviewed). HR's fallback for anything it won't
accept is the manual edit endpoints.

Accepted skills land as `Learning` / `self`-sourced, never higher: a document
saying somebody used Terraform is evidence they touched it, not that they are
an expert `find_mentor` should be recommending.

### One document at a time, then the document goes

The review screen is one card per uploaded document, not a page-wide queue:
check the suggestions you want, click **Update**, and that document is done.
`POST /docs/{id}/finalize` accepts the checked ids, **rejects everything else
still pending for that document** — an unchecked suggestion is a declined one,
not a skipped one — and then clears the document's own `extracted_text` and
stamps `content_scrubbed_at`.

The `uploaded_docs` row itself survives that scrub, deliberately:
`proposed_changes` and `doc_subject_matches` both hold a non-nullable FK into
it, and every decided row is kept as its own audit trail. So what goes is the
document's *content*, not the record that it existed —
`content_scrubbed_at` is what distinguishes "emptied on purpose after review"
from "nothing parsed out of it in the first place".

Two consequences worth knowing:

- **`correct` stops working on a finalized document**, since re-extraction has
  nothing left to read. `edit` still does — it commits the reviewer's own typed
  value and never consults the source text.
- **A subject nobody resolved keeps its rows pending** through a finalize.
  Those proposals have no `employee_id` yet, so finalize can't act on them
  either way; resolving that person later still works, because
  `accept`/`edit`/`reject` never read the document's text.

### Undoing an accept

`POST /proposed_changes/{id}/undo` flips an `accepted`/`edited` row back to
`pending` and reverses exactly what it wrote — from an effect recorded at
commit time (`undo_state`), never re-derived afterwards, because for an edited
row the current `proposed_value` is the reviewer's own text rather than what
`_commit` actually saw.

What it reverses is deliberately asymmetric:

- an `EmployeeSkill` row is deleted **only if that exact proposal created it** —
  a second document proposing a skill someone already holds is a no-op on
  accept, and undoing it must not delete the first proposal's work;
- an `EmployeeProject` row is **never** deleted, only the field this proposal
  set is restored. The membership row is shared: a `project_entry` proposal may
  have set the role on the same row a `contribution` proposal set the text on,
  and deleting it would destroy an unrelated, still-accepted change.

Only reachable while the source document hasn't been finalized — the same gate
`correct` uses, for the mirror-image reason. There's also a **✕ on each
document card** for the "I uploaded the wrong file" case: it's the same
finalize call with nothing selected, so everything is dismissed and the content
cleared in one click, without having to reason about checkboxes first.

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
      privileged write endpoints enforced server-side (see Roles and view
      modes). `it`'s extra privileges were later moved wholesale to `hr` —
      the re-key survived that unchanged, which is the point of it
- [x] 17. Rule 6 actually implemented — `app/search_reindex.py`, shared with
      `build_search_index.py` and wired into every write path including the
      pre-existing `update_own_bio`, which never re-indexed
- [x] 18. Document upload → typed-call extraction → HR review workflow
      (`uploaded_docs`, `proposed_changes`; see Document extraction and review)
- [x] 19. Review reshaped around the document rather than the queue: pick the
      suggestions you want, one Update applies them and dismisses the rest,
      then the document's own text is cleared (`content_scrubbed_at`). Plus
      `undo` on an accepted change and a one-click discard for a wrong upload
- [x] 20. `EmployeeProject.contribution` actually exposed on `GET /people/{id}` —
      accept() had always committed it, but `ProjectHistoryItem` had no field
      for it, so it was invisible on every profile however it got there
- [x] 21. HR employee lifecycle: create, deactivate (soft, blocked on active
      direct reports), restrict, reactivate, and the deactivated-employee
      browse view that makes reactivate reachable at all
- [x] 22. Maker-checker on the two irreversible ones: restrict and deactivate
      stage a request for the requester's own manager (delegate-first when
      away, escalating up the chain) and apply only on approval
      (see HR employee lifecycle)
- [x] 23. Self-service skills, community-graph roles, and the follow-up thread
      under an answer
- [x] 24. Workforce dashboards and workforce intelligence: skill supply vs.
      demand, training compliance, project coverage and concentration risk,
      all behind `analytics.resolve_scope` — the gate that discards a
      requested scope rather than validating it
- [x] 25. Multi-step chains, then the budget that bounds them: the flat
      `MAX_CHAIN_STEPS` constant became a per-plan-class budget over steps,
      distinct records and wall-clock, under an absolute ceiling asserted at
      startup (see The assistants)
- [x] 26. Build Team and Find a Team, plus the access gating both sit behind —
      the model reads a brief into roles and skills and does nothing else
- [x] 27. Renamed to **Mel**, with the new mark; guided tour extended to cover
      every feature
- [x] 28. PRD requirement capture and a second assistant: one `AssistantProfile`
      threaded through every model call site, its own tool set, its own chain
      budget, and no change to the loop (see PRD requirement capture)
- [x] 29. Saved conversations for both surfaces — stored as plans, replayed
      through the permission gate — and the cross-reference layer between them
- [x] 30. Profiled the assisted path, then cached the three model call sites on
      a key that includes the permission-filtered payload
- [x] 31. Query-entity typing and ranked people search, so a multi-part query
      ("senior data engineer with react, java") is ranked rather than flattened
