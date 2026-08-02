# Grafana dashboards

Dashboard JSON kept in the repo alongside the code that produces the data, so a
schema change and the panels reading it move together. Both read the collector's
PostgreSQL directly (datasource uid `ffrgyjxlmntogb`).

| File | Grafana title | uid | What it answers |
|---|---|---|---|
| `rating-stats.json` | Rating Stats | `cfsrndx5q1la8d` | Are the review comments any good — human 1–5 ratings, replies, worst-rated comments, per-model quality |
| `run-stats.json` | Run Stats | `efrh010c710jkf` | Is the reviewer healthy — run outcomes, durations, throughput, failover |

Update flow: edit in Grafana → **Dashboard settings → JSON Model** → copy over the
file here → commit. Or edit the file and import it. Keep the `uid` and panel `id`s
stable so an import updates the existing dashboard instead of creating a new one.

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
errors in another). To check every panel against a real schema, run the collector's
migrations into a scratch database, seed a run, and execute each `rawSql` with the
Grafana macros stubbed — `$project` → a literal, `$__timeFilter(x)` → a plain
comparison, `$__timeGroup(x, '1w')` → `date_trunc`. Run each query on its own
connection: one failure inside a shared transaction aborts it and every later query
reports the misleading "current transaction is aborted" instead of its own error.
