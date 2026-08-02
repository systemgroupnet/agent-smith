# collector

Second service of the `codereviewagent` monorepo (alongside [`reviewer/`](../reviewer)).

Ingests one **analytics event per review run** emitted by the reviewer and stores
it in PostgreSQL. A dashboard (e.g. DevLake/Grafana) can be built on top of the
read API or the database later.

It also runs the **comment-evaluation loop**, fed by two GitLab webhooks on one
endpoint. The reviewer seeds every posted review comment with the `:one:`…`:five:`
award emojis; a human clicks one to rate it and GitLab's **emoji** webhook posts
the reaction here, stored keyed to the comment's GitLab note id. When a human
**replies in the thread** of one of those comments, GitLab's **comment** webhook
delivers the reply and it is stored against the same comment — so both the
numeric score and the written feedback on each finding can be measured over time.

## Endpoints

| Method | Path          | Purpose |
|--------|---------------|---------|
| `POST` | `/api/events` | Ingest one run event (JSON body). Returns `201 {"status":"stored","id":N}`. |
| `GET`  | `/api/events` | List recent events, newest first. Query params: `limit` (1–500, default 50), `offset`, `project`. |
| `GET`  | `/api/run-events` | List captured errors/warnings, newest first. Query params: `limit` (1–500, default 100), `offset`, `run_id`, `severity` (`error`/`warning`). |
| `POST` | `/api/webhooks/gitlab` | Receive GitLab webhook events: **emoji** reactions on review comments → 1–5 scores, and **comments** in a review comment's thread → replies. Optionally guarded by `X-Gitlab-Token` (see `GITLAB_WEBHOOK_SECRET`). |
| `GET`  | `/api/review-comments` | List posted review comments with their current human scores and replies (each carries a `scores` array + `score_count`/`score_avg`, and a `replies` array + `reply_count`). Query params: `limit` (1–500, default 100), `offset`, `run_id`, `project`. |
| `GET`  | `/api/comment-replies` | List human replies to the agent's review comments, newest first. Query params: `limit` (1–500, default 100), `offset`, `run_id`, `project`, `note_id` (the parent agent comment). |
| `GET`  | `/health`     | Liveness + stored-event count. |

### Comment-evaluation loop

1. The reviewer posts each selected review comment, then awards `:one:`…`:five:`
   on that note via the GitLab API (`POST …/notes/:id/award_emoji`) as a rating
   scale. The posted note ids ride along in the run event's `posted_comments`
   array, which the collector turns into `review_comments` rows.
2. A human clicks one of the reactions on the comment. GitLab (≥16.4) fires an
   **Emoji events** webhook to `POST /api/webhooks/gitlab`
   (`X-Gitlab-Event: Emoji Hook`).
