"""PostgreSQL storage for review-run analytics events (SQLAlchemy Core).

One ``review_runs`` table: typed columns for the fields a dashboard will filter
and group on (project, status, review_type, counts, duration, timestamps) plus a
``raw_json`` JSONB column holding the exact event the reviewer sent. The JSONB
column makes the store lenient — unknown or future fields are preserved without
a migration, and missing fields simply land as NULL in their typed column.

PostgreSQL only (so DevLake can ingest the data); the backend URL comes from
``COLLECTOR_DATABASE_URL`` (e.g. ``postgresql+psycopg://user:pass@host:5432/collector``).
Schema is owned by **Alembic** (see ``migrations/``); ``run_migrations`` applies
``alembic upgrade head`` at startup. This module only defines the table (as the
Alembic autogenerate target) and the read/write queries.

A single ``Engine`` (with a connection pool) is created lazily and reused; each
call runs in its own transaction and the pool is safe across the threaded WSGI
worker.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
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
    "reviewer_version",
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

metadata = MetaData()
review_runs = Table(
    "review_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),  # server-side ingest time (ISO UTC)
    Column("duration_seconds", Float),
    *[Column(name, String) for name in _TYPED_TEXT_FIELDS],
    *[Column(name, Integer) for name in _TYPED_INT_FIELDS],
    # Errors captured during the run: count (for monitoring/alerting) + the full
    # structured array (for debugging drill-down). none_as_null so a run with no
    # errors stores SQL NULL, not a JSON `null` scalar.
    Column("error_count", Integer),
    Column("errors", JSONB(none_as_null=True)),
    Column("raw_json", JSONB, nullable=False),
    Index("idx_review_runs_project_mr", "project_path", "mr_id"),
    Index("idx_review_runs_timestamp", "timestamp"),
)

_engine = None


def database_url() -> str:
    """The PostgreSQL SQLAlchemy URL from COLLECTOR_DATABASE_URL (required)."""
    url = os.getenv("COLLECTOR_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "COLLECTOR_DATABASE_URL is required (PostgreSQL), e.g. "
            "postgresql+psycopg://user:pass@host:5432/collector"
        )
    return url


def get_engine():
    """Lazily create and cache the process-wide Engine."""
    global _engine
    if _engine is None:
        # pool_pre_ping avoids handing out a connection the DB already dropped
        # (idle timeouts, restarts).
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
    return _engine


def run_migrations() -> None:
    """Apply Alembic migrations up to head. Idempotent; safe on every startup."""
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url())
    command.upgrade(cfg, "head")


def insert_event(payload: dict[str, Any]) -> int:
    """Persist one analytics event. Returns the new row id.

    Lenient: any missing typed field is stored as NULL; the full payload is kept
    in ``raw_json`` regardless. Non-int values for int columns are coerced to NULL
    rather than rejected, so a malformed field can't drop the whole event.
    """
    errors = payload.get("errors")
    errors = errors if isinstance(errors, list) else None
    values: dict[str, Any] = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw_json": payload,
        "duration_seconds": _as_float(payload.get("duration_seconds")),
        "errors": errors,
        "error_count": len(errors) if errors is not None else 0,
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
        if isinstance(payload, str):  # defensive; JSONB normally hands back a dict
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
