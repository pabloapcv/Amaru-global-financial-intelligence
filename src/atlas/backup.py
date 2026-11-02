"""Portable backups: `atlas backup` / `atlas restore`.

A backup is one .tar.gz holding a manifest (ATLAS version, time, row counts, SHA-256 of every file),
every table as gzipped JSON lines (database-neutral, so a SQLite backup restores into PostgreSQL and
back), the report files and, unless skipped, the raw provider payloads (they can be re-fetched).

Not included on purpose: the key that encrypts two-factor secrets (ATLAS_SECRET_KEY or
data/secret.key). Keep it separately; storing it next to the data would defeat the encryption. Without
it, restored users with two-factor sign in with a recovery code and re-enroll.

Restore verifies every checksum and only writes into an empty database.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Date, DateTime, Integer, create_engine, func, insert, select, text

from atlas import __version__
from atlas import config as _config
from atlas.data.db import Base, get_engine, utcnow

FORMAT = 1
BATCH = 5000
# Tables whose content means "this database is in use".
IN_USE = ("users", "research_runs", "market_observations", "macro_observations")


def _json_default(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    raise TypeError(f"cannot serialize {type(v).__name__}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create(out_dir: str | Path, *, include_raw: bool = True) -> Path:
    """Write a backup archive into `out_dir`; returns its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    engine = get_engine()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tables").mkdir()
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                n = 0
                with gzip.open(root / "tables" / f"{table.name}.jsonl.gz", "wt", encoding="utf-8") as f:
                    for row in conn.execution_options(stream_results=True).execute(select(table)).mappings():
                        f.write(json.dumps(dict(row), default=_json_default) + "\n")
                        n += 1
                counts[table.name] = n
        store = Path(_config.settings.object_store)
        for sub in ("reports", "raw") if include_raw else ("reports",):
            if (store / sub).exists():
                shutil.copytree(store / sub, root / "objects" / sub)
        files = {str(p.relative_to(root)): _sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}
        manifest = {"format": FORMAT, "atlas_version": __version__, "created_at": stamp, "dialect": engine.dialect.name,
                    "tables": counts, "includes_raw_payloads": include_raw, "files": files,
                    "not_included": "two-factor encryption key (ATLAS_SECRET_KEY or data/secret.key): keep it separately"}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        archive = out_dir / f"atlas-backup-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    tar.add(p, arcname=str(p.relative_to(root)))
    return archive


def prune(out_dir: str | Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` ATLAS backup archives in `out_dir`; returns what was deleted."""
    if keep < 1:
        raise ValueError("keep at least one backup")
    archives = sorted(Path(out_dir).glob("atlas-backup-*.tar.gz"))  # names sort by time
    old = archives[:-keep]
    for p in old:
        p.unlink()
    return old


def _coerce(table, row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for col in table.columns:
        if col.name not in row:
            continue
        v = row[col.name]
        if v is not None and isinstance(v, str):
            if isinstance(col.type, DateTime):
                v = datetime.fromisoformat(v)
            elif isinstance(col.type, Date):
                v = date.fromisoformat(v[:10])
        out[col.name] = v
    return out


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Regular files with relative, non-escaping names only."""
    members = []
    for m in tar.getmembers():
        name = Path(m.name)
        if not m.isfile() or name.is_absolute() or ".." in name.parts:
            raise ValueError(f"unsafe entry in backup: {m.name}")
        members.append(m)
    return members


def _decrypted(archive: Path, tmp: Path) -> Path:
    """An off-server copy (.enc) is decrypted with ATLAS_BACKUP_ENCRYPTION_KEY into `tmp` first."""
    import os

    from atlas import offsite

    if not offsite.is_encrypted(archive):
        return archive
    key = os.getenv("ATLAS_BACKUP_ENCRYPTION_KEY")
    if not key:
        raise ValueError("this backup is encrypted: set ATLAS_BACKUP_ENCRYPTION_KEY to the key it was made with")
    out = tmp / "decrypted.tar.gz"
    offsite.decrypt_file(archive, out, key)
    return out


def restore(archive: str | Path, database_url: str | None = None, *, object_store: str | Path | None = None) -> dict[str, Any]:
    """Restore a backup into an empty database (and object store). Returns a summary."""
    url = database_url or _config.settings.database_url
    store = Path(object_store or _config.settings.object_store)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = _decrypted(Path(archive), root)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(root, members=_safe_members(tar), filter="data")  # members also validated above
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("format") != FORMAT:
            raise ValueError(f"unsupported backup format {manifest.get('format')}")
        for rel, digest in manifest["files"].items():
            p = root / rel
            if not p.exists() or _sha256(p) != digest:
                raise ValueError(f"backup is damaged: checksum mismatch for {rel}")

        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, **kwargs)
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            for name in IN_USE:
                if conn.execute(select(func.count()).select_from(Base.metadata.tables[name])).scalar():
                    raise ValueError(f"target database is not empty ({name} has rows); restore only into a new database")
        restored: dict[str, int] = {}
        with engine.begin() as conn:
            if engine.dialect.name == "sqlite":
                conn.execute(text("PRAGMA foreign_keys=OFF"))  # rows arrive table by table; checked again below
            for table in Base.metadata.sorted_tables:
                path = root / "tables" / f"{table.name}.jsonl.gz"
                if not path.exists():
                    continue
                conn.execute(table.delete())  # e.g. the default local workspace created on startup
                n, batch = 0, []
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    for line in f:
                        batch.append(_coerce(table, json.loads(line)))
                        if len(batch) >= BATCH:
                            conn.execute(insert(table), batch)
                            n, batch = n + len(batch), []
                if batch:
                    conn.execute(insert(table), batch)
                    n += len(batch)
                restored[table.name] = n
                expected = manifest["tables"].get(table.name)
                if expected is not None and expected != n:
                    raise ValueError(f"{table.name}: restored {n} rows, backup has {expected}")
            if engine.dialect.name == "postgresql":  # move id sequences past the restored rows
                for table in Base.metadata.sorted_tables:
                    pk = list(table.primary_key.columns)
                    if len(pk) == 1 and isinstance(pk[0].type, Integer) and pk[0].autoincrement:
                        conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                                          f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 1))"))
            if engine.dialect.name == "sqlite":
                conn.execute(text("PRAGMA foreign_keys=ON"))
                problems = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
                if problems:
                    raise ValueError(f"restored data violates foreign keys: {problems[:3]}")
        engine.dispose()
        copied = 0
        if (root / "objects").exists():
            for p in (root / "objects").rglob("*"):
                if p.is_file():
                    dest = store / p.relative_to(root / "objects")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    copied += 1
    return {"tables": restored, "rows": sum(restored.values()), "files": copied, "created_at": manifest["created_at"],
            "from_dialect": manifest["dialect"], "atlas_version": manifest["atlas_version"],
            "note": manifest.get("not_included")}


def describe(archive: str | Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        f = tar.extractfile("manifest.json")
        if f is None:
            raise ValueError("not an ATLAS backup")
        return json.load(io.TextIOWrapper(f, encoding="utf-8"))
