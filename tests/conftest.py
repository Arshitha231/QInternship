"""Test fixtures.

DATABASE_URL is pointed at a throwaway temp file BEFORE app.db is imported
by anything, so these tests never touch the real dev directory.db. All
routes under test are read-only (find_people / get_person), so the whole
fixture set is seeded once per test session rather than per test.

Search/OpenAI env vars are explicitly cleared before any app import too —
otherwise a real .env (see step 7/8) would make find_people's tests route
through the live Azure Search index, which has no idea the test fixtures
(Riley Report, Rory Restricted, ...) exist. Tests must stay hermetic
regardless of what's sitting in the developer's .env; this forces the
find_people's SQL fallback path deterministically, same as it always ran
before Search existed.
"""
import os
import tempfile
from datetime import date, datetime

import pytest
import pytest_asyncio

# Set (not pop!) to empty string: load_dotenv() only skips vars that are
# already *present* in os.environ, even if empty — an absent var would
# just get re-populated from .env the moment app.search_client imports it.
for _var in ("SEARCH_ENDPOINT", "SEARCH_KEY", "EMBEDDING_ENDPOINT", "EMBEDDING_KEY",
             "OPENAI_EMBEDDING_DEPLOYMENT", "CHAT_ENDPOINT", "CHAT_KEY"):
    os.environ[_var] = ""

_tmp_fd, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_tmp_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_PATH}"
os.environ["AUTH_MODE"] = "dev"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Employee,
    EmployeeProject,
    EmployeeSkill,
    Office,
    OrgUnit,
    Project,
    Skill,
)
from app.models.enums import (  # noqa: E402
    AvailabilityStatus,
    EmploymentType,
    ProjectClassification,
    ProjectType,
    SkillCategory,
    SkillLevel,
    SkillSource,
)
from app.people import MAX_RESULTS  # noqa: E402


def auth_headers(role: str, user_id: str = "caller-x") -> dict:
    return {"X-Dev-Role": role, "X-Dev-User-Id": user_id}


@pytest.fixture(scope="session", autouse=True)
def _database():
    Base.metadata.create_all(engine)
    _seed()
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()
    os.remove(_TMP_DB_PATH)