3. The collector maps the emoji (`one`…`five`) to a score `1`…`5` and upserts a
   `comment_scores` row keyed on `(note_id, user_id, emoji)` — so redeliveries are
   idempotent and a `revoke` deletes the score. The reviewer's own **seed
   reactions are ignored** (the reacting user is the note's author), and a
   reaction on a note that **isn't a tracked review comment** — the review marker
   note (`🤖 … reviewed up to commit …`) or any other MR note — is ignored too, so
   only ratings of real review comments are stored. Each score also records the
   `project_path` (from the webhook's `project.path_with_namespace`) so ratings
   are attributable to a project by name.

### Replies to review comments

Ratings say *how good* a comment was; replies say *why*. Every comment the
reviewer posts is a GitLab **thread** (an inline discussion, or an unpositioned
discussion for the general-note fallback), and every note in a thread carries the
same `discussion_id` — which the reviewer already records in `posted_comments`.
So:

1. A human replies to one of the agent's comments in the MR.
2. GitLab fires a **Comments** webhook (`X-Gitlab-Event: Note Hook`) to the same
   `POST /api/webhooks/gitlab` endpoint; the collector routes it by
   `object_kind` (`emoji` → scores, `note` → replies).
3. The reply's `discussion_id` is looked up against `review_comments`. A match
   means it is a reply in that comment's thread, and identifies which agent
   comment it answers — so the reply is stored in `comment_replies` with
   `parent_note_id` and the parent's `run_id`. **No GitLab API call is needed**
   and no token is required on the collector.

Everything else is ignored: comments in threads the agent didn't start, replies
between humans elsewhere on the MR, notes on issues rather than merge requests,
GitLab **system** notes (label/assignee chatter), and the agent's **own**
comments, which fire this hook too but open a thread rather than reply to one.
An edited reply re-fires the hook with the same note id and upserts, so each
reply is one row holding its current text.

One ordering caveat: a reply can only be matched once the run event that carries
the parent comment has arrived (it is sent at the end of the review run, seconds
after the comment is posted). A reply that somehow beat it — or arrived while the
collector was down — is ignored rather than queued.

Enable both in the GitLab project: **Settings → Webhooks**, URL
`https://<collector-host>/api/webhooks/gitlab`, triggers **Emoji events** and
**Comments** (and set the secret token to match `GITLAB_WEBHOOK_SECRET` if
configured).

The payload is validated **leniently**: it must be a JSON object, but unknown or
missing fields are tolerated (unknown fields are preserved in a `raw_json` JSONB
column; missing typed fields become `NULL`). This lets the reviewer and collector
evolve independently — the contract is JSON plus a `schema_version` field, not a
shared Python model (each service has its own Docker build context and can't
import the other's code). See [`reviewer/dto/analytics.py`](../reviewer/dto/analytics.py)
for the fields the reviewer currently sends.

## Storage

**PostgreSQL** (required; no SQLite fallback), via `COLLECTOR_DATABASE_URL`. Five
tables:

- `review_runs` — one row per run: typed columns for the fields a dashboard
  filters/groups on (project, status, review_type, counts, duration, timestamps,
  `reviewer_version`), denormalized `error_count`/`warning_count` for alerting,
  plus a `raw_json` **JSONB** column preserving the full payload.
  `model` is the *configured* model; `model_fallback_used` (bool) and
  `model_fallback_model` say whether a provider rate limit forced the run onto a
  second provider partway through. Such a run is **mixed** — filter it out of a
  model-quality comparison, or use per-comment `review_comments.model` instead.
- `run_events` — one row per captured error/warning (`severity` `error`/`warning`,
  `stage`, `context`, `message`, `traceback`, `occurred_at`), keyed to the run by
  `run_id` + `review_run_id`. Kept separate from the runs so failures/warnings are
  queryable and joinable on their own (debugging, alerting, DevLake).
- `review_comments` — one row per review comment actually posted to a MR (from the
  run event's `posted_comments` array): `note_id` (GitLab note id, **unique** —
  the reaction join key), `discussion_id`, `comment_type` (`inline`/`note`),
  `file`, `line`, `severity`, `model`, `mr_author_username`, keyed to the run.
  Upserted on `note_id` so a re-emitted run is idempotent. `model` is the model
  that actually reviewed that comment's file — the only per-comment attribution
  there is, and the one to group by when comparing models. It is NULL for rows
  written before migration `0009`, which means "unknown", not "no failover".
- `comment_scores` — one row per human 1–5 rating gathered from the emoji webhook:
  `note_id` (joins `review_comments`), `project_id`/`project_path` (the project the
  comment lives on), `mr_id`, reacting `user_id`/`username`/`user_name`, `emoji`
  (`one`…`five`), `score` (1–5), `awarded_at`. **Unique** on
  `(note_id, user_id, emoji)` (idempotent upsert; `revoke` deletes the row). Only
  ratings of tracked review comments are stored — reactions on the review marker
  note or other MR notes are ignored.
- `comment_replies` — one row per human reply in a review comment's thread,
  gathered from the comment webhook: `note_id` (the reply's own GitLab note id,
  **unique** — upserted so edits and redeliveries don't duplicate),
  `discussion_id`, `parent_note_id` (joins `review_comments.note_id`), the
  parent's `run_id`, `project_id`/`project_path`, `mr_id`, the replying
  `author_id`/`author_username`/`author_name`, the `body` text, `note_type`
  (`DiffNote`/`DiscussionNote`), `url`, `noted_at`, `updated_at`, `action`.

Schema is managed by **Alembic** (`migrations/`). `storage.run_migrations()`
applies `alembic upgrade head` at startup (called from `main.py` at import), so a
fresh database is created and an existing one is upgraded automatically. The
initial revision is guarded to adopt a table created by the earlier
`create_all`-based collector without dropping data; revision `0004` backfills the
old `review_runs.errors` JSONB into `run_events` before dropping that column;
revision `0005` adds `review_comments` + `comment_scores`; revision `0006` adds
`comment_scores.project_path`; revision `0007` renames `comment_scores.mr_iid`
→ `mr_id`; revision `0008` adds `comment_replies`. To run migrations by hand:
`COLLECTOR_DATABASE_URL=… alembic upgrade head` (from `collector/`).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `COLLECTOR_DATABASE_URL` | _(required)_ | PostgreSQL SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/collector`. |
| `COLLECTOR_TOKEN` | _(unset)_ | When set, `POST /api/events` requires a matching `X-Collector-Token` header. Unset → open (fine on a trusted network). |
| `GITLAB_WEBHOOK_SECRET` | _(unset)_ | When set, `POST /api/webhooks/gitlab` requires a matching `X-Gitlab-Token` header (the secret token you configure on the GitLab webhook). Unset → open. |
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
