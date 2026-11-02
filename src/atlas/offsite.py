"""Copy backups off the server: client-side encryption, then upload to S3-compatible storage.

Works with AWS S3, Cloudflare R2, Backblaze B2, MinIO and others that speak the S3 API, signed
with AWS Signature Version 4 over plain HTTPS (no extra dependency).

Encryption happens before upload, so the storage provider never sees the data: AES-256-GCM in
4 MiB chunks, each with its own nonce and its index (and whether it's the last) bound in as
associated data, so chunks can't be reordered, dropped or truncated without decryption failing.
The key (ATLAS_BACKUP_ENCRYPTION_KEY, 32 random bytes, base64) must be kept somewhere other than
the bucket: without it the copies can't be restored.

Only PUT and HEAD are needed, so the access key can be write-only. Expire old copies with the
bucket's own lifecycle rule rather than giving ATLAS permission to delete.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote, urlsplit

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"ATLASENC1\n"
CHUNK = 4 << 20
UNSIGNED = "UNSIGNED-PAYLOAD"


class OffsiteError(RuntimeError):
    pass


# ---------------------------------------------------------------- encryption


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _key(text: str) -> bytes:
    try:
        key = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise OffsiteError("ATLAS_BACKUP_ENCRYPTION_KEY must be base64 (create one with `atlas backup-key`)") from exc
    if len(key) != 32:
        raise OffsiteError("ATLAS_BACKUP_ENCRYPTION_KEY must decode to 32 bytes (create one with `atlas backup-key`)")
    return key


def _aad(index: int, last: bool) -> bytes:
    return MAGIC + struct.pack(">Q?", index, last)


def encrypt_file(src: Path, dst: Path, key_text: str) -> None:
    aes = AESGCM(_key(key_text))
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(MAGIC)
        index, chunk = 0, fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            last = not nxt
            nonce = os.urandom(12)
            sealed = aes.encrypt(nonce, chunk, _aad(index, last))
            fout.write(nonce + struct.pack(">I", len(sealed)) + sealed)
            if last:
                break
            index, chunk = index + 1, nxt


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def decrypt_file(src: Path, dst: Path, key_text: str) -> None:
    aes = AESGCM(_key(key_text))
    with src.open("rb") as fin, dst.open("wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise OffsiteError(f"{src} is not an encrypted ATLAS backup")
        index, done = 0, False
        while header := fin.read(16):
            if done:
                raise OffsiteError("data after the last chunk: the file was tampered with")
            if len(header) < 16:
                raise OffsiteError("truncated encrypted backup")
            nonce, (size,) = header[:12], struct.unpack(">I", header[12:])
            sealed = fin.read(size)
            if len(sealed) < size:
                raise OffsiteError("truncated encrypted backup")
            for last in (False, True):
                try:
                    fout.write(aes.decrypt(nonce, sealed, _aad(index, last)))
                    done = last
                    break
                except Exception:  # noqa: BLE001 — InvalidTag: try the other position flag, then fail
                    continue
            else:
                raise OffsiteError("decryption failed: wrong ATLAS_BACKUP_ENCRYPTION_KEY or a damaged file")
            index += 1
        if not done:
            raise OffsiteError("truncated encrypted backup: the last chunk is missing")


# ---------------------------------------------------------------- S3 signing (SigV4)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, url: str, headers: dict[str, str], *, access_key: str, secret_key: str, region: str,
         now: datetime, payload_hash: str = UNSIGNED, service: str = "s3") -> dict[str, str]:
    """Return headers with x-amz-date, x-amz-content-sha256 and Authorization added."""
    parts = urlsplit(url)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    out = {**headers, "host": parts.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    canon = {k.lower().strip(): " ".join(v.strip().split()) for k, v in out.items()}
    names = sorted(canon)
    query = "&".join(sorted(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, _, v in
                            (p.partition("=") for p in parts.query.split("&") if p)))
    request = "\n".join([method, quote(parts.path or "/", safe="/-_.~"), query, "".join(f"{n}:{canon[n]}\n" for n in names),
                         ";".join(names), payload_hash])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"
    return out


# ---------------------------------------------------------------- upload


@dataclass(frozen=True)
class Target:
    url: str  # https://<endpoint>/<bucket>[/<prefix>], path-style
    region: str
    access_key: str
    secret_key: str
    encryption_key: str

    @classmethod
    def from_env(cls) -> "Target | None":
        url = os.getenv("ATLAS_BACKUP_S3_URL", "").rstrip("/")
        if not url:
            return None
        missing = [n for n in ("ATLAS_BACKUP_S3_ACCESS_KEY_ID", "ATLAS_BACKUP_S3_SECRET_ACCESS_KEY", "ATLAS_BACKUP_ENCRYPTION_KEY") if not os.getenv(n)]
        if missing:
            raise OffsiteError(f"ATLAS_BACKUP_S3_URL is set but {', '.join(missing)} is not")
        if not url.startswith("https://") and os.getenv("ATLAS_BACKUP_S3_ALLOW_HTTP", "").lower() != "true":
            raise OffsiteError("ATLAS_BACKUP_S3_URL must use https")
        _key(os.environ["ATLAS_BACKUP_ENCRYPTION_KEY"])
        return cls(url, os.getenv("ATLAS_BACKUP_S3_REGION", "us-east-1"), os.environ["ATLAS_BACKUP_S3_ACCESS_KEY_ID"],
                   os.environ["ATLAS_BACKUP_S3_SECRET_ACCESS_KEY"], os.environ["ATLAS_BACKUP_ENCRYPTION_KEY"])

    def object_url(self, name: str) -> str:
        return f"{self.url}/{quote(name)}"


def _stream(f: BinaryIO) -> Iterator[bytes]:
    while chunk := f.read(1 << 20):
        yield chunk


def upload(archive: Path, target: Target, client: httpx.Client | None = None, now: datetime | None = None) -> dict[str, object]:
    """Encrypt `archive`, PUT it as <name>.enc, then HEAD it to confirm the stored size."""
    client = client or httpx.Client(timeout=600)
    enc = archive.with_name(archive.name + ".enc")
    try:
        encrypt_file(archive, enc, target.encryption_key)
        size = enc.stat().st_size
        url = target.object_url(enc.name)
        when = now or datetime.now(timezone.utc)
        creds = {"access_key": target.access_key, "secret_key": target.secret_key, "region": target.region}
        with enc.open("rb") as f:
            headers = sign("PUT", url, {"content-length": str(size), "content-type": "application/octet-stream"}, now=when, **creds)
            resp = client.put(url, content=_stream(f), headers=headers)
        if resp.status_code >= 300:
            raise OffsiteError(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")
        head = client.head(url, headers=sign("HEAD", url, {}, now=now or datetime.now(timezone.utc), **creds))
        stored = int(head.headers.get("content-length", -1))
        if head.status_code >= 300 or stored != size:
            raise OffsiteError(f"upload not confirmed: HTTP {head.status_code}, stored {stored} of {size} bytes")
        return {"url": url, "bytes": size}
    finally:
        enc.unlink(missing_ok=True)
