"""RFC 6238 TOTP (SHA-1, 6 digits, 30-second steps), compatible with common authenticator apps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP, DIGITS, WINDOW = 30, 6, 1


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // STEP)


def verify(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """The matching time step (within ±WINDOW) if `code` is valid and newer than `last_step`, else None."""
    code = "".join(c for c in code if c.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for s in range(step - WINDOW, step + WINDOW + 1):
        if (last_step is None or s > last_step) and hmac.compare_digest(code_at(secret, s), code):
            return s
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "ATLAS") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")
