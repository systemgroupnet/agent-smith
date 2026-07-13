# collector

Second service of the `codereviewagent` monorepo (alongside [`reviewer/`](../reviewer)).

Ingests one **analytics event per review run** emitted by the reviewer and stores
it in PostgreSQL. A dashboard (e.g. DevLake/Grafana) can be built on top of the
read API or the database later.

## Endpoints

| Method | Path          | Purpose |
|--------|---------------|---------|
| `POST` | `/api/events` | Ingest one run event (JSON body). Returns `201 {"status":"stored","id":N}`. |
| `GET`  | `/api/events` | List recent events, newest first. Query params: `limit` (1–500, default 50), `offset`, `project`. |
| `GET`  | `/api/run-events` | List captured errors/warnings, newest first. Query params: `limit` (1–500, default 100), `offset`, `run_id`, `severity` (`error`/`warning`). |
| `GET`  | `/health`     | Liveness + stored-event count. |

The payload is validated **leniently**: it must be a JSON object, but unknown or
missing fields are tolerated (unknown fields are preserved in a `raw_json` JSONB
column; missing typed fields become `NULL`). This lets the reviewer and collector
evolve independently — the contract is JSON plus a `schema_version` field, not a
shared Python model (each service has its own Docker build context and can't
import the other's code). See [`reviewer/dto/analytics.py`](../reviewer/dto/analytics.py)
for the fields the reviewer currently sends.

## Storage

**PostgreSQL** (required; no SQLite fallback), via `COLLECTOR_DATABASE_URL`. Two
tables:

- `review_runs` — one row per run: typed columns for the fields a dashboard
  filters/groups on (project, status, review_type, counts, duration, timestamps,
  `reviewer_version`), denormalized `error_count`/`warning_count` for alerting,
  plus a `raw_json` **JSONB** column preserving the full payload.
- `run_events` — one row per captured error/warning (`severity` `error`/`warning`,
  `stage`, `context`, `message`, `traceback`, `occurred_at`), keyed to the run by
  `run_id` + `review_run_id`. Kept separate from the runs so failures/warnings are
  queryable and joinable on their own (debugging, alerting, DevLake).

Schema is managed by **Alembic** (`migrations/`). `storage.run_migrations()`
applies `alembic upgrade head` at startup (called from `main.py` at import), so a
fresh database is created and an existing one is upgraded automatically. The
initial revision is guarded to adopt a table created by the earlier
`create_all`-based collector without dropping data; revision `0004` backfills the
old `review_runs.errors` JSONB into `run_events` before dropping that column. To
run migrations by hand: `COLLECTOR_DATABASE_URL=… alembic upgrade head` (from
`collector/`).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `COLLECTOR_DATABASE_URL` | _(required)_ | PostgreSQL SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/collector`. |
| `COLLECTOR_TOKEN` | _(unset)_ | When set, `POST /api/events` requires a matching `X-Collector-Token` header. Unset → open (fine on a trusted network). |
| `PORT` | `8000` | Only used by the local dev server (`python main.py`). |

## Build / run locally

```bash
# Directly (dev server on :8000) — needs a reachable PostgreSQL
cd collector
pip install -r requirements.txt
COLLECTOR_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/collector python main.py

# Or via Docker Compose (brings up Postgres + collector together)
docker compose -f ../docker-compose.collector.yml up -d

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
