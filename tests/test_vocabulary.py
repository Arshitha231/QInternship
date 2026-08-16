"""Phase 3 Round 1 (ARCHITECTURE_2.md §9): app/vocabulary.py's validate()
(reject on structure) and snap() (correct on values). snap() needs a real
DB vocabulary to resolve against -- uses the same conftest.py fixture data
(tests/conftest.py's `_database`) as every other test in the suite:
org units "Platform Engineering" / "Finance Operations", offices "Test HQ"
(Testville) / "Satellite Office" (Satellite City), skills "Terraform" /
"Power BI" / "Site Reliability Engineering" / "SRE" (technical), "French"
(language).
"""
import pytest

from app.query_plan import Filter, PeopleQuery
from app.vocabulary import SNAPPABLE_FIELDS, snap, validate

# ---------------------------------------------------------------------------
# validate() -- structural rejection only
# ---------------------------------------------------------------------------

def test_valid_plan_passes():
    plan = PeopleQuery(
        select=["id", "full_name", "org_unit"],
        filters=[Filter(field="skills", op="contains", value="Terraform")],
        order_by="full_name",
    )
    result = validate(plan)
    assert result.valid is True
    assert result.errors == []


def test_unlabelled_select_field_is_rejected():
    # personal_mobile is registered (every real column must be) but
    # sensitivity=None -- structurally can never appear in a plan for any
    # role, not a per-role permission question.
    result = validate(PeopleQuery(select=["id", "personal_mobile"]))
    assert result.valid is False
    assert any("personal_mobile" in e and "unlabelled" in e for e in result.errors)


def test_illegal_operator_for_field_is_rejected():
    # job_title is filterable (Piece 2) but its only legal op is "contains"
    # -- "eq" is still illegal for it, same rejection shape as a field with
    # no legal ops at all.
    result = validate(PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="eq", value="x")]))
    assert result.valid is False
    assert any("job_title" in e for e in result.errors)


def test_wrong_value_type_for_field_is_rejected():
    # org_unit is a str field; "eq" with a list value is structurally wrong.
    result = validate(PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="in", value="Infrastructure")]))
    assert result.valid is False
    assert any("org_unit" in e for e in result.errors)


def test_list_field_contains_takes_a_single_element_not_a_list():
    # skills is list[str], but `contains` asks "is this ONE name present" --
    # takes a plain str, not a list. A single-element list is still the
    # wrong shape for this operator.
    result = validate(PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value=["Terraform"])]))
    assert result.valid is False


def test_list_field_in_takes_a_list():
    result = validate(PeopleQuery(select=["id"], filters=[
        Filter(field="skills", op="in", value=["Terraform", "Kubernetes"])
    ]))
    assert result.valid is True


def test_order_by_on_non_filterable_field_is_rejected():
    result = validate(PeopleQuery(select=["id"], order_by="bio"))
    assert result.valid is False


def test_order_by_none_is_always_valid():
    result = validate(PeopleQuery(select=["id"]))
    assert result.valid is True


def test_multiple_errors_are_all_reported_not_just_the_first():
    result = validate(PeopleQuery(
        select=["personal_mobile"],
        filters=[Filter(field="job_title", op="eq", value="x")],
    ))
    assert result.valid is False
    assert len(result.errors) == 2


# ---------------------------------------------------------------------------
# snap() -- value correction against the real DB vocabulary. Same
# exact -> case-insensitive -> fuzzy -> unresolvable ladder
# test_name_resolution.py already covers for resolve_person_name, mirrored
# here for org_unit/office/skill/language.
# ---------------------------------------------------------------------------

def _snapped_value(notes, field_name):
    matches = [n for n in notes if n.field == field_name]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("field,value,expected", [
    ("org_unit", "Platform Engineering", "Platform Engineering"),
    ("office", "Test HQ", "Test HQ"),
    ("skills", "Terraform", "Terraform"),
    ("languages", "French", "French"),
])
def test_snap_exact_match(db_session, field, value, expected):
    plan = PeopleQuery(select=["id"], filters=[Filter(field=field, op="eq", value=value)])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == expected
    assert _snapped_value(notes, field).resolved == expected


@pytest.mark.parametrize("field,value,expected", [
    ("org_unit", "platform engineering", "Platform Engineering"),
    ("office", "test hq", "Test HQ"),
    ("skills", "terraform", "Terraform"),
    ("languages", "french", "French"),
])
def test_snap_case_insensitive_match(db_session, field, value, expected):
    plan = PeopleQuery(select=["id"], filters=[Filter(field=field, op="eq", value=value)])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == expected


def test_snap_office_resolves_by_city_name(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="office", op="eq", value="Testville")])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == "Testville"  # the city itself is a legitimate exact vocab hit


@pytest.mark.parametrize("field,typo,expected", [
    ("org_unit", "Platfrom Engineering", "Platform Engineering"),
    ("office", "Tset HQ", "Test HQ"),
    ("skills", "Terrafrom", "Terraform"),
])
def test_snap_fuzzy_typo_match(db_session, field, typo, expected):
    plan = PeopleQuery(select=["id"], filters=[Filter(field=field, op="eq", value=typo)])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == expected
    assert _snapped_value(notes, field).resolved == expected


@pytest.mark.parametrize("field", ["org_unit", "office", "skills", "languages"])
def test_snap_unresolvable_value_reports_and_keeps_original(db_session, field):
    plan = PeopleQuery(select=["id"], filters=[Filter(field=field, op="eq", value="Zzyzx Nonexistent Qqwrt")])
    snapped, notes = snap(db_session, plan)
    note = _snapped_value(notes, field)
    assert note.resolved is None
    # Unresolvable is reported, never silently dropped -- the original
    # value survives in the returned plan for the caller to see/explain.
    assert snapped.filters[0].value == "Zzyzx Nonexistent Qqwrt"


def test_snap_resolves_canonical_and_alias_skill_names_independently(db_session):
    # snap() resolves free text to a real DB value, not to a canonical
    # skill id -- "SRE" is itself a real row (an alias), so it snaps to
    # itself, not to "Site Reliability Engineering". Synonym resolution is
    # a separate concern (app.people.resolve_skill), untouched by this.
    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="eq", value="SRE")])
    snapped, _notes = snap(db_session, plan)
    assert snapped.filters[0].value == "SRE"


def test_snap_leaves_non_snappable_fields_untouched(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="availability_status", op="eq", value="available")])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == "available"
    assert notes == []


def test_snap_handles_a_list_value_element_by_element(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(
        field="org_unit", op="in",
        value=["platform engineering", "Platfrom Engineering", "Zzyzx Nonexistent"],
    )])
    snapped, notes = snap(db_session, plan)
    assert snapped.filters[0].value == ["Platform Engineering", "Platform Engineering", "Zzyzx Nonexistent"]
    assert len(notes) == 3
    assert notes[2].resolved is None


def test_snap_does_not_mutate_the_original_plan(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="eq", value="platform engineering")])
    snap(db_session, plan)
    assert plan.filters[0].value == "platform engineering"  # unchanged -- snap() returns a new plan


def test_all_snappable_fields_are_registered_as_filterable():
    from app.registry import REGISTRY

    for field_name in SNAPPABLE_FIELDS:
        assert field_name in REGISTRY
        assert REGISTRY[field_name].filterable
