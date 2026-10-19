"""Canonical database schema (spec §7.4) and session management.

SQLite is the zero-setup default; point ATLAS_DATABASE_URL at PostgreSQL/TimescaleDB
for production (see infrastructure/docker/docker-compose.yml).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from atlas.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


# Workspace that owns data created before accounts existed and by local CLI runs.
LOCAL_WORKSPACE = "local"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- reference data


class Instrument(Base):
    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_class: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(8))
    unit: Mapped[str] = mapped_column(String(64))
    frequency: Mapped[str] = mapped_column(String(16))
    price_basis: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    coverage_note: Mapped[str | None] = mapped_column(Text)


class InstrumentAlias(Base):
    __tablename__ = "instrument_aliases"

    provider_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider_symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)


class FuturesContract(Base):
    """Contract registry. Empty until a licensed futures feed is connected (spec §5)."""

    __tablename__ = "futures_contracts"

    contract_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    underlying_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    expiry: Mapped[date] = mapped_column(Date)
    first_notice: Mapped[date | None] = mapped_column(Date)
    last_trade: Mapped[date | None] = mapped_column(Date)
    roll_convention: Mapped[str | None] = mapped_column(String(64))


class MacroSeries(Base):
    __tablename__ = "macro_series"

    series_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(8), index=True)
    indicator: Mapped[str] = mapped_column(String(64), index=True)
    frequency: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(64))
    seasonal_adjustment: Mapped[str | None] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


# ---------------------------------------------------------------- observations


class MarketObservation(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        UniqueConstraint("instrument_id", "observed_at", "field", "provider_id", name="uq_market_obs"),
        Index("ix_market_obs_lookup", "instrument_id", "field", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    observed_at: Mapped[date] = mapped_column(Date)
    field: Mapped[str] = mapped_column(String(32), default="close")
    value: Mapped[float] = mapped_column(Float)
    provider_id: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(32))
    raw_id: Mapped[str | None] = mapped_column(String(32))


class MacroObservation(Base):
    """Point-in-time macro observations. A revised value is stored as a new vintage row."""

    __tablename__ = "macro_observations"
    __table_args__ = (
        UniqueConstraint("series_id", "period", "vintage_at", name="uq_macro_obs"),
        Index("ix_macro_obs_lookup", "series_id", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(ForeignKey("macro_series.series_id"))
    period: Mapped[date] = mapped_column(Date)
    release_at: Mapped[datetime | None] = mapped_column(DateTime)
    vintage_at: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    provider_id: Mapped[str] = mapped_column(String(32))
    pipeline_run_id: Mapped[str | None] = mapped_column(String(32))
    raw_id: Mapped[str | None] = mapped_column(String(32))


class PositioningObservation(Base):
    """Weekly CFTC Commitments of Traders positions (legacy futures-only report)."""

    __tablename__ = "positioning_observations"
    __table_args__ = (UniqueConstraint("series_id", "report_date", "provider_id", name="uq_positioning_obs"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(64), index=True)
    report_date: Mapped[date] = mapped_column(Date)
    open_interest: Mapped[float] = mapped_column(Float)
    noncomm_long: Mapped[float] = mapped_column(Float)
    noncomm_short: Mapped[float] = mapped_column(Float)
    noncomm_spread: Mapped[float] = mapped_column(Float)
    comm_long: Mapped[float] = mapped_column(Float)
    comm_short: Mapped[float] = mapped_column(Float)
    nonrept_long: Mapped[float] = mapped_column(Float)
    nonrept_short: Mapped[float] = mapped_column(Float)
    provider_id: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(32))
    raw_id: Mapped[str | None] = mapped_column(String(32))


class TradeFlow(Base):
    """Annual bilateral trade values (UN Comtrade), by flow and HS chapter ("TOTAL" = all goods)."""

    __tablename__ = "trade_flows"
    __table_args__ = (UniqueConstraint("dataset_id", "year", "flow", "cmd_code", name="uq_trade_flow"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(64), index=True)
    year: Mapped[date] = mapped_column(Date)
    flow: Mapped[str] = mapped_column(String(1))  # X = exports, M = imports (as declared by the reporter)
    cmd_code: Mapped[str] = mapped_column(String(8))
    value_usd: Mapped[float] = mapped_column(Float)
    provider_id: Mapped[str] = mapped_column(String(32))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(32))
    raw_id: Mapped[str | None] = mapped_column(String(32))


# ---------------------------------------------------------------- lineage & pipeline


class RawPayload(Base):
    __tablename__ = "raw_payloads"

    raw_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(String(32))
    dataset_id: Mapped[str] = mapped_column(String(64), index=True)
    request: Mapped[str] = mapped_column(Text)
    storage_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(32))


class DatasetState(Base):
    """Per-dataset watermark and freshness bookkeeping."""

    __tablename__ = "dataset_state"

    dataset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # market | macro | positioning | trade
    provider_id: Mapped[str] = mapped_column(String(32))
    watermark: Mapped[date | None] = mapped_column(Date)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    execution_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    pipeline_id: Mapped[str] = mapped_column(String(64), index=True)
    dataset_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    rows_fetched: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_revised: Mapped[int] = mapped_column(Integer, default=0)
    quality_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class SourceDocument(Base):
    __tablename__ = "source_documents"

    document_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    publisher: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    storage_uri: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- research & output


class ResearchRun(Base):
    __tablename__ = "research_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), default="local")
    workspace_id: Mapped[str] = mapped_column(String(32), default=LOCAL_WORKSPACE, index=True)
    query: Mapped[str] = mapped_column(Text)
    llm_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    cached_from: Mapped[str | None] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    engine: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    progress: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ResearchEvidence(Base):
    __tablename__ = "research_evidence"

    evidence_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    label: Mapped[str] = mapped_column(String(8))
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    citation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    template: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    formats: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # format -> storage_uri
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReportSchedule(Base):
    """A saved research question that re-runs on a cadence (spec FR-010)."""

    __tablename__ = "report_schedules"

    schedule_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), default="local")
    workspace_id: Mapped[str] = mapped_column(String(32), default=LOCAL_WORKSPACE, index=True)
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cadence: Mapped[str] = mapped_column(String(16))  # daily | weekly | monthly
    status: Mapped[str] = mapped_column(String(16), default="active")
    next_run_at: Mapped[datetime] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Portfolio(Base):
    __tablename__ = "portfolios"

    portfolio_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(32), default=LOCAL_WORKSPACE, index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="local")
    name: Mapped[str] = mapped_column(String(200))
    holdings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{dataset_id, weight}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MonitorAgent(Base):
    """A saved research agent: watches datasets and runs research when a trigger fires (FR-017)."""

    __tablename__ = "monitor_agents"

    agent_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(32), default=LOCAL_WORKSPACE, index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="local")
    name: Mapped[str] = mapped_column(String(200))
    watch: Mapped[list[str]] = mapped_column(JSON, default=list)
    triggers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    action: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {template, query, engine, depth}
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=24)
    notify_email: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("monitor_agents.agent_id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    run_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="fired")  # fired | suppressed
    read: Mapped[bool] = mapped_column(default=False)


class Alert(Base):
    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), default="local")
    workspace_id: Mapped[str] = mapped_column(String(32), default=LOCAL_WORKSPACE, index=True)
    name: Mapped[str] = mapped_column(String(200))
    rule: Mapped[dict[str, Any]] = mapped_column(JSON)
    schedule: Mapped[str] = mapped_column(String(32), default="on_ingest")
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.alert_id"), index=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- accounts


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class Workspace(Base):
    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(32), default="explorer")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner | admin | member


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class PasswordReset(Base):
    __tablename__ = "password_resets"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEvent(Base):
    """Security-relevant actions (spec §15: audit logs)."""

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(32))
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- engine/session

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(url: str | None = None):
    global _engine, _SessionLocal
    if _engine is None or url is not None:
        url = url or settings.database_url
        if url.startswith("sqlite:///"):
            from pathlib import Path

            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


# Columns added after the first release: (table, column, DDL type, default). create_all() does not alter
# existing tables, so these are added in place on startup.
_ADDED_COLUMNS = [
    ("research_runs", "workspace_id", "VARCHAR(32)", LOCAL_WORKSPACE),
    ("report_schedules", "workspace_id", "VARCHAR(32)", LOCAL_WORKSPACE),
    ("alerts", "workspace_id", "VARCHAR(32)", LOCAL_WORKSPACE),
    ("research_runs", "llm_usage", "JSON", None),
    ("research_runs", "cost_usd", "FLOAT", None),
    ("research_runs", "cache_key", "VARCHAR(64)", None),
    ("research_runs", "cached_from", "VARCHAR(32)", None),
]


def _migrate(engine) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl, default in _ADDED_COLUMNS:
            if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
                default_sql = "" if default is None else f" DEFAULT '{default}'"
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}{default_sql}"))


def init_db(url: str | None = None) -> None:
    engine = get_engine(url)
    _migrate(engine)
    Base.metadata.create_all(engine)
    with session_scope() as s:
        if s.get(Workspace, LOCAL_WORKSPACE) is None:
            s.add(Workspace(workspace_id=LOCAL_WORKSPACE, name="Local workspace", plan="pro"))


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
