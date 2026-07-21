"""PostgreSQL storage for review-run analytics events (SQLAlchemy Core).

Four tables:

* ``review_runs`` — one row per review run: typed columns for the fields a
  dashboard will filter and group on (project, status, review_type, counts,
  duration, timestamps), denormalized ``error_count``/``warning_count`` for
  alerting, plus a ``raw_json`` JSONB column holding the exact event the reviewer
  sent. The JSONB column makes the store lenient — unknown or future fields are
  preserved without a migration, and missing fields land as NULL in their column.
* ``run_events`` — one row per captured error/warning, discriminated by
  ``severity`` and keyed to the run (``run_id`` + ``review_run_id``). Kept
  separate from the runs so failures/warnings are queryable and joinable on their
  own (debugging, alerting, DevLake).
* ``review_comments`` — one row per review comment the reviewer actually posted
  to a GitLab MR (from the ``posted_comments`` array of the run event), keyed to
  the run and, via the GitLab ``note_id``, to any reaction scores.
* ``comment_scores`` — one row per human 1–5 rating: the reviewer seeds each
  posted comment with the :one:..:five: award emojis, a human clicks one, and
  GitLab's emoji webhook (``POST /api/webhooks/gitlab``) delivers the award here.
  Joined to ``review_comments`` on ``note_id``.

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
    BigInteger,
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
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
    # Denormalized counts for monitoring/alerting (the full structured events live
    # in the run_events table, one row per error/warning, keyed to this run).
    Column("error_count", Integer),
    Column("warning_count", Integer),
    Column("raw_json", JSONB, nullable=False),
    Index("idx_review_runs_project_mr", "project_path", "mr_id"),
    Index("idx_review_runs_timestamp", "timestamp"),
)

# Errors and warnings captured during a run, one row each, discriminated by
# ``severity`` and tied to the run via ``run_id`` (the reviewer's uuid) and
# ``review_run_id`` (review_runs.id). Kept in a dedicated table — not as a JSONB
# column on review_runs — so failures/warnings are queryable and joinable on
# their own (debugging, alerting, DevLake). The full payload still lives in
# review_runs.raw_json.
run_events = Table(
    "run_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),  # server-side ingest time (ISO UTC)
    Column("run_id", String),          # reviewer run uuid (review_runs.run_id)
    Column("review_run_id", Integer),  # review_runs.id of the owning run
    Column("severity", String, nullable=False),  # "error" | "warning"
    Column("stage", String),           # coarse bucket: comment_post, file_review, ...
    Column("context", String),         # locator, e.g. "path/to/file.go:42"
    Column("message", String),
    Column("traceback", String),
    Column("occurred_at", String),     # event time from the reviewer (entry "at")
    Index("idx_run_events_run_id", "run_id"),
    Index("idx_run_events_review_run_id", "review_run_id"),
    Index("idx_run_events_severity", "severity"),
)

# One row per review comment the reviewer posted to a GitLab MR, from the
# ``posted_comments`` array of the run event. ``note_id`` is the GitLab note id
# — globally unique per instance and the value emoji webhooks report as
# ``object_attributes.awardable_id`` — so it is the join key for scores.
review_comments = Table(
    "review_comments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),  # server-side ingest time (ISO UTC)
    Column("run_id", String),           # reviewer run uuid (review_runs.run_id)
    Column("review_run_id", Integer),   # review_runs.id of the owning run
    Column("project_path", String),
    Column("mr_id", String),            # MR IID, as the reviewer reports it
    Column("note_id", BigInteger, nullable=False),  # GitLab note id (webhook awardable_id)
    Column("discussion_id", String),    # set for inline comments (discussion threads)
    Column("comment_type", String),     # "inline" | "note"
    Column("file", String),
    Column("line", Integer),
    Column("severity", String),
    Column("mr_author_username", String),  # whose MR the comment is on
    Index("idx_review_comments_run_id", "run_id"),
    Index("idx_review_comments_note_id", "note_id", unique=True),
    Index("idx_review_comments_project_mr", "project_path", "mr_id"),
)

# One row per human 1–5 rating of a review comment, gathered via GitLab's emoji
# webhook: awarding :one:..:five: on a note upserts a row (unique per
# note+user+emoji, so webhook redeliveries are idempotent); revoking deletes it,
# so the table always holds the *current* scores. Self-awards (the reviewer
# seeding the scale on its own note) and awards on notes that are not tracked
# review comments (the review marker note, any other MR note) are filtered out
# before this table.
comment_scores = Table(
    "comment_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", String, nullable=False),  # server-side ingest time (ISO UTC)
    Column("note_id", BigInteger, nullable=False),  # joins review_comments.note_id
    Column("project_id", Integer),   # numeric GitLab project id (webhook payload)
    Column("project_path", String),  # human-readable project (webhook project.path_with_namespace)
    Column("mr_id", Integer),        # MR IID from the webhook's merge_request object
    Column("user_id", Integer),      # reacting GitLab user
    Column("username", String),      # reacting user's @handle
    Column("user_name", String),     # reacting user's display name
    Column("emoji", String),         # "one" .. "five"
    Column("score", Integer),        # 1 .. 5
    Column("awarded_at", String),    # award time from GitLab (object_attributes.created_at)
    Index("idx_comment_scores_note_id", "note_id"),
    Index("uq_comment_scores_note_user_emoji", "note_id", "user_id", "emoji", unique=True),
)

# Emoji-name → score mapping for the rating scale the reviewer seeds.
_SCORE_EMOJIS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

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
    """Persist one analytics event. Returns the new review_runs row id.

    Writes the run row (with denormalized error_count/warning_count) and one
    run_events row per error/warning, in a single transaction. Lenient: any
    missing typed field is stored as NULL and the full payload is kept in
    ``raw_json`` regardless, so a malformed field can't drop the whole event.
    """
    errors = payload.get("errors")
    errors = errors if isinstance(errors, list) else []
    warnings = payload.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []

    received_at = datetime.now(timezone.utc).isoformat()
    values: dict[str, Any] = {
        "received_at": received_at,
        "raw_json": payload,
        "duration_seconds": _as_float(payload.get("duration_seconds")),
        "error_count": len(errors),
        "warning_count": len(warnings),
    }
    for name in _TYPED_TEXT_FIELDS:
        val = payload.get(name)
        values[name] = None if val is None else str(val)
    for name in _TYPED_INT_FIELDS:
        values[name] = _as_int(payload.get(name))

    run_id = values.get("run_id")

    posted = payload.get("posted_comments")
    posted = posted if isinstance(posted, list) else []
    project_path = values.get("project_path")
    mr_id = values.get("mr_id")
    mr_author = payload.get("mr_author_username")
    mr_author = None if mr_author is None else str(mr_author)

    with get_engine().begin() as conn:
        result = conn.execute(review_runs.insert().values(**values))
        review_run_id = int(result.inserted_primary_key[0])

        event_rows = [
            _event_row(e, "error", run_id, review_run_id, received_at)
            for e in errors if isinstance(e, dict)
        ] + [
            _event_row(w, "warning", run_id, review_run_id, received_at)
            for w in warnings if isinstance(w, dict)
        ]
        if event_rows:
            conn.execute(run_events.insert(), event_rows)

        comment_rows = []
        for c in posted:
            if not isinstance(c, dict):
                continue
            note_id = _as_int(c.get("note_id"))
            if note_id is None:  # a comment we can't key reactions to is useless here
                continue
            comment_rows.append({
                "received_at": received_at,
                "run_id": _text(run_id),
                "review_run_id": review_run_id,
                "project_path": project_path,
                "mr_id": mr_id,
                "note_id": note_id,
                "discussion_id": _text(c.get("discussion_id")),
                "comment_type": _text(c.get("comment_type")),
                "file": _text(c.get("file")),
                "line": _as_int(c.get("line")),
                "severity": _text(c.get("severity")),
                "mr_author_username": mr_author,
            })
        if comment_rows:
            # A run can be re-emitted (retries) and note ids are stable, so upsert
            # on note_id keeps this idempotent rather than erroring on the unique idx.
            stmt = pg_insert(review_comments).values(comment_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["note_id"],
                set_={
                    "run_id": stmt.excluded.run_id,
                    "review_run_id": stmt.excluded.review_run_id,
                    "comment_type": stmt.excluded.comment_type,
                    "file": stmt.excluded.file,
                    "line": stmt.excluded.line,
                    "severity": stmt.excluded.severity,
                    "mr_author_username": stmt.excluded.mr_author_username,
                },
            )
            conn.execute(stmt)

    return review_run_id


def _text(value: Any) -> Optional[str]:
    """Lenient text coercion: None stays None, everything else becomes str."""
    return None if value is None else str(value)


def _review_comment_exists(note_id: int) -> bool:
    """True if ``note_id`` is a review comment the reviewer posted (and recorded).

    Used to keep ``comment_scores`` limited to ratings of real review comments —
    reactions on the review marker note or any other MR note have no matching
    row here and are ignored.
    """
    with get_engine().connect() as conn:
        row = conn.execute(
            select(review_comments.c.id)
            .where(review_comments.c.note_id == note_id)
            .limit(1)
        ).first()
    return row is not None


def _event_row(
    entry: dict[str, Any],
    default_severity: str,
    run_id: Any,
    review_run_id: int,
    received_at: str,
) -> dict[str, Any]:
    """Map one reviewer error/warning entry to a run_events row (all lenient)."""
    severity = entry.get("severity")
    severity = str(severity) if severity in ("error", "warning") else default_severity
    return {
        "received_at": received_at,
        "run_id": _text(run_id),
        "review_run_id": review_run_id,
        "severity": severity,
        "stage": _text(entry.get("stage")),
        "context": _text(entry.get("context")),
        "message": _text(entry.get("message")),
        "traceback": _text(entry.get("traceback")),
        "occurred_at": _text(entry.get("at")),
    }


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


def list_run_events(
    limit: int = 100,
    offset: int = 0,
    run_id: Optional[str] = None,
    severity: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return recent run_events (errors + warnings), newest first, as plain dicts.

    Optional filters: ``run_id`` (a specific run) and ``severity`` ("error"/"warning").
    """
    stmt = select(run_events)
    if run_id:
        stmt = stmt.where(run_events.c.run_id == run_id)
    if severity:
        stmt = stmt.where(run_events.c.severity == severity)
    stmt = stmt.order_by(run_events.c.id.desc()).limit(limit).offset(offset)

    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(row) for row in rows]


