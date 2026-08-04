# Employee Directory with Smart Search

Internship project at Quadrant Technologies — Project 4 of 11 in the AI internship
programme. Deploy and present by **20 August 2026**.

An internal employee directory with natural-language search: find people by name,
skill, team, or a plain-English description ("who knows Power BI in Bangalore").
Search runs on Azure AI Search hybrid retrieval; a language model turns messy
queries into typed function calls but never touches the database directly —
permission filtering happens in Python, between retrieval and the model.

## Team

| Area | Owner |
|---|---|
| Backend & AI layer (this repo) | Arshitha |
| Project management | Neev |
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
  fuzzy matching, and the app still starts, when `OPENAI_ENDPOINT` / `OPENAI_KEY`
  are unset.
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

## Project structure

```
app/
  main.py            FastAPI app — health check, /docs, /auth/whoami (no employee data yet)
  auth.py            pluggable get_current_user: dev header vs. real Entra JWT validation
  db.py              SQLAlchemy engine/session, reads DATABASE_URL
  models/            Employee, OrgUnit, Office, Skill, EmployeeSkill, Project,
                      EmployeeProject, EmployeeCertification, AuditLog
alembic/             migrations (SQLite now, Azure SQL later — same DDL)
seed.py              synthetic data generator + constraint verification summary
```

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
- [ ] 6. Recursive org chart endpoint (both directions, cycle-guard test)
- [ ] 7. Azure AI Search index + `build_profile_text()` + batch embedding
- [ ] 8. Hybrid search wired into `find_people`
- [ ] 9. Tool-calling layer with few-shot examples (mock first)
- [ ] 10. Golden evaluation set, scored per tier
- [ ] 11. Frontend: search, results, three graph views, profile panel
