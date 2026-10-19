import httpx
import pytest

from atlas import documents

FED = b"""<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel><title>FRB</title>
<item><title>Minutes of the Board&#39;s discount rate meetings</title>
<link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20261013a.htm]]></link>
<description><![CDATA[Minutes of the Board&#39;s discount rate meetings]]></description>
<category>Monetary Policy</category><pubDate><![CDATA[Tue, 13 Oct 2026 14:00:00 GMT]]></pubDate></item>
</channel></rss>"""
BEA = b"""<?xml version="1.0" encoding="us-ascii"?>
<rss version="2.0"><channel><title>BEA</title>
<item name="GDP"><title>GDP (Third Estimate), 2nd Quarter 2026</title>
<link>https://www.bea.gov/news/2026/gdp-third-estimate</link>
<description><![CDATA[<p>Real gross domestic product (GDP) increased at an annual rate of 2.2 percent.</p>]]></description>
<pubDate>Wed, 30 Sep 2026 08:30:00 EDT</pubDate></item>
<item><title>Corporate profits by industry</title><link>https://www.bea.gov/news/2026/profits</link>
<pubDate>Wed, 30 Sep 2026 08:30:00 EDT</pubDate></item>
</channel></rss>"""


def _client(cftc_status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        if "federalreserve" in req.url.host:
            return httpx.Response(200, content=FED)
        if "bea" in req.url.host:
            return httpx.Response(200, content=BEA)
        return httpx.Response(cftc_status)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_feed_cleans_markup_and_normalizes_dates():
    docs = documents.parse_feed(BEA, documents.FEEDS[1])
    assert docs[0]["summary"].startswith("Real gross domestic product (GDP) increased")
    assert "<p>" not in docs[0]["summary"]
    assert docs[0]["published_at"].isoformat() == "2026-09-30T12:30:00"  # EDT -> UTC, naive
    fed = documents.parse_feed(FED, documents.FEEDS[0])[0]
    assert fed["title"] == "Minutes of the Board's discount rate meetings" and fed["summary"] is None
    assert fed["category"] == "Monetary Policy"


def test_parse_feed_refuses_entity_declarations():
    with pytest.raises(documents.FeedError):
        documents.parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss/>', documents.FEEDS[0])


def test_refresh_is_idempotent_and_search_matches_whole_words(monkeypatch):
    status = documents.refresh(_client(cftc_status=503))
    assert status["fed_press"] == {"ok": True, "items": 1, "new": 1}
    assert status["cftc_press"]["ok"] is False  # one failing feed does not stop the others
    assert documents.refresh(_client())["bea"]["new"] == 0  # re-fetch updates, never duplicates
    monkeypatch.setattr(documents, "utcnow", lambda: __import__("datetime").datetime(2026, 10, 19))
    hits = documents.search("discount rate", days=30)["results"]
    # "rate" must not match "Corporate"; the GDP summary mentions "rate" so it ranks below the title hit.
    assert hits[0]["title"].startswith("Minutes") and all("Corporate" not in h["title"] for h in hits)
    assert [h["title"] for h in documents.search("GDP", days=30)["results"]] == ["GDP (Third Estimate), 2nd Quarter 2026"]
    assert len(documents.search("", days=30)["results"]) == 3
    assert documents.search("", days=5)["results"] == []


def test_search_documents_tool_records_evidence():
    from atlas.research import tools

    documents.refresh(_client())
    ctx = tools.ToolContext()
    out = tools.TOOLS["search_documents"]["fn"](ctx, {"query": "GDP", "days": 730})
    assert out["results"][0]["publisher"] == "U.S. Bureau of Economic Analysis"
    assert "No commercial news wire" in out["coverage"]
    assert ctx.evidence[0].sources[0]["dataset_id"] == "DOCS.BEA"
