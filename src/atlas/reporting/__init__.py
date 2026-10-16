"""Report rendering: versioned HTML (print-ready for PDF) and Excel exports (spec §12)."""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any

import markdown as md
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select

from atlas.config import settings
from atlas.data.db import Report, ResearchRun, session_scope

_env = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(["html"]),
)
PALETTE = ["#2f6fdf", "#e07b39", "#2a9d8f", "#b5179e", "#6c757d", "#e9c46a"]


# ---------------------------------------------------------------- charts


def svg_chart(chart: dict[str, Any], width: int = 760, height: int = 260) -> str:
    """Render a chart spec as standalone SVG (works in HTML, print and PDF without JS)."""
    series = [s for s in chart.get("series", []) if s.get("points")]
    if not series:
        return ""
    pad_l, pad_r, pad_t, pad_b = 64, 16, 16, 40
    import datetime as dt

    def ts(d: str) -> float:
        return dt.date.fromisoformat(d[:10]).toordinal()

    xs = [ts(p["date"]) for s in series for p in s["points"]]
    ys = [p["value"] for s in series for p in s["points"] if p["value"] is not None]
    for b in chart.get("band", []):
        ys += [v for v in (b.get("low"), b.get("high")) if v is not None]
    if not ys:
        return ""
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1
    span = y1 - y0
    y0, y1 = y0 - span * 0.05, y1 + span * 0.05
    x1 = x1 if x1 > x0 else x0 + 1

    def px(x: float) -> float:
        return pad_l + (x - x0) / (x1 - x0) * (width - pad_l - pad_r)

    def py(y: float) -> float:
        return pad_t + (1 - (y - y0) / (y1 - y0)) * (height - pad_t - pad_b)

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="{html.escape(chart.get("title", "chart"))}" class="chart">']
    # gridlines + y labels
    step = 10 ** math.floor(math.log10((y1 - y0) / 4)) if y1 > y0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        if (y1 - y0) / (step * mult) <= 6:
            step *= mult
            break
    tick = math.ceil(y0 / step) * step
    while tick <= y1:
        y = py(tick)
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        label = f"{tick:,.0f}" if step >= 1 else f"{tick:,.{max(0, -math.floor(math.log10(step)))}f}"
        parts.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" class="axis">{label}</text>')
        tick += step
    # x labels: years
    first_year = dt.date.fromordinal(int(x0)).year
    last_year = dt.date.fromordinal(int(x1)).year
    years = list(range(first_year + 1, last_year + 1))
    stride = max(1, len(years) // 8)
    for yr in years[::stride]:
        x = px(dt.date(yr, 1, 1).toordinal())
        parts.append(f'<text x="{x:.1f}" y="{height - pad_b + 18}" text-anchor="middle" class="axis">{yr}</text>')
    if len(years) <= 1:
        for d in (x0, x1):
            parts.append(f'<text x="{px(d):.1f}" y="{height - pad_b + 18}" text-anchor="middle" class="axis">{dt.date.fromordinal(int(d)).isoformat()}</text>')
    # band
    band = [b for b in chart.get("band", []) if b.get("low") is not None]
    if band and series:
        anchor = series[0]["points"][-1]
        pts = [(ts(anchor["date"]), anchor["value"], anchor["value"])] + [(ts(b["date"]), b["low"], b["high"]) for b in band]
        poly = " ".join(f"{px(x):.1f},{py(h):.1f}" for x, _, h in pts) + " " + " ".join(f"{px(x):.1f},{py(l):.1f}" for x, l, _ in reversed(pts))
        parts.append(f'<polygon points="{poly}" class="band"/>')
    for i, s in enumerate(series):
        pts = [p for p in s["points"] if p["value"] is not None]
        if chart.get("type") == "forecast" and s["name"] == "forecast" and series[0]["points"]:
            pts = [series[0]["points"][-1], *pts]
        path = " ".join(f"{'M' if j == 0 else 'L'}{px(ts(p['date'])):.1f},{py(p['value']):.1f}" for j, p in enumerate(pts))
        dash = ' stroke-dasharray="5 4"' if s["name"] == "forecast" else ""
        parts.append(f'<path d="{path}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" stroke-width="1.8"{dash}/>')
    parts.append("</svg>")
    legend = "".join(
        f'<span class="legend-item"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>{html.escape(s["name"])}</span>'
        for i, s in enumerate(series)
    )
    return "".join(parts) + f'<div class="legend">{legend}</div>'


# ---------------------------------------------------------------- builders


def _report_dir(report_id: str) -> Path:
    d = Path(settings.object_store) / "reports" / report_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_html(run: dict[str, Any], version: int) -> str:
    res = run["result"]
    body = md.markdown(res["narrative_markdown"], extensions=["tables", "sane_lists"])
    # Drop the H1: the template renders the title.
    if body.startswith("<h1>"):
        body = body[body.index("</h1>") + 5:]
    charts = [{"title": c.get("title", ""), "evidence": c["evidence"], "unit": c.get("unit", ""), "svg": svg_chart(c)} for c in res.get("charts", [])]
    return _env.get_template("report.html").render(
        title=res["title"], run=run, res=res, body=body, charts=charts, version=version,
    )


def render_xlsx(run: dict[str, Any], path: Path) -> None:
    res = run["result"]
    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3A5F")

    def header(ws, cols):
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.font, c.fill = head, fill

    ws = wb.active
    ws.title = "Summary"
    ws.append(["ATLAS research report"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in [("Title", res["title"]), ("Question", run["query"]), ("Run ID", run["run_id"]),
                 ("Engine", f"{res['engine']} {res.get('model') or ''}".strip()), ("Created", run["created_at"]),
                 ("Numbers checked", res["verification"]["numbers_checked"]),
                 ("Numbers verified", res["verification"]["numbers_verified"]),
                 ("Verification passed", res["verification"]["passed"])]:
        ws.append([k, v])
    ws.append([])
    ws.append(["Narrative (Markdown)"])
    for line in res["narrative_markdown"].splitlines():
        ws.append([line])
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 100

    ws = wb.create_sheet("Evidence")
    header(ws, ["Label", "Tool", "Arguments", "Citation"])
    for e in res["evidence"]:
        ws.append([e["label"], e["tool"], str(e["args"]), e["citation"]])
    ws.column_dimensions["D"].width = 120

    ws = wb.create_sheet("Sources")
    header(ws, ["Dataset", "Name", "Citation", "URL", "Observations through", "Ingested at", "Freshness", "Price basis"])
    for s in res["sources"]:
        ws.append([s["dataset_id"], s["name"], s["citation"], s["url"], s["observation_through"], s["ingested_at"], s["freshness"], s["price_basis"]])

    for c in res.get("charts", []):
        name = f"{c['evidence']} data"[:31]
        ws = wb.create_sheet(name)
        ws.append([c.get("title", ""), f"unit: {c.get('unit', '')}", f"evidence {c['evidence']}"])
        dates = sorted({p["date"] for s in c["series"] for p in s["points"]})
        lookup = [{p["date"]: p["value"] for p in s["points"]} for s in c["series"]]
        header(ws, ["date", *[s["name"] for s in c["series"]]])
        for d in dates:
            ws.append([d, *[lk.get(d) for lk in lookup]])
        for i in range(1, len(c["series"]) + 2):
            ws.column_dimensions[get_column_letter(i)].width = 22
    wb.save(path)


def build_report(run_id: str, formats: list[str] | None = None, template: str = "research_report") -> dict[str, Any]:
    from atlas.research.engine import get_run

    formats = formats or ["html", "xlsx"]
    run = get_run(run_id)
    if run is None or not run.get("result"):
        raise ValueError(f"run {run_id} has no result to report on")
    with session_scope() as s:
        version = (s.execute(select(func.max(Report.version)).where(Report.run_id == run_id)).scalar() or 0) + 1
        report = Report(run_id=run_id, title=run["result"]["title"], template=template, version=version, formats={})
        s.add(report)
        s.flush()
        report_id = report.report_id
    out_dir = _report_dir(report_id)
    uris: dict[str, str] = {}
    if "html" in formats or "pdf" in formats:
        p = out_dir / f"v{version}.html"
        p.write_text(render_html(run, version), encoding="utf-8")
        uris["html"] = str(p)
    if "xlsx" in formats:
        p = out_dir / f"v{version}.xlsx"
        render_xlsx(run, p)
        uris["xlsx"] = str(p)
    with session_scope() as s:
        r = s.get(Report, report_id)
        assert r is not None
        r.formats = uris
    return {"report_id": report_id, "version": version, "formats": uris}


def get_report(report_id: str, workspace_id: str | None = None) -> dict[str, Any] | None:
    with session_scope() as s:
        r = s.get(Report, report_id)
        if r is None:
            return None
        if workspace_id is not None and s.get(ResearchRun, r.run_id).workspace_id != workspace_id:  # type: ignore[union-attr]
            return None
        return {"report_id": r.report_id, "run_id": r.run_id, "title": r.title, "template": r.template,
                "version": r.version, "formats": sorted(r.formats), "paths": r.formats,
                "created_at": r.created_at.isoformat(timespec="seconds") + "Z"}


def list_reports(limit: int = 100, workspace_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(Report, ResearchRun.query).join(ResearchRun)
        if workspace_id is not None:
            q = q.where(ResearchRun.workspace_id == workspace_id)
        rows = s.execute(q.order_by(Report.created_at.desc()).limit(limit)).all()
        return [
            {"report_id": r.report_id, "run_id": r.run_id, "title": r.title, "query": q, "version": r.version,
             "formats": sorted(r.formats), "created_at": r.created_at.isoformat(timespec="seconds") + "Z"}
            for r, q in rows
        ]