def record_emoji_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest one GitLab emoji-webhook event into ``comment_scores``.

    Returns a small status dict describing what was done (for the HTTP response
    and logging). All paths are lenient — a malformed or irrelevant event is
    *ignored* (never raises) so GitLab sees a 2xx and doesn't disable the hook.

    Handled cases (in order):
      * not an ``emoji`` event, or not on a ``Note`` → ``ignored`` (wrong kind);
      * not one of our rating emojis (:one:..:five:) → ``ignored`` (other emoji);
      * a self-award (reactor is the note's author, i.e. the reviewer seeding the
        scale) → ``ignored`` (self-award);
      * ``revoke`` → delete the matching score (also clears any orphan rows);
      * an award on a note we did **not** post as a review comment — the review
        marker note ("reviewed up to commit …") or any other MR note — →
        ``ignored`` (not a tracked review comment), so scores stay attached only
        to real review comments;
      * ``award`` on a tracked comment → upsert the score (idempotent on
        note+user+emoji).
    """
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "not an object"}
    if payload.get("object_kind") != "emoji":
        return {"status": "ignored", "reason": "not an emoji event"}

    attrs = payload.get("object_attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    if attrs.get("awardable_type") != "Note":
        return {"status": "ignored", "reason": "not a note reaction"}

    emoji = attrs.get("name")
    score = _SCORE_EMOJIS.get(emoji)
    if score is None:
        return {"status": "ignored", "reason": f"non-rating emoji: {emoji}"}

    note_id = _as_int(attrs.get("awardable_id"))
    if note_id is None:
        return {"status": "ignored", "reason": "no awardable_id"}

    # event_type carries award/revoke; object_attributes.action mirrors it.
    action = payload.get("event_type") or attrs.get("action")

    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    reactor_id = _as_int(user.get("id"))

    # Drop the reviewer's own seed reactions: the award is by the same user who
    # authored the note (the bot). We compare the reacting user to the note author.
    note = payload.get("note")
    note = note if isinstance(note, dict) else {}
    author_id = _as_int(note.get("author_id"))
    if action == "award" and reactor_id is not None and reactor_id == author_id:
        return {"status": "ignored", "reason": "self-award (seed reaction)"}

    mr = payload.get("merge_request")
    mr = mr if isinstance(mr, dict) else {}

    if action == "revoke":
        with get_engine().begin() as conn:
            conn.execute(
                comment_scores.delete().where(
                    (comment_scores.c.note_id == note_id)
                    & (comment_scores.c.user_id == reactor_id)
                    & (comment_scores.c.emoji == emoji)
                )
            )
        return {"status": "revoked", "note_id": note_id, "emoji": emoji, "score": score}

    # Only record ratings on notes we actually posted as review comments. A
    # reaction on the review marker note ("🤖 … reviewed up to commit …") or any
    # other MR note is not a rating of a review comment, so it is ignored rather
    # than stored as an orphan score. The review_comments row lands with the run
    # event at the end of the run — before a human sits down to review and rate
    # the MR — so a genuine rating finds its comment here.
    if not _review_comment_exists(note_id):
        return {"status": "ignored", "reason": "not a tracked review comment"}

    project = payload.get("project")
    project = project if isinstance(project, dict) else {}
    project_path = _text(project.get("path_with_namespace")) or _text(project.get("name"))

    # Default: treat as an award (upsert; idempotent on note+user+emoji).
    row = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "note_id": note_id,
        "project_id": _as_int(payload.get("project_id")),
        "project_path": project_path,
        "mr_id": _as_int(mr.get("iid")),
        "user_id": reactor_id,
        "username": _text(user.get("username")),
        "user_name": _text(user.get("name")),
        "emoji": emoji,
        "score": score,
        "awarded_at": _text(attrs.get("created_at")),
    }
    with get_engine().begin() as conn:
        stmt = pg_insert(comment_scores).values([row])
        stmt = stmt.on_conflict_do_update(
            index_elements=["note_id", "user_id", "emoji"],
            set_={
                "received_at": stmt.excluded.received_at,
                "project_id": stmt.excluded.project_id,
                "project_path": stmt.excluded.project_path,
                "mr_id": stmt.excluded.mr_id,
                "username": stmt.excluded.username,
                "user_name": stmt.excluded.user_name,
                "score": stmt.excluded.score,
                "awarded_at": stmt.excluded.awarded_at,
            },
        )
        conn.execute(stmt)
    return {"status": "recorded", "note_id": note_id, "emoji": emoji, "score": score}


def list_review_comments(
    limit: int = 100,
    offset: int = 0,
    run_id: Optional[str] = None,
    project_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return posted review comments (newest first), each with its current scores.

    Each comment dict carries a ``scores`` list (the rows from ``comment_scores``
    for that note) plus a convenience ``score_count`` and ``score_avg``.
    """
    stmt = select(review_comments)
    if run_id:
        stmt = stmt.where(review_comments.c.run_id == run_id)
    if project_path:
        stmt = stmt.where(review_comments.c.project_path == project_path)
    stmt = stmt.order_by(review_comments.c.id.desc()).limit(limit).offset(offset)

    with get_engine().connect() as conn:
        comments = [dict(r) for r in conn.execute(stmt).mappings().all()]
        note_ids = [c["note_id"] for c in comments]
        scores_by_note: dict[int, list] = {}
        if note_ids:
            srows = conn.execute(
                select(comment_scores)
                .where(comment_scores.c.note_id.in_(note_ids))
                .order_by(comment_scores.c.id.asc())
            ).mappings().all()
            for s in srows:
                scores_by_note.setdefault(s["note_id"], []).append(dict(s))

    for c in comments:
        scores = scores_by_note.get(c["note_id"], [])
        c["scores"] = scores
        c["score_count"] = len(scores)
        vals = [s["score"] for s in scores if s.get("score") is not None]
        c["score_avg"] = round(sum(vals) / len(vals), 2) if vals else None
    return comments


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