def _seed() -> None:
    db = SessionLocal()
    try:
        office = Office(name="Test HQ", city="Testville", country="Testland", timezone="UTC")
        db.add(office)
        db.flush()

        company = OrgUnit(name="TestCo", parent_id=None, unit_type="company")
        db.add(company)
        db.flush()
        eng_div = OrgUnit(name="Engineering", parent_id=company.id, unit_type="division")
        fin_div = OrgUnit(name="Finance", parent_id=company.id, unit_type="division")
        db.add_all([eng_div, fin_div])
        db.flush()
        eng_dept = OrgUnit(name="Platform Engineering", parent_id=eng_div.id, unit_type="department")
        fin_dept = OrgUnit(name="Finance Operations", parent_id=fin_div.id, unit_type="department")
        db.add_all([eng_dept, fin_dept])
        db.flush()

        sre_canonical = Skill(name="Site Reliability Engineering", category=SkillCategory.technical, canonical_id=None)
        db.add(sre_canonical)
        db.flush()
        sre_alias = Skill(name="SRE", category=SkillCategory.technical, canonical_id=sre_canonical.id)
        db.add(sre_alias)
        db.flush()

        def mkemp(id_, full_name, job_title, email, **overrides) -> Employee:
            fields = dict(
                id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
                job_title=job_title, org_unit_id=eng_dept.id, office_id=office.id, manager_id=None,
                work_email=email, work_phone=None, slack_handle=None, timezone=None,
                employment_type=EmploymentType.fte, hire_date=date(2020, 1, 1), cost_centre=None,
                personal_mobile=None, availability_status=AvailabilityStatus.available,
                away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
            )
            fields.update(overrides)
            emp = Employee(**fields)
            db.add(emp)
            return emp

        # --- RBAC (hire_date / cost_centre) + department shape -----------
        mkemp("stranger-1", "Sam Stranger", "Financial Analyst", "sam@example.test",
              org_unit_id=fin_dept.id, cost_centre="CC-FIN-1")

        # --- ABAC (personal_mobile): mgr-1 is report-1's direct manager --
        mkemp("mgr-1", "Morgan Manager", "Engineering Manager", "morgan@example.test",
              cost_centre="CC-ENG-1")
        report = mkemp("report-1", "Riley Report", "Software Engineer", "riley@example.test",
                       manager_id="mgr-1", personal_mobile="+1-555-0001", cost_centre="CC-ENG-2")
        db.flush()
        db.add(EmployeeSkill(employee_id=report.id, skill_id=sre_canonical.id, level=SkillLevel.expert,
                             source=SkillSource.confirmed, verified_at=datetime.now()))

        # --- record-level restriction -------------------------------------
        mkemp("restricted-1", "Rory Restricted", "Legal Counsel", "rory@example.test",
              availability_status=AvailabilityStatus.restricted)

        # --- ARCHITECTURE_2.md §15 item 6: a restricted employee who is
        # someone else's manager/delegate. Nothing in the fixture set above
        # exercises this -- restricted-1 doesn't manage or delegate for
        # anyone -- which is exactly why the raw db.get() lookups this
        # covers were "currently unexploitable" before being routed through
        # enforce()+compile_query() (app.query_compiler.enforced_person_ref).
        mkemp("managed-by-restricted-1", "Quinn Reports", "Software Engineer", "quinn@example.test",
              manager_id="restricted-1")
        mkemp("delegates-to-restricted-1", "Drew Delegator", "Software Engineer", "drew@example.test",
              manager_id="mgr-1", delegate_id="restricted-1", availability_status=AvailabilityStatus.away)

        # --- confidential project membership: member-1's manager
        # (member-manager-1) is deliberately NOT a project member, to prove
        # the "line manager gets no access unless also a member" rule.
        conf_owner = mkemp("conf-owner-1", "Casey Owner", "Staff Engineer", "casey@example.test")
        mkemp("member-manager-1", "Max Overseer", "Engineering Manager", "max@example.test")
        member = mkemp("member-1", "Mel Member", "Software Engineer", "mel@example.test",
                       manager_id="member-manager-1")
        db.flush()

        project = Project(name="Project Secret", type=ProjectType.project, description="",
                          owning_unit_id=eng_dept.id, owner_id=conf_owner.id,
                          classification=ProjectClassification.confidential)
        db.add(project)
        db.flush()
        db.add(EmployeeProject(employee_id=conf_owner.id, project_id=project.id, role="Owner",
                               start_date=date(2023, 1, 1), end_date=None))
        db.add(EmployeeProject(employee_id=member.id, project_id=project.id, role="Contributor",
                               start_date=date(2023, 6, 1), end_date=None))

        # --- bulk pool for the result-cap test: more than MAX_RESULTS -----
        for i in range(MAX_RESULTS + 10):
            mkemp(f"bulk-{i}", f"Bulk Person {i}", "Software Engineer", f"bulk{i}@example.test")

        # --- org chart: a clean 3-level chain for up/down traversal -------
        mkemp("chain-3", "Casey Top", "VP of Engineering", "chain3@example.test")
        mkemp("chain-2", "Charlie Middle", "Engineering Manager", "chain2@example.test",
              manager_id="chain-3")
        mkemp("chain-1", "Chris Bottom", "Software Engineer", "chain1@example.test",
              manager_id="chain-2")

        # --- org chart: a restricted node partway up a chain, to prove
        # record-level filtering applies to every node, not just the root.
        mkemp("rchain-3", "Robin Top", "VP of Engineering", "rchain3@example.test")
        mkemp("rchain-2", "Rory Middle", "Engineering Manager", "rchain2@example.test",
              manager_id="rchain-3", availability_status=AvailabilityStatus.restricted)
        mkemp("rchain-1", "Ray Bottom", "Software Engineer", "rchain1@example.test",
              manager_id="rchain-2")

        # --- cyclic manager_id data: the cycle guard is what has to stop
        # this from hanging, since there's no legitimate top of this chain.
        mkemp("cyclic-a", "Alex Cyclic", "Software Engineer", "cyclic-a@example.test",
              manager_id="cyclic-b")
        mkemp("cyclic-b", "Bailey Cyclic", "Engineering Manager", "cyclic-b@example.test",
              manager_id="cyclic-a")

        # --- duplicate exact name: resolve_person_name (app/org_chart.py)
        # must treat this as unresolved, not silently pick one -- an org
        # chain walked from the wrong "Dana Ambiguous" answers a different
        # question than the one asked.
        mkemp("dup-name-1", "Dana Ambiguous", "Software Engineer", "danaambiguous1@example.test")
        mkemp("dup-name-2", "Dana Ambiguous", "Product Manager", "danaambiguous2@example.test")

        # --- search fixtures (step 8 hybrid search tests) ------------------
        satellite_office = Office(name="Satellite Office", city="Satellite City", country="Testland", timezone="UTC")
        db.add(satellite_office)
        db.flush()

        power_bi = Skill(name="Power BI", category=SkillCategory.technical, canonical_id=None)
        terraform = Skill(name="Terraform", category=SkillCategory.technical, canonical_id=None)
        french = Skill(name="French", category=SkillCategory.language, canonical_id=None)
        db.add_all([power_bi, terraform, french])
        db.flush()

        # Plain name target for fuzzy-matching tests — no distinguishing skill.
        mkemp("search-sean", "Sean Wilson", "Backend Engineer", "sean.wilson@example.test")

        # Vector-search target: bio deliberately avoids the word "dashboard"
        # so a "good with dashboards"-style query can only match via
        # semantic similarity, never keyword overlap.
        dana = mkemp("search-dana", "Dana Powers", "Data Analyst", "dana.powers@example.test",
                     bio="Dana turns raw numbers into something leadership can act on in a meeting.")
        db.flush()
        db.add(EmployeeSkill(employee_id=dana.id, skill_id=power_bi.id, level=SkillLevel.expert,
                             source=SkillSource.confirmed, verified_at=datetime.now()))

        # Filter-narrowing targets: same skill (Terraform), different org
        # unit / office / level / language, so org_unit, skill+level,
        # location, and language filters each have something real to
        # narrow away rather than just happening to return one result.
        filter_eng = mkemp("search-filter-eng", "Taylor Cloud", "Infrastructure Engineer",
                           "taylor.cloud@example.test", org_unit_id=eng_dept.id, office_id=office.id)
        filter_fin = mkemp("search-filter-fin", "Jordan Ledger", "Infrastructure Contractor",
                           "jordan.ledger@example.test", org_unit_id=fin_dept.id, office_id=satellite_office.id)
        db.flush()
        db.add(EmployeeSkill(employee_id=filter_eng.id, skill_id=terraform.id, level=SkillLevel.expert,
                             source=SkillSource.confirmed, verified_at=datetime.now()))
        db.add(EmployeeSkill(employee_id=filter_fin.id, skill_id=terraform.id, level=SkillLevel.learning,
                             source=SkillSource.self_reported, verified_at=None))
        db.add(EmployeeSkill(employee_id=filter_fin.id, skill_id=french.id, level=SkillLevel.working,
                             source=SkillSource.self_reported, verified_at=None))

        db.commit()
    finally:
        db.close()


@pytest_asyncio.fixture(scope="session")
async def client():
    # httpx.ASGITransport only implements the async request path in this
    # httpx version — a sync Client can't drive it at all — so tests use
    # AsyncClient (see pytest.ini's asyncio_mode = auto for why the test
    # functions can just be `async def` with no per-test decorator).
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
