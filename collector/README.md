# collector

Second service of the `codereviewagent` monorepo (alongside [`reviewer/`](../reviewer)).

Ingests one **analytics event per review run** emitted by the reviewer and stores
it in SQLite. A dashboard can be built on top of the read API later.

## Endpoints

| Method | Path          | Purpose |
|--------|---------------|---------|
| `POST` | `/api/events` | Ingest one run event (JSON body). Returns `201 {"status":"stored","id":N}`. |
| `GET`  | `/api/events` | List recent events, newest first. Query params: `limit` (1–500, default 50), `offset`, `project`. |
| `GET`  | `/health`     | Liveness + stored-event count. |

The payload is validated **leniently**: it must be a JSON object, but unknown or
missing fields are tolerated (unknown fields are preserved in a `raw_json` blob;
missing typed fields become `NULL`). This lets the reviewer and collector evolve
independently — the contract is JSON plus a `schema_version` field, not a shared
Python model (each service has its own Docker build context and can't import the
other's code). See [`reviewer/dto/analytics.py`](../reviewer/dto/analytics.py) for
the fields the reviewer currently sends.

## Storage

SQLite at `COLLECTOR_DB_PATH` (default `/data/collector.db`). The Docker image
mounts a named volume at `/data` so data survives container restarts. One
`review_runs` table: typed columns for the fields a dashboard filters/groups on
(project, status, review_type, counts, duration, timestamps) plus the `raw_json`
blob.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `COLLECTOR_DB_PATH` | `/data/collector.db` | SQLite file path. |
| `COLLECTOR_TOKEN` | _(unset)_ | When set, `POST /api/events` requires a matching `X-Collector-Token` header. Unset → open (fine on a trusted network). |
| `PORT` | `8000` | Only used by the local dev server (`python main.py`). |

## Build / run locally

```bash
# Directly (dev server on :8000)
cd collector
pip install -r requirements.txt
COLLECTOR_DB_PATH=./collector.db python main.py

# Or via Docker (serves with gunicorn on :8000)
docker build -t collector:dev collector
docker run --rm -p 8000:8000 -v collector-data:/data collector:dev

# Smoke test
curl localhost:8000/health
curl -X POST localhost:8000/api/events -H 'Content-Type: application/json' -d '{"run_id":"x","status":"success"}'
curl localhost:8000/api/events
```

The full stack (reviewer + MCP servers + collector) runs via the repo-root
`docker-compose.yml`. The reviewer only sends analytics when `COLLECTOR_ENABLED=true`.

The image is built and pushed by the repo's `.gitlab-ci.yml` (see the
`collector build` / `collector push` jobs) on pushes to the default branch and
on git tags.
