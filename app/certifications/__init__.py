"""Certification tracking, behind an internal interface.

    base.py          CertificationProvider (the Protocol) + CertStatus (the DTO)
    synthetic.py     SyntheticCertProvider — seeded fake data, powers the demo
    training_api.py  TrainingApiProvider — SHAPE ONLY, never wired (see its
                     module docstring for the three open questions)
    factory.py       get_provider(): picks one, gated on ENABLE_TRAINING_API_SYNC
    requirements.py  which courses are expected of whom (our side, not theirs)
    service.py       joins the two into what a profile renders

Import from this package, not from its modules, wherever practical — the
point of the layout is that callers depend on the interface and never on
which implementation happens to be active.
"""
from app.certifications.base import (
    CertificationProvider,
    CertProviderUnavailable,
    CertStatus,
    UnknownCourse,
)
from app.certifications.factory import get_provider
from app.certifications.requirements import required_courses
from app.certifications.service import (
    LocalStatusWritesDisabled,
    employee_training_status,
    record_course_status,
)
from app.certifications.synthetic import SyntheticCertProvider

__all__ = [
    "CertProviderUnavailable",
    "CertStatus",
    "CertificationProvider",
    "LocalStatusWritesDisabled",
    "SyntheticCertProvider",
    "UnknownCourse",
    "employee_training_status",
    "get_provider",
    "record_course_status",
    "required_courses",
]
