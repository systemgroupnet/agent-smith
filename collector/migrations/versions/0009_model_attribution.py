"""add model attribution for rate-limit failover runs

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-02

When a provider rate limit (HTTP 429) outlives the reviewer's retry budget, the
run switches to ``FALLBACK_LLM_MODEL`` for its remainder. Such a run is
**mixed**: files reviewed before the switch ran on the primary model, files
after on the fallback. Until now the store could not express that at all —
``review_runs.model`` was a single static read of ``DEFAULT_LLM_MODEL``, so a
run executed entirely on the fallback still reported the primary, and every
comment was implicitly credited to it.

Adds:
* ``review_runs.model_fallback_used`` (bool) — did this run fail over?
* ``review_runs.model_fallback_model`` (text) — what it switched to (NULL if not)
* ``review_comments.model`` (text) — the model that actually reviewed that
  comment's file, which is the only per-comment attribution there is.

All nullable with no backfill: rows written before this migration genuinely do
not know which model produced them, and NULL says that honestly. Read them as
"pre-attribution" rather than "no failover" — a dashboard comparing models
should filter on ``model IS NOT NULL``. Idempotent-safe: each column is skipped
if it already exists.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


_ADDITIONS = (
    ("review_runs", "model_fallback_used", lambda: sa.Column("model_fallback_used", sa.Boolean)),
    ("review_runs", "model_fallback_model", lambda: sa.Column("model_fallback_model", sa.String)),
    ("review_comments", "model", lambda: sa.Column("model", sa.String)),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table, column, make_column in _ADDITIONS:
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if column not in existing:
            op.add_column(table, make_column())


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table, column, _ in reversed(_ADDITIONS):
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        if column in existing:
            op.drop_column(table, column)
