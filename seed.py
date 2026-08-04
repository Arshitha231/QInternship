"""Synthetic seed data for the employee directory (~500 records, engineered not random).

No real Quadrant employee data is used anywhere in this file — every name, office,
project, and skill below is fabricated. Run with `python seed.py`; safe to re-run,
it clears its own tables first.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import (
    Employee,
    EmployeeCertification,
    EmployeeProject,
    EmployeeSkill,
    Office,
    OrgUnit,
    Project,
    Skill,
)
from app.models.enums import (
    AvailabilityStatus,
    EmploymentType,
    ProjectClassification,
    ProjectType,
    SkillCategory,
    SkillLevel,
    SkillSource,
)

RNG_SEED = 42
rng = random.Random(RNG_SEED)

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

OFFICES = [
    # name, city, country, timezone, relative weight
    ("Seattle HQ", "Seattle", "United States", "America/Los_Angeles", 3.0),
    ("Austin Office", "Austin", "United States", "America/Chicago", 1.5),
    ("New York Office", "New York", "United States", "America/New_York", 1.5),
    ("London Office", "London", "United Kingdom", "Europe/London", 1.5),
    ("Bangalore Office", "Bangalore", "India", "Asia/Kolkata", 2.5),
    ("Singapore Office", "Singapore", "Singapore", "Asia/Singapore", 1.0),
    ("Sydney Office", "Sydney", "Australia", "Australia/Sydney", 1.0),
]

LANGUAGE_SKILLS = [
    "English", "Spanish", "French", "German", "Mandarin",
    "Hindi", "Kannada", "Tamil", "Japanese", "Portuguese",
]

# Every name below is a canonical skill row. Synonyms/abbreviations live only
# in SKILL_CANONICAL_MAP and are never assigned to anyone directly — they
# exist purely so a search for the alias resolves to the canonical skill via
# canonical_id (build_skills() creates them as separate rows pointing back at
# their canonical row; no pool below references an alias name).
TECHNICAL_SKILLS = [
    "Python", "JavaScript", "TypeScript", "Java", "Go", "Rust", "C++", "SQL",
    "React", "Node.js", "AWS", "Azure", "GCP", "Kubernetes", "Docker",
    "Terraform", "CI/CD", "Site Reliability Engineering",
    "Swift", "Kotlin", "React Native",
    "Machine Learning", "Deep Learning", "NLP", "Computer Vision",
    "Data Engineering", "ETL Pipelines", "Power BI", "Tableau",
    "Advanced Excel", "Salesforce", "HubSpot", "SEO", "Content Marketing",
    "Product Analytics", "Figma", "UX Research", "Agile/Scrum",
    "Project Management", "Financial Modeling", "GAAP Accounting",
    "Payroll Systems", "Applicant Tracking Systems", "Employment Law",
    "Contract Negotiation", "Cybersecurity", "Penetration Testing",
    "Network Administration",
]
SKILL_CANONICAL_MAP = {
    "SRE": "Site Reliability Engineering",
    "K8s": "Kubernetes",
}

DOMAIN_SKILLS = [
    "SaaS Metrics", "B2B Enterprise Sales", "GDPR", "SOC 2 Compliance",
    "FinTech Regulations", "Healthcare Compliance", "Enterprise Procurement",
    "Change Management",
]

# Per-department technical/domain skill pools — secondary/cross-team skills.
# Team-level PRIMARY skills (below, TEAM_PRIMARY_SKILLS) take priority over
# these for the four named engineering teams; everyone else draws from here.
DEPT_SKILL_POOL = {
    "Platform Engineering": ["Python", "JavaScript", "TypeScript", "Go", "React", "Node.js",
                              "AWS", "Docker", "Kubernetes", "CI/CD", "SQL",
                              "Swift", "Kotlin", "React Native"],
    "Data & AI": ["Python", "SQL", "Machine Learning", "Deep Learning", "NLP",
                  "Computer Vision", "Data Engineering", "ETL Pipelines", "Power BI", "Tableau"],
    "Infrastructure": ["AWS", "Azure", "GCP", "Kubernetes", "Docker", "Terraform",
                        "Site Reliability Engineering", "Network Administration", "Cybersecurity"],
    "Quality Engineering": ["Python", "Java", "SQL", "CI/CD", "Agile/Scrum", "Cybersecurity"],
    "Product Management": ["Product Analytics", "Agile/Scrum", "Project Management", "SQL", "Figma"],
    "Design": ["Figma", "UX Research", "Product Analytics", "Agile/Scrum"],
    "Sales": ["Salesforce", "Contract Negotiation", "B2B Enterprise Sales", "Advanced Excel"],
    "Marketing": ["HubSpot", "SEO", "Content Marketing", "Product Analytics", "SaaS Metrics"],
    "Finance Operations": ["Advanced Excel", "Financial Modeling", "GAAP Accounting", "FinTech Regulations"],
    "Payroll": ["Payroll Systems", "Advanced Excel", "GAAP Accounting", "Employment Law"],
    "HR Operations": ["Employment Law", "Change Management", "Advanced Excel"],
    "Talent Acquisition": ["Applicant Tracking Systems", "Employment Law", "Change Management"],
    "Compliance": ["Employment Law", "GDPR", "SOC 2 Compliance", "Contract Negotiation"],
}

# Team-theme -> the skills that should clearly dominate that team's holders.
# Keyed by theme (e.g. "Backend"), not by the literal org_unit name, since an
# oversized team is split into several sibling org_units ("Backend Team",
# "Backend Team B", ...) that all share the same theme. Uses canonical skill
# names only (e.g. "Site Reliability Engineering", not the "SRE" alias).
TEAM_PRIMARY_SKILLS = {
    "Backend": ["Python", "Go", "Node.js", "SQL"],
    "Frontend": ["React", "TypeScript", "JavaScript", "Figma"],
    "Mobile": ["Swift", "Kotlin", "React Native"],
    "Cloud Operations": ["Terraform", "Azure", "AWS", "Kubernetes", "Site Reliability Engineering"],
}

# ---------------------------------------------------------------------------
# Names — large diverse pool, plus a forced-injection queue that guarantees
# duplicate / near-duplicate / plausibly-misspellable names regardless of
# random draws.
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Christopher", "Karen", "Daniel", "Nancy", "Matthew", "Lisa",
    "Anthony", "Betty", "Mark", "Margaret", "Steven", "Sandra", "Andrew", "Ashley",
    "Paul", "Emily", "Joshua", "Donna", "Kenneth", "Kimberly", "Kevin", "Michelle",
    "Priya", "Raj", "Amit", "Sunita", "Deepa", "Arjun", "Kavya", "Rohan", "Ananya", "Vikram",
    "Aditi", "Nikhil", "Meera", "Sanjay", "Divya", "Karthik", "Lakshmi", "Suresh", "Anjali", "Rahul",
    "Wei", "Ming", "Li", "Xiu", "Jing", "Hao", "Yan", "Feng", "Chen", "Lei",
    "Hiroshi", "Yuki", "Kenji", "Sakura", "Takeshi", "Naomi", "Ren", "Aiko",
    "Siobhan", "Aoife", "Niamh", "Cian", "Fionn", "Saoirse", "Padraig", "Orla",
    "Sean", "Shaun", "Kristen", "Kristin", "Catherine", "Katherine",
    "Xiomara", "Renata", "Mateo", "Valentina", "Diego", "Camila", "Santiago", "Isabela",
    "Zhiyuan", "Przemyslaw", "Aleksandra", "Krzysztof", "Kasia", "Wojciech",
    "Kshitij", "Ishaan", "Tanvi", "Advait", "Riya", "Vivaan",
    "Fatima", "Omar", "Layla", "Yusuf", "Amara", "Zain",
    "Ingrid", "Lars", "Freya", "Bjorn", "Astrid", "Magnus",
    "Chidi", "Ngozi", "Kwame", "Amara", "Tunde", "Folake",
    "Giulia", "Marco", "Francesca", "Luca", "Alessandra", "Matteo",
    "Sophie", "Lucas", "Charlotte", "Hugo", "Camille", "Antoine",
    "Ji-woo", "Min-jun", "Seo-yeon", "Do-yoon", "Ha-eun", "Joon-ho",
    "Grace", "Ethan", "Olivia", "Noah", "Ava", "Liam", "Emma", "Mason",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Sharma", "Gupta", "Patel", "Reddy", "Iyer", "Rao", "Nair", "Menon", "Krishnan", "Radhakrishnan",
    "Chen", "Wang", "Zhang", "Liu", "Yang", "Huang", "Zhao", "Tanaka", "Suzuki", "Sato",
    "Byrne", "Kelly", "Ryan", "Walsh", "Kavanagh", "Murphy", "OBrien", "Nguyen",
    "Delacroix", "Kowalczyk", "Dubois", "Moreau", "Rossi", "Bianchi", "Ferrari",
    "Kim", "Park", "Choi", "Jung", "Kang",
    "Okafor", "Adeyemi", "Mensah", "Diallo",
    "Larsen", "Andersen", "Berg", "Lindqvist",
    "Novak", "Dvorak", "Horvat",
    "Al-Sayed", "Hassan", "Khan", "Ahmed",
]

FORCED_NAME_QUEUE = [
    ("Priya", "Sharma"),      # exact duplicate #1
    ("Priya", "Sharma"),      # exact duplicate #2
    ("Sean", "Ryan"),         # near-duplicate pair (misspelling)
    ("Shaun", "Ryan"),
    ("Kristen", "Walsh"),     # near-duplicate pair (misspelling)
    ("Kristin", "Walsh"),
    ("Catherine", "Byrne"),   # near-duplicate pair (misspelling)
    ("Katherine", "Byrne"),
    ("Siobhan", "Nguyen"),    # plausibly misspellable
    ("Xiomara", "Delacroix"), # plausibly misspellable
    ("Przemyslaw", "Kowalczyk"),  # plausibly misspellable
    ("Aoife", "Kavanagh"),    # plausibly misspellable
    ("Zhiyuan", "Tanaka"),    # plausibly misspellable
    ("Kshitij", "Radhakrishnan"),  # plausibly misspellable
]

BIO_TEMPLATES = [
    "{name} works on {team} within {dept}, focused on {topic}.",
    "{name} is part of the {team} team, primarily supporting {topic}.",
    "Based in {office}, {name} contributes to {dept} initiatives around {topic}.",
    "{name} has been with {dept} since {year}, specializing in {topic}.",
    "{name} partners closely with {team} on {topic}.",
]
BIO_TOPICS = [
    "platform reliability", "customer onboarding", "data quality", "go-to-market execution",
    "internal tooling", "vendor relationships", "process automation", "quarterly planning",
    "cross-team enablement", "compliance readiness", "roadmap delivery", "stakeholder communication",
]

used_emails: set[str] = set()
used_slack: set[str] = set()


def slugify(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "" for c in s)


def make_email(first: str, last: str) -> str:
    base = f"{slugify(first)}.{slugify(last)}"
    candidate = f"{base}@example.com"
    n = 2
    while candidate in used_emails:
        candidate = f"{base}{n}@example.com"
        n += 1
    used_emails.add(candidate)
    return candidate


def make_slack(first: str, last: str) -> str:
    base = f"@{slugify(first)}.{slugify(last)}"
    candidate = base
    n = 2
    while candidate in used_slack:
        candidate = f"{base}{n}"
        n += 1
    used_slack.add(candidate)
    return candidate


def next_name() -> tuple[str, str]:
    if FORCED_NAME_QUEUE:
        return FORCED_NAME_QUEUE.pop(0)
    return rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)


# ---------------------------------------------------------------------------
# Org tree definition. Divisions (VP each) contain departments (Director
# each); a department's headcount is expressed as one or more named THEMES
# (e.g. Platform Engineering = Backend + Frontend + Mobile). Each theme is
# split into as many same-sized sibling team org_units as needed to keep
# every leaf team in the realistic 6-15 range — real teams don't run 80
# people deep under one manager. All teams from the same theme still share
# one manager-title scheme and, for the four call-out engineering themes,
# one skill-correlation profile (TEAM_PRIMARY_SKILLS).
# ---------------------------------------------------------------------------

DIVISION_DEPTS = {
    "Engineering": ["Platform Engineering", "Data & AI", "Infrastructure", "Quality Engineering"],
    "Product": ["Product Management", "Design"],
    "Sales & Marketing": ["Sales", "Marketing"],
    "Finance": ["Finance Operations", "Payroll"],
    "People & Culture": ["HR Operations", "Talent Acquisition"],
    "Legal": ["Compliance"],
}

# department -> {theme: headcount}. Headcount includes that theme's own
# team lead(s) — see split_target(). "Backend" absorbs the balancing
# shortfall below and hosts the deliberately deep reporting chain, so its
# first split team stays a natural home for it regardless of size drift.
DEPT_THEMES = {
    "Platform Engineering": {"Backend": 50, "Frontend": 45, "Mobile": 36},
    "Data & AI": {"Machine Learning": 28, "Data Platform": 22, "Analytics": 15},
    "Infrastructure": {"Cloud Operations": 27, "Networking": 18},
    "Quality Engineering": {"QA Automation": 36},
    "Product Management": {"Product Management": 27},
    "Design": {"Design": 25},
    "Sales": {"Enterprise Sales": 24, "SMB Sales": 21},
    "Marketing": {"Growth Marketing": 14, "Brand": 13},
    "Finance Operations": {"Finance Operations": 21},
    "Payroll": {"Payroll": 13},
    "HR Operations": {"HR Operations": 17},
    "Talent Acquisition": {"Talent Acquisition": 15},
    "Compliance": {"Compliance": 13},
}

IC_TITLES_BY_DEPT = {
    "Platform Engineering": ["Software Engineer", "Senior Software Engineer", "Staff Software Engineer"],
    "Data & AI": ["Data Scientist", "Senior Data Scientist", "Machine Learning Engineer", "Data Engineer"],
    "Infrastructure": ["Infrastructure Engineer", "Senior Infrastructure Engineer", "Site Reliability Engineer"],
    "Quality Engineering": ["QA Engineer", "Senior QA Engineer", "QA Automation Engineer"],
    "Product Management": ["Product Manager", "Senior Product Manager", "Associate Product Manager"],
    "Design": ["Product Designer", "Senior Product Designer", "UX Researcher"],
    "Sales": ["Account Executive", "Senior Account Executive", "Sales Development Representative"],
    "Marketing": ["Marketing Manager", "Content Marketer", "Growth Marketer"],
    "Finance Operations": ["Financial Analyst", "Senior Financial Analyst", "Accountant"],
    "Payroll": ["Payroll Specialist", "Senior Payroll Specialist"],
    "HR Operations": ["HR Generalist", "Senior HR Generalist", "HR Coordinator"],
    "Talent Acquisition": ["Recruiter", "Senior Recruiter", "Recruiting Coordinator"],
    "Compliance": ["Compliance Analyst", "Senior Compliance Analyst"],
}


def split_target(total: int, max_size: int = 15, target_avg: int = 11) -> list[int]:
    """Split `total` people into leaf-team-sized chunks, each <= max_size."""
    if total <= max_size:
        return [total]
    k = -(-total // target_avg)  # ceil
    base, extra = divmod(total, k)
    return [base + 1] * extra + [base] * (k - extra)


def theme_team_names(theme: str, sizes: list[int]) -> list[str]:
    if len(sizes) == 1:
        return [f"{theme} Team"]
    letters = "ABCDEFGH"
    return [f"{theme} Team"] + [f"{theme} Team {letters[i]}" for i in range(len(sizes) - 1)]


def _grand_total() -> int:
    n_divisions = len(DIVISION_DEPTS)
    n_departments = sum(len(depts) for depts in DIVISION_DEPTS.values())
    ic_total = sum(size for themes in DEPT_THEMES.values() for size in themes.values())
    return 1 + n_divisions + n_departments + ic_total  # CEO + VPs + Directors + ICs


_shortfall = 500 - _grand_total()
DEPT_THEMES["Platform Engineering"]["Backend"] += _shortfall


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def reset_tables(session) -> None:
    for model in (EmployeeCertification, EmployeeProject, EmployeeSkill, Project,
                  Employee, OrgUnit, Office, Skill):
        session.execute(delete(model))
    session.commit()


def build_offices(session) -> list[Office]:
    offices = []
    for name, city, country, tz, _weight in OFFICES:
        office = Office(name=name, city=city, country=country, timezone=tz)
        session.add(office)
        offices.append(office)
    session.flush()
    return offices


def weighted_office_choice(offices: list[Office]) -> Office:
    weights = [w for *_x, w in OFFICES]
    return rng.choices(offices, weights=weights, k=1)[0]


def build_org_units(session):
    """Returns (units, theme_units): units maps every org_unit name (company /
    division / department / team) to its row; theme_units maps (department,
    theme) -> [(team_unit, team_size), ...] for however many sibling teams
    that theme got split into.
    """
    units: dict[str, OrgUnit] = {}
    theme_units: dict[tuple[str, str], list[tuple[OrgUnit, int]]] = {}

    root = OrgUnit(name="Quadrant Technologies", parent_id=None, unit_type="company")
    session.add(root)
    session.flush()
    units["Quadrant Technologies"] = root

    for division, depts in DIVISION_DEPTS.items():
        div_unit = OrgUnit(name=division, parent_id=root.id, unit_type="division")
        session.add(div_unit)
        session.flush()
        units[division] = div_unit

        for dept in depts:
            dept_unit = OrgUnit(name=dept, parent_id=div_unit.id, unit_type="department")
            session.add(dept_unit)
            session.flush()
            units[dept] = dept_unit

            for theme, size in DEPT_THEMES[dept].items():
                sizes = split_target(size)
                names = theme_team_names(theme, sizes)
                team_list = []
                for name, team_size in zip(names, sizes):
                    team_unit = OrgUnit(name=name, parent_id=dept_unit.id, unit_type="team")
                    session.add(team_unit)
                    session.flush()
                    units[name] = team_unit
                    team_list.append((team_unit, team_size))
                theme_units[(dept, theme)] = team_list
    return units, theme_units


def build_skills(session) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}

    for name in TECHNICAL_SKILLS:
        if name in SKILL_CANONICAL_MAP:
            continue  # created after its canonical target exists
        skill = Skill(name=name, category=SkillCategory.technical, canonical_id=None)
        session.add(skill)
        session.flush()
        skills[name] = skill

    for synonym, canonical_name in SKILL_CANONICAL_MAP.items():
        skill = Skill(name=synonym, category=SkillCategory.technical,
                       canonical_id=skills[canonical_name].id)
        session.add(skill)
        session.flush()
        skills[synonym] = skill

    for name in LANGUAGE_SKILLS:
        skill = Skill(name=name, category=SkillCategory.language, canonical_id=None)
        session.add(skill)
        session.flush()
        skills[name] = skill

    for name in DOMAIN_SKILLS:
        skill = Skill(name=name, category=SkillCategory.domain, canonical_id=None)
        session.add(skill)
        session.flush()
        skills[name] = skill

    return skills


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

NICKNAMES = {
    "Robert": "Rob", "Elizabeth": "Liz", "William": "Will", "Richard": "Rich",
    "Joseph": "Joe", "Christopher": "Chris", "Matthew": "Matt", "Kenneth": "Ken",
    "Anthony": "Tony", "Jennifer": "Jen", "Patricia": "Pat", "Michael": "Mike",
    "Daniel": "Dan", "David": "Dave", "Thomas": "Tom", "Andrew": "Andy",
    "Steven": "Steve", "Kimberly": "Kim", "Katherine": "Kate", "Catherine": "Cat",
}
COUNTRY_CALLING_CODE = {
    "United States": "+1", "United Kingdom": "+44", "India": "+91",
    "Singapore": "+65", "Australia": "+61",
}
LEVEL_HIRE_YEAR_RANGE = {
    0: (6, 9), 1: (4, 8), 2: (3, 6), 3: (2, 5), 4: (1, 4), 5: (1, 3), 6: (0.5, 3), 7: (0, 2),
}

# Populated as employees are created: employee.id -> department name used for
# skill-pool assignment (None for CEO/company-wide roles), -> org level
# (0=CEO .. 7=IC) used for incompleteness injection, and -> team theme (e.g.
# "Backend") for the four engineering teams with a primary-skill profile.
EMPLOYEE_SKILL_DEPT: dict[str, str | None] = {}
EMPLOYEE_LEVEL: dict[str, int] = {}
EMPLOYEE_TEAM_THEME: dict[str, str | None] = {}
ALL_EMPLOYEES: list[Employee] = []


def make_employee(session, first, last, title, org_unit, manager, level, offices,
                   skill_dept: str | None, employment_type: EmploymentType | None = None,
                   forced_office: Office | None = None, team_theme: str | None = None) -> Employee:
    emp_id = str(uuid.uuid4())

    if employment_type is None:
        if level == 7:
            roll = rng.random()
            employment_type = (EmploymentType.intern if roll < 0.06
                                else EmploymentType.contractor if roll < 0.14
                                else EmploymentType.fte)
        elif level in (5, 6):
            employment_type = EmploymentType.contractor if rng.random() < 0.08 else EmploymentType.fte
        else:
            employment_type = EmploymentType.fte

    if forced_office is not None:
        office, timezone = forced_office, None
    else:
        remote_chance = 0.0 if level <= 2 else (0.05 if level <= 4 else 0.12)
        if rng.random() < remote_chance:
            office = None
            timezone = rng.choice([o.timezone for o in offices])
        else:
            office = weighted_office_choice(offices)
            timezone = None
            if rng.random() < 0.08:
                candidate_tz = rng.choice([o.timezone for o in offices])
                if candidate_tz != office.timezone:
                    timezone = candidate_tz

    lo, hi = LEVEL_HIRE_YEAR_RANGE.get(level, (0, 2))
    if employment_type == EmploymentType.intern:
        lo, hi = 0, 0.5
    hire_date = date.today() - timedelta(days=rng.uniform(lo, hi) * 365)

    country = office.country if office else "United States"
    calling_code = COUNTRY_CALLING_CODE.get(country, "+1")
    work_phone = f"{calling_code}-555-{rng.randint(1000, 9999)}" if rng.random() < 0.6 else None
    personal_mobile = f"{calling_code}-555-{rng.randint(1000, 9999)}" if rng.random() < 0.5 else None
    slack_handle = make_slack(first, last) if rng.random() < 0.85 else None
    preferred_name = NICKNAMES.get(first) if rng.random() < 0.5 else None

    bio = None
    if rng.random() < 0.7:
        template = rng.choice(BIO_TEMPLATES)
        bio = template.format(
            name=preferred_name or first, team=org_unit.name, dept=title,
            topic=rng.choice(BIO_TOPICS), office=office.name if office else "a remote location",
            year=hire_date.year,
        )

    photo_url = f"https://avatars.example.com/{emp_id}.jpg" if rng.random() < 0.5 else None

    cost_centre = None
    if employment_type == EmploymentType.fte and rng.random() < 0.75:
        cost_centre = f"CC-{slugify(org_unit.name)[:6].upper()}-{rng.randint(100, 999)}"

    directory_object_id = str(uuid.uuid4()) if rng.random() < 0.95 else None

    emp = Employee(
        id=emp_id,
        directory_object_id=directory_object_id,
        full_name=f"{first} {last}",
        preferred_name=preferred_name,
        job_title=title,
        org_unit_id=org_unit.id,
        office_id=office.id if office else None,
        manager_id=manager.id if manager else None,
        work_email=make_email(first, last),
        work_phone=work_phone,
        slack_handle=slack_handle,
        timezone=timezone,
        employment_type=employment_type,
        hire_date=hire_date,
        cost_centre=cost_centre,
        personal_mobile=personal_mobile,
        availability_status=AvailabilityStatus.available,
        away_until=None,
        delegate_id=None,
        bio=bio,
        photo_url=photo_url,
        is_active=True,
    )
    session.add(emp)
    ALL_EMPLOYEES.append(emp)
    EMPLOYEE_SKILL_DEPT[emp.id] = skill_dept
    EMPLOYEE_LEVEL[emp.id] = level
    EMPLOYEE_TEAM_THEME[emp.id] = team_theme
    return emp


def distribute_ics(session, offices, dept_name, unit, count, manager, ic_titles, level,
                    span=14, team_theme=None):
    if count <= 0:
        return
    if count <= span or level >= 7:
        for _ in range(count):
            first, last = next_name()
            title = rng.choice(ic_titles)
            seniority = 7 if level >= 7 else rng.choices([6, 7], weights=[1, 2])[0]
            make_employee(session, first, last, title, unit, manager, seniority, offices, dept_name,
                          team_theme=team_theme)
        return

    num_leads = max(2, -(-count // (span + 1)))
    ic_total = count - num_leads
    base, extra = divmod(ic_total, num_leads)
    for i in range(num_leads):
        first, last = next_name()
        lead_title = f"{unit.name} Team Lead" if unit.unit_type == "team" else f"{dept_name} Team Lead"
        lead = make_employee(session, first, last, lead_title, unit, manager, level, offices, dept_name,
                             team_theme=team_theme)
        my_ics = base + (1 if i < extra else 0)
        distribute_ics(session, offices, dept_name, unit, my_ics, lead, ic_titles, level + 1, span, team_theme)


def populate_unit(session, offices, dept_name, unit, size, top_manager, manager_level, manager_title_fn,
                  team_theme=None):
    if size <= 0:
        return top_manager
    first, last = next_name()
    manager_local = make_employee(session, first, last, manager_title_fn(unit), unit,
                                   top_manager, manager_level, offices, dept_name, team_theme=team_theme)
    ic_titles = IC_TITLES_BY_DEPT.get(dept_name, ["Specialist", "Senior Specialist"])
    distribute_ics(session, offices, dept_name, unit, size - 1, manager_local, ic_titles, manager_level + 1,
                   team_theme=team_theme)
    return manager_local


def build_deep_chain(session, offices, platform_eng_director, backend_unit):
    """CEO -> VP -> Director -> Sr Mgr -> Mgr -> Tech Lead -> Sr Engineer -> Engineer.

    Deliberately hard-coded (not left to the generic span-of-control splitter)
    so the >=6-levels-deep reporting-chain constraint is guaranteed, not just
    statistically likely. Lives in the first "Backend Team" sub-team.
    """
    dept_name = "Platform Engineering"
    chain_titles = [
        ("Senior Engineering Manager, Backend", 3),
        ("Engineering Manager, Backend", 4),
        ("Tech Lead, Backend", 5),
        ("Senior Software Engineer, Backend", 6),
        ("Software Engineer, Backend", 7),
    ]
    manager = platform_eng_director
    chain = [platform_eng_director]
    for title, level in chain_titles:
        # Draw straight from the pools, NOT next_name() — the forced-injection
        # queue is reserved for the generic IC pool, not this hard-coded chain.
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        person = make_employee(session, first, last, title, backend_unit, manager, level, offices, dept_name,
                               team_theme="Backend")
        chain.append(person)
        manager = person
    return chain  # chain[-2] is the Engineering Manager (level 4), used as top_manager for the rest of Backend Team


def build_employees(session, offices, units, theme_units):
    seattle = next(o for o in offices if o.name == "Seattle HQ")
    bangalore = next(o for o in offices if o.name == "Bangalore Office")

    ceo_first, ceo_last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    ceo = make_employee(session, ceo_first, ceo_last, "Chief Executive Officer",
                         units["Quadrant Technologies"], None, 0, offices, None, forced_office=seattle)

    dept_directors: dict[str, Employee] = {}
    division_vps: dict[str, Employee] = {}
    deep_chain: list[Employee] = []

    for division, depts in DIVISION_DEPTS.items():
        vp_first, vp_last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        forced = seattle if division == "Engineering" else None
        vp = make_employee(session, vp_first, vp_last, f"VP of {division}",
                            units[division], ceo, 1, offices, None, forced_office=forced)
        division_vps[division] = vp

        for dept in depts:
            dir_first, dir_last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            forced_dir_office = seattle if dept == "Platform Engineering" else None
            director = make_employee(session, dir_first, dir_last, f"Director of {dept}",
                                      units[dept], vp, 2, offices, dept, forced_office=forced_dir_office)
            dept_directors[dept] = director

            for theme in DEPT_THEMES[dept]:
                team_theme = theme if theme in TEAM_PRIMARY_SKILLS else None
                team_list = theme_units[(dept, theme)]

                if dept == "Platform Engineering" and theme == "Backend":
                    first_unit, first_size = team_list[0]
                    deep_chain = build_deep_chain(session, offices, director, first_unit)
                    chain_manager = deep_chain[2]  # "Engineering Manager, Backend" (level 4)
                    remaining = first_size - 5  # 5 = SrMgr, Mgr, TechLead, SrEngineer, Engineer
                    distribute_ics(session, offices, dept, first_unit, remaining, chain_manager,
                                   IC_TITLES_BY_DEPT[dept], level=5, team_theme="Backend")
                    rest = team_list[1:]
                else:
                    rest = team_list

                for team_unit, team_size in rest:
                    populate_unit(session, offices, dept, team_unit, team_size, director,
                                  manager_level=4, manager_title_fn=lambda u: f"{u.name} Manager",
                                  team_theme=team_theme)

    return {
        "ceo": ceo, "dept_directors": dept_directors, "division_vps": division_vps,
        "deep_chain": deep_chain, "seattle": seattle, "bangalore": bangalore,
    }


def inject_incompleteness_and_specials(ctx):
    """Post-pass: no-manager gaps, away+delegate, restricted records.

    Runs after every employee exists so it can freely cross-reference
    coworkers (e.g. picking a delegate on the same team), and deliberately
    avoids the hard-coded deep chain so it never breaks the depth guarantee.
    """
    chain_ids = {e.id for e in ctx["deep_chain"]}
    ic_pool = [e for e in ALL_EMPLOYEES
               if e.id not in chain_ids and EMPLOYEE_LEVEL[e.id] >= 5 and e.manager_id is not None]

    no_manager_picks = rng.sample(ic_pool, 9)
    for e in no_manager_picks:
        e.manager_id = None

    away_candidates = [e for e in ic_pool if e not in no_manager_picks]
    away_picks = rng.sample(away_candidates, 3)
    for e in away_picks:
        e.availability_status = AvailabilityStatus.away
        e.away_until = date.today() + timedelta(days=rng.randint(20, 90))
        same_unit = [c for c in ALL_EMPLOYEES
                     if c.org_unit_id == e.org_unit_id and c.id != e.id and c.id not in chain_ids]
        delegate_pool = same_unit or [c for c in ALL_EMPLOYEES if c.id != e.id]
        e.delegate_id = rng.choice(delegate_pool).id

    restricted_candidates = [e for e in ic_pool if e not in no_manager_picks and e not in away_picks]
    restricted_picks = rng.sample(restricted_candidates, 2)
    for e in restricted_picks:
        e.availability_status = AvailabilityStatus.restricted

    return {"no_manager": no_manager_picks, "away": away_picks, "restricted": restricted_picks}


LEADERSHIP_SKILL_POOL = ["Project Management", "Agile/Scrum", "Financial Modeling", "Change Management"]
CERT_POOL = [
    ("AWS Certified Solutions Architect", "Amazon Web Services", "AWS"),
    ("Certified Kubernetes Administrator", "Cloud Native Computing Foundation", "Kubernetes"),
    ("PMP", "Project Management Institute", "Project Management"),
    ("Certified ScrumMaster", "Scrum Alliance", "Agile/Scrum"),
    ("Salesforce Certified Administrator", "Salesforce", "Salesforce"),
    ("SHRM-CP", "Society for Human Resource Management", "Employment Law"),
    ("CPA", "AICPA", "GAAP Accounting"),
    ("Certified Information Systems Security Professional", "ISC2", "Cybersecurity"),
    ("Google Data Analytics Certificate", "Google", "Data Engineering"),
    ("HubSpot Content Marketing Certification", "HubSpot", "Content Marketing"),
]


def assign_skills(session, skills, ctx):
    pending: list[dict] = []

    for emp in ALL_EMPLOYEES:
        dept = EMPLOYEE_SKILL_DEPT[emp.id]
        theme = EMPLOYEE_TEAM_THEME[emp.id]
        primary = TEAM_PRIMARY_SKILLS.get(theme)

        if primary:
            # Team-primary skills dominate: most of this person's technical
            # skills come from their own team's core set, at levels that read
            # as real fluency (Working/Expert, not Learning).
            n_primary = rng.randint(max(2, len(primary) - 1), len(primary))
            for name in rng.sample(primary, n_primary):
                level = rng.choices([SkillLevel.working, SkillLevel.expert], weights=[55, 45])[0]
                source = rng.choices(
                    [SkillSource.self_reported, SkillSource.inferred, SkillSource.confirmed, SkillSource.certified],
                    weights=[35, 20, 30, 15])[0]
                verified_at = (datetime.now() - timedelta(days=rng.randint(10, 700))
                               if source in (SkillSource.confirmed, SkillSource.certified) else None)
                pending.append({"employee": emp, "skill_name": name, "level": level,
                                "source": source, "verified_at": verified_at})

            # Occasional secondary, cross-team skill — never at Expert, so it
            # can't be mistaken for another primary specialty.
            if rng.random() < 0.3:
                secondary_pool = [s for s in DEPT_SKILL_POOL.get(dept, []) if s not in primary]
                if secondary_pool:
                    name = rng.choice(secondary_pool)
                    level = rng.choices([SkillLevel.learning, SkillLevel.working], weights=[40, 60])[0]
                    pending.append({"employee": emp, "skill_name": name, "level": level,
                                    "source": SkillSource.self_reported, "verified_at": None})
        else:
            pool = DEPT_SKILL_POOL.get(dept, LEADERSHIP_SKILL_POOL)
            n = rng.randint(2, min(4, len(pool)))
            for name in rng.sample(pool, n):
                level = rng.choices(
                    [SkillLevel.learning, SkillLevel.working, SkillLevel.expert], weights=[20, 55, 25])[0]
                source = rng.choices(
                    [SkillSource.self_reported, SkillSource.inferred, SkillSource.confirmed, SkillSource.certified],
                    weights=[40, 30, 20, 10])[0]
                verified_at = (datetime.now() - timedelta(days=rng.randint(10, 700))
                               if source in (SkillSource.confirmed, SkillSource.certified) else None)
                pending.append({"employee": emp, "skill_name": name, "level": level,
                                "source": source, "verified_at": verified_at})

        langs = []
        if rng.random() < 0.92:
            langs.append("English")
        if rng.random() < 0.35:
            other = rng.choice([l for l in LANGUAGE_SKILLS if l != "English"])
            if other not in langs:
                langs.append(other)
        for name in langs:
            level = rng.choices([SkillLevel.working, SkillLevel.expert], weights=[60, 40])[0]
            source = SkillSource.self_reported
            pending.append({"employee": emp, "skill_name": name, "level": level,
                            "source": source, "verified_at": None})

        if rng.random() < 0.22:
            name = rng.choice(DOMAIN_SKILLS)
            level = rng.choices([SkillLevel.working, SkillLevel.expert], weights=[70, 30])[0]
            source = rng.choice([SkillSource.self_reported, SkillSource.confirmed])
            verified_at = datetime.now() - timedelta(days=rng.randint(10, 700)) if source == SkillSource.confirmed else None
            pending.append({"employee": emp, "skill_name": name, "level": level,
                            "source": source, "verified_at": verified_at})

    # --- Engineered overlap: "who knows Power BI in Bangalore" -> 3-5 people.
    bangalore_id = ctx["bangalore"].id

    def has_skill(emp, name):
        return any(p["employee"] is emp and p["skill_name"] == name for p in pending)

    pbi_bangalore = [p for p in pending if p["skill_name"] == "Power BI" and p["employee"].office_id == bangalore_id]
    while len(pbi_bangalore) > 4:
        victim = pbi_bangalore.pop()
        pending.remove(victim)
    if len(pbi_bangalore) < 4:
        candidates = [e for e in ALL_EMPLOYEES
                      if e.office_id == bangalore_id and not has_skill(e, "Power BI")]
        rng.shuffle(candidates)
        for e in candidates:
            if len(pbi_bangalore) >= 4:
                break
            entry = {"employee": e, "skill_name": "Power BI", "level": SkillLevel.working,
                     "source": SkillSource.confirmed, "verified_at": datetime.now() - timedelta(days=90)}
            pending.append(entry)
            pbi_bangalore.append(entry)

    # --- Engineered mentor pool: Terraform experts for find_mentor().
    # Cloud Operations is Terraform's home team (TEAM_PRIMARY_SKILLS), so
    # prefer boosting there; fall back to the wider Infrastructure department
    # only if that team somehow doesn't cover it.
    terraform_experts = [p for p in pending if p["skill_name"] == "Terraform" and p["level"] == SkillLevel.expert]
    cloud_ops_pool = [e for e in ALL_EMPLOYEES if EMPLOYEE_TEAM_THEME[e.id] == "Cloud Operations"]
    infra_pool = cloud_ops_pool + [e for e in ALL_EMPLOYEES
                                    if EMPLOYEE_SKILL_DEPT[e.id] == "Infrastructure" and e not in cloud_ops_pool]
    rng.shuffle(infra_pool)
    for e in infra_pool:
        if len(terraform_experts) >= 3:
            break
        existing = next((p for p in pending if p["employee"] is e and p["skill_name"] == "Terraform"), None)
        if existing:
            existing["level"] = SkillLevel.expert
            existing["source"] = SkillSource.certified
            existing["verified_at"] = datetime.now() - timedelta(days=200)
            terraform_experts.append(existing)
        elif not has_skill(e, "Terraform"):
            entry = {"employee": e, "skill_name": "Terraform", "level": SkillLevel.expert,
                     "source": SkillSource.certified, "verified_at": datetime.now() - timedelta(days=200)}
            pending.append(entry)
            terraform_experts.append(entry)
    if not any(p["skill_name"] == "Terraform" and p["level"] == SkillLevel.learning for p in pending):
        seeker = next(e for e in ALL_EMPLOYEES if EMPLOYEE_SKILL_DEPT[e.id] == "Platform Engineering"
                      and not has_skill(e, "Terraform"))
        pending.append({"employee": seeker, "skill_name": "Terraform", "level": SkillLevel.learning,
                        "source": SkillSource.self_reported, "verified_at": None})

    seen: set[tuple[str, int]] = set()
    for p in pending:
        key = (p["employee"].id, skills[p["skill_name"]].id)
        if key in seen:
            continue
        seen.add(key)
        session.add(EmployeeSkill(
            employee_id=p["employee"].id, skill_id=skills[p["skill_name"]].id,
            level=p["level"], source=p["source"], verified_at=p["verified_at"],
        ))


PROJECT_DEFS = [
    # name, type, classification, owning_unit, owner_key, member_dept, member_count
    ("Employee Directory Platform", ProjectType.project, ProjectClassification.internal,
     "Platform Engineering", ("dept_director", "Platform Engineering"), "Platform Engineering", 18),
    ("Billing API", ProjectType.system, ProjectClassification.internal,
     "Platform Engineering", ("dept_director", "Platform Engineering"), "Platform Engineering", 14),
    ("Payroll Processing System", ProjectType.system, ProjectClassification.confidential,
     "Payroll", ("dept_director", "Payroll"), "Payroll", 5),
    ("Project Nightingale", ProjectType.project, ProjectClassification.confidential,
     "Engineering", ("division_vp", "Engineering"), "Platform Engineering", 4),
    ("ML Personalization Engine", ProjectType.project, ProjectClassification.internal,
     "Data & AI", ("dept_director", "Data & AI"), "Data & AI", 16),
    ("Customer Data Retention Policy", ProjectType.policy, ProjectClassification.public,
     "Compliance", ("dept_director", "Compliance"), "Compliance", 6),
    ("SOC 2 Compliance Program", ProjectType.function, ProjectClassification.internal,
     "Compliance", ("dept_director", "Compliance"), "Compliance", 8),
    ("Talent Acquisition Function", ProjectType.function, ProjectClassification.internal,
     "Talent Acquisition", ("dept_director", "Talent Acquisition"), "Talent Acquisition", 12),
    ("Global Mobility Policy", ProjectType.policy, ProjectClassification.public,
     "HR Operations", ("dept_director", "HR Operations"), "HR Operations", 6),
    ("Enterprise Sales Playbook", ProjectType.function, ProjectClassification.internal,
     "Sales", ("dept_director", "Sales"), "Sales", 20),
]
PROJECT_ROLES = ["Contributor", "Lead", "Stakeholder", "Reviewer"]


def build_projects(session, units, ctx):
    projects_created = []
    for name, ptype, classification, owning_unit, owner_key, member_dept, member_count in PROJECT_DEFS:
        owner_kind, owner_ref = owner_key
        owner = ctx["dept_directors"][owner_ref] if owner_kind == "dept_director" else ctx["division_vps"][owner_ref]
        project = Project(
            name=name, type=ptype,
            description=f"{name} — owned by {owning_unit}.",
            owning_unit_id=units[owning_unit].id, owner_id=owner.id, classification=classification,
        )
        session.add(project)
        session.flush()
        projects_created.append(project)

        candidates = [e for e in ALL_EMPLOYEES
                      if EMPLOYEE_SKILL_DEPT[e.id] == member_dept and e.id != owner.id]
        rng.shuffle(candidates)
        members = candidates[:member_count]
        rows = [(owner, "Owner")] + [(m, rng.choice(PROJECT_ROLES)) for m in members]
        for person, role in rows:
            start = date.today() - timedelta(days=rng.randint(60, 900))
            ongoing = rng.random() < 0.6
            end = None if ongoing else start + timedelta(days=rng.randint(60, 500))
            session.add(EmployeeProject(
                employee_id=person.id, project_id=project.id, role=role,
                start_date=start, end_date=end,
            ))
    return projects_created


def build_certifications(session, skills):
    for emp in ALL_EMPLOYEES:
        if rng.random() >= 0.15:
            continue
        name, issuer, skill_name = rng.choice(CERT_POOL)
        issued = date.today() - timedelta(days=rng.randint(60, 1800))
        expiry = issued + timedelta(days=rng.randint(700, 1500)) if rng.random() < 0.7 else None
        session.add(EmployeeCertification(
            employee_id=emp.id, name=name, issuer=issuer,
            skill_id=skills[skill_name].id, issued_date=issued, expiry_date=expiry,
        ))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def longest_manager_chain(session):
    rows = session.execute(select(Employee.id, Employee.manager_id, Employee.full_name)).all()
    parent = {r.id: r.manager_id for r in rows}
    name = {r.id: r.full_name for r in rows}

    def depth_and_path(emp_id):
        path = [emp_id]
        seen = {emp_id}
        cur = emp_id
        while parent.get(cur) is not None and parent[cur] not in seen:
            cur = parent[cur]
            seen.add(cur)
            path.append(cur)
        return len(path) - 1, path

    best_depth, best_path = -1, []
    for emp_id in parent:
        d, path = depth_and_path(emp_id)
        if d > best_depth:
            best_depth, best_path = d, path
    return best_depth, [name[i] for i in best_path]


def print_verification(session, ctx, specials, offices, skills):
    print("\n" + "=" * 78)
    print("SEED DATA VERIFICATION SUMMARY")
    print("=" * 78)

    total = session.scalar(select(func.count()).select_from(Employee))
    status = "PASS" if total == 500 else "FAIL"
    print(f"\n[{status}] Total employee records: {total} (target 500)")

    team_counts = session.execute(
        select(OrgUnit.name, func.count(Employee.id))
        .join(Employee, Employee.org_unit_id == OrgUnit.id)
        .where(OrgUnit.unit_type == "team")
        .group_by(OrgUnit.name)
    ).all()
    oversized = [r for r in team_counts if r[1] > 15]
    max_name, max_size = max(team_counts, key=lambda r: r[1])
    status = "PASS" if not oversized else "FAIL"
    avg_size = sum(c for _, c in team_counts) / len(team_counts)
    print(f"\n[{status}] Team sizes rebalanced (target 6-15 per leaf team): "
          f"{len(team_counts)} teams, largest is '{max_name}' at {max_size}, avg {avg_size:.1f}")
    for name, cnt in oversized:
        print(f"        OVERSIZED: {name}: {cnt}")

    depth, path = longest_manager_chain(session)
    status = "PASS" if depth >= 6 else "FAIL"
    print(f"\n[{status}] Deepest reporting chain: {depth} levels")
    print("        " + " -> ".join(path))

    bangalore = ctx["bangalore"]
    pbi = session.execute(
        select(Employee.full_name)
        .join(EmployeeSkill, EmployeeSkill.employee_id == Employee.id)
        .where(EmployeeSkill.skill_id == skills["Power BI"].id, Employee.office_id == bangalore.id)
    ).scalars().all()
    status = "PASS" if 3 <= len(pbi) <= 5 else "FAIL"
    print(f"\n[{status}] Skill overlap tuned — 'Power BI' in Bangalore: {len(pbi)} people (target 3-5)")
    print(f"        {', '.join(pbi)}")

    alias_rows = session.execute(
        select(Skill.id, Skill.name, Skill.canonical_id).where(Skill.name.in_(SKILL_CANONICAL_MAP.keys()))
    ).all()
    synonym_ok = True
    synonym_lines = []
    for skill_id, name, canonical_id in alias_rows:
        canonical_name = SKILL_CANONICAL_MAP[name]
        linked_ok = canonical_id == skills[canonical_name].id
        assigned = session.scalar(
            select(func.count()).select_from(EmployeeSkill).where(EmployeeSkill.skill_id == skill_id))
        alias_ok = linked_ok and assigned == 0
        synonym_ok = synonym_ok and alias_ok
        synonym_lines.append(f"        '{name}' --canonical_id--> '{canonical_name}' "
                             f"({'linked' if linked_ok else 'BROKEN LINK'}, "
                             f"{assigned} people hold the alias directly{'' if assigned == 0 else ' — should be 0'})")
    status = "PASS" if synonym_ok else "FAIL"
    print(f"\n[{status}] Skill synonyms resolve via canonical_id, alias never directly assigned:")
    for line in synonym_lines:
        print(line)

    tricky_names = ["Siobhan Nguyen", "Xiomara Delacroix", "Przemyslaw Kowalczyk",
                     "Aoife Kavanagh", "Zhiyuan Tanaka", "Kshitij Radhakrishnan"]
    existing_names = set(session.execute(select(Employee.full_name)).scalars().all())
    found_tricky = [n for n in tricky_names if n in existing_names]
    status = "PASS" if found_tricky else "FAIL"
    print(f"\n[{status}] Plausibly misspellable names present: {len(found_tricky)}/{len(tricky_names)}")
    print(f"        {', '.join(found_tricky)}")

    dup_pairs = [("Priya Sharma", "Priya Sharma"), ("Sean Ryan", "Shaun Ryan"),
                 ("Kristen Walsh", "Kristin Walsh"), ("Catherine Byrne", "Katherine Byrne")]
    name_counts: dict[str, int] = {}
    for n in session.execute(select(Employee.full_name)).scalars().all():
        name_counts[n] = name_counts.get(n, 0) + 1
    dup_ok = name_counts.get("Priya Sharma", 0) >= 2
    near_dup_ok = all(a in existing_names and b in existing_names for a, b in dup_pairs[1:])
    status = "PASS" if dup_ok and near_dup_ok else "FAIL"
    print(f"\n[{status}] Duplicate / near-duplicate names present:")
    print(f"        exact duplicate: Priya Sharma x{name_counts.get('Priya Sharma', 0)}")
    for a, b in dup_pairs[1:]:
        print(f"        near-duplicate pair: {a} / {b}")

    away_rows = session.execute(
        select(Employee.full_name, Employee.delegate_id)
        .where(Employee.availability_status == AvailabilityStatus.away)
    ).all()
    away_with_delegate = [r for r in away_rows if r.delegate_id is not None]
    status = "PASS" if away_with_delegate else "FAIL"
    print(f"\n[{status}] Employees away with a delegate assigned: {len(away_with_delegate)}")
    print(f"        {', '.join(r.full_name for r in away_with_delegate)}")

    restricted_count = session.scalar(
        select(func.count()).select_from(Employee)
        .where(Employee.availability_status == AvailabilityStatus.restricted))
    status = "PASS" if restricted_count >= 1 else "FAIL"
    print(f"\n[{status}] Restricted records: {restricted_count}")

    conf_projects = session.execute(
        select(Project.name).where(Project.classification == ProjectClassification.confidential)
    ).scalars().all()
    status = "PASS" if conf_projects else "FAIL"
    print(f"\n[{status}] Confidential projects: {len(conf_projects)} — {', '.join(conf_projects)}")

    # Department membership is tracked via EMPLOYEE_SKILL_DEPT (set from the
    # department each employee's team belongs to) rather than an org_unit
    # name list, since oversized teams are now split across several
    # differently-named sibling org_units (see split_target()).
    finance_depts = {"Finance Operations", "Payroll"}
    eng_depts = {"Platform Engineering", "Data & AI", "Infrastructure", "Quality Engineering"}
    finance_count = sum(1 for e in ALL_EMPLOYEES if EMPLOYEE_SKILL_DEPT[e.id] in finance_depts)
    eng_count = sum(1 for e in ALL_EMPLOYEES if EMPLOYEE_SKILL_DEPT[e.id] in eng_depts)
    status = "PASS" if finance_count > 0 and eng_count > 0 else "FAIL"
    print(f"\n[{status}] Departments with different sensitivity: "
          f"Finance-side={finance_count} employees, Engineering-side={eng_count} employees")

    tz_count = len({o[3] for o in OFFICES})
    status = "PASS" if len(OFFICES) >= 2 and tz_count == len(OFFICES) else "FAIL"
    print(f"\n[{status}] Offices across time zones: {len(offices)} offices, {tz_count} distinct time zones")
    for name, city, country, tz, _w in OFFICES:
        print(f"        {name} ({city}, {country}) — {tz}")

    used_langs = session.execute(
        select(Skill.name).join(EmployeeSkill, EmployeeSkill.skill_id == Skill.id)
        .where(Skill.category == SkillCategory.language).distinct()
    ).scalars().all()
    status = "PASS" if len(used_langs) >= 2 else "FAIL"
    print(f"\n[{status}] Spoken languages represented: {len(used_langs)} — {', '.join(sorted(used_langs))}")

    source_counts = dict(session.execute(
        select(EmployeeSkill.source, func.count()).group_by(EmployeeSkill.source)).all())
    status = "PASS" if source_counts.get(SkillSource.inferred, 0) > 0 else "FAIL"
    print(f"\n[{status}] employee_skills.source mix (includes 'inferred'):")
    for k, v in source_counts.items():
        print(f"        {k.value}: {v}")

    level_counts = dict(session.execute(
        select(EmployeeSkill.level, func.count()).group_by(EmployeeSkill.level)).all())
    status = "PASS" if level_counts.get(SkillLevel.learning, 0) > 0 else "FAIL"
    print(f"\n[{status}] employee_skills.level mix (includes 'Learning'):")
    for k, v in level_counts.items():
        print(f"        {k.value}: {v}")

    print(f"\n[INFO] Realistic incompleteness (null count / {total}):")
    for label, col in [
        ("bio", Employee.bio), ("work_phone", Employee.work_phone),
        ("office_id (no office)", Employee.office_id),
        ("personal_mobile", Employee.personal_mobile),
        ("slack_handle", Employee.slack_handle),
        ("preferred_name", Employee.preferred_name),
        ("cost_centre", Employee.cost_centre),
        ("photo_url", Employee.photo_url),
    ]:
        n_null = session.scalar(select(func.count()).select_from(Employee).where(col.is_(None)))
        print(f"        {label}: {n_null} missing, {total - n_null} present")

    no_manager = session.scalar(
        select(func.count()).select_from(Employee).where(Employee.manager_id.is_(None)))
    status = "PASS" if no_manager >= 2 else "FAIL"  # CEO + at least one data-gap case
    print(f"\n[{status}] People with no manager: {no_manager} (1 expected at the top + deliberate data gaps)")

    unverified = session.scalar(
        select(func.count()).select_from(EmployeeSkill).where(EmployeeSkill.verified_at.is_(None)))
    total_es = session.scalar(select(func.count()).select_from(EmployeeSkill))
    status = "PASS" if unverified > 0 else "FAIL"
    print(f"\n[{status}] Unverified skills (source self/inferred, no verified_at): {unverified}/{total_es}")

    print(f"\n[PASS] SCIM 2.0 / Microsoft Graph field naming: "
          f"directory_object_id~externalId, work_email~mail, full_name~displayName, "
          f"job_title~jobTitle, work_phone~businessPhones, manager_id~manager (schema-level, no data check)")

    print("\n" + "=" * 78)
    print("SKILL-BY-TEAM BREAKDOWN (primary-skill correlation check)")
    print("=" * 78)
    for theme, primary_list in TEAM_PRIMARY_SKILLS.items():
        theme_ids = [e.id for e in ALL_EMPLOYEES if EMPLOYEE_TEAM_THEME[e.id] == theme]
        n_people = len(theme_ids)
        rows = session.execute(
            select(Skill.name, func.count())
            .join(EmployeeSkill, EmployeeSkill.skill_id == Skill.id)
            .where(EmployeeSkill.employee_id.in_(theme_ids))
            .group_by(Skill.name)
            .order_by(func.count().desc())
        ).all()
        print(f"\n{theme} team(s) — {n_people} people. Skill holders (desc.), '*' = team-primary:")
        for name, cnt in rows[:8]:
            marker = " *PRIMARY*" if name in primary_list else ""
            pct = (cnt / n_people * 100) if n_people else 0
            print(f"        {name:32s} {cnt:4d} people ({pct:5.1f}%){marker}")

    print("\n" + "=" * 78)


def main():
    session = SessionLocal()
    try:
        reset_tables(session)
        offices = build_offices(session)
        units, theme_units = build_org_units(session)
        skills = build_skills(session)
        ctx = build_employees(session, offices, units, theme_units)
        specials = inject_incompleteness_and_specials(ctx)
        assign_skills(session, skills, ctx)
        build_projects(session, units, ctx)
        build_certifications(session, skills)
        session.commit()
        print(f"Seeded {len(ALL_EMPLOYEES)} employees, {len(offices)} offices, "
              f"{len(units)} org units, {len(skills)} skills.")
        print_verification(session, ctx, specials, offices, skills)
    finally:
        session.close()


if __name__ == "__main__":
    main()
