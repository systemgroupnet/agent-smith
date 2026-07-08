"""Storage for review-run analytics events (SQLAlchemy Core, engine-agnostic).

One ``review_runs`` table: typed columns for the fields a dashboard will filter
and group on (project, status, review_type, counts, duration, timestamps) plus a
``raw_json`` payload column holding the exact event the reviewer sent. The
payload column makes the store lenient — unknown or future fields are preserved
without a migration, and missing fields simply land as NULL in their typed
column.

Backend is chosen by ``COLLECTOR_DATABASE_URL`` (a SQLAlchemy URL), e.g.
``postgresql+psycopg://user:pass@host:5432/collector``. PostgreSQL is the
production target so the data lands in a DevLake-compatible database (DevLake
ingests from PostgreSQL/MySQL). When the URL is unset the store falls back to a
local SQLite file at ``COLLECTOR_DB_PATH`` — convenient for local dev and tests,
since the same code runs on both. On PostgreSQL ``raw_json`` is JSONB
(queryable/indexable); on SQLite it is the generic JSON type (TEXT-backed).

A single ``Engine`` (with a connection pool) is created lazily and reused. Each
call runs in its own transaction (``with engine.begin()``); the engine/pool is
safe to share across the threaded WSGI worker.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

DEFAULT_DB_PATH = "/data/collector.db"

# Typed columns pulled out of each payload for querying. Everything (including
# these) is also preserved verbatim in raw_json.
_TYPED_INT_FIELDS = (
    "files_changed_total",
    "files_reviewed",
    "files_failed",
    "files_skipped",
    "issues_total",
    "issues_high",
    "issues_medium",
    "issues_low",
    "comments_selected",
    "comments_posted",
    "comments_failed",
)
_TYPED_TEXT_FIELDS = (
    "schema_version",
    "run_id",
    "timestamp",
    "project_path",
    "mr_id",
    "mr_url",
    "mr_title",
    "source_branch",
    "target_branch",
    "head_sha",
    "last_reviewed_sha",
    "review_type",
    "model",
    "status",
)

# JSONB on PostgreSQL (queryable/indexable), generic JSON elsewhere (SQLite).
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

_metadata = MetaData()
review_runs = Table(
    "review_runs",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),  # server-side ingest time (ISO UTC)
    Column("duration_seconds", Float),
    *[Column(name, String) for name in _TYPED_TEXT_FIELDS],
    *[Column(name, Integer) for name in _TYPED_INT_FIELDS],
    Column("raw_json", _JSON_TYPE, nullable=False),
    Index("idx_review_runs_project_mr", "project_path", "mr_id"),
    Index("idx_review_runs_timestamp", "timestamp"),
)

_engine = None


def database_url() -> str:
    """Resolve the SQLAlchemy URL: COLLECTOR_DATABASE_URL, else a local SQLite file."""
    url = os.getenv("COLLECTOR_DATABASE_URL", "").strip()
    if url:
        return url
    path = os.getenv("COLLECTOR_DB_PATH", DEFAULT_DB_PATH)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return f"sqlite:///{path}"


def get_engine():
    """Lazily create and cache the process-wide Engine."""
    global _engine
    if _engine is None:
        # pool_pre_ping avoids handing out a connection the DB has already
        # dropped (idle timeouts, restarts); harmless for SQLite.
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
    return _engine


def init_db() -> None:
    """Create the table and indexes if they don't exist. Safe to call repeatedly."""
    _metadata.create_all(get_engine())


def insert_event(payload: dict[str, Any]) -> int:
    """Persist one analytics event. Returns the new row id.

    Lenient: any missing typed field is stored as NULL; the full payload is kept
    in ``raw_json`` regardless. Non-int values for int columns are coerced to NULL
    rather than rejected, so a malformed field can't drop the whole event.
    """
    values: dict[str, Any] = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_json": payload,
        "duration_seconds": _as_float(payload.get("duration_seconds")),
    }
    for name in _TYPED_TEXT_FIELDS:
        val = payload.get(name)
        values[name] = None if val is None else str(val)
    for name in _TYPED_INT_FIELDS:
        values[name] = _as_int(payload.get(name))

    with get_engine().begin() as conn:
        result = conn.execute(review_runs.insert().values(**values))
        return int(result.inserted_primary_key[0])


def list_events(
    limit: int = 50,
    offset: int = 0,
    project_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return recent events (newest first) as the payloads originally sent, each
    annotated with server-side ``_id`` and ``_received_at``."""
    stmt = select(review_runs.c.id, review_runs.c.received_at, review_runs.c.raw_json)
    if project_path:
        stmt = stmt.where(review_runs.c.project_path == project_path)
    stmt = stmt.order_by(review_runs.c.id.desc()).limit(limit).offset(offset)

    with get_engine().connect() as conn:
        rows = conn.execute(stmt).all()

    events = []
    for row in rows:
        payload = row.raw_json
        # PostgreSQL JSONB returns a dict; SQLite's generic JSON may hand back a
        # str on some driver paths — normalize both to a dict.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["_id"] = row.id
        payload["_received_at"] = row.received_at
        events.append(payload)
    return events


def count_events() -> int:
    with get_engine().connect() as conn:
        return int(conn.execute(select(func.count()).select_from(review_runs)).scalar_one())


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
