"""The interface everything else in this project codes against.

Nothing outside app/certifications/ knows whether course statuses came from
a seeded table or from another team's HTTP API. The profile page, the
notification triggers, and the tests all depend on CertificationProvider and
CertStatus — never on a concrete implementation — which is what makes going
live a config flip rather than a refactor.

Same shape of decision as app/auth.py's dev-vs-Entra split: one interface,
two providers, a config value choosing between them, and every caller
downstream unaware of which one is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from app.models.enums import CourseDisplayStatus, CourseStatus, display_status


class CertProviderUnavailable(RuntimeError):
    """The provider could not answer — timeout, transport error, auth
    failure, malformed response.

    Raised, never swallowed into a status value. A failed lookup is NOT
    `not_started`: "we couldn't reach the training system" and "this person
    has never opened the course" are different facts, and conflating them
    would send a real employee a real "you haven't started X yet" reminder
    because someone else's service was down for 30 seconds. Callers decide
    what to do with the exception (the profile omits the section; the
    notification pipeline raises nothing at all), but nobody gets to turn it
    into a status.
    """


class UnknownCourse(LookupError):
    """The course id isn't in the catalogue at all.

    Kept distinct from CertProviderUnavailable: "no such course" is a caller
    bug or a stale requirement row, and retrying won't fix it — whereas an
    unavailable provider is a transient fact about the network. Also
    distinct from not_started, for the same reason as above.
    """


@dataclass(frozen=True)
class CertStatus:
    """One employee's status on one course, as reported by a provider.

    `course_id` is the provider-facing external id (TrainingCourse.code),
    not our internal integer PK — the interface is deliberately expressed in
    the ids the other team's system uses.
    """

    employee_id: str
    course_id: str
    course_name: str
    status: CourseStatus
    attempted_at: date | None = None
    completed_at: date | None = None

    # Which provider produced this — "synthetic" or "training_api". Carried
    # so a demo screenshot can never be mistaken for live data.
    source: str = "synthetic"

    @property
    def display_status(self) -> CourseDisplayStatus:
        """The two-value derivation for user-facing copy. The four-value
        `status` above stays intact underneath it — this is a projection,
        not a replacement."""
        return display_status(self.status)


@runtime_checkable
class CertificationProvider(Protocol):
    """A source of truth for course completion.

    Implementations must:
      - return a CertStatus with status=not_started for a course the
        employee simply has no record against (a genuine, known-good "never
        touched it"),
      - raise CertProviderUnavailable when they cannot answer at all, and
      - raise UnknownCourse for a course id the catalogue doesn't have.

    Those three cases are the whole contract. Everything else about how the
    data is fetched is the implementation's business.
    """

    #: Human-readable provider name, surfaced in logs and the API payload.
    name: str

    def get_status(self, employee_id: str, course_id: str) -> CertStatus:
        ...

    def list_statuses(self, employee_id: str) -> list[CertStatus]:
        ...
