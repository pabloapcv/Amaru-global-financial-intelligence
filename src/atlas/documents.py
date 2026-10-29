"""Official document index (spec §8.2 search_documents).

Indexes public press-release feeds from official sources: the Federal Reserve Board, the
Bureau of Economic Analysis, the CFTC, USDA's Agricultural Marketing Service, the European Central
Bank and the Bank of England. These are primary-source announcements, not a news
wire: commercial news and sell-side research still need a licensed source, and search results
say so.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import or_, select

from atlas.config import settings
from atlas.data.db import SourceDocument, session_scope, utcnow

log = logging.getLogger("atlas.documents")

FEEDS = [
    {"source": "fed_press", "publisher": "Federal Reserve Board", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"source": "bea", "publisher": "U.S. Bureau of Economic Analysis", "url": "https://apps.bea.gov/rss/rss.xml"},
    {"source": "cftc_press", "publisher": "Commodity Futures Trading Commission", "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"},
    {"source": "usda_ams", "publisher": "USDA Agricultural Marketing Service", "url": "https://www.ams.usda.gov/rss.xml"},
    {"source": "ecb", "publisher": "European Central Bank", "url": "https://www.ecb.europa.eu/rss/press.html"},
    {"source": "boe", "publisher": "Bank of England", "url": "https://www.bankofengland.co.uk/rss/news"},
]
REFRESH_HOURS = 6
COVERAGE = ("Official press releases from the Federal Reserve Board, BEA, CFTC, USDA's Agricultural Marketing Service, "
            "the European Central Bank and the Bank of England only. "
            "No commercial news wire or sell-side research is connected.")
STOPWORDS = set("""a an and are as at be by can do does for from has have how i in is it its me of on or
our over so than that the their this to vs was what whats when where which who why will with
about after against all any between could did during into just like more most much now right should
some today tell them then there these they those under up us very would you your happening latest
recent news report analyze analysis main drivers""".split())


class FeedError(ValueError):
    pass


def _text(el: ET.Element | None) -> str:
    return "" if el is None or el.text is None else el.text.strip()


def _clean(raw: str, limit: int = 2000) -> str:
    """Strip markup and collapse whitespace."""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(raw)))
    return re.sub(r"\s+", " ", txt).strip()[:limit]


def parse_feed(payload: bytes, feed: dict[str, str]) -> list[dict[str, Any]]:
    head = payload[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise FeedError("feed declares a DTD or entities; refusing to parse")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedError(f"malformed feed XML: {exc}") from exc
    out = []
    for item in root.iter("item"):
        url = _text(item.find("link")) or _text(item.find("guid"))
        title = _clean(_text(item.find("title")), 500)
        if not url or not title:
            continue
        published = None
        if pub := _text(item.find("pubDate")):
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                published = None
        summary = _clean(_text(item.find("description")))
        out.append({"source": feed["source"], "publisher": feed["publisher"], "title": title, "url": url,
                    "published_at": published, "category": _clean(_text(item.find("category")), 128) or None,
                    "summary": summary if summary and summary != title else None})
    return out


def _upsert(docs: list[dict[str, Any]]) -> int:
    added = 0
    now = utcnow()
    with session_scope() as s:
        existing = {d.url: d for d in s.execute(
            select(SourceDocument).where(SourceDocument.url.in_([d["url"] for d in docs]))).scalars()}
        for d in docs:
            row = existing.get(d["url"])
            if row is None:
                s.add(SourceDocument(fetched_at=now, first_seen_at=now, **d))
                added += 1
            else:
                for k, v in d.items():
                    setattr(row, k, v)
                row.fetched_at = now
    return added


def refresh(client: httpx.Client | None = None) -> dict[str, Any]:
    """Fetch every feed; a failing feed is reported and the others still update."""
    client = client or httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent},
                                    follow_redirects=True)
    status: dict[str, Any] = {}
    for feed in FEEDS:
        try:
            r = client.get(feed["url"])
            r.raise_for_status()
            docs = parse_feed(r.content, feed)
            if not docs:
                raise FeedError("feed has no items")
            status[feed["source"]] = {"ok": True, "items": len(docs), "new": _upsert(docs)}
        except Exception as exc:  # noqa: BLE001 — keep indexed documents, report the failure
            log.warning("feed %s failed: %s", feed["source"], exc)
            status[feed["source"]] = {"ok": False, "error": str(exc)[:200]}
    return status


def new_since(since: datetime, keywords: list[str], sources: list[str] | None = None,
              limit: int = 5, by_published: bool = False) -> list[dict[str, Any]]:
    """Documents first indexed after `since` (and published no earlier than two days before it), or
    with `by_published` simply published after `since`, whose title or summary contains any keyword
    phrase as whole words."""
    pats = [re.compile(rf"\b{re.escape(k.strip())}\b", re.I) for k in keywords if k.strip()]
    with session_scope() as s:
        if by_published:
            q = select(SourceDocument).where(SourceDocument.published_at >= since)
        else:
            q = select(SourceDocument).where(
                SourceDocument.first_seen_at > since,
                or_(SourceDocument.published_at.is_(None), SourceDocument.published_at >= since - timedelta(days=2)))
        if sources:
            q = q.where(SourceDocument.source.in_(sources))
        rows = s.execute(q.order_by(SourceDocument.published_at.desc())).scalars().all()
        out = []
        for r in rows:
            hit = next((p.pattern for p in pats if p.search(r.title) or p.search(r.summary or "")), None)
            if hit:
                out.append({"title": r.title, "publisher": r.publisher, "source": r.source, "url": r.url,
                            "published": r.published_at.date().isoformat() if r.published_at else None})
    return out[:limit]


def last_fetched() -> datetime | None:
    from sqlalchemy import func

    with session_scope() as s:
        return s.execute(select(func.max(SourceDocument.fetched_at))).scalar()


def refresh_if_due() -> dict[str, Any] | None:
    last = last_fetched()
    if last is None or utcnow().replace(tzinfo=None) - last.replace(tzinfo=None) > timedelta(hours=REFRESH_HOURS):
        return refresh()
    return None


def terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.\-]*[a-z0-9]|[a-z0-9]", query.lower())
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1))


def search(query: str = "", days: int = 90, limit: int = 10, sources: list[str] | None = None) -> dict[str, Any]:
    """Keyword search over indexed documents, newest first among equally relevant hits.
    An empty query returns the most recent documents."""
    since = utcnow().replace(tzinfo=None) - timedelta(days=days)
    words = terms(query)
    with session_scope() as s:
        q = select(SourceDocument).where(SourceDocument.source.is_not(None),
                                         or_(SourceDocument.published_at >= since, SourceDocument.published_at.is_(None)))
        if sources:
            q = q.where(SourceDocument.source.in_(sources))
        if words:
            like = [f"%{w}%" for w in words]
            q = q.where(or_(*[SourceDocument.title.ilike(p) for p in like], *[SourceDocument.summary.ilike(p) for p in like]))
        rows = s.execute(q).scalars().all()
        indexed = s.execute(select(SourceDocument.source).where(SourceDocument.source.is_not(None)).distinct()).scalars().all()
    patterns = [re.compile(rf"\b{re.escape(w)}", re.I) for w in words]
    scored = []
    for r in rows:
        score = sum(3 * bool(p.search(r.title)) + bool(p.search(r.summary or "")) for p in patterns)
        if words and not score:
            continue  # substring hit only (e.g. "rate" in "corporate")
        scored.append((score, r.published_at or datetime.min, r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    hits = [{"title": r.title, "publisher": r.publisher, "source": r.source, "category": r.category,
             "published": r.published_at.date().isoformat() if r.published_at else None, "url": r.url,
             "summary": (r.summary[:400] + "…") if r.summary and len(r.summary) > 400 else r.summary,
             "relevance": score}
            for score, _, r in scored[:limit]]
    return {"query": query, "terms": words, "days": days, "results": hits, "indexed_sources": sorted(indexed),
            "coverage": COVERAGE}
