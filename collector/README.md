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
| `GET`  | `/api/points` | Engagement points leaderboard, highest first, each total with its breakdown (ratings, replies, bonuses) and level. Query params: `limit`, `project`, `days` (window; omit for all-time). |
| `GET`  | `/api/achievements` | Unlocked badges, newest first, enriched with emoji/label/blurb from the catalogue. Query params: `limit`, `offset`, `project`, `username`. |
| `GET`  | `/api/weekly-awards` | Weekly winners per ISO week. Query params: `limit`, `project`, `week` (e.g. `2026-W34`). |
| `POST` | `/api/achievements/recompute` | Re-evaluate every participant over all history and award what they have earned but not been credited with. Idempotent; honours `COLLECTOR_TOKEN`. |
| `POST` | `/api/weekly-awards/refresh` | Settle one ISO week (`?week=2026-W34`, optionally `?project=`), or every week that has ever had activity when `week` is omitted. Idempotent; honours `COLLECTOR_TOKEN`. |
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
- `review_summaries` / `summary_scores` — the reviewer's one-off **review summary**
  comment per MR, and its human ratings. Deliberately *not* stored in
  `review_comments`/`comment_scores` with a `comment_type` discriminator: a summary
  and a finding answer different questions, so pooling their ratings is
  meaningless, and a filter-based split would have silently changed every existing
  count and average the moment the first summary was rated. Separating them at
  write time (`_scores_table_for` routes each webhook award by which table owns the
  note) means **every query over `review_comments`/`comment_scores` still means
  findings only, with no filter**. Replies carry `comment_replies.parent_kind`
  (`finding`|`summary`) for the same reason. Note that `review_summaries.discussion_id`
  is **NULL for summaries posted by a current reviewer**: the summary is now a plain
  note rather than a resolvable thread (nothing about it is resolvable, and an
  unresolved thread blocks merging on projects that require them all closed), and
  GitLab's note API returns no discussion id — so no new `parent_kind = 'summary'`
  reply can ever be recorded, and GitLab offers no thread reply on such a note to
  begin with. Existing rows keep their meaning; the summary's **ratings** are
  unaffected, since those key on `note_id`.
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

- `achievements` — one row per unlocked badge, unique on
  `(username, achievement_key, project_path)`. **`awarded_at` is when the
  threshold was crossed**, derived from the rating or reply that crossed it —
  not when the row was written — so a backfill over a year of history produces a
  real timeline instead of a thousand badges stamped with the migration date.
  `progress_value` records the counter at unlock, which is how a live unlock at
  exactly 100 is told apart from a backfill that crossed several thresholds at
  once. Per-project by design: every dashboard panel filters `$project`, so a row
  with no project could not honour the picker.
- `weekly_awards` — one row per `(iso_week, project_path, category)`: who won.
  `iso_week` is `2026-W34` (ISO year **and** week — plain week numbers collide
  across years, and 1 Jan 2027 belongs to `2026-W53`); `week_start` carries that
  week's Monday as an ISO date because Grafana's `$__timeFilter` cannot filter on
  a week label.

### Engagement: points, badges, weekly awards

`engagement.py` owns all of it — the catalogue, the queries, the points weights.
`storage.py` only holds the table definitions and thin wrappers, because
migration `0011` imports the module to backfill and must not drag the ingest
layer in with it.

Three deliberate splits:

- **Points are derived, never stored.** A revoked reaction has to take its points
  back with it, and a materialized counter would get that wrong exactly when
  somebody noticed.
- **Achievements are stored and never revoked.** A milestone must not vanish when
  a dashboard window rolls past it.
- **Weekly awards are stored per ISO week, never computed over a rolling window.**
  A "last 7 days" board changes every time you look at it, so nobody can be told
  they won; a named week closes, gets a winner, and stays settled.

Points:

| Action | Points |
|---|---|
| Rate a finding | 2 |
| Rate a review summary | 1 |
| Bonus: the merge request is someone else's | +1 |
| Bonus: rated within 24 h of the comment being posted | +1 |
| First substantive reply in a thread (≥ 15 chars) | 5 |
| Any other reply | 1 |

Levels: 🥉 Bronze < 50, 🥈 Silver < 150, 🥇 Gold < 400, 💎 Diamond.

**Nothing scores a rater on the value they gave.** Rewarding "highest average
awarded" would inflate the exact signal the ratings exist to measure, so no
weight above reads `score` — a rule pinned down by
`tests/test_engagement.py::test_no_achievement_is_earned_by_the_score_a_rater_gave`.
For the same reason nothing rewards an author for *receiving* few findings: that
would pay people to open smaller merge requests. There is also no per-MR cap on
rating points — rating all 30 findings on a large MR is the behaviour worth
paying for, and the anti-farm metric is coverage, which is bounded at 100% per
person. Replies are the one action cheap enough to repeat, which is why only the
first substantive one in a thread earns full value.

Findings and summaries are **unioned** for engagement (a rating is a rating) and
stay separate for quality: `weekly_stats`' `agent_grade` reads
`kind = 'finding'` only, so what migration `0010` split apart is not silently
pooled back together.

### Weekly digest → Mattermost

`digest.py` posts a closed week's result to a channel: the five award winners, the
points table, badges crossed that week, and the two numbers that say whether the
loop is working (coverage, and the grade the reviewer earned).

```bash
python digest.py                    # last completed ISO week, all projects
python digest.py --week 2026-W34    # a past week — regenerates byte-identically
python digest.py --current          # the week in progress, as a preview
python digest.py --dry-run          # print it, post nothing
```

It settles the week's awards before reading them, so a missed run or a late
rating cannot leave the announcement disagreeing with the dashboard. One HTTP
POST via `urllib` — no new dependency in a service that would need `requests` for
nothing else. The token is a write credential for the channel and is never
printed, including in error paths.

