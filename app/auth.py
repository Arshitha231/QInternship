"""Pluggable caller authentication.

Both providers resolve to the same AuthenticatedUser shape, so every route
downstream (find_people, get_person, ...) depends only on get_current_user
and never knows or cares whether the caller came in via a dev header or a
real Entra ID token. That's what makes swapping in real credentials later a
config change, not a code change.

AUTH_MODE selects the provider explicitly ("dev" or "entra"). Left unset, it
auto-detects: "entra" once ENTRA_TENANT_ID and ENTRA_CLIENT_ID are both
present, "dev" otherwise — so the API runs with zero Azure setup from day
one, per the project's credentials-arrive-late constraint.
"""
from __future__ import annotations

import os
import time
from typing import Literal

import httpx
from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel

# "it" is the IT department's role: the people who administer the directory
# itself. Like the other three it arrives per request (dev header or Entra
# app-role claim) and is never stored on Employee — see app/config.py's
# hr_org_unit_name for why the org tree is only ever the fallback signal, in
# contexts that have no request to read a claim from.
#
# It is a privileged role but deliberately not a superset of hr: it may edit
# project descriptions and review AI-extracted changes, and it may not read
# salaries. See app/permissions.py's ALLOWED table for the split.
Role = Literal["employee", "manager", "hr", "it"]
VALID_ROLES: set[str] = {"employee", "manager", "hr", "it"}


class AuthenticatedUser(BaseModel):
    id: str
    role: Role
    name: str | None = None
    email: str | None = None


def auth_mode() -> str:
    """Which provider get_current_user will use. Public because the demo
    login shim (app/demo_auth.py) must refuse to run outside dev mode, and
    that decision has to be read from the same place this module makes it."""
    mode = os.environ.get("AUTH_MODE")
    if mode:
        return mode
    if os.environ.get("ENTRA_TENANT_ID") and os.environ.get("ENTRA_CLIENT_ID"):
        return "entra"
    return "dev"


def assert_dev_auth_is_intentional() -> None:
    """Fail loudly at startup if dev auth mode was reached by omission.

    auth_mode() falls back to "dev" whenever ENTRA_TENANT_ID/ENTRA_CLIENT_ID
    are simply unset — the right default for a laptop with zero Azure setup,
    and the wrong one for a deploy where those two env vars were forgotten.
    In dev mode, role and identity come straight from a client-supplied
    X-Dev-Role header with no credential check at all, so "forgotten" there
    means total auth bypass, not a broken feature.

    Same shape as assert_registry_covers_schema: a real deploy must say what
    it means. Set ALLOW_DEV_AUTH=1 for local/CI use; setting AUTH_MODE=dev
    explicitly counts too, since that's a deliberate choice rather than a
    fallback nobody noticed.
    """
    if auth_mode() != "dev":
        return
    if os.environ.get("AUTH_MODE") == "dev":
        return
    if os.environ.get("ALLOW_DEV_AUTH") == "1":
        return
    raise RuntimeError(
        "AUTH_MODE resolved to 'dev' by default (ENTRA_TENANT_ID/ENTRA_CLIENT_ID are "
        "unset) — this trusts a client-supplied X-Dev-Role header with no credential "
        "check. If this is local dev or CI, set ALLOW_DEV_AUTH=1. If this is a real "
        "deploy, set ENTRA_TENANT_ID and ENTRA_CLIENT_ID instead."
    )


# --- Dev mode: role comes from a header, for local testing only -----------

async def _authenticate_dev(request: Request) -> AuthenticatedUser:
    role = request.headers.get("X-Dev-Role")
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Dev auth requires an X-Dev-Role header set to one of {sorted(VALID_ROLES)}",
        )
    return AuthenticatedUser(
        id=request.headers.get("X-Dev-User-Id", "dev-user"),
        role=role,  # narrowed by the membership check above
        name=request.headers.get("X-Dev-Name", "Dev User"),
        email=request.headers.get("X-Dev-Email"),
    )


# --- Entra mode: validate a real Bearer JWT against Entra's JWKS ----------

_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict[str, object] = {"keys": None, "fetched_at": 0.0}

# Entra app-role -> internal directory role. The app registration must
# define app roles named exactly "employee" / "manager" / "hr" / "it";
# anything else falls back to "employee" (least privilege), never to "hr"
# or "it".
_ENTRA_ROLE_MAP: dict[str, Role] = {
    "employee": "employee", "manager": "manager", "hr": "hr", "it": "it",
}


async def _get_jwks(tenant_id: str) -> dict:
    now = time.time()
    if _jwks_cache["keys"] is not None and now - _jwks_cache["fetched_at"] < _JWKS_TTL_SECONDS:  # type: ignore[operator]
        return _jwks_cache["keys"]  # type: ignore[return-value]
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    keys = resp.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


async def _authenticate_entra(request: Request) -> AuthenticatedUser:
    tenant_id = os.environ.get("ENTRA_TENANT_ID")
    client_id = os.environ.get("ENTRA_CLIENT_ID")
    if not tenant_id or not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Entra auth is not configured (ENTRA_TENANT_ID / ENTRA_CLIENT_ID missing)",
        )

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        jwks = await _get_jwks(tenant_id)
        unverified_header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == unverified_header.get("kid")), None)
        if key is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown signing key")
        claims = jwt.decode(
            # Pinned, not read from the token's own header/JWK — trusting an
            # attacker-influenced "alg" field to pick the verification
            # algorithm is the textbook JWT "alg confusion" footgun. Entra
            # issues RS256 today; nothing here should ever accept another.
            token, key, algorithms=["RS256"],
            audience=client_id,
            issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        )
    except HTTPException:
        raise
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}") from exc

    app_roles = claims.get("roles", [])
    role = next((_ENTRA_ROLE_MAP[r] for r in app_roles if r in _ENTRA_ROLE_MAP), "employee")

    return AuthenticatedUser(
        id=claims.get("oid") or claims.get("sub"),
        role=role,
        name=claims.get("name"),
        email=claims.get("preferred_username") or claims.get("email"),
    )


# --- Public dependency: every authenticated route depends on this ---------

async def get_current_user(request: Request) -> AuthenticatedUser:
    mode = auth_mode()
    if mode == "dev":
        return await _authenticate_dev(request)
    if mode == "entra":
        return await _authenticate_entra(request)
    raise HTTPException(status_code=500, detail=f"Unknown AUTH_MODE '{mode}'")
