"""Synthetic seed data for the employee directory (~500 records, engineered not random).

No real Quadrant employee data is used anywhere in this file — every name, office,
project, and skill below is fabricated. Run with `python seed.py`; safe to re-run,
it clears its own tables first.
"""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import (
    CourseRequirement,
    Employee,
    EmployeeCertification,
    EmployeeCourseStatus,
    EmployeeProject,
    EmployeeSkill,
    Notification,
    Office,
    OrgUnit,
    Project,
    Skill,
    TrainingCourse,
)
from app.models.enums import (
    AvailabilityStatus,
    CourseStatus,
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
    "IT Operations": ["Network Administration", "Cybersecurity", "Azure", "Docker",
                       "Project Management", "Advanced Excel"],
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
    # Its own division rather than a department under Engineering: internal
    # IT (laptops, accounts, the service desk) reports separately from product
    # engineering in most companies this size, and burying it under
    # Engineering would also quietly hand everyone in it the Secure Coding
    # Practices requirement, which is scoped to that division.
    "IT": ["IT Operations"],
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
    "IT Operations": {"IT Service Desk": 16, "Identity & Endpoints": 12},
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
    "IT Operations": ["IT Support Specialist", "Senior IT Support Specialist",
                       "Systems Administrator", "Identity & Access Analyst",
                       "Endpoint Engineer"],
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


# Backend absorbs the difference between this target and the sum of the
# headcounts declared above, so the total stays a round number.
#
# Raised from 500 to 530 when the IT division was added, deliberately. Leaving
# it at 500 meant the ~30 IT people were carved straight out of Backend
# (50 -> 20) rather than added to the company: Backend is the flagship team
# here — it hosts the deliberately deep reporting chain and the primary-skill
# correlation checks — so halving it as a side effect of an unrelated change
# would quietly weaken both. IT is additive; everything else keeps its size.
TARGET_HEADCOUNT = 530

_shortfall = TARGET_HEADCOUNT - _grand_total()
DEPT_THEMES["Platform Engineering"]["Backend"] += _shortfall


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def reset_tables(session) -> None:
    # Training/notification tables first: they reference employees and
    # training_courses, so they have to go before either of those does.
    for model in (Notification, EmployeeCourseStatus, CourseRequirement, TrainingCourse,
                  EmployeeCertification, EmployeeProject, EmployeeSkill, Project,
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

# Current age by org level. Seniority correlates with age, loosely and with
# wide overlapping bands — a 30-year-old director and a 55-year-old IC are
# both ordinary, and ranges this wide keep the synthetic data from implying
# otherwise.
LEVEL_AGE_RANGE = {
    0: (46, 63), 1: (41, 60), 2: (37, 57), 3: (34, 54),
    4: (31, 51), 5: (28, 47), 6: (25, 42), 7: (22, 36),
}

# Annual gross, in USD, before the country multiplier below. Synthetic and
# deliberately round — this is demo data for a directory, not a compensation
# band anyone should read as Quadrant's.
LEVEL_BASE_SALARY_USD = {
    0: 420_000, 1: 285_000, 2: 215_000, 3: 178_000,
    4: 150_000, 5: 128_000, 6: 108_000, 7: 88_000,
}
INTERN_SALARY_USD = 52_000

# Rough local-market conversion, currency included. Not FX rates: an engineer
# in Bangalore isn't paid a Seattle salary converted at spot, so these are
# market multipliers that happen to also change the unit.
COUNTRY_SALARY = {
    "United States": ("USD", 1.0),
    "United Kingdom": ("GBP", 0.62),
    "India": ("INR", 22.0),
    "Singapore": ("SGD", 1.15),
    "Australia": ("AUD", 1.35),
}

# Populated as employees are created: employee.id -> department name used for
# skill-pool assignment (None for CEO/company-wide roles), -> org level
# (0=CEO .. 7=IC) used for incompleteness injection, and -> team theme (e.g.
# "Backend") for the four engineering teams with a primary-skill profile.
EMPLOYEE_SKILL_DEPT: dict[str, str | None] = {}
EMPLOYEE_LEVEL: dict[str, int] = {}
EMPLOYEE_TEAM_THEME: dict[str, str | None] = {}
ALL_EMPLOYEES: list[Employee] = []


# Fraction of people with no date of birth on file. Real HR data has gaps —
# a record migrated from an old system, someone who never completed onboarding
# — and the sweep must skip them silently rather than assume anything.
NO_DOB_RATE = 0.04


def make_date_of_birth(level, employment_type, hire_date) -> date | None:
    """Age from the level band, then walked back from today.

    Constrained so nobody is implied to have been hired as a child: if the
    drawn age minus tenure lands under 18, the age is pushed up until it
    doesn't. That's a real risk with the long-tenure levels — a 25-year-old
    with nine years of service would otherwise be an ordinary-looking row.
    """
    if rng.random() < NO_DOB_RATE:
        return None

    lo, hi = LEVEL_AGE_RANGE.get(level, (25, 45))
    if employment_type == EmploymentType.intern:
        lo, hi = 20, 25

    tenure_years = (date.today() - hire_date).days / 365.25
    min_age = tenure_years + 18
    age = max(rng.uniform(lo, hi), min_age)

    born = date.today() - timedelta(days=age * 365.25)
    # Nudge off the exact anniversary of today so the seeded population
    # doesn't accidentally cluster birthdays on the day it was generated —
    # inject_date_milestones() places those deliberately instead.
    return born - timedelta(days=rng.randint(1, 300))


def make_salary(level, employment_type, office) -> tuple[Decimal | None, str | None]:
    """Annual gross in the office's local currency, or (None, None).

    Contractors get nothing on file on purpose: they're paid through an
    agency, so the company genuinely doesn't hold a salary for them. That
    makes the null meaningful rather than missing data, and gives the
    profile's "absent" case something real to represent.
    """
    if employment_type == EmploymentType.contractor:
        return None, None

    base = (INTERN_SALARY_USD if employment_type == EmploymentType.intern
            else LEVEL_BASE_SALARY_USD.get(level, 100_000))
    country = office.country if office else "United States"
    currency, multiplier = COUNTRY_SALARY.get(country, ("USD", 1.0))

    amount = base * multiplier * rng.uniform(0.88, 1.14)  # individual spread within the band
    step = 10_000 if currency == "INR" else 500          # INR salaries aren't quoted to the rupee
    rounded = round(amount / step) * step
    return Decimal(str(rounded)).quantize(Decimal("0.01")), currency


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

    date_of_birth = make_date_of_birth(level, employment_type, hire_date)
    salary, salary_currency = make_salary(level, employment_type, office)

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
        date_of_birth=date_of_birth,
        salary=salary,
        salary_currency=salary_currency,
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


def inject_date_milestones(session) -> dict:
    """Force a handful of birthdays and milestone anniversaries onto today.

    Without this the date sweep is untestable and undemoable in practice: with
    ~500 people, roughly one birthday falls on any given day and a milestone
    anniversary might be weeks away, so "run it and see" would usually show an
    empty result that's indistinguishable from a broken sweep.

    Picks people who are neither restricted nor the CEO, so the resulting
    notifications are visible to ordinary HR staff and don't all concentrate
    at the top of the org chart.
    """
    today = date.today()
    candidates = [
        e for e in ALL_EMPLOYEES
        if e.availability_status != AvailabilityStatus.restricted
        and e.manager_id is not None
        and EMPLOYEE_LEVEL[e.id] >= 3
    ]
    picks = rng.sample(candidates, k=min(6, len(candidates)))

    birthday_people = picks[:3]
    for emp in birthday_people:
        # Keep whatever age the level band gave them; move only month/day.
        current_age = int((today - (emp.date_of_birth or today - timedelta(days=30 * 365))).days / 365.25)
        emp.date_of_birth = _same_day_years_ago(today, max(current_age, 22))

    anniversary_people = picks[3:6]
    milestones = [1, 5, 10]
    for emp, years in zip(anniversary_people, milestones):
        emp.hire_date = _same_day_years_ago(today, years)
        # A 10-year anniversary for someone born 25 years ago would mean they
        # started at 15 — push the birth date back rather than leave that.
        if emp.date_of_birth is not None:
            youngest_allowed = _same_day_years_ago(emp.hire_date, 18)
            if emp.date_of_birth > youngest_allowed:
                emp.date_of_birth = _same_day_years_ago(today, years + 22)

    session.flush()
    return {
        "birthdays_today": [(e.full_name, e.date_of_birth) for e in birthday_people],
        "anniversaries_today": [(e.full_name, y) for e, y in zip(anniversary_people, milestones)],
    }


def _same_day_years_ago(reference: date, years: int) -> date:
    """`reference` minus whole years, keeping month and day.

    The 29 February case can't be expressed in a non-leap year, so it lands on
    the 28th — the same convention app/notifications.py uses when deciding
    which day a leap-day anniversary is observed on.
    """
    try:
        return reference.replace(year=reference.year - years)
    except ValueError:
        return reference.replace(year=reference.year - years, day=28)


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

# ARCHITECTURE_2.md §11: 114 of 118 project descriptions were the literal
# template f"{name} — owned by {dept}." (median 58 characters) — no semantic
# content to embed, so mode 3 (semantic search) would just be name-matching
# with an embedding round-trip bolted on. Real 2-4 sentence descriptions
# (problem, tech, what went wrong) below fix that. Written once, offline, not
# generated by a model call at request time — same "generate offline once,
# paste in" the doc itself calls for.
FLAGSHIP_PROJECT_DESCRIPTIONS: dict[str, str] = {
    "Employee Directory Platform": (
        "The internal system of record for who works where, replacing three years of drift "
        "across spreadsheets, a stale Confluence wiki, and word of mouth. Search, org charts, "
        "and skills all read from one API instead of being manually reconciled by hand. The "
        "original launch undercounted contractors because the HRIS feed didn't distinguish "
        "employment type, which took two sprints to unwind after go-live."
    ),
    "Billing API": (
        "Consolidates invoicing and usage metering for every product line behind one versioned "
        "API, replacing per-product billing logic that had quietly diverged over several years "
        "of feature work. Idempotent webhook delivery means a retried event can't double-charge "
        "a customer. Payment latency was the long-running problem: p99 response times crept up "
        "for months until connection-pool exhaustion under end-of-month invoice runs started "
        "causing gateway timeouts and stuck transactions, and a slow memory leak in the "
        "settlement worker meant it had to be restarted nightly before anyone found the "
        "retained-reference bug behind it."
    ),
    "Payroll Processing System": (
        "Handles salary calculation, tax withholding, and disbursement for the full employee "
        "population, integrating with the payroll provider's API rather than re-implementing "
        "tax logic in house. Maintained by a small, tightly-scoped team given the sensitivity "
        "of the data it touches. An early version miscalculated withholding for employees who "
        "changed states mid-year, caught in the first quarterly audit."
    ),
    "Project Nightingale": (
        "A confidential initiative scoped directly under Engineering leadership, evaluating a "
        "potential acquisition target's technical stack for integration risk. A small, "
        "need-to-know team performs due diligence on the target's codebase, infrastructure, "
        "and data practices. The engagement uncovered enough undocumented legacy dependencies "
        "that the integration timeline slipped by a full quarter."
    ),
    "ML Personalization Engine": (
        "Ranks and personalizes in-product recommendations using a model trained on clickstream "
        "and account-tier data, replacing a static rules engine that hadn't been touched in two "
        "years. Evaluated offline against held-out click data before each rollout. The first "
        "production model overfit to power users and had to be retrained with better sampling "
        "to serve the long tail fairly."
    ),
    "Customer Data Retention Policy": (
        "Defines how long customer data is retained across every system after an account "
        "closes, and the automated deletion jobs that enforce it, replacing an ad hoc mix of "
        "manual deletion requests and indefinite retention. Written to satisfy GDPR's "
        "storage-limitation principle and reviewed jointly with Engineering to confirm every "
        "downstream system actually honors the schedule. The first draft missed backup "
        "retention entirely, which the external audit flagged before publication."
    ),
    "SOC 2 Compliance Program": (
        "The ongoing control framework and evidence collection process behind the company's "
        "annual SOC 2 Type II audit, covering access reviews, change management, and incident "
        "response. Runs on a mix of manual quarterly attestations and automated log collection "
        "from the core platform. The first audit cycle surfaced gaps in offboarding timeliness "
        "that took a dedicated remediation sprint to close."
    ),
    "Talent Acquisition Function": (
        "The end-to-end hiring pipeline from requisition approval through offer, standardized "
        "across departments after each team had been running its own ad hoc process. Uses an "
        "applicant tracking system integrated with structured interview scorecards to reduce "
        "variance between interviewers. Time-to-fill improved, but scorecard adoption stayed "
        "uneven until interviewer training became mandatory."
    ),
    "Global Mobility Policy": (
        "Governs visa sponsorship, relocation support, and cross-border transfers for employees "
        "moving between offices, replacing a patchwork of country-specific exceptions handled "
        "case by case. Coordinated with outside immigration counsel to keep the policy current "
        "against each jurisdiction's changing rules. An early version didn't account for "
        "dependent visas, which delayed several approved transfers until it was amended."
    ),
    "Enterprise Sales Playbook": (
        "The standardized methodology, objection-handling guide, and deal-desk process for "
        "enterprise accounts, built after inconsistent discounting and messaging across the "
        "sales team started showing up in win-rate data. Grounded in pipeline data and a common "
        "qualification framework. Adoption lagged for two quarters until it was tied directly "
        "to deal-desk approval instead of just training."
    ),
}

# Generic past-initiative names each department draws from, on top of the
# named flagship projects above — this is what gives each person a varied,
# non-repetitive project *history* instead of at-most-one membership.
COMPLETED_PROJECT_TEMPLATES = [
    "{dept} Process Improvement Initiative", "{dept} Tooling Upgrade",
    "{dept} Onboarding Revamp", "{dept} Vendor Consolidation",
    "{dept} Reporting Overhaul", "{dept} Workflow Automation",
    "{dept} Knowledge Base Migration", "{dept} Annual Planning Cycle",
]

# One real, varied 2-3 sentence description per template *shape*, filled in
# with each department's actual DEPT_SKILL_POOL entries so "Sales Tooling
# Upgrade" and "Platform Engineering Tooling Upgrade" read as genuinely
# different work, not the same sentence with a noun swapped. Picked by
# position (pool[0]/pool[1]), not rng — this must not consume any of the
# shared `rng` stream, since every employee/skill draw downstream of project
# generation depends on that stream staying exactly as long as it already is.
GENERIC_PROJECT_DESCRIPTION_TEMPLATES: dict[str, str] = {
    "{dept} Process Improvement Initiative": (
        "A working effort to cut the time {dept} spends on its most repetitive recurring "
        "process, after headcount growth made the old ad hoc approach unsustainable. The team "
        "leaned on {tech1} to document and standardize steps that had previously lived in a "
        "few people's heads. Measured cycle time improved, but adoption outside the pilot "
        "group lagged until the new process became the default rather than an alternative."
    ),
    "{dept} Tooling Upgrade": (
        "Replaces the aging toolchain {dept} had outgrown with a stack built around {tech1} "
        "and {tech2}, chosen after the old tools stopped scaling with the team's headcount. "
        "Migration ran in parallel with day-to-day work rather than a hard cutover. A subset "
        "of historical records didn't map cleanly to the new schema and needed manual "
        "reconciliation after go-live."
    ),
    "{dept} Onboarding Revamp": (
        "Redesigns how a new hire in {dept} ramps up in their first 90 days, after exit "
        "interviews kept naming a confusing, undocumented ramp-up as a top complaint. The new "
        "program pairs a structured {tech1} curriculum with a named buddy for the first month "
        "instead of leaving new hires to piece it together informally. Early cohorts ramped "
        "faster, though the buddy step was inconsistently followed until it was added to the "
        "buddy's own goals."
    ),
    "{dept} Vendor Consolidation": (
        "Reduces {dept}'s sprawl of overlapping vendor contracts down to a shortlist chosen "
        "for actual usage rather than legacy habit, after a spend review found licenses paid "
        "for tools nobody had opened in months. The evaluation weighted integration with the "
        "team's existing {tech1} workflow alongside price. One migrated workflow broke "
        "silently for several weeks because the new vendor's API behaved differently under "
        "load than the sales demo had suggested."
    ),
    "{dept} Reporting Overhaul": (
        "Replaces {dept}'s manually assembled monthly reporting deck with a live dashboard "
        "pulling directly from source systems, after recurring discrepancies between the deck "
        "and the underlying data eroded trust in the numbers. Built around {tech1}, with a "
        "single agreed definition for every metric that previously had two or three quietly "
        "different versions floating around. The first version was too slow to load with a "
        "full quarter of history, which needed a follow-up pass on the underlying queries."
    ),
    "{dept} Workflow Automation": (
        "Automates a workflow inside {dept} that previously required someone to manually "
        "shepherd a request through several handoffs, using {tech1} to trigger the next step "
        "automatically instead of waiting on someone to notice an email. Turnaround time "
        "improved noticeably once live. The automation didn't originally handle the exception "
        "path for rejected requests, which routed silently nowhere until a teammate happened "
        "to notice a backlog."
    ),
    "{dept} Knowledge Base Migration": (
        "Moves {dept}'s documentation off a wiki that had become unsearchable after years of "
        "copy-pasted pages into a structured knowledge base with actual ownership per article. "
        "Content review leaned on people with hands-on {tech1} experience to flag what was "
        "still accurate versus quietly outdated. A meaningful chunk of pages turned out to "
        "describe a process the team had already stopped using, and got archived rather than "
        "migrated."
    ),
    "{dept} Annual Planning Cycle": (
        "The recurring process {dept} uses to set priorities and headcount asks for the year "
        "ahead, run each cycle against the previous year's actuals rather than from a blank "
        "page. Planning inputs are pulled together using {tech1} so every team submits "
        "comparable numbers instead of a mix of formats. The first cycle run this way still "
        "slipped past its deadline because two teams' inputs depended on a third team's "
        "numbers that weren't ready yet."
    ),
}

COMPANY_WIDE_PROJECT_NAMES = [
    "Company All-Hands Modernization", "Leadership Offsite Planning",
    "Annual Strategy Refresh", "Cross-Functional Culture Initiative",
]
COMPANY_WIDE_PROJECT_DESCRIPTIONS: dict[str, str] = {
    "Company All-Hands Modernization": (
        "Redesigns the monthly company all-hands after employee survey feedback consistently "
        "rated it a one-way broadcast not worth attending live. Adds a structured Q&A segment "
        "and pre-submitted questions instead of an open mic a handful of people dominated. "
        "Attendance improved, but the pre-submission window is still short enough that some "
        "questions arrive too late to prep a real answer."
    ),
    "Leadership Offsite Planning": (
        "The annual process for scoping, budgeting, and running the leadership offsite, "
        "standardized after a previous year's offsite went noticeably over budget with no "
        "clear owner for the overrun. Centralizes venue, travel, and agenda planning under one "
        "accountable owner instead of splitting it across whoever volunteered. Agenda planning "
        "still tends to start later than the budget does, which compresses prep time for "
        "facilitators."
    ),
    "Annual Strategy Refresh": (
        "The yearly process for revisiting company strategy against the prior year's actual "
        "results, rather than treating the previous plan as fixed. Each division head brings a "
        "one-page summary of what changed in their world, reviewed together before "
        "company-wide priorities are finalized. The first cycle under this format ran long "
        "because divisions hadn't agreed in advance on how much detail belonged in the "
        "one-pager."
    ),
    "Cross-Functional Culture Initiative": (
        "A company-wide effort to strengthen collaboration between departments that "
        "historically operated in isolation, after an internal survey flagged interdepartmental "
        "friction as a recurring theme. Introduced regular cross-team shadowing and a shared "
        "internal newsletter highlighting work from other departments. Participation was "
        "strong at launch but tapered once the initial novelty wore off, prompting a mid-year "
        "refresh of the format."
    ),
}


def flagship_project_description(name: str, owning_unit: str) -> str:
    return FLAGSHIP_PROJECT_DESCRIPTIONS.get(name, f"{name} — owned by {owning_unit}.")


# Bespoke descriptions for specific (dept, template) projects, keyed by the
# project's FULL name. Checked ahead of GENERIC_PROJECT_DESCRIPTION_TEMPLATES.
#
# Why these exist at all: the generic templates above are keyed by project
# TYPE, so all 15 "{dept} Tooling Upgrade" projects get the same three
# sentences with only {dept}/{tech1}/{tech2} swapped. That reads fine to a
# human and is nearly identical to an embedding model -- which is what made
# mode 3 (app/project_search.py) answer "our Kubernetes cluster keeps
# dropping pods" with "Infrastructure Onboarding Revamp": nothing in the
# corpus was actually ABOUT Kubernetes, so the nearest neighbour was
# whichever Infrastructure project happened to be closest in a very flat
# space. Description quality was already fixed; corpus DIVERSITY was not.
#
# Each entry below is a real, specific engineering problem with the
# vocabulary someone would actually use when stuck on it. They're spread
# across departments on purpose, so the project->employee hop lands on
# people whose job titles make sense for the problem.
#
# Deliberately only a handful. This is not an attempt to make all 129
# projects unique -- it's coverage for the problem shapes mode 3 is most
# likely to be asked about.
SPECIALIZED_PROJECT_DESCRIPTIONS: dict[str, str] = {
    "Infrastructure Tooling Upgrade": (
        "Migrated the last of the self-managed virtual machines onto a managed Kubernetes "
        "platform, standardizing container builds, pod scheduling, and cluster networking "
        "across every service instead of per-team snowflake deployments. Ingress, service "
        "discovery, and the CNI plugin were the hardest parts: DNS resolution inside the "
        "cluster failed intermittently under load, and pods were being evicted during node "
        "drains because resource limits had been copied between services without being "
        "re-measured. Autoscaling now handles traffic spikes that used to need someone "
        "manually adding capacity at 3am."
    ),
    "Infrastructure Reporting Overhaul": (
        "Replaced a wall of unowned dashboards with a single observability stack covering "
        "metrics, distributed tracing, and structured logs, after an incident where three "
        "engineers spent forty minutes disagreeing about whether latency had actually "
        "regressed. Alert rules were rewritten around symptoms customers feel rather than "
        "individual host CPU, which cut paging volume enough that on-call stopped ignoring "
        "the channel. Trace sampling was initially too aggressive to debug rare failures, "
        "so the tail-based sampler had to be retuned after the first month."
    ),
    "Infrastructure Workflow Automation": (
        "Rebuilt the deployment pipeline after releases routinely timed out, hung waiting on "
        "manual approval, or half-applied and left environments in an inconsistent state with "
        "no clean way back. Deploys are now blue-green with automated rollback on health-check "
        "failure, and every environment is provisioned from the same infrastructure-as-code "
        "definitions instead of drifting apart over months. The rollback path itself went "
        "untested for the first two releases, which is exactly when it was first needed."
    ),
    "Data & AI Workflow Automation": (
        "Rewrote the nightly ETL pipeline that had been silently failing and back-filling "
        "itself for weeks, leaving downstream dashboards confidently reporting stale numbers. "
        "The orchestration DAG now fails loudly, retries with backoff, and refuses to publish "
        "a partition that didn't pass its row-count and freshness checks. A long-tail problem "
        "was upstream schema drift: a source system renamed a column with no notice and every "
        "job depending on it failed at 2am for three nights running before anyone was paged."
    ),
    "Data & AI Tooling Upgrade": (
        "Introduced data quality contracts between producing and consuming teams after a "
        "quarter in which two separate executive dashboards disagreed about revenue and "
        "neither owner could explain why. Schema changes now break a contract test in CI "
        "instead of breaking a dashboard in production, and every table has a declared owner "
        "and freshness expectation. Backfilling historical partitions to satisfy the new "
        "checks took considerably longer than building the checks themselves."
    ),
    "Quality Engineering Tooling Upgrade": (
        "Attacked a test suite that had become slow and flaky enough that engineers were "
        "re-running failed builds on reflex rather than reading them, which meant real "
        "regressions were being merged behind noise. Flaky tests are now quarantined "
        "automatically on repeat non-deterministic failure, and the suite runs in parallel "
        "shards that cut wall-clock time substantially. Most of the flakiness turned out to "
        "be shared mutable fixtures and hard-coded waits rather than genuine race conditions "
        "in the product code."
    ),
    # Acronyms are written alongside the spelled-out terms on purpose, here
    # and elsewhere in this dict. The keyword arm of mode 3's hybrid search
    # is literal substring matching, so a description that only ever says
    # "single sign-on" cannot be found by someone typing "SSO" -- measured:
    # "nobody can log in, SSO and MFA keep failing" ranked Billing API first
    # on the keyword side purely because this text had neither acronym in
    # it. Writing both is also just how people actually write these.
    "IT Operations Tooling Upgrade": (
        "Consolidated identity onto single sign-on (SSO) with hardware-backed multi-factor "
        "authentication (MFA), retiring a long tail of local accounts and shared credentials "
        "nobody would admit to using. Covers VPN access, device enrolment, and the "
        "joiner-mover-leaver process, so login rights are revoked on the last working day "
        "rather than whenever someone remembers. The rollout's worst week involved "
        "conditional-access rules locking staff out mid-flight, which needed an emergency "
        "exception path."
    ),
}


def generic_project_description(template: str, dept: str) -> str:
    """Fills a template-shape description with dept's own real skills.
    pool[0]/pool[1] (not an rng draw) -- see the module comment above these
    templates for why this must stay deterministic and rng-free.

    A bespoke SPECIALIZED_PROJECT_DESCRIPTIONS entry for this exact project
    name wins over the type-level template -- see that dict's comment for
    why a handful of projects need to be genuinely distinct rather than
    dept-substituted copies of each other.
    """
    name = template.format(dept=dept)
    specialized = SPECIALIZED_PROJECT_DESCRIPTIONS.get(name)
    if specialized is not None:
        return specialized
    pool = DEPT_SKILL_POOL.get(dept, LEADERSHIP_SKILL_POOL)
    tech1 = pool[0]
    tech2 = pool[1] if len(pool) > 1 else pool[0]
    body = GENERIC_PROJECT_DESCRIPTION_TEMPLATES.get(template)
    if body is None:
        return f"{template.format(dept=dept)} — owned by {dept}."
    return body.format(dept=dept, tech1=tech1, tech2=tech2)


def company_wide_project_description(name: str) -> str:
    return COMPANY_WIDE_PROJECT_DESCRIPTIONS.get(name, f"{name} — company-wide.")

# Below this many days of tenure, a confidential-project member with no
# other completed project history is plausible ("explained by their join
# date") rather than a data-quality gap.
NEW_HIRE_DAYS = 90


def tenure_days(emp: Employee) -> int:
    return (date.today() - emp.hire_date).days


def build_flagship_projects(session, units, ctx):
    """The 10 named, thematically real projects — including the 2
    confidential ones. Returns (projects, membership_state), where
    membership_state tracks what each member already has so the general
    portfolio pass (build_portfolios) tops up rather than piles on.
    """
    projects_created = []
    membership_state: dict[str, dict] = {}

    def note(emp_id: str, project_id: int, is_current: bool):
        state = membership_state.setdefault(emp_id, {"projects": set(), "completed": 0, "current": 0})
        state["projects"].add(project_id)
        state["current" if is_current else "completed"] += 1

    for name, ptype, classification, owning_unit, owner_key, member_dept, member_count in PROJECT_DEFS:
        owner_kind, owner_ref = owner_key
        owner = ctx["dept_directors"][owner_ref] if owner_kind == "dept_director" else ctx["division_vps"][owner_ref]
        project = Project(
            name=name, type=ptype,
            description=flagship_project_description(name, owning_unit),
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
            ongoing = rng.random() < 0.6
            if ongoing:
                start = date.today() - timedelta(days=rng.randint(30, 500))
                end = None
            else:
                start, end = _past_dates()  # guarantees end < today, unlike start+duration
            session.add(EmployeeProject(
                employee_id=person.id, project_id=project.id, role=role,
                start_date=start, end_date=end,
            ))
            note(person.id, project.id, is_current=ongoing)

    return projects_created, membership_state


def build_project_pool(session, units, ctx) -> dict[str | None, list[Project]]:
    """Per-department (plus a company-wide, keyed None) pool of generic
    past initiatives. 7-8 per bucket so a person's 1-6-project portfolio
    can be sampled without repeats, while still letting many different
    people share membership in the same initiative — projects naturally
    have more than one member.
    """
    pool: dict[str | None, list[Project]] = {}

    for dept, director in ctx["dept_directors"].items():
        dept_unit = units[dept]
        names = rng.sample(COMPLETED_PROJECT_TEMPLATES, len(COMPLETED_PROJECT_TEMPLATES))
        dept_projects = []
        for template in names:
            name = template.format(dept=dept)
            ptype = rng.choices([ProjectType.project, ProjectType.function, ProjectType.system],
                                weights=[70, 20, 10])[0]
            classification = ProjectClassification.public if rng.random() < 0.1 else ProjectClassification.internal
            project = Project(name=name, type=ptype, description=generic_project_description(template, dept),
                              owning_unit_id=dept_unit.id, owner_id=director.id, classification=classification)
            session.add(project)
            dept_projects.append(project)
        pool[dept] = dept_projects

    company_unit = units["Quadrant Technologies"]
    leadership_projects = []
    for name in COMPANY_WIDE_PROJECT_NAMES:
        ptype = rng.choice([ProjectType.function, ProjectType.policy])
        project = Project(name=name, type=ptype, description=company_wide_project_description(name),
                          owning_unit_id=company_unit.id, owner_id=ctx["ceo"].id,
                          classification=ProjectClassification.internal)
        session.add(project)
        leadership_projects.append(project)
    pool[None] = leadership_projects

    session.flush()
    return pool


def _roll_completed_target(tenure: int) -> int:
    if tenure < NEW_HIRE_DAYS:
        return rng.choices([0, 1, 2], weights=[50, 35, 15])[0]
    return rng.choices([1, 2, 3, 4], weights=[30, 35, 20, 15])[0]


def _roll_current_target() -> int:
    return rng.choices([0, 1, 2], weights=[35, 45, 20])[0]


def _past_dates() -> tuple[date, date]:
    end = date.today() - timedelta(days=rng.randint(10, 700))
    start = end - timedelta(days=rng.randint(60, 400))
    return start, end


def build_portfolios(session, pool: dict, membership_state: dict) -> None:
    """Give every employee a realistic mix of completed (past end_date) and
    current (open-ended) projects, on top of whatever flagship membership
    they already picked up — most people land at 1-4 completed + 0-2
    current overall, per the target distribution above.
    """
    for emp in ALL_EMPLOYEES:
        dept = EMPLOYEE_SKILL_DEPT[emp.id]
        dept_pool = pool.get(dept) or pool[None]
        state = membership_state.setdefault(emp.id, {"projects": set(), "completed": 0, "current": 0})

        need_completed = max(0, _roll_completed_target(tenure_days(emp)) - state["completed"])
        need_current = max(0, _roll_current_target() - state["current"])

        available = [p for p in dept_pool if p.id not in state["projects"]]
        rng.shuffle(available)
        picks = available[:need_completed + need_current]

        for i, project in enumerate(picks):
            role = rng.choice(PROJECT_ROLES)
            if i < need_completed:
                start, end = _past_dates()
            else:
                start = date.today() - timedelta(days=rng.randint(30, 500))
                end = None
            session.add(EmployeeProject(
                employee_id=emp.id, project_id=project.id, role=role, start_date=start, end_date=end,
            ))
            state["projects"].add(project.id)


def top_up_confidential_members(session, pool: dict) -> dict:
    """Anyone on a confidential project should also have real project
    history elsewhere — otherwise their record reads as having no life
    outside the confidential work, which is both unrealistic and a subtle
    tell about what they're on. The only allowed exception is someone too
    new to have accumulated history yet, and even then only sometimes.
    """
    session.flush()
    confidential_ids = {
        pid for (pid,) in session.query(Project.id).filter(Project.classification == ProjectClassification.confidential)
    }
    conf_member_ids = {
        row.employee_id for row in session.query(EmployeeProject.employee_id)
        .filter(EmployeeProject.project_id.in_(confidential_ids)).distinct()
    }
    employees_by_id = {e.id: e for e in ALL_EMPLOYEES}

    topped_up, exempted_new_hires = 0, 0
    for emp_id in conf_member_ids:
        emp = employees_by_id[emp_id]
        has_completed_noncon = session.query(EmployeeProject).join(
            Project, EmployeeProject.project_id == Project.id
        ).filter(
            EmployeeProject.employee_id == emp_id,
            Project.classification != ProjectClassification.confidential,
            EmployeeProject.end_date.isnot(None),
        ).first() is not None
        if has_completed_noncon:
            continue

        if tenure_days(emp) < NEW_HIRE_DAYS and rng.random() < 0.5:
            exempted_new_hires += 1  # empty history explained by join date
            continue

        dept = EMPLOYEE_SKILL_DEPT[emp_id]
        dept_pool = pool.get(dept) or pool[None]
        existing_ids = {pid for (pid,) in session.query(EmployeeProject.project_id)
                        .filter(EmployeeProject.employee_id == emp_id)}
        available = [p for p in dept_pool if p.id not in existing_ids]
        rng.shuffle(available)
        for project in available[:rng.choice([1, 2])]:
            start, end = _past_dates()
            session.add(EmployeeProject(
                employee_id=emp_id, project_id=project.id, role=rng.choice(PROJECT_ROLES),
                start_date=start, end_date=end,
            ))
        topped_up += 1
    session.flush()
    return {"confidential_members": len(conf_member_ids), "topped_up": topped_up,
            "exempted_new_hires": exempted_new_hires}


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
# Training courses — the synthetic dataset SyntheticCertProvider reads.
#
# Entirely fabricated, same hard constraint as the rest of this file: no real
# Quadrant training records exist anywhere in this project. Once
# ENABLE_TRAINING_API_SYNC flips on, the other team's system supplies these
# statuses instead and this section stops being the source of truth.
# ---------------------------------------------------------------------------

# code, name, description
TRAINING_COURSES = [
    ("SEC-101", "Information Security Awareness",
     "Annual security basics: phishing, device hygiene, incident reporting."),
    ("CONDUCT-102", "Code of Conduct & Ethics",
     "Company conduct standards, conflicts of interest, and how to raise a concern."),
    ("SECDEV-210", "Secure Coding Practices",
     "Threat modelling, input validation, secrets handling, dependency risk."),
    ("FINCTRL-150", "Financial Controls Basics",
     "Segregation of duties, approval thresholds, and audit evidence."),
    ("LEAD-301", "Manager Essentials: Leading a Team",
     "First-line management: feedback, one-to-ones, and performance conversations."),
]

# course code -> (org_unit name or None, job_title_keyword or None,
#                 employment_type or None, note)
#
# The scoping clauses are what let a profile say "doesn't apply" instead of
# "not completed" — an Account Executive is not out of compliance for never
# having taken Secure Coding Practices.
#
# Scoped so every employee lands on exactly ONE or TWO courses: SEC-101 is
# company-wide, and the other four are keyed to divisions that don't overlap,
# so nothing can stack a third onto anyone. Real compliance training doesn't
# partition itself this tidily — this is demo data, shaped to keep every
# profile's Training card short and readable rather than to model an actual
# training programme.
#
# The four narrow rows also cover one scoping clause each, so the resolver is
# exercised end to end: division alone (SECDEV-210, FINCTRL-150), division +
# job title (LEAD-301), division + employment type (CONDUCT-102).
#
# Note LEAD-301's keyword: job_title is the only role signal in this schema,
# so a title substring is exactly as precise as titles are. Within Product it
# catches Product Managers, who have no reports — a real limitation of
# deriving role from title rather than a bug in the matcher. If it bites, the
# fix is a proper track taxonomy on the employee, which CourseRequirement is
# already shaped to accept.
#
# Divisions with no second row (People & Culture, Legal) get exactly one
# course, which is the point: one course everywhere is the floor, never zero.
TRAINING_REQUIREMENTS = [
    ("SEC-101", None, None, None, "Everyone, annually."),
    ("SECDEV-210", "Engineering", None, None, "Anyone in the Engineering division."),
    ("FINCTRL-150", "Finance", None, None, "Anyone in the Finance division."),
    ("LEAD-301", "Product", "Manager", None, "Product managers — division AND title must match."),
    ("CONDUCT-102", "Sales & Marketing", None, EmploymentType.fte,
     "Sales & Marketing employees — contractors sign an equivalent through their agency."),
]

# Weighted so the demo has a realistic spread rather than a uniform one:
# most people are compliant, a minority are mid-course, a few failed.
STATUS_WEIGHTS = [
    (CourseStatus.completed, 0.55),
    (CourseStatus.in_progress, 0.15),
    (CourseStatus.not_started, 0.20),
    (CourseStatus.failed, 0.10),
]

# Fraction of (employee, required course) pairs left with NO row at all.
# Distinct from a stored not_started: it exercises the "provider has no
# record for a course we expect" path, which is the one place not_started is
# legitimately inferred rather than reported.
NO_RECORD_RATE = 0.15

# Fraction of employees given a course they were never required to take, so
# the profile's expected/not-expected distinction has something to show.
EXTRA_COURSE_RATE = 0.07


def build_training_courses(session) -> dict[str, TrainingCourse]:
    courses: dict[str, TrainingCourse] = {}
    for code, name, description in TRAINING_COURSES:
        course = TrainingCourse(code=code, name=name, description=description, is_active=True)
        session.add(course)
        session.flush()
        courses[code] = course
    return courses


def build_course_requirements(session, courses, units) -> None:
    for code, unit_name, title_keyword, employment_type, note in TRAINING_REQUIREMENTS:
        session.add(CourseRequirement(
            course_id=courses[code].id,
            org_unit_id=units[unit_name].id if unit_name else None,
            job_title_keyword=title_keyword,
            employment_type=employment_type,
            note=note,
        ))
    session.flush()


def _roll_status() -> CourseStatus:
    roll = rng.random()
    cumulative = 0.0
    for status, weight in STATUS_WEIGHTS:
        cumulative += weight
        if roll < cumulative:
            return status
    return STATUS_WEIGHTS[-1][0]


def build_course_statuses(session, courses, units) -> dict:
    """One row per (employee, applicable course), minus a deliberate slice
    left with no row at all.

    Uses the same requirements resolver the API uses, rather than
    reimplementing the scoping rules here — if the two ever disagreed, the
    seeded data would be quietly testing the wrong thing.
    """
    from app.certifications.requirements import required_courses

    counts = {status: 0 for status, _ in STATUS_WEIGHTS}
    no_record = 0
    extra = 0
    applicable_pairs = 0
    all_codes = [code for code, _, _ in TRAINING_COURSES]

    for emp in ALL_EMPLOYEES:
        expected = required_courses(session, emp)
        expected_codes = {c.code for c in expected}
        applicable_pairs += len(expected)

        for course in expected:
            if rng.random() < NO_RECORD_RATE:
                no_record += 1
                continue
            status = _roll_status()
            counts[status] += 1
            session.add(_course_status_row(emp, course, status))

        # A course nobody required of them — shows up on the profile flagged
        # as not expected, never as an outstanding requirement.
        if rng.random() < EXTRA_COURSE_RATE:
            spare = [c for c in all_codes if c not in expected_codes]
            if spare:
                course = courses[rng.choice(spare)]
                extra += 1
                session.add(_course_status_row(emp, course, CourseStatus.completed))

    session.flush()
    return {"applicable_pairs": applicable_pairs, "by_status": counts,
            "no_record": no_record, "extra": extra}


def _course_status_row(emp: Employee, course: TrainingCourse, status: CourseStatus) -> EmployeeCourseStatus:
    attempted = None
    completed = None
    if status is not CourseStatus.not_started:
        attempted = date.today() - timedelta(days=rng.randint(5, 400))
    if status is CourseStatus.completed:
        completed = attempted
    return EmployeeCourseStatus(
        employee_id=emp.id, course_id=course.id, status=status,
        attempted_at=attempted, completed_at=completed,
        source="synthetic", last_synced_at=datetime.now(),
    )


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


def print_verification(session, ctx, specials, offices, skills, topup_stats):
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

    conf_ids = {pid for (pid,) in session.query(Project.id)
                .filter(Project.classification == ProjectClassification.confidential)}
    conf_member_ids = {row.employee_id for row in session.query(EmployeeProject.employee_id)
                       .filter(EmployeeProject.project_id.in_(conf_ids)).distinct()}
    employees_by_id = {e.id: e for e in ALL_EMPLOYEES}
    bare_non_new_hire = []
    for emp_id in conf_member_ids:
        has_completed_noncon = session.query(EmployeeProject).join(
            Project, EmployeeProject.project_id == Project.id
        ).filter(
            EmployeeProject.employee_id == emp_id,
            Project.classification != ProjectClassification.confidential,
            EmployeeProject.end_date.isnot(None),
        ).first() is not None
        if not has_completed_noncon and tenure_days(employees_by_id[emp_id]) >= NEW_HIRE_DAYS:
            bare_non_new_hire.append(emp_id)
    status = "PASS" if not bare_non_new_hire else "FAIL"
    print(f"\n[{status}] Confidential-project members also have non-confidential history: "
          f"{len(conf_member_ids)} members, {topup_stats['topped_up']} topped up, "
          f"{topup_stats['exempted_new_hires']} exempted as new hires (empty history explained by join date)")
    if bare_non_new_hire:
        print(f"        BARE (not new hires, still no completed non-confidential project): {bare_non_new_hire}")

    portfolio_counts = session.execute(
        select(EmployeeProject.employee_id, EmployeeProject.end_date)
    ).all()
    completed_n: dict[str, int] = {}
    current_n: dict[str, int] = {}
    for emp_id, end_date in portfolio_counts:
        bucket = completed_n if end_date is not None else current_n
        bucket[emp_id] = bucket.get(emp_id, 0) + 1
    in_target_range = sum(1 for e in ALL_EMPLOYEES if 1 <= completed_n.get(e.id, 0) <= 4)
    zero_completed = sum(1 for e in ALL_EMPLOYEES if completed_n.get(e.id, 0) == 0)
    over_two_current = sum(1 for e in ALL_EMPLOYEES if current_n.get(e.id, 0) > 2)
    status = "PASS" if in_target_range / len(ALL_EMPLOYEES) >= 0.8 and over_two_current == 0 else "FAIL"
    print(f"\n[{status}] Realistic project portfolio distribution:")
    print(f"        {in_target_range}/{len(ALL_EMPLOYEES)} employees have 1-4 completed projects "
          f"({in_target_range / len(ALL_EMPLOYEES) * 100:.0f}%)")
    print(f"        {zero_completed} have zero completed projects (expected: mostly new hires)")
    print(f"        {over_two_current} exceed the 0-2 current-project target (expected: 0)")

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


def print_people_data_verification(session, date_milestones: dict) -> None:
    print("\n" + "=" * 78)
    print("SALARY / DATE OF BIRTH / DATE MILESTONES")
    print("=" * 78)

    with_dob = sum(1 for e in ALL_EMPLOYEES if e.date_of_birth is not None)
    with_salary = sum(1 for e in ALL_EMPLOYEES if e.salary is not None)
    print(f"  date of birth on file : {with_dob}/{len(ALL_EMPLOYEES)} "
          f"({len(ALL_EMPLOYEES) - with_dob} deliberately blank)")
    print(f"  salary on file        : {with_salary}/{len(ALL_EMPLOYEES)} "
          f"({len(ALL_EMPLOYEES) - with_salary} contractors, paid via agency)")

    ages = [int((date.today() - e.date_of_birth).days / 365.25)
            for e in ALL_EMPLOYEES if e.date_of_birth is not None]
    print(f"  age range             : {min(ages)}-{max(ages)} (mean {sum(ages) / len(ages):.0f})")

    youngest_at_hire = min(
        int((e.hire_date - e.date_of_birth).days / 365.25)
        for e in ALL_EMPLOYEES if e.date_of_birth is not None
    )
    print(f"  youngest age at hire  : {youngest_at_hire} "
          f"({'ok' if youngest_at_hire >= 18 else 'CONSTRAINT VIOLATED'})")

    by_currency: dict[str, list] = {}
    for e in ALL_EMPLOYEES:
        if e.salary is not None:
            by_currency.setdefault(e.salary_currency, []).append(e.salary)
    print("  salary by currency:")
    for currency, values in sorted(by_currency.items()):
        lo, hi = min(values), max(values)
        print(f"        {currency}  n={len(values):3d}  {lo:>12,.0f} - {hi:>12,.0f}")

    print(f"\n  birthdays falling today ({date.today().isoformat()}):")
    for name, dob in date_milestones["birthdays_today"]:
        print(f"        {name:24s} born {dob}")
    print("  milestone anniversaries today:")
    for name, years in date_milestones["anniversaries_today"]:
        print(f"        {name:24s} {years} year{'s' if years != 1 else ''}")

    it_count = sum(1 for e in ALL_EMPLOYEES if EMPLOYEE_SKILL_DEPT.get(e.id) == "IT Operations")
    print(f"\n  IT Operations headcount: {it_count}")
    print("\n" + "=" * 78)


def print_training_verification(stats: dict) -> None:
    print("\n" + "=" * 78)
    print("TRAINING COURSES (synthetic provider dataset)")
    print("=" * 78)
    print(f"  applicable (employee, required course) pairs : {stats['applicable_pairs']}")
    total_rows = sum(stats["by_status"].values())
    for status, count in stats["by_status"].items():
        pct = (count / total_rows * 100) if total_rows else 0
        print(f"        {status.value:14s} {count:5d} rows ({pct:5.1f}%)")
    print(f"  left with no record at all                   : {stats['no_record']} "
          "(reads as not_started — the inferred case)")
    print(f"  non-required courses taken anyway            : {stats['extra']} "
          "(shown flagged, never as an outstanding requirement)")
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
        date_milestones = inject_date_milestones(session)
        assign_skills(session, skills, ctx)
        _projects, membership_state = build_flagship_projects(session, units, ctx)
        project_pool = build_project_pool(session, units, ctx)
        build_portfolios(session, project_pool, membership_state)
        topup_stats = top_up_confidential_members(session, project_pool)
        build_certifications(session, skills)
        courses = build_training_courses(session)
        build_course_requirements(session, courses, units)
        training_stats = build_course_statuses(session, courses, units)
        session.commit()
        print(f"Seeded {len(ALL_EMPLOYEES)} employees, {len(offices)} offices, "
              f"{len(units)} org units, {len(skills)} skills, {len(courses)} training courses.")
        print_verification(session, ctx, specials, offices, skills, topup_stats)
        print_training_verification(training_stats)
        print_people_data_verification(session, date_milestones)
    finally:
        session.close()


if __name__ == "__main__":
    main()
