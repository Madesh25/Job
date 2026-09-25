"""Who may call the web app (spec section 1 rules 4 and 5).

- Telegram: the X-Telegram-Bot-Api-Secret-Token header must equal TELEGRAM_WEBHOOK_SECRET.
- Cloud Scheduler: a Google-signed OIDC token whose audience is SERVICE_URL and whose email
  is SCHEDULER_SA_EMAIL (verified).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

from jobengine.settings import Settings

log = logging.getLogger("jobengine.web")

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# token -> claims. Raises ValueError when the token is not a valid Google-signed ID token.
Verifier = Callable[[str], dict[str, Any]]


class AuthError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


def check_telegram_secret(header: str | None, s: Settings) -> None:
    """401 unless the secret header matches. An unset secret refuses everything."""
    expected = s.telegram_webhook_secret or ""
    if not expected or not header or not hmac.compare_digest(header, expected):
        raise AuthError(401, "bad or missing secret token")


def google_verifier(token: str) -> dict[str, Any]:
    """Signature, expiry and issuer checked by google-auth. The audience is checked by
    check_scheduler_token, so a wrong audience is a 403 and not a 401."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, Request(), audience=None)


def check_scheduler_token(authorization: str | None, s: Settings,
                          verify: Verifier = google_verifier) -> dict[str, Any]:
    """401 without a valid token; 403 for a valid token from someone else."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "missing bearer token")
    try:
        claims = verify(token.strip())
    except Exception as exc:  # any verification failure (bad signature, expired) is a 401
        log.warning("OIDC token rejected: %s", type(exc).__name__)
        raise AuthError(401, "invalid token") from None
    audience = s.service_url or ""
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if not audience or audience.rstrip("/") not in [str(a).rstrip("/") for a in audiences]:
        raise AuthError(403, "wrong audience")
    if not s.scheduler_sa_email or claims.get("email") != s.scheduler_sa_email:
        raise AuthError(403, "wrong caller")
    if claims.get("email_verified") is not True:
        raise AuthError(403, "email not verified")
    return claims
