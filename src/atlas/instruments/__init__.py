"""Canonical instrument registry: loading, DB sync and natural-language resolution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from atlas.data.db import Instrument, InstrumentAlias, MacroSeries

REGISTRY_PATH = Path(__file__).with_name("registry.json")


@dataclass(frozen=True)
class Dataset:
    """A unit of ingestion: one canonical instrument or macro series from one provider."""

    dataset_id: str
    kind: str  # "market" | "macro"
    name: str
    provider_id: str | None
    provider_symbol: str | None
    frequency: str
    unit: str
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def has_source(self) -> bool:
        return bool(self.provider_id and self.provider_symbol)


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text())


@lru_cache(maxsize=1)
def datasets() -> dict[str, Dataset]:
    reg = load_registry()
    out: dict[str, Dataset] = {}
    for inst in reg["instruments"]:
        src = inst["sources"][0] if inst["sources"] else {}
        out[inst["instrument_id"]] = Dataset(
            dataset_id=inst["instrument_id"],
            kind="market",
            name=inst["name"],
            provider_id=src.get("provider_id"),
            provider_symbol=src.get("provider_symbol"),
            frequency=inst["frequency"],
            unit=inst["unit"],
            keywords=tuple(inst.get("keywords", [])),
            tags=tuple(inst.get("tags", [])),
            meta=inst,
        )
    for pos in reg.get("positioning", []):
        out[pos["series_id"]] = Dataset(
            dataset_id=pos["series_id"],
            kind="positioning",
            name=pos["name"],
            provider_id=pos["provider_id"],
            provider_symbol=pos["provider_symbol"],
            frequency=pos["frequency"],
            unit=pos["unit"],
            keywords=tuple(pos.get("keywords", [])),
            tags=tuple(pos.get("tags", [])),
            meta={**pos, "asset_class": "positioning"},
        )
    cm = reg.get("country_macro")
    if cm:
        for c in cm["countries"]:
            for ind in cm["indicators"]:
                sid = country_series_id(c["iso3"], ind["key"])
                out[sid] = Dataset(
                    dataset_id=sid,
                    kind="macro",
                    name=f"{c['name']}: {ind['name']}",
                    provider_id=cm["provider_id"],
                    provider_symbol=f"{c['iso3']}:{ind['code']}",
                    frequency="annual",
                    unit=ind["unit"],
                    # No keywords: countries are resolved separately so 200+ series don't swamp entity extraction.
                    tags=("country", c["iso3"].lower(), *c["groups"]),
                    meta={"country": c["iso3"], "country_name": c["name"], "indicator": ind["indicator"], "refresh": "weekly_poll",
                          "indicator_key": ind["key"], "groups": c["groups"], "asset_class": "macro",
                          "description": f"{ind['name']} for {c['name']}, annual (World Bank WDI, code {ind['code']})."},
                )
    for s in reg["macro_series"]:
        out[s["series_id"]] = Dataset(
            dataset_id=s["series_id"],
            kind="macro",
            name=s["name"],
            provider_id=s["provider_id"],
            provider_symbol=s["provider_symbol"],
            frequency=s["frequency"],
            unit=s["unit"],
            keywords=tuple(s.get("keywords", [])),
            tags=tuple(s.get("tags", [])),
            meta=s,
        )
    return out


def country_series_id(iso3: str, key: str) -> str:
    return f"WB.{iso3}.{key}"


def countries() -> list[dict[str, Any]]:
    return load_registry().get("country_macro", {}).get("countries", [])


def country_indicators() -> list[dict[str, Any]]:
    return load_registry().get("country_macro", {}).get("indicators", [])


def extract_countries(text: str) -> list[str]:
    """ISO3 codes of countries named in free text, plus group expansions (Latin America, G20)."""
    q = _norm(text)
    found: list[str] = []
    for c in countries():
        names = [c["name"].lower(), *c.get("aliases", [])]
        if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", q) for n in names) and c["iso3"] not in found:
            found.append(c["iso3"])
    groups = []
    if re.search(r"latin america|latam|south america", q):
        groups.append("latam")
    if re.search(r"\bg-?20\b", q):
        groups.append("g20")
    for g in groups:
        found += [c["iso3"] for c in countries() if g in c["groups"] and c["iso3"] not in found]
    return found


def positioning_for(instrument_id: str) -> str | None:
    """COT positioning dataset linked to a market instrument, if any."""
    for pos in load_registry().get("positioning", []):
        if pos["instrument_id"] == instrument_id:
            return pos["series_id"]
    return None


def get_dataset(dataset_id: str) -> Dataset | None:
    return datasets().get(dataset_id)


def sync_registry(session: Session) -> int:
    """Upsert the registry into the instruments / aliases / macro_series tables."""
    reg = load_registry()
    n = 0
    for inst in reg["instruments"]:
        cols = {k: inst.get(k) for k in (
            "instrument_id", "name", "asset_class", "symbol", "exchange", "currency", "unit",
            "frequency", "price_basis", "description", "tags", "coverage_note",
        )}
        session.merge(Instrument(**cols))
        for src in inst["sources"]:
            session.merge(InstrumentAlias(
                provider_id=src["provider_id"],
                provider_symbol=src["provider_symbol"],
                instrument_id=inst["instrument_id"],
            ))
        n += 1
    for ds in datasets().values():
        if ds.dataset_id.startswith("WB."):
            session.merge(MacroSeries(
                series_id=ds.dataset_id, name=ds.name, country=ds.meta["country"], indicator=ds.meta["indicator"],
                frequency="annual", unit=ds.unit, seasonal_adjustment=None, provider_id=ds.provider_id,
                provider_symbol=ds.provider_symbol, description=ds.meta["description"], tags=list(ds.tags),
            ))
            n += 1
    for s in reg["macro_series"]:
        cols = {k: s.get(k) for k in (
            "series_id", "name", "country", "indicator", "frequency", "unit", "seasonal_adjustment",
            "provider_id", "provider_symbol", "description", "tags",
        )}
        session.merge(MacroSeries(**cols))
        n += 1
    return n


# ---------------------------------------------------------------- resolution


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def resolve(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Resolve free text to canonical datasets.

    Scoring is deterministic: exact id/symbol matches beat keyword phrase matches, which
    beat tag and name-token matches. Returns ranked candidates with the matched term so
    the caller can show *why* something resolved.
    """
    q = _norm(query)
    tokens = set(re.findall(r"[a-z0-9/&\.]+", q))
    scored: list[tuple[float, Dataset, str]] = []
    for ds in datasets().values():
        best, why = 0.0, ""
        if q == ds.dataset_id.lower() or q == str(ds.meta.get("symbol", "")).lower():
            best, why = 100.0, "exact id/symbol"
        for kw in ds.keywords:
            kw_n = _norm(kw)
            if re.search(rf"(?<![a-z0-9]){re.escape(kw_n)}(?![a-z0-9])", q):
                score = 50 + len(kw_n)  # prefer longer, more specific phrases
                if score > best:
                    best, why = score, f"keyword '{kw}'"
        for tag in ds.tags:
            if tag.replace("_", " ") in tokens or tag in tokens:
                if 10 > best:
                    best, why = 10.0, f"tag '{tag}'"
        name_tokens = set(re.findall(r"[a-z0-9]+", ds.name.lower())) - {"us", "u", "s", "the", "per", "of"}
        overlap = len(name_tokens & tokens)
        if overlap and 5 * overlap > best:
            best, why = 5.0 * overlap, "name match"
        if best > 0:
            scored.append((best, ds, why))
    scored.sort(key=lambda x: (-x[0], x[1].dataset_id))
    return [
        {
            "dataset_id": ds.dataset_id,
            "kind": ds.kind,
            "name": ds.name,
            "score": score,
            "matched": why,
            "has_source": ds.has_source,
            "frequency": ds.frequency,
            "unit": ds.unit,
        }
        for score, ds, why in scored[:limit]
    ]


def extract_entities(text: str, min_score: float = 50) -> list[str]:
    """All datasets mentioned by keyword in a longer question (used by the planner)."""
    hits = resolve(text, limit=50)
    return [h["dataset_id"] for h in hits if h["score"] >= min_score]
