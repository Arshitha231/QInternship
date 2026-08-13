"""Certification-tracking settings.

Read through functions, not module-level constants, deliberately: a constant
captured at import time can't be changed by a test (or by a runtime config
reload) without reimporting half the app, and the whole point of
ENABLE_TRAINING_API_SYNC is that flipping it is the *only* change needed to
go live. Same os.environ + load_dotenv convention as app/db.py and
app/auth.py — no new config framework.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

_TRUTHY = {"1", "true", "yes", "on"}

# Unlimited depth for notify_levels_up. Still bounded in practice by
# org_chart.MAX_DEPTH, which is the cycle guard, not a policy setting.
UNLIMITED_LEVELS = -1


def enable_training_api_sync() -> bool:
    """False (the default) means the real training API is not merely
    error-handled — TrainingApiProvider is never imported, constructed, or
    called. See app/certifications/factory.py, where the import itself lives
    inside the enabled branch.

    Flipping this to true is the entire go-live change on our side, once the
    open questions in app/certifications/training_api.py are answered.
    """
    return os.environ.get("ENABLE_TRAINING_API_SYNC", "false").strip().lower() in _TRUTHY


def notify_levels_up() -> int:
    """How far up the reporting chain a status resolution is reported.

    -1 (the default) is unlimited: every level, which is the confirmed
    requirement today. It is a setting rather than a hardcoded "walk the
    whole chain" so that narrowing it later — to direct manager only, say,
    if the notification volume turns out to annoy the top of the tree — is a
    config change and not an edit to notification logic. 0 disables the
    management-facing trigger entirely while leaving the employee-facing one
    running.

    Note this governs who gets *told*. It does not govern who may *look*:
    profile visibility of training status is the full chain regardless (see
    app/permissions.py), so turning notifications down never silently
    revokes a manager's ability to check.
    """
    raw = os.environ.get("NOTIFY_LEVELS_UP", str(UNLIMITED_LEVELS)).strip()
    try:
        return int(raw)
    except ValueError:
        return UNLIMITED_LEVELS


def hr_org_unit_name() -> str:
    """Which org unit's people count as "HR" for notification purposes.

    There is no role column on Employee — roles arrive per request, from a dev
    header or an Entra app-role claim (app/auth.py), and a scheduled sweep has
    no request to read one from. The org tree is the only durable signal we
    own, so HR recipients are resolved as "everyone in this unit or below it".

    A setting rather than a constant because the unit's name is a fact about
    one company's org chart, not about this code: rename the division and the
    birthday notifications shouldn't silently stop going out.

    Defaults to the HR Operations department rather than the People & Culture
    division above it. The division also contains Talent Acquisition, and
    recruiters are not the audience for "it's someone's 10-year anniversary" —
    including them roughly doubled the volume for no one's benefit. Set this
    to "People & Culture" to notify the whole division.
    """
    return os.environ.get("HR_ORG_UNIT_NAME", "HR Operations")


# --- Training API connection settings -------------------------------------
# Shape only. Nothing reads these unless enable_training_api_sync() is true.

def training_api_base_url() -> str:
    return os.environ.get("TRAINING_API_BASE_URL", "").rstrip("/")


def training_api_key() -> str:
    return os.environ.get("TRAINING_API_KEY", "")


def training_api_timeout_seconds() -> float:
    try:
        return float(os.environ.get("TRAINING_API_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5.0
