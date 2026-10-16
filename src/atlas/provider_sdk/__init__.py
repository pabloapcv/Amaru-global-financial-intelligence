"""Data-source adapter interface (spec §17 Phase 1: 'Build data-source adapter interfaces').

Every provider connector implements `ProviderAdapter`. The ingestion pipeline only talks to
this interface, so adding EIA, USDA, CFTC or a licensed market-data vendor means writing one
adapter class and registering it in `ADAPTERS`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Callable

import httpx


@dataclass(frozen=True)
class FetchResult:
    request: str          # canonical description of the request (URL), for lineage
    payload: bytes        # raw provider response, stored verbatim before parsing
    content_type: str


@dataclass(frozen=True)
class ParsedPoint:
    observed_at: date
    value: float | None   # None = provider reported a missing value (e.g. holiday '.')
    fields: dict[str, float] | None = None  # multi-field records (e.g. COT position breakdown)
    dims: tuple[tuple[str, str], ...] | None = None  # extra key dimensions (e.g. trade flow, HS chapter)


class ProviderError(RuntimeError):
    pass


class ProviderAdapter(ABC):
    provider_id: str
    display_name: str
    homepage: str
    licence_note: str

    @abstractmethod
    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        """Retrieve raw data for `symbol`, incrementally from `since` when supported."""

    @abstractmethod
    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        """Parse a raw payload into observations. Must be pure (replayable from raw storage)."""

    @abstractmethod
    def citation(self, symbol: str) -> str:
        """Human-readable citation for a dataset."""

    @abstractmethod
    def source_url(self, symbol: str) -> str:
        """Public landing page for a dataset."""


def with_retries(
    fn: Callable[[], httpx.Response],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Call `fn` with exponential backoff on transport errors, 429 and 5xx.

    Honors Retry-After when the provider sends one.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = fn()
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else base_delay * 2**i
                last_exc = ProviderError(f"HTTP {resp.status_code}")
                if i < attempts - 1:
                    sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except httpx.TransportError as exc:
            last_exc = exc
            if i < attempts - 1:
                sleep(base_delay * 2**i)
    raise ProviderError(f"failed after {attempts} attempts: {last_exc}")


def get_adapter(provider_id: str) -> ProviderAdapter:
    from atlas.ingestion.cftc import CftcAdapter
    from atlas.ingestion.comtrade import ComtradeAdapter
    from atlas.ingestion.fred import FredAdapter
    from atlas.ingestion.worldbank import WorldBankAdapter

    adapters: dict[str, Callable[[], ProviderAdapter]] = {
        "fred": FredAdapter, "cftc": CftcAdapter, "worldbank": WorldBankAdapter, "comtrade": ComtradeAdapter,
    }
    if provider_id not in adapters:
        raise ProviderError(f"no adapter registered for provider '{provider_id}'")
    return adapters[provider_id]()
