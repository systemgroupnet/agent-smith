# Grafana dashboards

Dashboard JSON kept in the repo alongside the code that produces the data, so a
schema change and the panels reading it move together. Both read the collector's
PostgreSQL directly (datasource uid `ffrgyjxlmntogb`).

| File | Grafana title | uid | What it answers |
|---|---|---|---|
| `rating-stats.json` | Rating Stats | `cfsrndx5q1la8d` | Are the review comments any good — human 1–5 ratings, replies, worst-rated comments, per-model quality |
| `run-stats.json` | Run Stats | `efrh010c710jkf` | Is the reviewer healthy — run outcomes, durations, throughput, failover |

## Applying a file to Grafana

Use **Dashboards → New → Import → paste the JSON → Load → Import (overwrite)**.
Because the file keeps its `uid`, that updates the existing dashboard in place
rather than creating a copy. Panel `id`s are stable for the same reason — keep
them.

Do **not** paste into *Dashboard settings → JSON Model*: that path submits the
model as an edit of the currently-loaded dashboard and is what produces the
"someone else has updated this dashboard" error described below.

### "Someone else has updated this dashboard" (when nobody has)

Grafana version-locks dashboards optimistically. Every save increments a
`version` counter, and a save whose payload carries a *different* version than
the one stored is rejected as a concurrent edit — whether or not anyone else
touched it. Editing a saved file and pasting it back is the usual way to trip
this: the file's `version` is whatever it was when exported, and Grafana has
moved on.

These files therefore carry **no `version` field and `"id": null`** on purpose:

* `version` — Grafana's optimistic lock. Stale by definition in a file, and the
  sole cause of the phantom-conflict error. Omitted, so Grafana applies the
  import to whatever version it currently holds.
* `id` — the internal database row id, meaningful only to one Grafana instance.
  `null` is what the import API expects; `uid` is the portable identity.

If you re-export from Grafana to update a file here, **strip both again** before
committing, or the next person hits the same error:

```bash
python3 - <<'PY'
import json, glob
for f in glob.glob("*.json"):
    d = json.load(open(f)); d["id"] = None; d.pop("version", None)
    json.dump(d, open(f, "w"), indent=2)
PY
```

Update flow: edit in Grafana → **Dashboard settings → JSON Model** → copy the
model into the file here → run the snippet above → commit.

## Things that will bite you

**Timestamps are `TEXT`, not `timestamptz`.** Every time filter must cast:
`$__timeFilter(received_at::timestamptz)`. Without the cast Postgres compares
strings, which happens to work for ISO-8601 UTC and silently stops working the
moment a value carries a different fractional-second or offset format. `awarded_at`
was missing its cast and has been fixed.

**Findings and summaries are separate tables, on purpose.** The reviewer posts two
different artifacts: review comments (findings) and a one-off review summary. Both
can be rated with the same emoji scale, but averaging them together is meaningless.
They are split at write time rather than by a `comment_type` filter, so a query
cannot accidentally combine them by forgetting one:

| Findings | Summaries |
|---|---|
| `review_comments` | `review_summaries` |
| `comment_scores` | `summary_scores` |
| `comment_replies WHERE parent_kind = 'finding'` | `comment_replies WHERE parent_kind = 'summary'` |

So every panel over `review_comments` / `comment_scores` means **findings only**,
with no filter needed. That is the whole point of the split — see collector
migration `0010`.

**Issues *found* and comments *posted* are different numbers in different tables.**
`review_runs.issues_high/medium/low` count what the reviewers reported; the severity
of what a human actually sees is one row per comment in `review_comments.severity`
(`Comments posted by severity` / `Posted severity mix`, and the `PostedHigh` /
`PostedMedium` columns of `Latest runs`, matching the 🔴/🟠/🔵 badge in the comment
body). Posted is always the smaller number, and the gap is not loss — it is the
in-diff gate, the compile-time purge and the verbosity cap doing their job.

**Do not put a found count and a posted count in the same row** unless the row also
explains the gap. `Latest runs` used to show `issues_high` beside `comments_posted`,
where "HighSeverity 2, CommentsPosted 0" reads as a bug but is usually the in-diff
gate having dropped both, and "MediumSeverity 2, posted 0" is the default verbosity
discarding mediums exactly as designed. It now counts `review_comments` per run
(`LEFT JOIN LATERAL … WHERE review_run_id = r.id`) and carries the run's `Verbosity`
beside them, so every number in the row is the same kind of number. Verbosity comes
from `raw_json ->> 'review_verbosity'` — there is no typed column yet — and reads `—`
for runs predating the field.

