"""Unit tests for app/registry_view.py — a read-only HR screen over
app/registry.py's access-control model. No HTTP, no model call: every
assertion here is either a direct equivalence against app.registry.is_visible()
itself (so this can never pass while disagreeing with what actually
enforces access) or a check against REGISTRY/IGNORED_COLUMNS data, same
"equivalence over hand-written expectation" discipline as test_registry.py.
"""
from __future__ import annotations

import pytest

from app.auth import AuthenticatedUser
from app.models import AuditLog
from app.permissions import SELF_ONLY_FIELDS
from app.registry import IGNORED_COLUMNS, REGISTRY, Sensitivity, is_visible
from app.registry_view import IGNORED_COLUMN_DISPLAY, RegistryViewForbidden, get_registry_view

HR = AuthenticatedUser(id="registry-view-hr", role="hr", name="HR Caller")
EMPLOYEE = AuthenticatedUser(id="registry-view-employee", role="employee", name="Employee Caller")
MANAGER = AuthenticatedUser(id="registry-view-manager", role="manager", name="Manager Caller")
IT = AuthenticatedUser(id="registry-view-it", role="it", name="IT Caller")

_ROLES = ("employee", "manager", "hr", "it")


def _expected_columns() -> list[tuple[str, str, str]]:
    """(label, role, view_mode) computed independently from is_visible()
    itself — not imported from app.registry_view — so this is a genuine
    equivalence check, not the module testing its own arithmetic."""
    columns = []
    for role in _ROLES:
        vec = {
            mode: tuple(is_visible(name, role, mode) for name in REGISTRY)
            for mode in ("employee", "work")
        }
        if vec["employee"] == vec["work"]:
            columns.append((role, role, "employee"))
        else:
            columns.append((f"{role}·employee", role, "employee"))
            columns.append((f"{role}·work", role, "work"))
    return columns


# ---------------------------------------------------------------------------
# HR-only gate
# ---------------------------------------------------------------------------

def test_forbidden_for_employee(db_session):
    with pytest.raises(RegistryViewForbidden):
        get_registry_view(db_session, EMPLOYEE)


def test_forbidden_for_manager(db_session):
    with pytest.raises(RegistryViewForbidden):
        get_registry_view(db_session, MANAGER)


def test_forbidden_for_it(db_session):
    with pytest.raises(RegistryViewForbidden):
        get_registry_view(db_session, IT)


def test_hr_succeeds_and_writes_one_audit_row(db_session):
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    get_registry_view(db_session, HR)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1


# ---------------------------------------------------------------------------
# Columns: computed, not configured
# ---------------------------------------------------------------------------

def test_columns_match_independent_is_visible_computation(db_session):
    expected = [label for label, _role, _mode in _expected_columns()]
    view = get_registry_view(db_session, HR)
    assert view.columns == expected


def test_columns_are_exactly_five_today(db_session):
    # Pinned against the CURRENT app.registry.ALLOWED_SENSITIVITY, not a
    # bare assumption — if that table ever changes so another role's modes
    # diverge, test_columns_match_independent_is_visible_computation still
    # passes (it stays honest) while this one will need updating, which is
    # the point: it's a tripwire on the shape of the permission model today.
    view = get_registry_view(db_session, HR)
    assert view.columns == ["employee", "manager", "hr·employee", "hr·work", "it"]


# ---------------------------------------------------------------------------
# Field rows: direct equivalence against is_visible()
# ---------------------------------------------------------------------------

def test_every_field_visibility_matches_is_visible_directly(db_session):
    columns = _expected_columns()
    view = get_registry_view(db_session, HR)
    by_name = {f.name: f for f in view.fields}

    assert set(by_name) == set(REGISTRY)
    for name in REGISTRY:
        field = by_name[name]
        for label, role, mode in columns:
            assert field.visible_by[label] == is_visible(name, role, mode), (
                f"{name}.visible_by[{label!r}] disagrees with is_visible({name!r}, {role!r}, {mode!r})"
            )


