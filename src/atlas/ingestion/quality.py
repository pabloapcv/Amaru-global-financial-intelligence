"""Pre-publication data quality checks (spec §7.1 step 03, §7.3).

Checks return findings with a severity. `error` findings block publication of the batch;
`warning` findings are published alongside the data so they stay visible.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from atlas.provider_sdk import ParsedPoint

MAX_GAP_DAYS = {"daily": 10, "weekly": 21, "monthly": 62, "quarterly": 190, "annual": 400}
# Ratio of new median level vs previously stored median that indicates a unit/scale change.
UNIT_CHANGE_RATIO = 5.0
# Day-over-day moves this many robust SDs from the median move are flagged for review.
EXTREME_MOVE_Z = 16.0


@dataclass
class Finding:
    check: str
    severity: str  # info | warning | error
    message: str
    detail: dict[str, Any] | None = None


def run_checks(
    points: list[ParsedPoint],
    *,
    frequency: str,
    allow_non_positive: bool,
    previous_values: list[float] | None = None,
) -> tuple[list[ParsedPoint], list[Finding], dict[str, Any]]:
    """Validate a parsed batch. Returns (clean points, findings, metrics)."""
    findings: list[Finding] = []
    metrics: dict[str, Any] = {"rows_raw": len(points)}

    if not points:
        findings.append(Finding("non_empty", "warning", "provider returned no observations"))
        metrics["rows_clean"] = 0
        return [], findings, metrics

    # Duplicates: keep the last occurrence per key (date plus any extra dimensions), in provider order.
    seen: dict[tuple, ParsedPoint] = {}
    dupes = 0
    for p in points:
        key = (p.observed_at, p.dims)
        if key in seen:
            dupes += 1
        seen[key] = p
    if dupes:
        findings.append(Finding("duplicates", "warning", f"{dupes} duplicate keys in payload; last value kept"))
    ordered = sorted(seen.values(), key=lambda p: (p.observed_at, p.dims or ()))

    missing = [p for p in ordered if p.value is None]
    clean = [p for p in ordered if p.value is not None and np.isfinite(p.value)]
    metrics["rows_missing"] = len(missing)
    if missing:
        findings.append(Finding(
            "missing_values", "info",
            f"{len(missing)} dates reported without a value (holidays / not yet published)",
        ))

    future = [p for p in clean if p.observed_at > date.today() + timedelta(days=1)]
    if future:
        findings.append(Finding("future_dates", "error", f"{len(future)} observations dated in the future"))

    if any(p.dims for p in clean):
        # Multi-dimensional records (e.g. trade by product) are not one time series, so the
        # gap / extreme-move / unit-change checks below do not apply.
        negative = [p for p in clean if p.value < 0]
        if negative and not allow_non_positive:
            findings.append(Finding("non_positive", "warning", f"{len(negative)} negative values"))
        metrics["rows_clean"] = len(clean)
        metrics["first_date"] = clean[0].observed_at.isoformat() if clean else None
        metrics["last_date"] = clean[-1].observed_at.isoformat() if clean else None
        return clean, findings, metrics

    if not allow_non_positive:
        nonpos = [p for p in clean if p.value <= 0]
        if nonpos:
            # Not blocking: WTI settled negative on 2020-04-20. Flag for review instead.
            findings.append(Finding(
                "non_positive", "warning", f"{len(nonpos)} non-positive values in a price series",
                {"dates": [p.observed_at.isoformat() for p in nonpos[:10]]},
            ))

    # Gaps larger than the series frequency allows.
    max_gap = MAX_GAP_DAYS.get(frequency, 400)
    gaps = [
        (a.observed_at, b.observed_at)
        for a, b in zip(clean, clean[1:])
        if (b.observed_at - a.observed_at).days > max_gap
    ]
    metrics["gaps"] = len(gaps)
    if gaps:
        findings.append(Finding(
            "gaps", "warning", f"{len(gaps)} gaps longer than {max_gap} days",
            {"examples": [(a.isoformat(), b.isoformat()) for a, b in gaps[:5]]},
        ))

    values = np.array([p.value for p in clean], dtype=float)

    # Outlier moves: robust z-score of first differences (works for yields and prices).
    if len(values) > 30:
        diffs = np.diff(values)
        med = np.median(diffs)
        mad = np.median(np.abs(diffs - med)) * 1.4826
        if mad > 0:
            z = np.abs(diffs - med) / mad
            idx = np.where(z > EXTREME_MOVE_Z)[0]
            metrics["extreme_moves"] = int(len(idx))
            if len(idx):
                findings.append(Finding(
                    "extreme_moves", "warning",
                    f"{len(idx)} day-over-day moves beyond {EXTREME_MOVE_Z:.0f} robust SDs; verify against source",
                    {"dates": [clean[i + 1].observed_at.isoformat() for i in idx[:10]]},
                ))

    # Unit / scale change versus what is already stored.
    if previous_values and len(values):
        prev_med = float(np.median(np.abs(previous_values[-30:])))
        new_med = float(np.median(np.abs(values[-30:])))
        if prev_med > 0 and new_med > 0:
            ratio = max(new_med / prev_med, prev_med / new_med)
            metrics["level_ratio_vs_stored"] = round(ratio, 3)
            if ratio > UNIT_CHANGE_RATIO:
                findings.append(Finding(
                    "unit_change", "error",
                    f"median level changed {ratio:.1f}x versus stored data; possible unit change",
                ))

    metrics["rows_clean"] = len(clean)
    metrics["first_date"] = clean[0].observed_at.isoformat() if clean else None
    metrics["last_date"] = clean[-1].observed_at.isoformat() if clean else None
    return clean, findings, metrics


def blocking(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


def to_json(findings: list[Finding]) -> list[dict[str, Any]]:
    return [asdict(f) for f in findings]
