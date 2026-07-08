#!/usr/bin/env python3
"""Collector — receives and stores per-run analytics from the reviewer.

Second service of the codereviewagent monorepo (alongside ``reviewer/``). The
reviewer emits one analytics event per MR-review run (see
``reviewer/core/analytics/client.py``); this service ingests those events into a
SQLite database and exposes a read API a future dashboard can build on.

Endpoints:
  POST /api/events   ingest one event (JSON body). Optionally token-guarded.
  GET  /api/events   list recent events, newest first (?limit, ?offset, ?project).
  GET  /health       liveness + stored-event count.

Contract note: the payload is validated leniently. Each service has its own
Docker build context and cannot import the other's models, so the contract is
JSON. Unknown/extra fields are stored, not rejected, and a ``schema_version``
field on the payload lets the two evolve independently.
"""

import os

from flask import Flask, jsonify, request

import storage

app = Flask(__name__)

# Init the DB at import time so it's ready under any WSGI server (gunicorn imports
# the module; there is no __main__). Safe to call repeatedly (CREATE IF NOT EXISTS).
storage.init_db()


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


if __name__ == "__main__":
    # Local dev only; in Docker the CMD runs gunicorn against main:app.
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
