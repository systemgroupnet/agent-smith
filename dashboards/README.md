# Grafana dashboards

Dashboard JSON kept in the repo alongside the code that produces the data, so a
schema change and the panels reading it move together. Both read the collector's
PostgreSQL directly (datasource uid `ffrgyjxlmntogb`).

| File | Grafana title | uid | What it answers |
|---|---|---|---|
| `rating-stats.json` | Rating Stats | `cfsrndx5q1la8d` | Are the review comments any good — human 1–5 ratings, replies, worst-rated comments, per-model quality |
| `engagement.json` | Engagement | `aid-engagement-1` | Who is engaging, and what have they earned — points, levels, badges, weekly award winners, coverage. **Filterable by person** (`$user`), so it is the one dashboard meant to be shared with the team rather than read by whoever maintains the reviewer |
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

**Every panel filters on both `$project` and `$__timeFilter` — there are no
exceptions, and a new panel without them is a bug.** A dashboard where some
panels honour the Project picker and some quietly ignore it is worse than one
that ignores it everywhere: the numbers still look plausible, so nobody notices
that `Recent comments` was listing another team's ratings beside this team's
run counts. Four panels had drifted (`Recent comments` and `Version stat` in
rating-stats, `Reviews per project` and `Re-review churn per MR` in run-stats)
and are fixed; the check is two lines, so run it after any edit:

```bash
python3 -c "
import json,glob
for f in glob.glob('*.json'):
    for p in json.load(open(f))['panels']:
        for t in p.get('targets',[]):
            s=t.get('rawSql','') or ''
            miss=[n for n,k in (('project','\$project'),('time','\$__timeFilter')) if k not in s]
            if s.strip() and miss: print(f, p['id'], p['title'], 'missing', miss)"
```

Two consequences worth knowing:

* **`project_path IN ($project)` drops rows where `project_path` is NULL.** The
  variable's own query is `where project_path is not null`, and `NULL IN (…)` is
  never true, so "All" does not mean *all rows*. Only pre-fix runs can have a NULL
  project — identity is populated before anything that can fail now — but it is why
  `Reviews per project` shows fewer runs than it used to: its `coalesce(project_path,
  'Unknown')` bucket is unreachable through the filter. That bucket was legacy
  early-failure runs, not live data.
* **Filter on the column that anchors the panel's question, not the nearest one.**
  `Version stat` is `review_comments LEFT JOIN comment_scores`; a `WHERE` on the
  left-joined table would silently turn it into an inner join, and the panel counts
  two different things (comments posted vs. ratings given) that cannot share one time
  column. Both filters therefore sit on the already-joined `review_runs`
  (`r.project_path`, `r.timestamp`), i.e. *versions that ran in the window, and every
  rating of their comments*.

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

**`engagement.json` has a `$user` variable, and every panel on it filters
`$project`, `$user` *and* the time picker.** The exception is deliberate and
labelled in the panel description: `Team rating coverage` ignores `$user` because
it is the one number the whole team owns (slicing it per person answers a
different question — that is what `Coverage by developer` is for), and
`Weekly wins` ignores `$project` because it counts wins on the global (`'*'`)
scope, which is what the Mattermost digest announces and therefore what the team
treats as a win.

**Two traps when generating these panels from the catalogue.** Badge labels and
blurbs go into SQL `CASE` arms as string literals, and one of them contains an
apostrophe — "Rated 10 comments on other people's merge requests" — which closed
the string and broke the panel until the generator escaped it. And when checking
the panels against a database, use **one connection per query**: the first
failure aborts a shared transaction and every later query then reports "current
transaction is aborted" instead of its own error, which hides the real one behind
four fake ones.

**The points formula is duplicated here, and `collector/engagement.py` is the
source of truth.** The engagement layer keeps every query in Python — there are
no database views — so the `Points & levels` panel carries its own copy of the
ledger CTE. That copy was generated from `engagement.POINTS_LEDGER_SQL` rather
than retyped, and the weights (`rate a finding 2 · rate a summary 1 · +1 peer ·
+1 within 24h · first substantive reply 5 · other replies 1`) exist in both
places. **Change them in `engagement.py` and regenerate the panel**, or the
dashboard and the Mattermost digest will quietly disagree about who is winning.
The same applies to the emoji/label `CASE` expressions in `Achievement wall` and
`Recently unlocked`, which mirror `engagement.ACHIEVEMENTS`, and to the level
thresholds in `Points & levels`, which mirror `engagement.POINT_LEVELS`.

Two things about the engagement panels that are easy to get wrong:

- **Count `DISTINCT note_id`, never `COUNT(*)`, over `comment_scores`.** That
  table is unique on `(note_id, user_id, emoji)`, so a rater who clicked 3,
  reconsidered, clicked 4 and never revoked the 3 leaves **two rows** — which is
  what the pre-existing `Rater leaderboard` was doing until it was given the
  `DISTINCT ON (note_id, user_id) … ORDER BY … received_at DESC` canonical CTE it
  now uses. Any panel that scores people needs the same treatment.
- **Weekly awards filter on `week_start::timestamptz`, not on `iso_week`.**
  `$__timeFilter` cannot filter a `2026-W34` label, and a panel that ignores the
  time picker is the failure mode described above. `week_start` is that week's
  Monday, stored precisely so the picker keeps working.

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
