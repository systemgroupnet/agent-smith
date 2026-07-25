#!/usr/bin/env python3
"""Collector — receives and stores per-run analytics from the reviewer.

Second service of the codereviewagent monorepo (alongside ``reviewer/``). The
reviewer emits one analytics event per MR-review run (see
``reviewer/core/analytics/client.py``); this service ingests those events into a
PostgreSQL database and exposes a read API a future dashboard can build on.

Endpoints:
  POST /api/events            ingest one event (JSON body). Optionally token-guarded.
  GET  /api/events            list recent events, newest first (?limit, ?offset, ?project).
  GET  /api/run-events        list captured errors/warnings (?run_id, ?severity).
  POST /api/webhooks/gitlab   receive GitLab emoji + comment events.
  GET  /api/review-comments   list posted review comments with scores and replies.
  GET  /api/comment-replies   list human replies to the agent's review comments.
  GET  /health                liveness + stored-event count.

The webhook endpoint powers the review-evaluation loop, on two triggers:

* **Emoji events** — the reviewer seeds each posted comment with the
  :one:..:five: award emojis; a human clicks one; GitLab posts an "Emoji Hook"
  event here and we store it as a 1–5 score keyed to the comment's note id.
* **Comments** — when a human replies in the thread of a comment the agent left,
  GitLab posts a "Note Hook" event; we match its ``discussion_id`` to a tracked
  review comment and store the reply. Notes elsewhere on the MR are ignored.

Configure the hook in the GitLab project (Settings → Webhooks) with the "Emoji
events" and "Comments" triggers enabled.

Contract note: the payload is validated leniently. Each service has its own
Docker build context and cannot import the other's models, so the contract is
JSON. Unknown/extra fields are stored, not rejected, and a ``schema_version``
field on the payload lets the two evolve independently.
"""

import os

from flask import Flask, jsonify, request

import storage

app = Flask(__name__)

# Apply Alembic migrations at import time so the schema is ready under any WSGI
# server (gunicorn imports the module; there is no __main__). Idempotent.
storage.run_migrations()


def _clamp_int(raw, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _token_ok() -> bool:
    """When COLLECTOR_TOKEN is set, require a matching X-Collector-Token header on
    writes. Unset → open (fine on a trusted compose network)."""
    expected = os.getenv("COLLECTOR_TOKEN")
    if not expected:
        return True
    return request.headers.get("X-Collector-Token") == expected


def _gitlab_webhook_token_ok() -> bool:
    """When GITLAB_WEBHOOK_SECRET is set, require a matching X-Gitlab-Token header
    on webhook deliveries (GitLab sends the configured secret in that header).
    Unset → open (fine on a trusted network / when relying on a hard-to-guess URL)."""
    expected = os.getenv("GITLAB_WEBHOOK_SECRET")
    if not expected:
        return True
    return request.headers.get("X-Gitlab-Token") == expected


@app.route("/health", methods=["GET"])
def health():
    try:
        count = storage.count_events()
    except Exception as e:  # noqa: BLE001 — health must not raise
        return jsonify({"status": "error", "error": str(e)}), 500
    return jsonify({"status": "ok", "events": count})


@app.route("/api/events", methods=["POST"])
def ingest_event():
    if not _token_ok():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    try:
        row_id = storage.insert_event(payload)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to store event: {e}"}), 500

    return jsonify({"status": "stored", "id": row_id}), 201


@app.route("/api/events", methods=["GET"])
def get_events():
    limit = _clamp_int(request.args.get("limit"), default=50, low=1, high=500)
    offset = _clamp_int(request.args.get("offset"), default=0, low=0, high=1_000_000)
    project = request.args.get("project") or None

    try:
        events = storage.list_events(limit=limit, offset=offset, project_path=project)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to read events: {e}"}), 500

    return jsonify({"count": len(events), "events": events})


@app.route("/api/run-events", methods=["GET"])
def get_run_events():
    """List captured errors/warnings (newest first). Filters: ?run_id, ?severity."""
    limit = _clamp_int(request.args.get("limit"), default=100, low=1, high=500)
    offset = _clamp_int(request.args.get("offset"), default=0, low=0, high=1_000_000)
    run_id = request.args.get("run_id") or None
    severity = request.args.get("severity") or None
    if severity is not None and severity not in ("error", "warning"):
        return jsonify({"error": "severity must be 'error' or 'warning'"}), 400

    try:
        events = storage.list_run_events(
            limit=limit, offset=offset, run_id=run_id, severity=severity
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to read run events: {e}"}), 500

    return jsonify({"count": len(events), "run_events": events})


@app.route("/api/webhooks/gitlab", methods=["POST"])
def gitlab_webhook():
    """Receive GitLab webhook events feeding the review-evaluation loop.

    Two triggers post here. "Emoji events" carry a rating reaction on one of our
    notes → a 1–5 comment score. "Comments" carry a note; when it sits in the
    thread of a comment the agent posted, it is a human replying to the agent →
    a comment_replies row. Every other event (or a malformed body) is
    acknowledged and ignored. We always return 2xx on an accepted delivery so
    GitLab does not disable the hook — a storage failure is the only thing that
    yields 500 (GitLab will retry)."""
    if not _gitlab_webhook_token_ok():
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    try:
        result = storage.record_webhook_event(payload)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to store webhook event: {e}"}), 500

    return jsonify(result)


@app.route("/api/review-comments", methods=["GET"])
def get_review_comments():
    """List posted review comments with their current human scores (newest first).

    Filters: ?run_id, ?project. Each comment includes a ``scores`` array plus a
    ``score_count`` and ``score_avg``."""
    limit = _clamp_int(request.args.get("limit"), default=100, low=1, high=500)
    offset = _clamp_int(request.args.get("offset"), default=0, low=0, high=1_000_000)
    run_id = request.args.get("run_id") or None
    project = request.args.get("project") or None

    try:
        comments = storage.list_review_comments(
            limit=limit, offset=offset, run_id=run_id, project_path=project
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to read review comments: {e}"}), 500

    return jsonify({"count": len(comments), "review_comments": comments})


@app.route("/api/comment-replies", methods=["GET"])
def get_comment_replies():
    """List human replies to the agent's review comments (newest first).

    Filters: ?run_id, ?project, ?note_id (the parent agent comment)."""
    limit = _clamp_int(request.args.get("limit"), default=100, low=1, high=500)
    offset = _clamp_int(request.args.get("offset"), default=0, low=0, high=1_000_000)
    run_id = request.args.get("run_id") or None
    project = request.args.get("project") or None
    note_id_raw = request.args.get("note_id")
    note_id = None
    if note_id_raw:
        try:
            note_id = int(note_id_raw)
        except ValueError:
            return jsonify({"error": "note_id must be an integer"}), 400

    try:
        replies = storage.list_comment_replies(
            limit=limit, offset=offset, run_id=run_id, project_path=project, note_id=note_id
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"failed to read comment replies: {e}"}), 500

    return jsonify({"count": len(replies), "comment_replies": replies})


if __name__ == "__main__":
    # Local dev only; in Docker the CMD runs gunicorn against main:app.
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
