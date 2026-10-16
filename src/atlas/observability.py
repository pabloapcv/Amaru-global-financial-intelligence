"""Prometheus-format metrics (spec §6 monitoring, §16 targets) without extra dependencies.

HTTP metrics are recorded in-process by middleware; business metrics (research runs,
pipeline health, dataset freshness) are read from the database at scrape time.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_lock = threading.Lock()
_requests: dict[tuple[str, str, str], int] = defaultdict(int)
_latency_sum: dict[tuple[str, str], float] = defaultdict(float)
_latency_count: dict[tuple[str, str], int] = defaultdict(int)
_latency_buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
_started = time.time()


def record(method: str, route: str, status: int, seconds: float) -> None:
    key = (method, route)
    with _lock:
        _requests[(method, route, str(status))] += 1
        _latency_sum[key] += seconds
        _latency_count[key] += 1
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                _latency_buckets[key][i] += 1


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**kv: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in kv.items()) + "}"


def render() -> str:
    from atlas.data.access import data_status
    from atlas.data.db import PipelineRun, ResearchRun, session_scope, utcnow
    from atlas.research import llm

    out: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[str, float]]) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{labels} {value}" for labels, value in samples)

    with _lock:
        metric("atlas_http_requests_total", "counter", "HTTP requests by method, route template and status.",
               [(_labels(method=m, route=r, status=s), n) for (m, r, s), n in sorted(_requests.items())])
        hist: list[tuple[str, float]] = []
        for (m, r), counts in sorted(_latency_buckets.items()):
            for b, c in zip(BUCKETS, counts):
                hist.append((f"_bucket{_labels(method=m, route=r, le=str(b))}", c))
            hist.append((f"_bucket{_labels(method=m, route=r, le='+Inf')}", _latency_count[(m, r)]))
            hist.append((f"_sum{_labels(method=m, route=r)}", round(_latency_sum[(m, r)], 6)))
            hist.append((f"_count{_labels(method=m, route=r)}", _latency_count[(m, r)]))
    out.append("# HELP atlas_http_request_duration_seconds HTTP request latency.")
    out.append("# TYPE atlas_http_request_duration_seconds histogram")
    out.extend(f"atlas_http_request_duration_seconds{suffix} {v}" for suffix, v in hist)

    since = utcnow() - timedelta(days=30)
    with session_scope() as s:
        runs = s.execute(select(ResearchRun.status, ResearchRun.engine, func.count()).group_by(ResearchRun.status, ResearchRun.engine)).all()
        pipes = s.execute(select(PipelineRun.status, func.count()).where(PipelineRun.started_at >= since).group_by(PipelineRun.status)).all()
    metric("atlas_research_runs", "gauge", "Research runs by status and engine (all time).",
           [(_labels(status=st, engine=eng or "none"), n) for st, eng, n in runs])
    metric("atlas_pipeline_runs_30d", "gauge", "Ingestion runs in the last 30 days by status.",
           [(_labels(status=st), n) for st, n in pipes])
    fresh: dict[tuple[str, str], int] = defaultdict(int)
    for row in data_status():
        fresh[(row["kind"], row["freshness"])] += 1
    metric("atlas_datasets", "gauge", "Datasets by kind and freshness status.",
           [(_labels(kind=k, freshness=f), n) for (k, f), n in sorted(fresh.items())])
    metric("atlas_llm_configured", "gauge", "1 if Anthropic credentials are configured.", [("", int(llm.available()))])
    metric("atlas_uptime_seconds", "gauge", "Seconds since the API process started.", [("", round(time.time() - _started, 1))])
    return "\n".join(out) + "\n"
