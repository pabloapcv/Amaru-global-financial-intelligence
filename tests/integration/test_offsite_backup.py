"""Off-server backups: encryption, S3 signing, upload and restoring an encrypted copy."""

from datetime import datetime, timezone

import httpx
import pytest

from atlas import offsite


def test_signature_matches_the_aws_documentation_example():
    # AWS S3 docs, "Signature Calculations for the Authorization Header", GET Object example.
    headers = offsite.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                           access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                           region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc),
                           payload_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert headers["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_encryption_round_trips_and_detects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(offsite, "CHUNK", 1000)  # several chunks
    key = offsite.new_key()
    src, enc, out = tmp_path / "a.tar.gz", tmp_path / "a.enc", tmp_path / "b.tar.gz"
    src.write_bytes(bytes(range(256)) * 20)
    offsite.encrypt_file(src, enc, key)
    assert offsite.is_encrypted(enc) and bytes(range(256)) * 2 not in enc.read_bytes()
    offsite.decrypt_file(enc, out, key)
    assert out.read_bytes() == src.read_bytes()

    with pytest.raises(offsite.OffsiteError, match="wrong ATLAS_BACKUP_ENCRYPTION_KEY"):
        offsite.decrypt_file(enc, out, offsite.new_key())
    data = enc.read_bytes()
    record = 12 + 4 + 1000 + 16
    truncated = tmp_path / "t.enc"
    truncated.write_bytes(data[: len(offsite.MAGIC) + 2 * record])  # drop the final chunks
    with pytest.raises(offsite.OffsiteError, match="last chunk is missing"):
        offsite.decrypt_file(truncated, out, key)
    flipped = bytearray(data)
    flipped[len(offsite.MAGIC) + 40] ^= 1
    truncated.write_bytes(bytes(flipped))
    with pytest.raises(offsite.OffsiteError, match="decryption failed"):
        offsite.decrypt_file(truncated, out, key)
    with pytest.raises(offsite.OffsiteError, match="32 bytes"):
        offsite.encrypt_file(src, enc, "c2hvcnQ=")


def test_target_needs_every_setting_and_https(monkeypatch):
    for name in ("ATLAS_BACKUP_S3_URL", "ATLAS_BACKUP_S3_ACCESS_KEY_ID", "ATLAS_BACKUP_S3_SECRET_ACCESS_KEY", "ATLAS_BACKUP_ENCRYPTION_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert offsite.Target.from_env() is None
    monkeypatch.setenv("ATLAS_BACKUP_S3_URL", "https://s3.example.test/bucket/atlas/")
    with pytest.raises(offsite.OffsiteError, match="ATLAS_BACKUP_S3_ACCESS_KEY_ID"):
        offsite.Target.from_env()
    monkeypatch.setenv("ATLAS_BACKUP_S3_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("ATLAS_BACKUP_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("ATLAS_BACKUP_ENCRYPTION_KEY", offsite.new_key())
    assert offsite.Target.from_env().object_url("x.enc") == "https://s3.example.test/bucket/atlas/x.enc"
    monkeypatch.setenv("ATLAS_BACKUP_S3_URL", "http://s3.example.test/bucket")
    with pytest.raises(offsite.OffsiteError, match="https"):
        offsite.Target.from_env()


def test_backup_is_uploaded_encrypted_and_restores_from_the_copy(isolated_env, tmp_path, monkeypatch):
    from atlas import backup

    stored: dict[str, bytes] = {}

    def bucket(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=id/")
        if req.method == "PUT":
            stored[req.url.path] = req.read()
            return httpx.Response(200)
        body = stored.get(req.url.path)
        return httpx.Response(200, headers={"content-length": str(len(body))}) if body is not None else httpx.Response(404)

    key = offsite.new_key()
    target = offsite.Target("https://s3.example.test/bucket", "auto", "id", "secret", key)
    archive = backup.create(tmp_path / "local", include_raw=False)
    sent = offsite.upload(archive, target, client=httpx.Client(transport=httpx.MockTransport(bucket)))
    assert sent["url"] == f"https://s3.example.test/bucket/{archive.name}.enc"
    copy = tmp_path / "downloaded.enc"
    copy.write_bytes(stored[f"/bucket/{archive.name}.enc"])
    assert not (tmp_path / "local" / f"{archive.name}.enc").exists()  # the encrypted temp file is cleaned up

    fresh = f"sqlite:///{tmp_path / 'restored.db'}"
    monkeypatch.delenv("ATLAS_BACKUP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="encrypted"):
        backup.restore(copy, fresh, object_store=tmp_path / "objects")
    monkeypatch.setenv("ATLAS_BACKUP_ENCRYPTION_KEY", key)
    out = backup.restore(copy, fresh, object_store=tmp_path / "objects")
    assert out["rows"] > 0 and "workspaces" in out["tables"]


def test_failed_upload_is_reported(tmp_path):
    src = tmp_path / "atlas-backup-x.tar.gz"
    src.write_bytes(b"data")
    target = offsite.Target("https://s3.example.test/bucket", "auto", "id", "secret", offsite.new_key())
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403, text="AccessDenied")))
    with pytest.raises(offsite.OffsiteError, match="HTTP 403"):
        offsite.upload(src, target, client=client)
    assert not (tmp_path / "atlas-backup-x.tar.gz.enc").exists()
