"""SQLite storage for review-run analytics events.

One ``review_runs`` table: typed columns for the fields a dashboard will filter
and group on (project, status, review_type, counts, duration, timestamps) plus a
``raw_json`` blob holding the exact payload the reviewer sent. The blob makes the
store lenient — unknown or future fields are preserved without a migration, and
missing fields simply land as NULL in their typed column.

A fresh connection is opened per call (SQLite connections are not safe to share
across threads, and the collector serves requests from a threaded WSGI worker).
Connections are cheap for SQLite; the file is opened in WAL mode with a busy
timeout so concurrent writers from the same worker don't trip over each other.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

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


def db_path() -> str:
    return os.getenv("COLLECTOR_DB_PATH", DEFAULT_DB_PATH)


def _connect() -> sqlite3.Connection:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Create the table and indexes if they don't exist."""
    columns = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "received_at TEXT NOT NULL",  # server-side ingest time (ISO UTC)
        "duration_seconds REAL",
    ]
    columns += [f"{name} TEXT" for name in _TYPED_TEXT_FIELDS]
    columns += [f"{name} INTEGER" for name in _TYPED_INT_FIELDS]
    columns += ["raw_json TEXT NOT NULL"]
    columns_sql = ",\n  ".join(columns)

    with _connect() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS review_runs (\n  {columns_sql}\n)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_runs_project_mr "
            "ON review_runs (project_path, mr_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_runs_timestamp "
            "ON review_runs (timestamp)"
        )


def insert_event(payload: dict[str, Any]) -> int:
    """Persist one analytics event. Returns the new row id.

    Lenient: any missing typed field is stored as NULL; the full payload is kept
    in ``raw_json`` regardless. Non-int values for int columns are coerced to NULL
    rather than rejected, so a malformed field can't drop the whole event.
    """
    received_at = datetime.now(timezone.utc).isoformat()

    values: dict[str, Any] = {
        "received_at": received_at,
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "duration_seconds": _as_float(payload.get("duration_seconds")),
    }
    for name in _TYPED_TEXT_FIELDS:
        val = payload.get(name)
        values[name] = None if val is None else str(val)
    for name in _TYPED_INT_FIELDS:
        values[name] = _as_int(payload.get(name))

    cols = list(values.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO review_runs ({', '.join(cols)}) VALUES ({placeholders})"

    with _connect() as conn:
        cursor = conn.execute(sql, values)
        return int(cursor.lastrowid)


def list_events(
    limit: int = 50,
    offset: int = 0,
    project_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return recent events (newest first) as the payloads originally sent,
    each annotated with server-side ``_id`` and ``_received_at``."""
    where = ""
    params: list[Any] = []
    if project_path:
        where = "WHERE project_path = ?"
        params.append(project_path)
    params += [limit, offset]

    sql = (
        f"SELECT id, received_at, raw_json FROM review_runs {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    events = []
    for row in rows:
        try:
            payload = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        payload["_id"] = row["id"]
        payload["_received_at"] = row["received_at"]
        events.append(payload)
    return events


def count_events() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0])


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
