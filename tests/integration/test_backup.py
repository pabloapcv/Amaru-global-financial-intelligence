import io
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select

from atlas import backup
from atlas.config import settings
from atlas.data.db import Base, ResearchRun, get_engine
from atlas.research import engine


@pytest.fixture
def archive(seeded, tmp_path):
    run = engine.create_run("copper outlook", {"engine": "deterministic", "depth": "quick", "formats": ["html"]})
    report_id = engine.execute_run(run)["report_id"]
    path = backup.create(tmp_path / "out", include_raw=False)
    return path, report_id


def _counts(url):
    eng = create_engine(url)
    with eng.connect() as c:
        out = {t.name: c.execute(select(func.count()).select_from(t)).scalar() for t in Base.metadata.sorted_tables}
    eng.dispose()
    return out


def test_round_trip_into_a_fresh_database(archive, tmp_path):
    path, report_id = archive
    manifest = backup.describe(path)
    assert manifest["format"] == 1 and manifest["includes_raw_payloads"] is False and "secret" in manifest["not_included"]
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert not any("secret" in n or n.endswith(".key") for n in names)  # the 2FA key never goes in a backup
    assert any(n.startswith(f"objects/reports/{report_id}/") for n in names)

    target = f"sqlite:///{tmp_path / 'restored.db'}"
    summary = backup.restore(path, target, object_store=tmp_path / "store")
    assert summary["rows"] == sum(manifest["tables"].values())
    assert _counts(target) == manifest["tables"]
    assert (tmp_path / "store" / "reports" / report_id).is_dir()
    eng = create_engine(target)
    with eng.connect() as c:
        src = get_engine().connect()
        q = select(ResearchRun.run_id, ResearchRun.query, ResearchRun.created_at, ResearchRun.result).order_by(ResearchRun.run_id).limit(5)
        assert c.execute(q).all() == src.execute(q).all()  # values and types (dates, JSON) survive
        src.close()
    eng.dispose()
    with pytest.raises(ValueError, match="not empty"):
        backup.restore(path, target, object_store=tmp_path / "store")


def test_damaged_or_unsafe_archives_are_refused(archive, tmp_path):
    path, _ = archive
    # Change one table file but keep the old manifest.
    bad = tmp_path / "tampered.tar.gz"
    with tarfile.open(path) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "tables/workspaces.jsonl.gz":
                data = data[:-4] + b"oops"
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    with pytest.raises(ValueError, match="checksum"):
        backup.restore(bad, f"sqlite:///{tmp_path / 'a.db'}", object_store=tmp_path / "s1")
    # Entries escaping the extraction folder are rejected before anything is written.
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as dst:
        payload = json.dumps({"format": 1}).encode()
        info = tarfile.TarInfo("../outside.txt")
        info.size = len(payload)
        dst.addfile(info, io.BytesIO(payload))
    with pytest.raises(ValueError, match="unsafe"):
        backup.restore(evil, f"sqlite:///{tmp_path / 'b.db'}", object_store=tmp_path / "s2")
    assert not (tmp_path.parent / "outside.txt").exists()
