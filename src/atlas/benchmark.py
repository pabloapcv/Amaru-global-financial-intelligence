"""Acceptance benchmark against the spec's non-functional targets (§16).

Measures, on the data actually stored in this deployment:
- cached query latency (quotes, history, data status) through the API, p50/p95;
- research job latency for a set of representative questions, p50/p95;
- numerical traceability and citation validity of every research answer;
- reproducibility: re-running a calculation on the same data snapshot gives identical numbers;
- pipeline reliability over the last 30 days;
- freshness visibility on every dataset;
- cross-workspace isolation (a second workspace cannot read the first one's research).

Research runs use the deterministic engine so the benchmark costs nothing and is repeatable.
"""

from __future__ import annotations

import os
import statistics
import time
from typing import Any

QUESTIONS = [
    "What's happening with sugar today, and what are the main drivers?",
    "Analyze the relationship between the Brazilian real and sugar prices over the last five years.",
    "What happens to copper, gold, and emerging-market currencies if the Federal Reserve cuts interest rates?",
    "How is the US economy performing right now?",
    "Identify unusual price movements across commodities.",
    "Compare the economic outlook for Brazil, Mexico, Bolivia, Chile, and Peru.",
    "Is gold positioning crowded?",
    "Generate a weekly report on Bolivia–U.S. commodity trading opportunities.",
]
QUOTE_IDS = "CMD.SUGAR.ISA,CMD.COPPER.LME,CMD.CRUDE.BRENT,FX.USDBRL,RATE.UST.10Y,EQ.US.SP500"


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[k]


def run(repeats: int = 20) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    os.environ.setdefault("ATLAS_AUTH", "off")
    from atlas.api.main import app
    from atlas.data.access import data_status, pipeline_status
    from atlas.research import engine

    results: dict[str, Any] = {}
    client = TestClient(app)

    # 1) Cached query latency (data already stored; no network).
    lat: list[float] = []
    for _ in range(repeats):
        for path in (f"/api/v1/markets/quotes?ids={QUOTE_IDS}", "/api/v1/markets/history?id=FX.USDBRL&lookback_years=5",
                     "/api/v1/macro/series?id=US.CPI.HEADLINE&lookback_years=10", "/api/v1/data/status"):
            t = time.perf_counter()
            r = client.get(path)
            lat.append(time.perf_counter() - t)
            assert r.status_code == 200, (path, r.status_code)
    results["cached_query"] = {"target": "p95 < 3 s", "n": len(lat), "p50_s": round(_pct(lat, 0.5), 3),
                               "p95_s": round(_pct(lat, 0.95), 3), "pass": _pct(lat, 0.95) < 3.0}

    # 2) Research latency + 3) traceability and citations.
    times, checked, verified, bad_cites, failures = [], 0, 0, 0, []
    for q in QUESTIONS:
        t = time.perf_counter()
        rid = engine.create_run(q, {"engine": "deterministic", "formats": [], "cache": False})
        res = engine.execute_run(rid)
        times.append(time.perf_counter() - t)
        if "error" in res:
            failures.append({"question": q, "error": res["error"]})
            continue
        v = res["verification"]
        checked += v["numbers_checked"]
        verified += v["numbers_verified"]
        bad_cites += len(v["invalid_citations"])
    results["research_latency"] = {"target": "simple research p95 < 45 s", "n": len(times),
                                   "p50_s": round(statistics.median(times), 2), "p95_s": round(_pct(times, 0.95), 2),
                                   "pass": _pct(times, 0.95) < 45 and not failures, "failures": failures}
    results["traceability"] = {"target": "100% of numerical claims traceable", "numbers_checked": checked,
                               "numbers_verified": verified,
                               "pct": round(verified / checked * 100, 2) if checked else None, "pass": checked > 0 and verified == checked}
    results["citations"] = {"target": "100% of citations valid", "invalid": bad_cites, "pass": bad_cites == 0}

    # 4) Reproducibility: same snapshot, same numbers (fingerprint + values).
    from atlas import analytics
    from atlas.data.access import get_series
    from atlas.instruments import get_dataset

    repro = []
    for dsid in ("CMD.SUGAR.ISA", "FX.USDBRL", "RATE.UST.10Y"):
        ds = get_dataset(dsid)
        s, _ = get_series(dsid)
        a = analytics.performance(s, unit=ds.unit, frequency=ds.frequency)  # type: ignore[union-attr]
        b = analytics.performance(s.copy(), unit=ds.unit, frequency=ds.frequency)  # type: ignore[union-attr]
        repro.append(a == b)
    corr_a = analytics.correlation({k: get_series(k)[0] for k in ("CMD.SUGAR.ISA", "FX.USDBRL")},
                                   {"CMD.SUGAR.ISA": "monthly", "FX.USDBRL": "daily"}, {"CMD.SUGAR.ISA": "c", "FX.USDBRL": "c"})
    corr_b = analytics.correlation({k: get_series(k)[0] for k in ("CMD.SUGAR.ISA", "FX.USDBRL")},
                                   {"CMD.SUGAR.ISA": "monthly", "FX.USDBRL": "daily"}, {"CMD.SUGAR.ISA": "c", "FX.USDBRL": "c"})
    repro.append(corr_a == corr_b)
    results["reproducibility"] = {"target": "same snapshot reproduces deterministic metrics", "checks": len(repro),
                                  "identical": sum(repro), "pass": all(repro)}

    # 5) Pipeline reliability, 6) freshness visibility.
    ps = pipeline_status(1)
    rate = ps["success_rate_30d"]
    results["pipeline_reliability"] = {"target": "≥ 99% successful runs over 30 days", "runs_30d": ps["runs_30d"],
                                       "success_rate_pct": round(rate * 100, 2) if rate is not None else None,
                                       "pass": rate is not None and rate >= 0.99}
    rows = data_status()
    visible = [r for r in rows if r["freshness"] and r["freshness_note"]]
    results["freshness_visible"] = {"target": "freshness visible for every dataset", "datasets": len(rows),
                                    "with_status": len(visible), "pass": len(visible) == len(rows)}

    # 7) Cross-workspace isolation (auth on, two throwaway accounts, cleaned up after).
    results["isolation"] = _isolation_check(app)
    results["all_pass"] = all(v["pass"] for v in results.values() if isinstance(v, dict) and "pass" in v)
    return results