def test_field_metadata_matches_registry(db_session):
    view = get_registry_view(db_session, HR)
    by_name = {f.name: f for f in view.fields}
    for name, spec in REGISTRY.items():
        field = by_name[name]
        assert field.type == spec.type
        assert field.filterable == spec.filterable
        assert field.derived_from == list(spec.derived_from)
        expected_sensitivity = spec.sensitivity.value if spec.sensitivity is not None else None
        assert field.sensitivity == expected_sensitivity


# ---------------------------------------------------------------------------
# DERIVED_HR: empty today, and the note says so
# ---------------------------------------------------------------------------

def test_derived_hr_tier_is_currently_empty(db_session):
    view = get_registry_view(db_session, HR)
    assert all(f.sensitivity != Sensitivity.DERIVED_HR.value for f in view.fields)


def test_derived_hr_note_reflects_the_empty_tier(db_session):
    view = get_registry_view(db_session, HR)
    # Consistency, not wording: since no field is tagged, the note must not
    # claim one is.
    assert "Tagged on:" not in view.derived_hr_note
    assert view.derived_hr_note.strip()


# ---------------------------------------------------------------------------
# IGNORED_COLUMNS: reasons as data, shown in their HR-plain-language form
# ---------------------------------------------------------------------------

def test_ignored_columns_rendered_with_plain_language_reasons(db_session):
    # Equivalence against IGNORED_COLUMN_DISPLAY, not IGNORED_COLUMNS -- the
    # screen shows the HR wording, never the engineer-facing one, per
    # get_registry_view's IGNORED_COLUMN_DISPLAY.get(name, reason) fallback.
    view = get_registry_view(db_session, HR)
    by_name = {c.name: c.reason for c in view.ignored_columns}
    assert by_name == IGNORED_COLUMN_DISPLAY
    assert len(by_name) == len(IGNORED_COLUMNS)
    for reason in by_name.values():
        assert reason.strip()


def test_ignored_column_display_covers_every_ignored_column():
    # Structural drift guard only -- catches a column added/removed from one
    # dict and not the other. A reason changing on one side without the
    # other is not something this (or anything) can catch mechanically; see
    # app/registry_view.py's module docstring.
    assert set(IGNORED_COLUMN_DISPLAY) == set(IGNORED_COLUMNS)


# ---------------------------------------------------------------------------
# personal_mobile / SELF_ONLY_FIELDS: the two-layer (RBAC + ABAC) note
# ---------------------------------------------------------------------------

def test_abac_note_present_on_exactly_the_expected_fields(db_session):
    view = get_registry_view(db_session, HR)
    expected = {"personal_mobile"} | set(SELF_ONLY_FIELDS)
    noted = {f.name for f in view.fields if f.abac_note}
    assert noted == expected


def test_abac_note_absent_elsewhere(db_session):
    view = get_registry_view(db_session, HR)
    expected = {"personal_mobile"} | set(SELF_ONLY_FIELDS)
    for f in view.fields:
        if f.name not in expected:
            assert f.abac_note is None


# ---------------------------------------------------------------------------
# Schema-completeness header
# ---------------------------------------------------------------------------

def test_schema_check_is_fully_accounted_for(db_session):
    view = get_registry_view(db_session, HR)
    assert view.schema_check.registered_count == len(REGISTRY)
    assert view.schema_check.ignored_count == len(IGNORED_COLUMNS)
    assert view.schema_check.unaccounted_count == 0
    assert view.schema_check.unaccounted == []


# ---------------------------------------------------------------------------
# The two HR-facing notes and the methodology statement are present
# ---------------------------------------------------------------------------

def test_fixed_notes_are_present_and_nonempty(db_session):
    view = get_registry_view(db_session, HR)
    assert view.methodology.strip()
    assert view.filterable_note.strip()
    assert view.results_scoping_note.strip()
