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
from app.vocabulary import (
    SNAPPABLE_FIELDS, _load_vocab, _snap_one, snap, snap_tool_arguments, validate,
)

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
# filter_groups -- bounded DNF. Each group gets the identical per-filter
# structural check as plan.filters; an empty group is its own rejection
# (see app/query_plan.py's PeopleQuery docstring for why: it would compile
# to "OR unconditionally true").
# ---------------------------------------------------------------------------

def test_valid_filter_groups_plan_passes():
    result = validate(PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="skills", op="contains", value="Kubernetes")],
        [Filter(field="org_unit", op="eq", value="Cloud Operations Team")],
    ]))
    assert result.valid is True
    assert result.errors == []


def test_empty_group_is_rejected():
    result = validate(PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
        [],
    ]))
    assert result.valid is False
    assert any("filter_groups" in e and "at least one filter" in e for e in result.errors)


def test_illegal_operator_inside_a_group_is_rejected():
    # Same rule as test_illegal_operator_for_field_is_rejected, just for a
    # filter living inside a group instead of the flat filters list -- a
    # group isn't a different vocabulary, just a different combinator.
    result = validate(PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="job_title", op="eq", value="x")],
    ]))
    assert result.valid is False
    assert any("job_title" in e for e in result.errors)


def test_illegal_operator_in_the_second_group_specifically_is_rejected():
    # Proves every group is checked, not just group 0.
    result = validate(PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
        [Filter(field="job_title", op="eq", value="x")],
    ]))
    assert result.valid is False
    assert any("job_title" in e for e in result.errors)


def test_unlabelled_field_inside_a_group_is_rejected():
    result = validate(PeopleQuery.model_construct(
        select=["id"], filters=[], order_by=None, limit=None,
        filter_groups=[[Filter.model_construct(field="personal_mobile", op="eq", value="x")]],
    ))
    assert result.valid is False
    assert any("personal_mobile" in e and "unlabelled" in e for e in result.errors)


def test_filter_groups_and_flat_filters_errors_both_reported():
    result = validate(PeopleQuery(
        select=["id"],
        filters=[Filter(field="job_title", op="eq", value="x")],
        filter_groups=[[Filter(field="job_title", op="eq", value="y")]],
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


def test_snap_resolves_values_inside_filter_groups(db_session):
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="platform engineering")],
        [Filter(field="office", op="eq", value="Tset HQ")],
    ])
    snapped, notes = snap(db_session, plan)
    assert snapped.filter_groups[0][0].value == "Platform Engineering"
    assert snapped.filter_groups[1][0].value == "Test HQ"
    assert {n.field for n in notes} == {"org_unit", "office"}


def test_snap_leaves_filter_groups_shape_untouched_when_nothing_to_snap(db_session):
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="availability_status", op="eq", value="available")],
    ])
    snapped, notes = snap(db_session, plan)
    assert snapped.filter_groups == plan.filter_groups
    assert notes == []


def test_snap_does_not_mutate_the_original_plans_filter_groups(db_session):
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="platform engineering")],
    ])
    snap(db_session, plan)
    assert plan.filter_groups[0][0].value == "platform engineering"  # unchanged -- snap() returns a new plan


def test_all_snappable_fields_are_registered_as_filterable():
    from app.registry import REGISTRY

    for field_name in SNAPPABLE_FIELDS:
        assert field_name in REGISTRY
        assert REGISTRY[field_name].filterable


# ---------------------------------------------------------------------------
# snap_tool_arguments(): snap() only ever ran on a PeopleQuery, so
# search_people got vocabulary correction and find_people -- which the router
# picks far more often -- did not.
# ---------------------------------------------------------------------------

def test_a_plausible_but_wrong_org_unit_snaps_to_the_real_one(db_session):
    """Golden eval t2-07: the model emits "Cloud Infrastructure" for a
    department actually called "Infrastructure", find_people's org_unit
    resolution returns nothing, and it hard-empties -- 0 results for a
    question with real answers."""
    snapped, _ = snap_tool_arguments(db_session, {"skill": "Terraform", "org_unit": "Engineerin"})
    assert snapped["org_unit"] == "Engineering"


def test_an_already_correct_value_is_left_exactly_alone(db_session):
    args = {"org_unit": "Engineering", "skill": "Terraform"}
    snapped, _ = snap_tool_arguments(db_session, args)
    assert snapped == args


def test_an_unresolvable_value_is_kept_so_the_result_stays_honestly_empty(db_session):
    """"do we have anyone who knows Quantum Computing" must still be
    answerable with "no" -- snapping it to the nearest real skill would turn
    an honest empty result into a confidently wrong one."""
    snapped, notes = snap_tool_arguments(db_session, {"skill": "Underwater Basket Weaving"})
    assert snapped["skill"] == "Underwater Basket Weaving"
    assert [n.resolved for n in notes if n.field == "skill"] == [None]


def test_language_is_deliberately_not_snapped(db_session):
    """find_related_language_speakers already handles a language miss, and
    handles it better: it offers speakers of a related language and says so.
    Snapping would replace that with a silent guess."""
    snapped, _ = snap_tool_arguments(db_session, {"language": "Telugu"})
    assert snapped["language"] == "Telugu"


def test_non_string_and_absent_values_are_untouched(db_session):
    args = {"available": True, "level": "Expert", "name": None}
    snapped, notes = snap_tool_arguments(db_session, args)
    assert snapped == args
    assert notes == []


# ---------------------------------------------------------------------------
# A snap must be a CLEAR winner, not merely above the threshold. This
# vocabulary has a common suffix -- 60 of 75 org units end in "Team" -- so
# WRatio's partial/token passes score all of them alike against any
# "<something> Team" input.
# ---------------------------------------------------------------------------

def test_a_plausible_team_name_that_does_not_exist_does_not_snap(db_session):
    """Measured before the margin: "Payments Team", "Search Team", "Growth
    Team" and "Security Team" all snapped to "Machine Learning Team", and
    "Billing API Team" to "Product Management Team A" -- silently answering
    a different question, which is the one thing snapping must never do."""
    vocab = _load_vocab(db_session, "org_unit")
    for invented in ("Payments Team", "Search Team", "Growth Team", "Security Team"):
        assert _snap_one(invented, vocab) is None, invented


def test_a_real_near_miss_still_snaps(db_session):
    """A genuine near-miss shares the DISTINCTIVE token rather than the
    common one, so it still has a clear winner."""
    vocab = _load_vocab(db_session, "org_unit")
    assert _snap_one("Engineerin", vocab) == "Engineering"


def test_skill_typos_are_unaffected_by_the_margin(db_session):
    """Skill names share no common suffix, so the margin never bites here --
    pinned so a future tightening of it cannot silently break typo
    correction."""
    vocab = _load_vocab(db_session, "skills")
    assert _snap_one("Terrafrom", vocab) == "Terraform"
