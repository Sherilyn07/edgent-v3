"""Verifying a person, not a machine.

New in v3, and deliberately kept separate from the broker's bearer token
(routes/broker.py). The two solve different problems and must not be
confused: the broker token authenticates a machine — the Colab GPU — that can
never hold an AWS or Cognito identity, and it grants exactly one thing, "ask
for a job." This module authenticates a person using the app through Cognito,
and what it grants is "see your own sessions, and nothing else's."

Version 2's schema already anticipated this: `Session.owner_id` existed,
nullable, unused, with a comment that said "from the JWT, once there is one."
This is that JWT.

How it works
------------
The frontend signs a user in against a Cognito User Pool (see
frontend/src/auth.js) and attaches the pool's **ID token** — not the access
token — as `Authorization: Bearer <token>` on every API call
(frontend/src/api.js). The ID token carries `sub` and `email` directly, so
verifying it needs no extra round trip to Cognito.

Verification means: the token is a JWT, signed with RS256, by a key Cognito's
own JWKS publishes for this user pool, not expired, issued by this pool
(`iss`), and intended for this app client (`aud`). All four are checked below.
The JWKS is fetched once and cached in this process for as long as the `kid`
(key id) on an incoming token stays one we already have — Cognito rotates keys
rarely, so a cache miss is the rare, cheap path, not the common one.

If Cognito is not configured (`COGNITO_USER_POOL_ID` unset), every check is
skipped and the API runs with no auth at all — this is what lets a laptop run
v3 against local SQLite before a User Pool exists, without a separate
"local mode" flag anywhere else in the codebase.
"""
import logging
import threading
import time
from typing import Any

import requests
from fastapi import Depends, Header, HTTPException
from jose import jwk, jwt
from jose.utils import base64url_decode

from .config import get_settings

log = logging.getLogger(__name__)
settings = get_settings()

_JWKS_TTL_SECONDS = 3600


class _JwksCache:
    """The pool's signing keys, refetched at most once an hour or on a
    key id we have not seen before (a real rotation, not a guess)."""

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _url(self) -> str:
        return (f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/"
               f"{settings.cognito_user_pool_id}/.well-known/jwks.json")

    def _refresh(self) -> None:
        response = requests.get(self._url(), timeout=10)
        response.raise_for_status()
        self._keys = {k["kid"]: k for k in response.json()["keys"]}
        self._fetched_at = time.time()

    def get(self, kid: str) -> dict[str, Any] | None:
        with self._lock:
            stale = time.time() - self._fetched_at > _JWKS_TTL_SECONDS
            if kid not in self._keys or stale:
                self._refresh()
            return self._keys.get(kid)


_jwks = _JwksCache()


def cognito_configured() -> bool:
    return bool(settings.cognito_region and settings.cognito_user_pool_id
               and settings.cognito_app_client_id)


def _issuer() -> str:
    return f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/{settings.cognito_user_pool_id}"


def _verify(token: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:                          # noqa: BLE001
        raise HTTPException(status_code=401, detail="unreadable token") from exc

    key_data = _jwks.get(header.get("kid", ""))
    if key_data is None:
        raise HTTPException(status_code=401, detail="unknown signing key")

    public_key = jwk.construct(key_data)
    message, signature = token.rsplit(".", 1)
    if not public_key.verify(message.encode(), base64url_decode(signature.encode())):
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        claims = jwt.get_unverified_claims(token)
        jwt.decode(
            token,
            key_data,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=_issuer(),
        )
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc

    if claims.get("token_use") != "id":
        # An access token would also verify -- it is signed by the same pool --
        # but it carries no `sub`-identifying email and is not what the
        # frontend sends. Reject anything that is not the ID token explicitly,
        # so a client cannot substitute one for the other by accident.
        raise HTTPException(status_code=401, detail="expected an ID token")

    return claims


def _claims(authorization: str = Header(default="")) -> dict[str, Any] | None:
    """Verify the bearer token, once.

    FastAPI caches a dependency's result for the lifetime of one request (the
    default `use_cache=True`), so `require_user` and `require_admin` below
    can both depend on this and still only pay for one JWKS lookup and one
    signature/issuer/audience check per request, not two.

    Returns `None` when Cognito is not configured — the local-development
    fallback described in the module docstring — rather than raising, so
    `require_user` can turn that into "local-dev" and `require_admin` can
    turn it into "let everyone through" without re-deriving the same check.
    """
    if not cognito_configured():
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return _verify(token)


def require_user(claims: dict[str, Any] | None = Depends(_claims)) -> str:
    """FastAPI dependency: verify the caller, return their Cognito `sub`.

    This is what every session-scoped route depends on. It does not check
    *ownership* of a particular session -- that is one extra line at each call
    site, comparing the returned `sub` against `session.owner_id`, because
    only the route loading the session knows which row is in play.
    """
    if claims is None:
        # No pool configured: auth is off. Used for local development only --
        # see the module docstring. A fixed id keeps owner_id populated even
        # then, so the rest of the code never has to special-case "no auth".
        return "local-dev"
    return claims["sub"]


def require_admin(claims: dict[str, Any] | None = Depends(_claims),
                  user_id: str = Depends(require_user)) -> str:
    """Same as require_user, plus membership in the "admins" Cognito Group.

    Used only by `PUT /config/services` — the call that repoints where the
    embedding/STT/LLM traffic goes. Version 2 left this endpoint unauthenticated
    entirely; that is limitation 08 ("anyone can read anyone's files") one
    layer further down the stack. `GET /config/services` is `require_user`,
    not this — every signed-in user needs to know whether the services are
    ready before uploading, not just admins.
    """
    if claims is None:
        return user_id
    groups = claims.get("cognito:groups") or []
    if "admins" not in groups:
        raise HTTPException(status_code=403, detail="admin group membership required")
    return user_id
