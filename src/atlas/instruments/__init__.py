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
STALE_POLICY_RATE_DAYS = 60  # some banks reach the BIS weeks late (South Korea was 23 days behind); longer means the series has probably stopped


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
    import os

    commercial = os.getenv("ATLAS_COMMERCIAL_MODE", "false").lower() == "true"
    for inst in reg["instruments"]:
        src = inst["sources"][0] if inst["sources"] else {}
        lic = inst.get("licence") or {}
        if commercial and lic.get("class") == "third_party_restricted":
            # Redistributing these series needs the owner's licence: leave them off until then.
            src = {}
            inst = {**inst, "coverage_note": f"Not available in this commercial deployment: {lic.get('note')}"}
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
                          "licence": cm.get("licence"),
                          "indicator_key": ind["key"], "groups": c["groups"], "asset_class": "macro",
                          "description": f"{ind['name']} for {c['name']}, annual (World Bank WDI, code {ind['code']})."},
                )
    pol = reg.get("policy_rates", {})
    for a in pol.get("areas", []):
        sid = policy_rate_id(a["code"])
        out[sid] = Dataset(
            dataset_id=sid, kind="macro", name=f"{a['name']}: central bank policy rate", provider_id=pol["provider_id"],
            provider_symbol=f"CBPOL.{a['code']}", frequency="daily", unit="percent", keywords=tuple(a["keywords"]), tags=("policy_rate",),
            meta={"country": a["code"], "countries": a["countries"], "indicator": "policy_rate", "asset_class": "macro", "refresh": "weekly_poll",
                  "expected_lag_days": STALE_POLICY_RATE_DAYS // 2, "licence": pol.get("licence"), "description": f"Central bank policy rate for {a['name']}, daily (BIS)."},
        )
    cs = {c["iso3"]: c for c in reg.get("country_macro", {}).get("countries", [])}
    for spec in reg.get("country_series", {}).get("series", []):
        for iso3 in spec["countries"]:
            c = cs[iso3]
            sid = country_extra_id(spec, iso3)
            out[sid] = Dataset(
                dataset_id=sid, kind="macro", name=f"{c['name']}: {spec['name']}", provider_id=spec["provider_id"],
                provider_symbol=spec["symbol"].format(iso3=iso3, iso2=c["iso2"]), frequency=spec["frequency"], unit=spec["unit"],
                tags=("country", iso3.lower(), spec["key"].lower()),
                meta={"country": iso3, "country_name": c["name"], "indicator": spec["key"].lower(), "indicator_key": spec["key"],
                      "asset_class": "macro", "refresh": "weekly_poll" if spec["frequency"] in ("quarterly", "annual") else "once_daily",
                      "licence": spec["licence"], "description": spec["description"], "forecast": bool(spec.get("forecast")),
                      **({"expected_lag_days": spec["expected_lag_days"]} if "expected_lag_days" in spec else {})},
            )
    names = {c["iso3"]: c["name"] for c in reg.get("country_macro", {}).get("countries", [])} | {"WLD": "World"}
    for rep, par in reg.get("trade_pairs", {}).get("pairs", []):
        tid = trade_dataset_id(rep, par)
        out[tid] = Dataset(
            dataset_id=tid, kind="trade", name=f"Trade: {names[rep]} with {names[par]}", provider_id="comtrade",
            provider_symbol=f"{rep}:{par}", frequency="annual", unit="USD",
            tags=("trade", rep.lower(), par.lower()),
            meta={"reporter": rep, "partner": par, "asset_class": "trade", "refresh": "weekly_poll",
                  "licence": reg.get("trade_pairs", {}).get("licence"),
                  "description": f"{names[rep]}'s declared goods trade with {names[par]} (UN Comtrade, annual, USD): totals and HS-chapter breakdown."},
        )
    fund = reg.get("fundamentals", {})
    for c in fund.get("commodities", []):
        fid = f"FUND.{c['key']}"
        out[fid] = Dataset(
            dataset_id=fid, kind="fundamentals", name=f"{c['name']}: supply and demand by country (USDA PSD)",
            provider_id=fund["provider_id"], provider_symbol=c["provider_symbol"], frequency="annual", unit=c["unit"],
            # No keywords: commodity questions resolve to the price instrument; fundamentals attach to it.
            tags=("fundamentals", c["key"].lower()),
            meta={**c, "asset_class": "fundamentals", "refresh": "weekly_poll", "licence": fund.get("licence"),
                  "description": f"{c['name']} production, consumption, trade and stocks by country and market year "
                                 f"(USDA FAS PSD; the latest market year is a USDA projection, revised monthly)."},
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


LICENCE_LABELS = {"public_domain": "Public domain", "cc_by": "CC BY 4.0", "check_terms": "Check terms",
                  "third_party_restricted": "Restricted (third party)"}


def licence_of(ds: Dataset | None) -> dict[str, Any] | None:
    """{class, label, owner, note} for a dataset, or None when no source is configured."""
    lic = (ds.meta.get("licence") if ds else None) or None
    return {**lic, "label": LICENCE_LABELS.get(lic["class"], lic["class"])} if lic else None


def fundamentals_for(text_or_id: str) -> str | None:
    """FUND.* dataset for a commodity key, alias, name or price instrument id; None if not covered."""
    t = _norm(text_or_id)
    for c in load_registry().get("fundamentals", {}).get("commodities", []):
        if text_or_id in (f"FUND.{c['key']}", c.get("instrument_id")) or t == c["key"].lower() \
                or any(re.search(rf"\b{re.escape(a)}\b", t) for a in c["aliases"]):
            return f"FUND.{c['key']}"
    return None


def trade_dataset_id(reporter: str, partner: str) -> str:
    return f"TRADE.{reporter}.{partner}"


def trade_pairs() -> list[tuple[str, str]]:
    return [tuple(p) for p in load_registry().get("trade_pairs", {}).get("pairs", [])]  # type: ignore[misc]


@lru_cache(maxsize=1)
def hs_chapters() -> dict[str, str]:
    return json.loads(REGISTRY_PATH.with_name("hs_chapters.json").read_text())["chapters"]


def country_series_id(iso3: str, key: str) -> str:
    return f"WB.{iso3}.{key}"


def country_extra_id(spec: dict[str, Any], iso3: str) -> str:
    """IMF.BRA.CPI_YOY, BIS.BRA.REER, WEO.BRA.WEO_GDP_GROWTH and so on."""
    return f"{spec['prefix']}.{iso3}.{spec['key']}"


def country_series_specs() -> list[dict[str, Any]]:
    return load_registry().get("country_series", {}).get("series", [])


def policy_rate_id(area: str) -> str:
    return f"BIS.POLICY.{area}"


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
        if ds.dataset_id.startswith(("WB.", "BIS.", "IMF.", "WEO.")):
            session.merge(MacroSeries(
                series_id=ds.dataset_id, name=ds.name, country=ds.meta["country"], indicator=ds.meta["indicator"],
                frequency=ds.frequency, unit=ds.unit, seasonal_adjustment=None, provider_id=ds.provider_id,
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
    hits = [h for h in resolve(text, limit=50) if h["score"] >= min_score]
    # A keyword only inside a longer matched one names the longer thing: "palm oil" is not crude "oil".
    words = {h["dataset_id"]: _norm(h["matched"][len("keyword '"):-1]) for h in hits if h["matched"].startswith("keyword '")}
    q = _norm(text)
    out = []
    for h in hits:
        kw = words.get(h["dataset_id"])
        longer = [w for w in words.values() if kw and w != kw and re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", w)]
        rest = q
        for w in longer:
            rest = re.sub(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", " ", rest)
        if kw and longer and not re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", rest):
            continue
        out.append(h["dataset_id"])
    return out
