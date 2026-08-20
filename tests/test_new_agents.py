"""Routing + basic behaviour for the four new agents."""
from app.tool_calling import ResolvedToolCall, _deterministic_resolve


def test_coverage_routes_deterministically():
    turn = _deterministic_resolve("Who's covering for Sean Wilson?")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="find_coverage", arguments={"person": "Sean Wilson"})


def test_escalation_project_routes_deterministically():
    turn = _deterministic_resolve("Escalation path for the Billing API")
    assert turn is not None
    assert turn.tool_call.name == "find_escalation"
    assert turn.tool_call.arguments.get("project") == "Billing API"


def test_escalation_person_if_stuck_routes():
    turn = _deterministic_resolve("who should I escalate to if Diego Hernandez is stuck")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="find_escalation", arguments={"person": "Diego Hernandez"})


def test_training_self_routes():
    turn = _deterministic_resolve("What training should I take next?")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="recommend_training", arguments={"person": "self"})


def test_brief_person_routes():
    turn = _deterministic_resolve("Brief me on Diego Hernandez")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="brief_person", arguments={"person": "Diego Hernandez"})


def test_brief_does_not_steal_project_questions():
    assert _deterministic_resolve("tell me about the Billing API") is None


def test_skill_training_routes_to_find_skill_trainees():
    turn = _deterministic_resolve("Who should be trained next for Terraform / Kubernetes?")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="find_skill_trainees", arguments={"skills": ["Terraform", "Kubernetes"]})


def test_skill_learn_phrasing_routes():
    turn = _deterministic_resolve("Who should learn Terraform next?")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="find_skill_trainees", arguments={"skills": ["Terraform"]})


def test_referring_followup_replays_prior_tool(db_session):
    """'which of those are available?' reuses the prior plan instead of
    last-resort find_people(query=...)."""
    from app.auth import AuthenticatedUser
    from app.schemas import HistoryTurn
    from app.tool_calling import answer

    caller = AuthenticatedUser(id="hr-1", role="hr", name="HR")
    # Seed a tiny prior plan that returns people the fixture already has.
    history = [HistoryTurn(
        message="Who knows Site Reliability Engineering?",
        tool_call="find_people",
        arguments={"skill": "Site Reliability Engineering"},
    )]
    raw = answer(
        db_session, caller, "which of those are available?",
        view_mode="work", history=history,
    )
    assert raw["tool_call"] == "find_people"
    assert raw["arguments"] == {"skill": "Site Reliability Engineering"}
    assert raw["message"]
    assert "No one in the directory matched that" not in (raw["message"] or "")
    people = raw["result"] or []
    assert all(getattr(p, "availability_status", None) == "available" for p in people)


