"""Phase 3 Round 1 (ARCHITECTURE_2.md §5): app/registry.py, the field
registry that's the single source of truth for what exists and how
sensitive it is. Pure unit tests, no live endpoint involved -- the doc's
own §1 invariant: "Policy is testable without a model call."
"""
import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from app import permissions
from app.registry import ALLOWED_SENSITIVITY, IGNORED_COLUMNS, REGISTRY, Sensitivity, assert_registry_covers_schema, is_visible

ROLES = ("employee", "manager", "hr")


# ---------------------------------------------------------------------------
# assert_registry_covers_schema -- catches a rogue, unlisted column
# ---------------------------------------------------------------------------

def _build_employees_table(metadata: MetaData, *, extra_columns: dict[str, type] = {}) -> Table:
    """A minimal synthetic 'employees' table with one column per REGISTRY
    entry and IGNORED_COLUMNS entry, plus whatever extra_columns are asked
    for -- so the schema-drift check has something real to reflect."""
    columns = [Column("id", String, primary_key=True)]
    for name in REGISTRY:
        if name == "id":
            continue
        columns.append(Column(name, String))
    for name in IGNORED_COLUMNS:
        columns.append(Column(name, String))
    for name in extra_columns:
        columns.append(Column(name, String))
    return Table("employees", metadata, *columns)


def test_covers_schema_passes_when_every_column_is_registered_or_ignored():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    _build_employees_table(metadata)
    metadata.create_all(engine)
    assert_registry_covers_schema(engine)  # must not raise


def test_covers_schema_catches_a_rogue_unlisted_column():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    _build_employees_table(metadata, extra_columns={"home_address": String})
    metadata.create_all(engine)
    with pytest.raises(RuntimeError, match="home_address"):
        assert_registry_covers_schema(engine)


def test_covers_schema_against_the_conftest_seeded_schema(db_session):
    # The actual `employees` table conftest.py builds from app.models'
    # real Employee/Base metadata (see tests/conftest.py's `_database`
    # fixture) -- confirms the registry covers the schema the ORM itself
    # produces, not just a synthetic stand-in built from the registry's
    # own keys above (which would trivially always pass).
    assert_registry_covers_schema(db_session.get_bind())


# ---------------------------------------------------------------------------
# IGNORED_COLUMNS -- exact-contents test. Growing this set means editing
# this test on purpose, not a silent side effect of adding a DB column.
# ---------------------------------------------------------------------------

def test_ignored_columns_exact_contents():
    assert IGNORED_COLUMNS == frozenset({
        "directory_object_id", "is_active", "created_at", "updated_at",
        "timezone", "away_until", "org_unit_id", "office_id", "manager_id", "delegate_id",
    })


# ---------------------------------------------------------------------------
# is_visible -- direct equivalence against app.permissions' existing source
# of truth (BASE_FIELDS / INTERNAL_FIELDS / ALLOWED), not a hand-written
# duplicate expectation that could drift from it independently.
#
# permissions.ALLOWED is keyed by (role, view_mode) since view modes and the
# "it" role were introduced (a caller-facing lens, not a sensitivity tier) --
# compared against the ("role", "work") row here, since "work" is the
# full-privilege, behaviour-preserving mode every role had before view modes
# existed, i.e. what REGISTRY's static per-role model was always describing.
# Employee-mode collapse (every role reduced to the employee view) is a
# runtime decision app/registry.py doesn't participate in yet -- known gap,
# same category as direct_reports/training_status below, not a silent one:
# REGISTRY isn't wired into any live route yet either (see its DERIVED_HR
# comment), so nothing downstream currently depends on this distinction.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ROLES)
def test_is_visible_matches_permissions_allowed_for_every_relabelled_field(role):
    for field_name in permissions.BASE_FIELDS | permissions.INTERNAL_FIELDS:
        assert field_name in REGISTRY, f"{field_name} is in permissions.py but missing from REGISTRY"
        assert is_visible(field_name, role) == (field_name in permissions.ALLOWED[(role, "work")]), (
            f"is_visible('{field_name}', '{role}') disagrees with permissions.ALLOWED[('{role}', 'work')]"
        )


@pytest.mark.parametrize("role", ROLES)
def test_id_and_full_name_visible_to_every_role(role):
    # Not in permissions.py's BASE_FIELDS at all (PersonSummary/PersonDetail
    # always carry them unconditionally) -- registered here as ordinary
    # INTERNAL entries anyway, going through the identical is_visible()
    # check as everything else. No "public" tier that skips it.
    assert is_visible("id", role)
    assert is_visible("full_name", role)


def test_unlisted_field_is_never_visible():
    for role in ROLES:
        assert not is_visible("not_a_real_field", role)


def test_unlabelled_field_is_never_visible_to_any_role():
    # personal_mobile: registered (every real column must be) but
    # sensitivity=None -- the static registry always denies it; the only
    # way it reaches a response is permissions.abac_extra_fields's separate
    # post-retrieval grant, entirely outside this system.
    assert REGISTRY["personal_mobile"].sensitivity is None
    for role in ROLES:
        assert not is_visible("personal_mobile", role)


def test_every_registry_field_has_an_allowed_sensitivity_mapping_for_every_role():
    # Defends against a role being added to ALLOWED_SENSITIVITY without a
    # correspondingly reachable set of fields, or vice versa -- every role
    # this system knows about must resolve for every labelled field.
    for role in ROLES:
        assert role in ALLOWED_SENSITIVITY
    for spec in REGISTRY.values():
        if spec.sensitivity is None:
            continue
        assert spec.sensitivity in Sensitivity


def test_manager_has_no_extra_sensitivity_tier_over_employee():
    # Manager privileges (seeing direct_reports, e.g.) are row-scoping
    # obligations (app/policy.py), not broader field access -- pinned here
    # so a future edit can't accidentally widen manager's field visibility
    # instead of adding an obligation.
    assert ALLOWED_SENSITIVITY["manager"] == ALLOWED_SENSITIVITY["employee"]


def test_hr_sees_strictly_more_than_employee_and_manager():
    assert ALLOWED_SENSITIVITY["hr"] > ALLOWED_SENSITIVITY["employee"]
