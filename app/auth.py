"""Token checks (DESIGN section 9). Every /jobs/* call needs a Cloud Scheduler OIDC token, every
/tasks/* call (v1.3) a Cloud Tasks OIDC token and every /chat call a Google Chat token; anything
else gets 401 with no detail. /health(z) is open; /wa/webhook checks Meta's signature itself.

- Scheduler: Google-signed ID token, audience = SERVICE_URL, email = SCHEDULER_SA_EMAIL (verified).
- Cloud Tasks: the same, with email = RUNTIME_SA_EMAIL (tasks run as the runtime account).
- Chat: token signed by chat@system.gserviceaccount.com, audience = CHAT_PROJECT_NUMBER.

Google's public certificates are fetched with the standard library and cached for an hour.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException

from app.settings import Settings, get_settings

logger = logging.getLogger("grc.auth")

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS_URL = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"
CERTS_TTL_SECONDS = 3600
CLOCK_SKEW_SECONDS = 60


class Unauthorized(Exception):
    pass


def _fetch_json(url: str) -> dict[str, str]:
    with urllib.request.urlopen(url, timeout=10) as response:  # only the fixed Google URLs above
        return json.loads(response.read().decode("utf-8"))


class TokenVerifier:
    def __init__(self, settings: Settings, fetch: Callable[[str], dict[str, str]] = _fetch_json):
        self._settings = settings
        self._fetch = fetch
        self._certs: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def _certs_for(self, url: str) -> dict[str, str]:
        with self._lock:
            cached = self._certs.get(url)
            if cached is None or time.monotonic() - cached[0] > CERTS_TTL_SECONDS:
                cached = (time.monotonic(), self._fetch(url))
                self._certs[url] = cached
            return cached[1]

    def _decode(self, authorization: str | None, certs_url: str, audience: str) -> dict[str, Any]:
        from google.auth import jwt

        if not audience:
            raise Unauthorized("verifier not configured")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthorized("missing bearer token")
        try:
            return jwt.decode(
                token.strip(),
                certs=self._certs_for(certs_url),
                audience=audience,
                clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
            )
        except Exception as error:  # bad signature, expired, wrong audience, malformed
            raise Unauthorized(type(error).__name__) from None

    def _verify_google(self, authorization: str | None, expected_email: str) -> None:
        claims = self._decode(authorization, GOOGLE_CERTS_URL, self._settings.service_url)
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise Unauthorized("issuer")
        expected = expected_email.strip().lower()
        if not expected or str(claims.get("email", "")).lower() != expected:
            raise Unauthorized("email")
        if claims.get("email_verified") is not True:
            raise Unauthorized("email not verified")

    def verify_scheduler(self, authorization: str | None) -> None:
        self._verify_google(authorization, self._settings.scheduler_sa_email)

    def verify_task(self, authorization: str | None) -> None:
        self._verify_google(authorization, self._settings.runtime_sa_email)

    def verify_chat(self, authorization: str | None) -> None:
        claims = self._decode(authorization, CHAT_CERTS_URL, self._settings.chat_project_number)
        if claims.get("iss") != CHAT_ISSUER:
            raise Unauthorized("issuer")


_verifier: TokenVerifier | None = None


def get_verifier() -> TokenVerifier:
    global _verifier
    if _verifier is None:
        _verifier = TokenVerifier(get_settings())
    return _verifier


VerifierDep = Annotated[TokenVerifier, Depends(get_verifier)]


def _reject(kind: str, error: Unauthorized) -> HTTPException:
    logger.warning("unauthorized", extra={"caller": kind, "reason": str(error)})
    return HTTPException(status_code=401, detail="unauthorized")


def require_scheduler(
    verifier: VerifierDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    try:
        verifier.verify_scheduler(authorization)
    except Unauthorized as error:
        raise _reject("scheduler", error) from None


def require_task(
    verifier: VerifierDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    try:
        verifier.verify_task(authorization)
    except Unauthorized as error:
        raise _reject("task", error) from None


def require_chat(
    verifier: VerifierDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    try:
        verifier.verify_chat(authorization)
    except Unauthorized as error:
        raise _reject("chat", error) from None
