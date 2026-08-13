"""Which provider is active — the one place that decision is made.

`ENABLE_TRAINING_API_SYNC` false (the default) means SyntheticCertProvider,
and means TrainingApiProvider is never imported, never constructed, never
called. The import below sits *inside* the enabled branch on purpose: with
the flag off, that module's code is not in the call path at all, so a broken
or half-finished stub cannot affect a demo, a test run, or production —
there is nothing to error-handle because there is nothing running.

Flipping the flag to true is the entire go-live change on our side.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import config
from app.certifications.base import CertificationProvider
from app.certifications.synthetic import SyntheticCertProvider


def get_provider(db: Session) -> CertificationProvider:
    """The active provider for this request.

    Per-request rather than a module-level singleton because a provider is
    bound to a database session (the synthetic one reads through it; the API
    one resolves join keys through it), and sessions are per-request here.
    """
    if config.enable_training_api_sync():
        # Imported here, not at module scope — see the module docstring.
        from app.certifications.training_api import TrainingApiProvider

        return TrainingApiProvider(
            db,
            base_url=config.training_api_base_url(),
            api_key=config.training_api_key(),
            timeout=config.training_api_timeout_seconds(),
        )
    return SyntheticCertProvider(db)
