from datetime import date

import httpx

from atlas import calendar

PAGE = """
<div class="panel"><div class="panel-heading"><h4><a id="1">2026 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col"><strong>January</strong></div>
<div class="fomc-meeting__date col">27-28</div>
<div class="fomc-meeting__month col"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col">30-1*</div>
<div class="fomc-meeting__month col"><strong>August</strong></div>
<div class="fomc-meeting__date col">22 (notation vote)</div>
<div class="fomc-meeting__month col"><strong>December</strong></div>
<div class="fomc-meeting__date col">8-9*</div>
</div>
<div class="panel"><div class="panel-heading"><h4><a id="2">2025 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col"><strong>October</strong></div>
<div class="fomc-meeting__date col">28-29</div>
</div>
"""


def test_parse_fomc_handles_cross_month_projections_and_skips_notation_votes():
    ev = {(e["start_date"], e["end_date"]): e for e in calendar.parse_fomc(PAGE)}
    assert set(ev) == {(date(2026, 1, 27), date(2026, 1, 28)), (date(2026, 4, 30), date(2026, 5, 1)),
                       (date(2026, 12, 8), date(2026, 12, 9)), (date(2025, 10, 28), date(2025, 10, 29))}
    assert ev[(date(2026, 4, 30), date(2026, 5, 1))]["detail"]["summary_of_economic_projections"] is True
    assert ev[(date(2026, 1, 27), date(2026, 1, 28))]["detail"]["summary_of_economic_projections"] is False


def _transport(fomc_status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        if "fomccalendars" in req.url.path:
            return httpx.Response(fomc_status, text=PAGE)
        if req.url.path.endswith("/series/release"):
            return httpx.Response(200, json={"releases": [{"id": 10, "name": "Consumer Price Index"}]})
        if req.url.path.endswith("/release/dates"):
            return httpx.Response(200, json={"release_dates": [{"release_id": 10, "date": "2026-11-12"},
                                                               {"release_id": 10, "date": "2026-12-10"}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_refresh_keeps_last_good_copy_and_upcoming_window(monkeypatch):
    import dataclasses

    monkeypatch.setattr(calendar, "settings", dataclasses.replace(calendar.settings, fred_api_key=""))
    status = calendar.refresh(httpx.Client(transport=_transport()))
    assert status["fomc"] == {"ok": True, "events": 4}
    # A failing refresh must not wipe stored events.
    assert calendar.refresh(httpx.Client(transport=_transport(503)))["fomc"]["ok"] is False
    cal = calendar.upcoming(60, today=date(2026, 11, 1))
    assert [e["date"] for e in cal["events"]] == ["2026-12-08"]
    assert cal["sources"]["fomc"]
    assert any("FRED_API_KEY" in n for n in cal["notes"])
    # A meeting in progress (started yesterday, ends today) is still listed.
    assert [e["date"] for e in calendar.upcoming(5, today=date(2026, 1, 28))["events"]] == ["2026-01-27"]


def test_fred_release_dates_are_mapped_to_datasets():
    ev = calendar.fetch_fred_releases([("US.CPI.HEADLINE", "U.S. CPI", "CPIAUCSL")], "test-key",
                                      httpx.Client(transport=_transport()), today=date(2026, 11, 1))
    assert [e["start_date"] for e in ev] == [date(2026, 11, 12), date(2026, 12, 10)]
    assert ev[0]["detail"]["datasets"] == [{"dataset_id": "US.CPI.HEADLINE", "name": "U.S. CPI"}]


def test_calendar_tool_and_planner(monkeypatch):
    from atlas.research import planner, tools

    calendar.refresh(httpx.Client(transport=_transport()))
    monkeypatch.setattr(calendar, "date", type("D", (date,), {"today": classmethod(lambda cls: date(2026, 1, 1))}))
    ctx = tools.ToolContext()
    out = tools.TOOLS["get_economic_calendar"]["fn"](ctx, {"days": 40})
    assert out["events"][0]["date"] == "2026-01-27" and out["events"][0]["kind"] == "fomc_meeting"
    assert ctx.evidence[0].sources[0]["dataset_id"] == "CALENDAR.FOMC"
    steps = planner.build_plan("When is the next FOMC meeting?")["steps"]
    assert any(s["tool"] == "get_economic_calendar" for s in steps)
