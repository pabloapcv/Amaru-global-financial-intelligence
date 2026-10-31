"""Verification layer (spec §8.4, §12 step 4).

Every number in a narrative must trace to a retrieved observation or executed calculation,
and every citation label must reference a real evidence item.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

NUM_RE = re.compile(r"(?<![\w.])[-+−]?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])[-+−]?\$?\d+(?:\.\d+)?")
CITE_RE = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")
DATE_RE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:19|20)\d{2}\b")
# Ignore horizon/count phrases such as "5-year", "3 months", "12-month", "Q3", "G20", "No. 11".
SKIP_CONTEXT = re.compile(
    r"^\s*-?\s*(?:%\s*(?:CI|confidence|interval)|y\b|yr|year|month|quarter|week|day|period|observation|obs\b|lag|fold|origin|sd\b|robust|standard dev)",
    re.I,
)


CONFIDENCE_LEVELS = {90.0, 95.0, 97.5, 99.0, 99.5, 99.9}
SIGNIFICANCE_LEVELS = {1.0, 5.0, 10.0}


def _collect_numbers(obj: Any, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        for m in NUM_RE.finditer(obj):
            try:
                out.append(float(m.group().replace(",", "").replace("$", "").replace("−", "-")))
            except ValueError:
                pass


def evidence_numbers(evidence: Iterable[dict[str, Any]]) -> list[float]:
    nums: list[float] = []
    for e in evidence:
        _collect_numbers(e.get("result"), nums)
    # Allowed derived forms: sign flips, %↔fraction, %↔bp scale.
    derived = set()
    for n in nums:
        for v in (n, -n, abs(n), n * 100, n / 100, n * 10000):
            derived.add(round(v, 10))
    return sorted(derived)


def _matches(value: float, decimals: int, pool: list[float]) -> bool:
    tol_abs = 0.5 * 10 ** (-decimals) + 1e-9
    import bisect

    lo = bisect.bisect_left(pool, value - max(tol_abs, abs(value) * 0.005))
    hi = bisect.bisect_right(pool, value + max(tol_abs, abs(value) * 0.005))
    for cand in pool[lo:hi]:
        if abs(cand - value) <= tol_abs or (abs(value) >= 10 and abs(cand - value) / abs(value) <= 0.005):
            return True
    return False


_THOUSANDS_DOT = re.compile(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+)(?:,(\d+))?(?![\d.,]\d)")
_DECIMAL_COMMA = re.compile(r"(?<![\w.,])(\d+),(\d+)(?![\d.,]\d)")


def normalize_decimal_comma(text: str) -> str:
    """'1.234,56' -> '1234.56' and '14,81' -> '14.81' (Spanish/Portuguese number format)."""
    text = _THOUSANDS_DOT.sub(lambda m: m.group(1).replace(".", "") + (f".{m.group(2)}" if m.group(2) else ""), text)
    return _DECIMAL_COMMA.sub(r"\1.\2", text)


def verify(narrative: str, evidence: list[dict[str, Any]], question: str = "", decimal_comma: bool = False) -> dict[str, Any]:
    labels = {e["label"] for e in evidence}
    cited = set()
    invalid = []
    for m in CITE_RE.finditer(narrative):
        for lab in re.split(r"\s*,\s*", m.group(1)):
            if lab in labels:
                cited.add(lab)
            else:
                invalid.append(lab)

    text = CITE_RE.sub(" ", narrative)
    text = DATE_RE.sub(" ", text)
    if decimal_comma:
        text = normalize_decimal_comma(text)
    pool = evidence_numbers(evidence)
    question_nums = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", question)}
    checked, unverified = 0, []
    for m in NUM_RE.finditer(text):
        raw = m.group()
        tail = text[m.end(): m.end() + 14]
        if SKIP_CONTEXT.match(tail):
            continue
        clean = raw.replace(",", "").replace("$", "").replace("−", "-").lstrip("+")
        try:
            value = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        if decimals == 0 and abs(value) <= 12 and not tail.lstrip().startswith(("%", "bp")):
            continue  # small integers are almost always counts or ordinals
        if abs(value) in question_nums:
            continue
        if value in CONFIDENCE_LEVELS and tail.lstrip().startswith("%") and re.search(r"\bat\s*$", text[max(0, m.start() - 6): m.start()]):
            continue  # "VaR at 95%": a confidence level, not a data claim
        if value in SIGNIFICANCE_LEVELS and tail.lstrip().startswith("%") and re.search(r"\bsignificant at\s*$", text[max(0, m.start() - 16): m.start()]):
            continue  # "significant at 5%": a test threshold, not a data claim
        checked += 1
        if not _matches(value, decimals, pool):
            start = max(0, m.start() - 50)
            unverified.append({"value": raw, "context": text[start: m.end() + 30].replace("\n", " ").strip()})
    return {
        "numbers_checked": checked,
        "numbers_verified": checked - len(unverified),
        "unverified_numbers": unverified,
        "citations_used": sorted(cited, key=lambda x: int(x[1:])),
        "invalid_citations": sorted(set(invalid)),
        "uncited_evidence": sorted(labels - cited, key=lambda x: int(x[1:])),
        "passed": not unverified and not invalid,
    }