Two consequences for a query:

* At the default verbosity only **high** findings are collected at all, so mediums
  in the posted mix mean someone put a `review::-vv` label on that MR. Mediums in the
  *found* counts mean nothing of the sort — they are counted and then discarded.
* Severity is stored as the model emitted it, so match with `lower(severity)`.
* **A `low` row before v0.7.4 is an artifact, not a posted low.** Lows are out of
  scope at every verbosity level, so nothing low can ever be posted. The comment
  reviewer used to be able to drop the `severity` field, and `ReviewIssue.severity`
  defaults to `"low"` — five rows from 2026-08-05 are genuine **highs** recorded as
  lows. The reviewer now restores the severity deterministically
  (`restore_issue_severities`); the historical rows are still wrong in the table, and
  `UPDATE review_comments SET severity = 'high' WHERE severity = 'low';` is provably
  correct for them (their runs were all default-verbosity, i.e. highs-only input).

Both panels bucket on `coalesce(review_runs.timestamp, review_comments.received_at)`:
the run's own timestamp is what `Issues found over time` uses, so found and posted
bars for the same review land in the same bucket, and the `LEFT JOIN` + `coalesce`
keeps a comment whose run row is missing from vanishing. That expression is computed
in a **derived table** (`… ) posted`) and the macros then take the bare column
`occurred_at` — see the next item for why it cannot be inlined.

**A Grafana macro argument may not contain a comma or a `)`.** The macro engine
matches `$__timeGroup\((.*?)\)` — non-greedy, so the **first** `)` ends it — and then
splits what it captured on `,`. A function call as an argument therefore falls apart
silently in the file and only fails in the browser:

```sql
-- ✗ Grafana reads `c.received_at` as the interval, and $__timeFilter takes
--   `coalesce(r.timestamp` as its column — a TEXT column compared to a timestamptz:
--     interpolation failed: error parsing interval c.received_at
--     operator does not exist: character varying <= timestamp with time zone
$__timeGroup(coalesce(r.timestamp, c.received_at) :: timestamptz, '1d')

-- ✓ compute it once in a subquery, pass a bare column name
FROM ( SELECT coalesce(r.timestamp, c.received_at) :: timestamptz AS occurred_at … ) posted
$__timeGroup(occurred_at, '1d')
```

A cast is fine (`$__timeGroup(timestamp :: timestamptz, '1d')`) — it has neither
character. This is also why hand-expanding a macro is not a test: the expansion is
correct while the *parse* is what breaks.

**`review_runs.model` is the *configured* model, not necessarily the one that ran.**
When a rate limit forces a failover mid-run, `model` still reports the primary;
`model_fallback_used` / `model_fallback_model` record the switch. A failed-over run
is **mixed** — some comments from each model — so:

* group per-comment quality on **`review_comments.model`** (the model that actually
  produced that comment), not `review_runs.model`;
* for run-level breakdowns, key on
  `CASE WHEN model_fallback_used THEN model || ' → ' || model_fallback_model ELSE model END`.

`review_comments.model` is `NULL` for rows written before migration `0009`. That
means *unknown*, not *no failover* — filter `model IS NOT NULL` when comparing
models, or those rows land in a bogus bucket.

## Validating changes

Panel SQL is easy to break silently (a bad cast returns rows in one environment and
errors in another). To check every panel against a real schema, create a scratch
database from `collector/storage.py`'s own SQLAlchemy metadata (or run the
migrations), seed a run, and execute each `rawSql` with the Grafana macros expanded.
Run each query on its own connection: one failure inside a shared transaction aborts
it and every later query reports the misleading "current transaction is aborted"
instead of its own error.

**Expand the macros the way Grafana *parses* them, not the way they are documented.**
Substituting `$__timeGroup(x, '1d')` → `date_trunc(...)` by hand tests the meaning and
misses the argument-splitting rule above — which is exactly how the two errors quoted
there reached a live dashboard. A faithful stub is ~30 lines: match
`\$__(timeGroup|timeGroupAlias|timeFilter|timeFrom|timeTo|unixEpoch…)\b(?:\((.*?)\))?`,
split the capture on `,`, then emit
`floor(extract(epoch from args[0])/S)*S` for `timeGroup` and
`args[0] BETWEEN 'from' AND 'to'` for `timeFilter` — and make it **raise** on an
unparseable interval, since that is the failure you are hunting. Render `$project` as
a quoted comma-separated list, the way a multi-value variable arrives.
