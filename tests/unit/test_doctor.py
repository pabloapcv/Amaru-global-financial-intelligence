"""atlas doctor: configuration checks."""

from atlas import doctor


def _levels(report):
    return {c["check"]: c["level"] for c in report["checks"]}


def test_production_misconfiguration_is_reported(monkeypatch):
    for k in ("ATLAS_SECRET_KEY", "ATLAS_COOKIE_SECURE", "ATLAS_SMTP_HOST", "STRIPE_PRICE_PRO", "ATLAS_REQUIRE_EMAIL_VERIFICATION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATLAS_PUBLIC_URL", "https://app.example.com")
    monkeypatch.setenv("ATLAS_API_PUBLIC_URL", "http://localhost:8010")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("ATLAS_WEBHOOKS_ALLOW_PRIVATE", "true")
    report = doctor.run(check_database=False)
    lv = _levels(report)
    assert report["production"] and not report["ok"]
    for check in ("Encryption key", "Secure cookies", "Public URLs", "Email", "Billing", "Webhooks", "CORS"):
        assert lv[check] == "error", check
    assert report["checks"][0]["level"] == "error"  # errors first
    text = doctor.format_text(report)
    assert "sk_test_x" not in text and "✗ Secure cookies" in text


def test_good_production_configuration_passes(monkeypatch):
    import atlas.config as config

    env = {"ATLAS_PUBLIC_URL": "https://app.example.com", "ATLAS_API_PUBLIC_URL": "https://api.example.com", "ATLAS_SECRET_KEY": "x" * 64,
           "ATLAS_COOKIE_SECURE": "true", "ATLAS_SMTP_HOST": "smtp.example.com", "ATLAS_SMTP_FROM": "ATLAS <no-reply@example.com>",
           "ATLAS_OPERATOR_EMAILS": "ops@example.com", "ATLAS_METRICS_TOKEN": "t", "ATLAS_REQUIRE_EMAIL_VERIFICATION": "true", "ATLAS_AUTH": "required"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_INVESTOR", "STRIPE_PRICE_RESEARCH_PRO", "STRIPE_PRICE_PRO",
              "ATLAS_WEBHOOKS_ALLOW_PRIVATE", "ATLAS_PLAN_PRICES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "settings", config.Settings(cors_origins=("https://app.example.com",), database_url="postgresql+psycopg://x/y"))
    report = doctor.run(check_database=False)
    assert report["ok"], [c for c in report["checks"] if c["level"] == "error"]
    assert _levels(report)["CORS"] == "ok" and _levels(report)["Public URLs"] == "ok"


def test_backup_prune_keeps_the_newest(tmp_path):
    import pytest

    from atlas import backup

    names = [f"atlas-backup-2026101{i}T000000Z.tar.gz" for i in range(5)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("keep me")
    removed = backup.prune(tmp_path, 2)
    assert sorted(p.name for p in removed) == names[:3]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[3:] + ["notes.txt"])
    with pytest.raises(ValueError):
        backup.prune(tmp_path, 0)
