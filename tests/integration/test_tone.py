"""Central-bank tone: fetching statements, the reading, the decision check and where it surfaces."""

import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

BODY = "Recent indicators suggest that economic activity has continued to expand at a solid pace, and the Committee is attentive to both sides of its mandate. "


def fed_page(target: str, extra: str = "") -> str:
    return f"""<html><div id="other"><p>Skip me</p></div><div id="article"><h3>Federal Reserve issues FOMC statement</h3>
    <p>For release at 2:00 p.m. EDT</p><script>var x = "The Committee";</script>
    <p>{BODY}</p><p>The Committee decided to maintain the target range for the federal funds rate at {target} percent.</p>{extra}
    <p>For media inquiries, please email us.</p><p>Implementation Note issued today</p></div></html>"""


def ecb_page(decision: str) -> str:
    return f"""<html><head><script>x</script></head><main><div class="section"><p>Jump to the transcript of the questions and answers</p>
    <p>{BODY}</p><p>The Governing Council today decided to {decision}.</p><p>We are now ready to take your questions.</p>
    <p>Question: Will you cut again?</p><p>I would not speculate.</p></div></main></html>"""


class Sites:
    """The Fed's and ECB's sites, as far as Amaru can tell."""

    def __init__(self, today):
        self.fed = [(today - timedelta(days=50), fed_page("4 to 4-1/4")), (today - timedelta(days=2), fed_page("3-3/4 to 4"))]
        self.ecb = [(today - timedelta(days=1), ecb_page("keep the three key ECB interest rates unchanged"))]
        self.gets: list[str] = []
        self.fed_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.gets.append(path)
        if path == "/json/ne-press.json":
            if self.fed_down:
                return httpx.Response(503)
            items = [{"d": d.strftime("%m/%d/%Y") + " 2:00:00 PM", "t": "Federal Reserve issues FOMC statement", "l": f"/fomc{i}.htm"} for i, (d, _) in enumerate(self.fed)]
            items.append({"d": "1/2/2026 4:00:00 PM", "t": "Federal Reserve Board announces approval of application", "l": "/orders.htm"})
            return httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps(items).encode())
        if path.endswith("index_include.en.html"):
            links = "".join(f'<a href="/press/press_conference/monetary-policy-statement/{d.year}/html/ecb.is{d:%y%m%d}~abc.en.html">x</a>' for d, _ in self.ecb)
            return httpx.Response(200, text=links)
        if path.startswith("/fomc"):
            return httpx.Response(200, text=self.fed[int(path[5:-4])][1])
        for d, page in self.ecb:
            if f"is{d:%y%m%d}" in path:
                return httpx.Response(200, text=page)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class Reader:
    """Stands in for Claude: replies per bank, and records what it was given."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0) if self.replies else {}
        if reply == "refused":
            return SimpleNamespace(stop_reason="refusal", content=[], usage=SimpleNamespace(), model="m")
        base = {"action": "hold", "change_bp": 0, "stance": 0, "balance_sheet": "steady", "dissents": 0, "summary": "Nothing much changed."}
        text = reply if isinstance(reply, str) else json.dumps({**base, **reply})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)], model="claude-test",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


@pytest.fixture()
def sites(monkeypatch):
    from atlas import tone
    from atlas.data.db import CentralBankStatement, session_scope, utcnow

    def wipe():
        with session_scope() as s:
            s.query(CentralBankStatement).delete()

    monkeypatch.setattr(tone, "PAUSE", {"fed": 0, "ecb": 0})
    wipe()
    yield Sites(utcnow().date())
    wipe()


def test_statement_pages_give_only_the_statement():
    from atlas import tone

    fed = tone.fed_text(fed_page("3-1/2 to 3-3/4", "<p>Voting against were A and B, who preferred to raise the target range by 1/4 percentage point.</p>"))
    assert fed.startswith("Recent indicators") and "Voting against" in fed
    assert "Skip me" not in fed and "var x" not in fed and "media inquiries" not in fed
    assert tone.fed_target(fed) == 3.75
    ecb = tone.ecb_text(ecb_page("keep the three key ECB interest rates unchanged"))
    assert ecb.startswith("Recent indicators") and ecb.endswith("ready to take your questions.") and "speculate" not in ecb

    # The check reads the decision from the statement's own words.
    assert tone.stated_change_bp("fed", "target range for the federal funds rate to 3-1/2 to 3-3/4 percent", "target range for the federal funds rate at 3-3/4 to 4 percent") == -25
    assert tone.stated_change_bp("fed", "no target here", "target range for the federal funds rate at 4 percent") is None
    assert tone.stated_change_bp("ecb", "we decided to lower the three key ECB interest rates by 25 basis points.", None) == -25
    assert tone.stated_change_bp("ecb", "The Governing Council decided to raise the key ECB interest rates by 0.50 percentage points.", None) == 50
    assert tone.stated_change_bp("ecb", "We decided to keep the three key ECB interest rates unchanged.", None) == 0
    assert tone.consistent("hold", 0, 0) and not tone.consistent("hold", 0, -25) and tone.consistent("cut", -25, -25)
    assert not tone.consistent("hike", 25, -25) and not tone.consistent("cut", -50, -25) and tone.consistent("cut", -25, None)


def test_refresh_reads_each_statement_once_against_the_previous_one(sites, monkeypatch):
    from atlas import tone

    reader = Reader([{"summary": "Oldest Fed statement."},
                     {"action": "cut", "change_bp": -25, "stance": -1, "summary": "The Fed cut and softened its language.", "dissents": 2},
                     {"stance": 1, "summary": "ECB more worried about inflation."}])
    # Readings go oldest first across banks, so the ECB's comes last here (its statement is the newest).
    res = tone.refresh(sites.client(), reader)
    assert res["fed"] == {"ok": True, "new": 2, "failed": []} and res["ecb"]["new"] == 1 and res["reading"] == {"read": 3, "failed": 0}

    second = reader.calls[1]
    assert "Previous statement" in second["messages"][0]["content"] and "4 to 4-1/4" in second["messages"][0]["content"]
    assert second["output_config"]["format"]["type"] == "json_schema" and "Federal Open Market" in second["system"]
    assert "European Central Bank" in reader.calls[2]["system"]

    fed = tone.history("fed")
    assert [s["action"] for s in fed] == ["cut", "hold"]  # newest first
    new, old = fed
    assert new["stance_label"] == "more dovish" and new["compared_with"] == old["meeting_date"]
    assert new["text_change_bp"] == -25 and new["consistent"] is True and new["dissents"] == 2
    assert old["compared_with"] is None and old["stance_label"] == "no earlier statement to compare"
    assert "text" not in new
    ecb = tone.latest_reading("ecb")
    assert ecb["stance_label"] == "no earlier statement to compare" and ecb["text_change_bp"] == 0 and ecb["dissents"] is None
    assert tone.describe(new).endswith("cut rates 25 bp; language more dovish (-1).")

    # Nothing new: no page is fetched again and nothing is re-read.
    sites.gets.clear()
    calls = len(reader.calls)
    tone.refresh(sites.client(), reader)
    assert len(reader.calls) == calls and not any(p.startswith("/fomc") or ".en.html" in p and "index_include" not in p for p in sites.gets)


def test_misreads_are_flagged_and_bad_replies_are_not_stored(sites):
    from atlas import tone

    tone.sync(sites.client())
    # The second statement cuts 25 bp, but this reading says hold: kept, flagged.
    bad = Reader(["not json", "refused", {"stance": 7}])
    assert tone.read_pending(bad, limit=1)["failed"] == 1
    assert tone.read_pending(bad, limit=1)["failed"] == 1
    assert tone.read_pending(bad, limit=1)["failed"] == 1
    assert tone.read_pending(Reader(), limit=1)["read"] == 1  # the first has hit its limit; the next statement is read
    assert [s["read"] for s in tone.history("fed")] == [True, False]
    fed = tone.latest_reading("fed")
    assert fed["consistent"] is False and fed["text_change_bp"] == -25 and fed["action"] == "hold"

    assert tone.overview()["unread"] == 2
    assert tone.read_pending()["skipped"] == "no Anthropic credentials"  # ATLAS_LLM=off in tests


def test_fed_index_handles_old_entries_without_a_time():
    from atlas import tone

    items = [{"d": "10/28/2026 2:00:00 PM", "t": "Federal Reserve issues FOMC statement", "l": "/new.htm"},
             {"d": "1/31/2006", "t": "FOMC statement", "l": "/old.htm"}]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps(items).encode())))
    assert [str(e["meeting_date"]) for e in tone.fed_index(client)] == ["2026-10-28", "2006-01-31"]


def test_an_api_outage_stops_reading_without_counting_against_the_statement(sites):
    import anthropic

    from atlas import tone

    tone.sync(sites.client())
    reader = Reader()

    def broken(**kw):
        raise anthropic.APIConnectionError(request=None)

    reader.beta.messages.create = broken
    res = tone.read_pending(reader)
    assert res["read"] == 0 and res["failed"] == 0 and "stopped" in res
    assert tone.read_pending(Reader())["read"] == 3  # all still waiting, none used up an attempt


def test_one_bank_failing_keeps_the_other_and_thin_pages_are_refused(sites):
    from atlas import tone

    sites.fed_down = True
    sites.ecb.append((sites.ecb[0][0] - timedelta(days=30), "<main><p>Short.</p></main>"))
    res = tone.sync(sites.client())
    assert res["fed"]["ok"] is False and res["ecb"]["new"] == 1 and "no statement text" in res["ecb"]["failed"][0]


def test_tone_reaches_the_api_reports_scenarios_and_alerts(sites, seeded, monkeypatch):
    from atlas import auth, intelligence, mailer, scenarios, tone
    from atlas.api.main import app
    from atlas.research import engine

    uid, ws = auth.create_user("tone@scen.test", "correct horse battery", workspace_name="Tone")
    with TestClient(app) as c:
        assert c.get("/api/v1/central-banks/tone").json()["banks"][0]["statements"] == []
        bad = c.post("/api/v1/alerts", json={"name": "x", "rule": {"type": "central_bank", "bank": "boj"}})
        assert bad.status_code == 422
        made = c.post("/api/v1/alerts", json={"name": "Fed shift", "rule": {"type": "central_bank", "bank": "fed"}})
        assert made.status_code in (200, 201)

    with pytest.raises(ValueError):
        intelligence.validate_rule({"type": "central_bank", "bank": "fed", "min_stance": 3})

    # Without any reading, a scenario report still runs and says nothing about tone.
    plain = engine.execute_run(engine.create_run("Scenario outlook first", {"engine": "deterministic", "template": "scenario_outlook", "formats": [],
                                                                            "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}, uid, ws))
    assert "## Scenarios" in plain["narrative_markdown"] and "## Central-bank tone" not in plain["narrative_markdown"]

    reader = Reader([{}, {"action": "cut", "change_bp": -25, "stance": -1, "summary": "The Fed cut 25 bp and softened its language.", "dissents": 1}])
    res = tone.refresh(sites.client(), reader)
    assert res["alerts_fired"] >= 1
    from atlas.data.db import Alert, AlertEvent, session_scope

    with session_scope() as s:
        alert = s.query(Alert).filter(Alert.name == "Fed shift").one()
        events = s.query(AlertEvent).filter(AlertEvent.alert_id == alert.alert_id).all()
        assert len(events) == 1 and "cut rates 25 bp" in events[0].message and alert.last_trigger_key
        alert_id = alert.alert_id
    # The same statement never fires twice; market-data refreshes leave these alerts alone.
    intelligence.evaluate_alerts(["central_bank"])
    intelligence.evaluate_alerts()
    assert all(f["alert_id"] != alert_id for f in intelligence.evaluate_alerts())

    # A stale statement doesn't fire on a new alert.
    assert intelligence.evaluate_rule_keyed({"type": "central_bank", "bank": "ecb", "min_stance": 2}) is None

    with TestClient(app) as c:
        body = c.get("/api/v1/central-banks/tone").json()
        assert body["banks"][0]["statements"][0]["summary"].startswith("The Fed cut") and "not a measurement" in body["caveat"]

    # A softer Fed adds a signal to the rate-cut scenario.
    scan, _ = scenarios.scan(["CMD.CRUDE.BRENT"], max_scenarios=8)
    down = next(s for s in scan["scenarios"] if s["id"] == "rates_down")
    assert any("read as more dovish" in g["text"] and "Claude's reading" in g["text"] for g in down["signals"])
    assert not any("dovish" in g["text"] for s in scan["scenarios"] if s["id"] == "rates_up" for g in s["signals"])

    # A flagged decision isn't used.
    from atlas.data.db import CentralBankStatement

    with session_scope() as s:
        row = s.query(CentralBankStatement).filter(CentralBankStatement.statement_id == tone.latest_reading("fed")["statement_id"]).one()
        row.consistent = False
    scan, _ = scenarios.scan(["CMD.CRUDE.BRENT"], max_scenarios=8)
    assert not any("Claude's reading" in g["text"] for s in scan["scenarios"] for g in s["signals"])
    with session_scope() as s:
        s.query(CentralBankStatement).filter(CentralBankStatement.statement_id == row.statement_id).update({"consistent": True})

    out = engine.execute_run(engine.create_run("Scenario outlook with tone", {"engine": "deterministic", "template": "scenario_outlook", "formats": [],
                                                                              "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}, uid, ws))
    assert out["verification"]["passed"], out["verification"]["unverified_numbers"][:5]
    md = out["narrative_markdown"]
    assert "## Central-bank tone" in md and "cut 25 bp" in md and "more dovish (-1)" in md and "not a measurement" in md