def _isolation_check(app) -> dict[str, Any]:
    import secrets

    from fastapi.testclient import TestClient
    from sqlalchemy import delete, select

    from atlas import auth
    from atlas.data.db import AuthSession, Membership, ResearchRun, User, Workspace, session_scope
    from atlas.research import engine

    prev = os.environ.get("ATLAS_AUTH")
    os.environ["ATLAS_AUTH"] = "required"
    tag = secrets.token_hex(4)
    emails = [f"bench-a-{tag}@example.invalid", f"bench-b-{tag}@example.invalid"]
    pw = secrets.token_urlsafe(16)
    created_ws: list[str] = []
    try:
        uid_a, ws_a = auth.create_user(emails[0], pw, "bench a", f"bench {tag} a", verified=True)
        uid_b, ws_b = auth.create_user(emails[1], pw, "bench b", f"bench {tag} b", verified=True)
        created_ws += [ws_a, ws_b]
        run_id = engine.create_run("copper outlook", {"engine": "deterministic", "depth": "quick", "formats": []}, uid_a, ws_a)
        engine.execute_run(run_id)
        c = TestClient(app)
        c.post("/api/v1/auth/login", json={"email": emails[1], "password": pw})
        leaked = c.get(f"/api/v1/research/{run_id}").status_code != 404 or any(
            r["run_id"] == run_id for r in c.get("/api/v1/research").json())
        return {"target": "no cross-tenant access", "pass": not leaked}
    finally:
        with session_scope() as s:  # remove the throwaway accounts and their data
            uids = s.execute(select(User.user_id).where(User.email.in_(emails))).scalars().all()
            for model, col in ((AuthSession, AuthSession.user_id), (Membership, Membership.user_id)):
                s.execute(delete(model).where(col.in_(uids)))
            for ws in created_ws:
                from atlas.data.db import ResearchEvidence

                rids = s.execute(select(ResearchRun.run_id).where(ResearchRun.workspace_id == ws)).scalars().all()
                s.execute(delete(ResearchEvidence).where(ResearchEvidence.run_id.in_(rids)))
                s.execute(delete(ResearchRun).where(ResearchRun.workspace_id == ws))
            s.execute(delete(User).where(User.user_id.in_(uids)))
            s.execute(delete(Workspace).where(Workspace.workspace_id.in_(created_ws)))
        if prev is None:
            os.environ.pop("ATLAS_AUTH", None)
        else:
            os.environ["ATLAS_AUTH"] = prev
