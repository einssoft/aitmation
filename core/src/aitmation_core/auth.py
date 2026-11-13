"""Token verification for the household login (ADR-0025).

The launcher is the identity store: it keeps users and the household
secret and mints ``v1.<payload>.<hmac>`` tokens. A core never sees a
password - it verifies the signature with the shared secret
(``AITMATION_AUTH_SECRET``, environment only, never in config files) and
reads the principal from the payload. Without a secret the instance runs
open on 127.0.0.1 as before: tests, development, a single core without
launcher.

What a verified principal may do here is deliberately coarse (v1): admins
see everything, members see the instances listed in their token, and the
mailroom - the shared inbox - is visible to every signed-in person.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field

COOKIE_NAME = "aitmation_session"
EVERYTHING = "*"
TOKEN_PREFIX = "v1"


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str = "member"
    instances: tuple[str, ...] = field(default_factory=tuple)
    exp: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def may_access(self, instance_id: str, instance_type: str) -> bool:
        if self.is_admin or EVERYTHING in self.instances:
            return True
        if instance_type == "mailroom":
            return True
        return instance_id in self.instances


def _b64decode(part: str) -> bytes:
    padding = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + padding)


def verify(secret: bytes, token: str, *, now: float | None = None) -> Principal:
    """Check signature and expiry; raise :class:`AuthError` otherwise."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise AuthError("malformed token")
    body = f"{parts[0]}.{parts[1]}".encode()
    want = hmac.new(secret, body, hashlib.sha256).digest()
    try:
        got = _b64decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise AuthError("bad signature") from exc
    if not hmac.compare_digest(got, want):
        raise AuthError("bad signature")
    try:
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, TypeError) as exc:
        raise AuthError("malformed token") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sub"), str):
        raise AuthError("malformed token")
    exp = int(payload.get("exp") or 0)
    if exp < (now if now is not None else time.time()):
        raise AuthError("token expired")
    instances = payload.get("instances") or []
    return Principal(
        sub=payload["sub"],
        role=str(payload.get("role") or "member"),
        instances=tuple(str(i) for i in instances),
        exp=exp,
    )


def sign(secret: bytes, principal: Principal) -> str:
    """Mint a token the way the launcher does - for tests and tooling only;
    the core never issues credentials in production."""
    payload = {
        "sub": principal.sub,
        "role": principal.role,
        "instances": list(principal.instances),
        "exp": principal.exp,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = f"{TOKEN_PREFIX}." + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    mac = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token_from_request(headers, cookies) -> str | None:
    """Bearer header first (programmatic clients), then the session cookie."""
    header = headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return cookies.get(COOKIE_NAME) or None