def test_find_skill_trainees_uses_projects_not_already_capable(db_session):
    """Upskill candidates come from related project work and must not already
    be Working/Expert on the target skill."""
    from datetime import date

    from app.auth import AuthenticatedUser
    from app.agent_tools import find_skill_trainees
    from app.models import (
        Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill,
    )
    from app.models.enums import (
        AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType,
        SkillCategory, SkillLevel, SkillSource,
    )
    from app.people import resolve_skill

    office = db_session.query(Office).first()
    unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").first()
    assert office and unit

    def ensure_skill(name: str) -> Skill:
        existing = resolve_skill(db_session, name)
        if existing:
            return existing
        sk = Skill(name=name, category=SkillCategory.technical, canonical_id=None)
        db_session.add(sk)
        db_session.flush()
        return sk

    terraform = ensure_skill("Terraform")
    aws = ensure_skill("AWS")
    k8s = ensure_skill("Kubernetes")

    trainee = Employee(
        id="upskill-trainee-1", directory_object_id=None, full_name="Upskill Trainee",
        preferred_name=None, job_title="Infra Engineer", org_unit_id=unit.id,
        office_id=office.id, manager_id=None, work_email="upskill.trainee@example.test",
        work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    expert = Employee(
        id="upskill-expert-1", directory_object_id=None, full_name="Upskill Expert",
        preferred_name=None, job_title="Infra Lead", org_unit_id=unit.id,
        office_id=office.id, manager_id=None, work_email="upskill.expert@example.test",
        work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2018, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    db_session.add_all([trainee, expert])
    db_session.flush()

    db_session.add(EmployeeSkill(
        employee_id=trainee.id, skill_id=aws.id, level=SkillLevel.working,
        source=SkillSource.self_reported, verified_at=None,
    ))
    db_session.add(EmployeeSkill(
        employee_id=trainee.id, skill_id=k8s.id, level=SkillLevel.working,
        source=SkillSource.self_reported, verified_at=None,
    ))
    db_session.add(EmployeeSkill(
        employee_id=expert.id, skill_id=terraform.id, level=SkillLevel.expert,
        source=SkillSource.confirmed, verified_at=None,
    ))
    db_session.add(EmployeeSkill(
        employee_id=expert.id, skill_id=aws.id, level=SkillLevel.expert,
        source=SkillSource.confirmed, verified_at=None,
    ))

    project = Project(
        name="Infrastructure Workflow Automation Upskill",
        type=ProjectType.project,
        description=(
            "Rebuilt the deployment pipeline with infrastructure-as-code, "
            "Kubernetes cluster automation, and cloud operations runbooks."
        ),
        owning_unit_id=unit.id, owner_id=expert.id,
        classification=ProjectClassification.internal,
    )
    db_session.add(project)
    db_session.flush()
    db_session.add(EmployeeProject(
        employee_id=trainee.id, project_id=project.id, role="Engineer",
        start_date=date(2024, 1, 1), end_date=None,
    ))
    db_session.add(EmployeeProject(
        employee_id=expert.id, project_id=project.id, role="Lead",
        start_date=date(2024, 1, 1), end_date=None,
    ))
    db_session.commit()

    caller = AuthenticatedUser(id="hr-1", role="hr", name="HR")
    results = find_skill_trainees(db_session, caller, skills=["Terraform"])
    ids = {p.id for p in results}
    assert "upskill-trainee-1" in ids
    assert "upskill-expert-1" not in ids
    match = next(p for p in results if p.id == "upskill-trainee-1")
    assert match.skill == "Terraform"
    assert "AWS" in match.adjacent_skills or "Kubernetes" in match.adjacent_skills
    assert match.project_name


def test_who_is_routes_to_find_people_by_name():
    turn = _deterministic_resolve("who is luca")
    assert turn is not None
    assert turn.tool_call == ResolvedToolCall(
        name="find_people", arguments={"name": "luca"})


def test_who_is_does_not_steal_manager_questions():
    turn = _deterministic_resolve("who is my manager")
    assert turn is not None
    assert turn.tool_call.name == "get_org_chain"


def test_followup_who_is_prefers_prior_result_person(db_session):
    """After a prior set that includes Luca Followup, 'who is luca' briefs
    that person instead of returning every Luca in the directory."""
    from datetime import date

    from app.auth import AuthenticatedUser
    from app.models import Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill
    from app.models.enums import (
        AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType,
        SkillCategory, SkillLevel, SkillSource,
    )
    from app.people import resolve_skill
    from app.schemas import HistoryTurn
    from app.tool_calling import answer

    office = db_session.query(Office).first()
    unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").first()
    assert office and unit

    def ensure_skill(name: str) -> Skill:
        existing = resolve_skill(db_session, name)
        if existing:
            return existing
        sk = Skill(name=name, category=SkillCategory.technical, canonical_id=None)
        db_session.add(sk)
        db_session.flush()
        return sk

    python = ensure_skill("Python")
    ensure_skill("SQL")

    luca = Employee(
        id="followup-luca-1", directory_object_id=None, full_name="Luca Followup",
        preferred_name=None, job_title="Data Engineer", org_unit_id=unit.id,
        office_id=office.id, manager_id=None, work_email="luca.followup@example.test",
        work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    db_session.add(luca)
    db_session.flush()
    db_session.add(EmployeeSkill(
        employee_id=luca.id, skill_id=python.id, level=SkillLevel.working,
        source=SkillSource.self_reported, verified_at=None,
    ))
    project = Project(
        name="Data SQL Upskill Fixture",
        type=ProjectType.project,
        description="SQL analytics pipeline and data engineering warehouse work.",
        owning_unit_id=unit.id, owner_id=luca.id,
        classification=ProjectClassification.internal,
    )
    db_session.add(project)
    db_session.flush()
    db_session.add(EmployeeProject(
        employee_id=luca.id, project_id=project.id, role="Engineer",
        start_date=date(2024, 1, 1), end_date=None,
    ))
    db_session.commit()

    caller = AuthenticatedUser(id="hr-1", role="hr", name="HR")
    history = [HistoryTurn(
        message="Who should be trained next for SQL?",
        tool_call="find_skill_trainees",
        arguments={"skills": ["SQL"]},
    )]
    raw = answer(db_session, caller, "who is luca", view_mode="work", history=history)
    assert raw["tool_call"] == "brief_person"
    assert raw["arguments"]["person"] == "Luca Followup"
    assert raw["message"]
    assert "No one in the directory matched" not in (raw["message"] or "")