**The service schedules it — there is no host crontab.** `scheduler.py` starts a
daemon thread from `main.py` (after the migrations, since its first pass reads
`digest_runs`). Setting `MATTERMOST_URL` + `MATTERMOST_TOKEN` +
`MATTERMOST_CHANNEL_ID` turns it on; `DIGEST_ENABLED=false` is an explicit
off-switch, and an **empty** `DIGEST_ENABLED` means enabled, because the
credentials are already the opt-in. Cadence is `DIGEST_DAY` / `DIGEST_HOUR` /
`DIGEST_TIMEZONE`, default Monday 09:00 UTC.

Three properties worth knowing:

- **It is a ticker, not a timer.** Every `DIGEST_TICK_S` (default 900 s) the thread
  asks "which week is due, and has anyone dealt with it?" — it never sleeps until
  Monday. So a restart costs nothing, a deploy at the firing moment does not lose
  a digest, and a service that was down posts as soon as it is back. The schedule
  holds no state of its own.
- **The week is claimed before it is posted.** `digest_runs` is unique on
  `iso_week`, so at most one worker can announce a given week however many
  replicas run. **A claim is never released, even on failure** — a post that fails
  after the request reached Mattermost is indistinguishable from one that failed
  before it, and a duplicate announcement in a channel is permanent while a missed
  week is one command away. A failure logs loudly and records `status='failed'`
  with the error; re-send it with `python digest.py --week 2026-W34`.
- **A week with no activity is recorded and not posted.** A weekly "nothing to
  report" message is how a digest channel gets muted. `python digest.py --force`
  overrides that for a first announcement.

Only the most recent due week is ever posted: a service down for a fortnight sends
one digest, not three, because dumping a wall of history into the channel during
an outage helps nobody. Earlier weeks are still reachable with `--week`.

Inspect what the scheduler has done with `storage.list_digest_runs()`, or:

```sql
SELECT iso_week, status, posted_at, error FROM digest_runs ORDER BY iso_week DESC;
```

Schema is managed by **Alembic** (`migrations/`). `storage.run_migrations()`
applies `alembic upgrade head` at startup (called from `main.py` at import), so a
fresh database is created and an existing one is upgraded automatically. The
initial revision is guarded to adopt a table created by the earlier
`create_all`-based collector without dropping data; revision `0004` backfills the
old `review_runs.errors` JSONB into `run_events` before dropping that column;
revision `0005` adds `review_comments` + `comment_scores`; revision `0006` adds
`comment_scores.project_path`; revision `0007` renames `comment_scores.mr_iid`
→ `mr_id`; revision `0008` adds `comment_replies`; revision `0011` adds
`achievements` + `weekly_awards` **and backfills both over the entire rating
history** — unlocks are otherwise only awarded on the ingest path, so a deploy
would leave everyone who rated before it at zero badges, and the people with the
most ratings to their name would be exactly the ones it looked broken for. That
backfill is non-fatal: a failure warns and leaves the tables empty rather than
aborting the migration, since the schema is what the service needs to start and
the data credit is one API call away (`POST /api/achievements/recompute`). To run
migrations by hand: `COLLECTOR_DATABASE_URL=… alembic upgrade head` (from
`collector/`).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `COLLECTOR_DATABASE_URL` | _(required)_ | PostgreSQL SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/collector`. |
| `COLLECTOR_TOKEN` | _(unset)_ | When set, `POST /api/events` requires a matching `X-Collector-Token` header. Unset → open (fine on a trusted network). |
| `GITLAB_WEBHOOK_SECRET` | _(unset)_ | When set, `POST /api/webhooks/gitlab` requires a matching `X-Gitlab-Token` header (the secret token you configure on the GitLab webhook). Unset → open. |
| `PORT` | `8000` | Only used by the local dev server (`python main.py`). |
| `MATTERMOST_URL` | _(unset)_ | Mattermost base URL for the weekly digest, e.g. `https://mattermost.example.com`. |
| `MATTERMOST_TOKEN` | _(unset)_ | Bot account or personal access token. A **write credential for the channel** — keep it out of git. |
| `MATTERMOST_CHANNEL_ID` | _(unset)_ | Target channel id (Channel → View Info). Overridable per run with `--channel`. |
| `DIGEST_PROJECTS` | _(unset)_ | Comma-separated allowlist of project paths the digest reports on. Unset → every project with any engagement data. |
| `DIGEST_ENABLED` | _(enabled)_ | Explicit off-switch for the in-service scheduler. The three `MATTERMOST_*` values are the opt-in, so an **empty value means enabled** — set `false`/`0`/`no`/`off` to stop the service posting. |
| `DIGEST_DAY` | `mon` | Day the digest goes out (`mon`…`sun`, or `0`…`6`). Only changes *when* it is announced — the week reported is always the one before the firing moment's week. Since ISO weeks close on Sunday midnight, `sun` is always a week behind (7 days old) and `mon` reports the week that closed the previous night. |
| `DIGEST_HOUR` | `9` | When it goes out, in `DIGEST_TIMEZONE`. Takes `HH` or `HH:MM` — `11:45` means a quarter to twelve. |
| `DIGEST_MINUTE` | _(from `DIGEST_HOUR`)_ | Minute past the hour, if you would rather keep it separate from `DIGEST_HOUR`. Set explicitly, it wins over the `HH:MM` form. |
| `DIGEST_TIMEZONE` | `UTC` | IANA zone the schedule is expressed in, e.g. `Asia/Tehran`. Every date in the due-week calculation is derived from this one clock. |
| `DIGEST_TICK_S` | `900` | How often the thread checks whether a week is due (floor 60). It is a ticker, not a timer, so a restart loses nothing. |

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
