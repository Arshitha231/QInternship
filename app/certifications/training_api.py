"""TrainingApiProvider — SHAPE ONLY. Not wired, not runnable, not tested
against anything real.

This file exists so that going live is a config flip plus filling in three
clearly-marked bodies, rather than a design exercise under time pressure. It
is never imported while ENABLE_TRAINING_API_SYNC is false — app/
certifications/factory.py imports the module inside the enabled branch, so
with the flag off this code is not merely unused, it is not in the call path
at all.

=============================================================================
OPEN QUESTIONS — confirm with the training-courses team before implementing
=============================================================================

1. JOIN KEY: employee id or work email?
   We hold both. Their system almost certainly keys on whatever their
   sign-in uses, which is probably email — but our employee ids are stable
   across a name change and email is not, so email as the join key means a
   marriage or a rebrand silently orphans someone's training history.
   Preference: their system stores our `employees.id` as an external id and
   we join on that; email as a fallback lookup only. Whichever we agree,
   only `_employee_key()` below changes.

2. PUSH OR PULL: webhook or poll?
   Pull (this class, called per profile view) is simplest and always fresh,
   but puts their API on our page-load path and gives us no event to hang
   notifications off — notifications need a *transition*, which a pull-only
   design can only detect by diffing against a cached previous status.
   Push (they POST status changes to us) gives a real event, survives their
   downtime better, and is what the notification triggers actually want.
   Preference: push for notifications, pull for backfill/reconciliation.
   Note the notification pipeline (app/notifications.py) is already written
   against a (previous, current) transition, so a webhook handler is a thin
   route that calls the existing service function — no rework either way.

3. TIMEOUT / ERROR SEMANTICS: what does a failed call mean?
   Settled on our side, needs stating to theirs: a timeout or 5xx maps to
   CertProviderUnavailable and NOTHING ELSE. It must never degrade to
   not_started — that would mail a real employee "you haven't started X yet"
   because their service blipped, and would tell that employee's entire
   management chain about it. Also needs agreeing: is a 404 for an
   employee/course pair "no record, genuinely not started" or "unknown
   employee, something is wrong with our join key"? Those want opposite
   handling and the status code alone can't distinguish them.

Related, smaller, same conversation: their status vocabulary. Ours is
not_started | in_progress | failed | completed. If they report, say,
"enrolled" or "expired", _parse_status() below is where the mapping is
pinned down — and an unrecognised value must raise, not silently become
not_started.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.certifications.base import CertStatus
from app.models import Employee


class TrainingApiProvider:
    """HTTP client against the training-courses team's API.

    Constructed only by the factory, only when ENABLE_TRAINING_API_SYNC is
    true. The db session is here for one reason: resolving our employee id
    to whatever join key we agree on (open question 1).
    """

    name = "training_api"

    def __init__(self, db: Session, *, base_url: str, api_key: str, timeout: float) -> None:
        self._db = db
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout

    # -- interface ---------------------------------------------------------

    def get_status(self, employee_id: str, course_id: str) -> CertStatus:
        # Shape of the call, for review with the other team:
        #   GET {base_url}/v1/enrollments?employee={key}&course={course_id}
        #   Authorization: Bearer {api_key}   (auth pattern unconfirmed —
        #   could equally be an api-key header like Azure Search uses)
        # then _parse_status() on the response body.
        raise NotImplementedError(
            "TrainingApiProvider is a stub — see the open questions at the top of this module"
        )

    def list_statuses(self, employee_id: str) -> list[CertStatus]:
        #   GET {base_url}/v1/enrollments?employee={key}
        raise NotImplementedError(
            "TrainingApiProvider is a stub — see the open questions at the top of this module"
        )

    # -- internals, deliberately unfinished --------------------------------

    def _employee_key(self, employee_id: str) -> str:
        """Open question 1. Today it returns our id unchanged; if we agree on
        email, this becomes a lookup on Employee.work_email and nothing else
        in the codebase moves."""
        employee = self._db.get(Employee, employee_id)
        if employee is None:
            raise ValueError(f"No such employee {employee_id!r}")
        return employee.id

    def _parse_status(self, payload: dict) -> CertStatus:
        """Open question 3 (and the status-vocabulary note). Must raise on an
        unrecognised status value rather than defaulting — a default here is
        exactly how a service outage turns into a wrong reminder."""
        raise NotImplementedError
