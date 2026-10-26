"""Encryption at rest for small secrets (e.g. TOTP seeds), using Fernet (AES-128-CBC + HMAC-SHA256).

The key comes from ATLAS_SECRET_KEY (any string; a Fernet key is derived from it). Without it, a
random key is created once at data/secret.key (owner-only permissions) for local development.
Production deployments should set ATLAS_SECRET_KEY: if the key changes, stored secrets cannot be
decrypted (users then sign in with a recovery code and re-enroll).
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from atlas.config import ROOT


@lru_cache(maxsize=1)
def _key() -> bytes:
    """The urlsafe-base64 Fernet key (from ATLAS_SECRET_KEY, or the key file)."""
    env = os.getenv("ATLAS_SECRET_KEY")
    if env:
        return base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())
    path = Path(os.getenv("ATLAS_SECRET_KEY_FILE", str(ROOT / "data" / "secret.key")))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return path.read_bytes().strip()


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_key())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def sign(message: str) -> str:
    """HMAC-SHA256 of `message` under a key derived from the encryption key (for unsubscribe links)."""
    import hmac

    key = hashlib.sha256(b"atlas-signing:" + _key()).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()[:32]


def verify_signature(message: str, signature: str) -> bool:
    import hmac

    return hmac.compare_digest(sign(message), signature or "")


def reset() -> None:
    """Forget cached keys (tests, or after rotating ATLAS_SECRET_KEY)."""
    _key.cache_clear()
    _fernet.cache_clear()
