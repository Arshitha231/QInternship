"""HR-only, read-only rendering of app/registry.py's access-control model.

Strictly a viewer: every value here is either produced by calling
app.registry.is_visible() directly or read verbatim off REGISTRY /
IGNORED_COLUMNS — there is no second description of what's enforced, and
nothing in this module writes anything or mutates the registry. Same shape
as app/continuity.py: one HR-gate, one AuditLog row per call, typed
Pydantic return.

Two things worth knowing before touching this file:

`personal_mobile` and `salary`/`salary_currency`/`date_of_birth` each carry
an ABAC grant on top of (or instead of) their static REGISTRY sensitivity —
app.permissions.abac_extra_fields, entirely outside this registry. Read on
its own, is_visible() makes personal_mobile look unreachable by anyone
(sensitivity=None denies every role) and understates who sees the other
three (their real audience is "hr, OR the record's own subject" — REGISTRY
only knows about "hr"). The two note constants below are this module's one
hand-maintained exception to "never re-describe what's already enforced
elsewhere": three of the four fields they're attached to are matched via
app.permissions.SELF_ONLY_FIELDS, a real import, not a retyped list;
personal_mobile's manager-grant has no importable symbol of its own (it's a
literal string inside abac_extra_fields's body), so it's the one bare
literal here — flagged explicitly so a future change to abac_extra_fields is
the obvious place to notice this needs revisiting.

The column set is NEVER hardcoded — it's whatever calling is_visible()
across all 8 (role, view_mode) pairs actually produces, per role, once a
role's two modes are merged when identical. If app.registry.ALLOWED_SENSITIVITY
ever changes so another role's two modes diverge, this screen picks that up
on its own.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.db import engine
from app.models import AuditLog
from app.permissions import SELF_ONLY_FIELDS, ViewMode
from app.registry import IGNORED_COLUMNS, REGISTRY, Sensitivity, is_visible
from app.schemas import IgnoredColumnView, RegistryFieldView, RegistryView, SchemaCheck

_ROLES = ("employee", "manager", "hr", "it")
_VIEW_MODES: tuple[ViewMode, ...] = ("employee", "work")

_FILTERABLE_NOTE = (
    "Some fields are visible on a profile but can't be searched, filtered, or "
    "sorted — a request that tries is refused outright rather than partly "
    "answered, and that refusal can look exactly like the assistant not "
    "understanding the question."
)

_RESULTS_SCOPING_NOTE = (
    "Search and directory results are automatically limited to the people the "
    "caller is allowed to see — if someone gets fewer results than expected, "
    "that's this rule applying, not a search problem."
)

_METHODOLOGY = (
    "Columns and every cell in the grid are produced by calling "
    "app.registry.is_visible() for each role/mode pair across every registered "
    "field, then merging a role's two view modes into one column only when "
    "is_visible() agrees on all of them for that role — never configured by hand."
)

# The one bare literal in this module — see the module docstring's second
# paragraph for why personal_mobile can't be checked against an imported
# symbol the way the SELF_ONLY_FIELDS-based note below is.
_MANAGER_GRANT_NOTE = (
    "Not visible to any role above — reaches the caller only through "
    "app.permissions.abac_extra_fields, post-retrieval: the record's own "
    "subject, or their direct manager."
)

_SELF_ONLY_NOTE = (
    "The grid above is the RBAC floor only — app.permissions.abac_extra_fields "
    "additionally grants this to the record's own subject regardless of role."
)


class RegistryViewForbidden(Exception):
    """Raised for a non-hr caller — same shape as continuity.ContinuityForbidden."""


def _require_hr(caller: AuthenticatedUser) -> None:
    if caller.role != "hr":
        raise RegistryViewForbidden("The registry view is an HR-only screen")


def _write_audit(db: Session, caller: AuthenticatedUser, result_count: int) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action="registry_view.view", query_text="",
        result_count=result_count,
        fields_returned=json.dumps(["schema_check", "columns", "fields", "ignored_columns"]),
        timestamp=datetime.now(),
    ))
    db.commit()


def _schema_check() -> SchemaCheck:
    """Live re-derivation of what app.registry.assert_registry_covers_schema
    already checked at startup — inspected again here, not trusted from that
    past run, so this line is a proof rather than a claim."""
    inspector = sa_inspect(engine)
    actual_columns = {col["name"] for col in inspector.get_columns("employees")}
    covered = set(REGISTRY) | set(IGNORED_COLUMNS)
    unaccounted = sorted(actual_columns - covered)
    return SchemaCheck(
        registered_count=len(REGISTRY), ignored_count=len(IGNORED_COLUMNS),
        unaccounted_count=len(unaccounted), unaccounted=unaccounted,
    )


def _resolve_columns() -> list[tuple[str, str, ViewMode]]:
    """One entry per column that actually differs: (label, role, view_mode).

    Per role, both view modes collapse to a single column labeled by the
    role name alone when is_visible() agrees on every REGISTRY field for
    that role regardless of mode; otherwise the role gets two columns,
    "role·employee" and "role·work". A decision made by calling
    is_visible() itself, never hardcoded.
    """
    columns: list[tuple[str, str, ViewMode]] = []
    for role in _ROLES:
        vector_by_mode = {
            view_mode: tuple(is_visible(name, role, view_mode) for name in REGISTRY)
            for view_mode in _VIEW_MODES
        }
        if vector_by_mode["employee"] == vector_by_mode["work"]:
            columns.append((role, role, "employee"))
        else:
            for view_mode in _VIEW_MODES:
                columns.append((f"{role}·{view_mode}", role, view_mode))
    return columns


def _derived_hr_note() -> str:
    """Checked, not asserted — if a future change ever tags a real field
    Sensitivity.DERIVED_HR, this reflects it instead of repeating a stale
    claim that the tier is empty."""
    tagged = sorted(name for name, spec in REGISTRY.items() if spec.sensitivity is Sensitivity.DERIVED_HR)
    if not tagged:
        return (
            "No field is currently tagged this tier. Work-authorization/continuity "
            "data is kept out of this registry entirely rather than labeled and "
            "blocked at read time — it's never joined into the people-search "
            "pipeline at all, which is a stronger guarantee than a tag this table "
            "could still expose."
        )
    return f"Tagged on: {', '.join(tagged)}."


def _abac_note(name: str) -> str | None:
    if name == "personal_mobile":
        return _MANAGER_GRANT_NOTE
    if name in SELF_ONLY_FIELDS:
        return _SELF_ONLY_NOTE
    return None


def get_registry_view(db: Session, caller: AuthenticatedUser) -> RegistryView:
    _require_hr(caller)

    columns = _resolve_columns()
    column_labels = [label for label, _role, _view_mode in columns]

    fields = [
        RegistryFieldView(
            name=name,
            type=spec.type,
            sensitivity=spec.sensitivity.value if spec.sensitivity is not None else None,
            filterable=spec.filterable,
            derived_from=list(spec.derived_from),
            visible_by={
                label: is_visible(name, role, view_mode) for label, role, view_mode in columns
            },
            abac_note=_abac_note(name),
        )
        for name, spec in REGISTRY.items()
    ]

    ignored_columns = [
        IgnoredColumnView(name=name, reason=reason) for name, reason in IGNORED_COLUMNS.items()
    ]

    view = RegistryView(
        schema_check=_schema_check(),
        columns=column_labels,
        methodology=_METHODOLOGY,
        filterable_note=_FILTERABLE_NOTE,
        results_scoping_note=_RESULTS_SCOPING_NOTE,
        fields=fields,
        ignored_columns=ignored_columns,
        derived_hr_note=_derived_hr_note(),
    )
    _write_audit(db, caller, len(fields))
    return view
