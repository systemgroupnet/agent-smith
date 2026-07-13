"""move errors into a dedicated run_events table (+ warnings, warning_count)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-13

Errors used to live as a JSONB column on review_runs. This moves them into a
dedicated ``run_events`` table (one row per error/warning, discriminated by
``severity``, keyed to the run) so they can be queried/joined on their own, and
adds ``warning_count`` to review_runs. Existing ``review_runs.errors`` data is
backfilled into ``run_events`` before the column is dropped, so no data is lost.
All steps are guarded so the migration is safe to re-run and on a DB created by
an earlier revision.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1. Dedicated events table (errors + warnings).
    if not insp.has_table("run_events"):
        op.create_table(
            "run_events",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("run_id", sa.String),
            sa.Column("review_run_id", sa.Integer),
            sa.Column("severity", sa.String, nullable=False),
            sa.Column("stage", sa.String),
            sa.Column("context", sa.String),
            sa.Column("message", sa.String),
            sa.Column("traceback", sa.String),
            sa.Column("occurred_at", sa.String),
        )
        op.create_index("idx_run_events_run_id", "run_events", ["run_id"])
        op.create_index("idx_run_events_review_run_id", "run_events", ["review_run_id"])
        op.create_index("idx_run_events_severity", "run_events", ["severity"])

    review_cols = {c["name"] for c in insp.get_columns("review_runs")}

    # 2. warning_count alongside the existing error_count.
    if "warning_count" not in review_cols:
        op.add_column("review_runs", sa.Column("warning_count", sa.Integer))

    # 3. Backfill existing errors into run_events, then drop the JSONB column.
    if "errors" in review_cols:
        op.execute(
            sa.text(
                """
                INSERT INTO run_events
                    (received_at, run_id, review_run_id, severity,
                     stage, context, message, traceback, occurred_at)
                SELECT r.received_at, r.run_id, r.id, 'error',
                       e->>'stage', e->>'context', e->>'message',
                       e->>'traceback', e->>'at'
                FROM review_runs r
                CROSS JOIN LATERAL jsonb_array_elements(r.errors) AS e
                WHERE r.errors IS NOT NULL
                  AND jsonb_typeof(r.errors) = 'array';
                """
            )
        )
        op.drop_column("review_runs", "errors")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    review_cols = {c["name"] for c in insp.get_columns("review_runs")}

    # Restore the errors JSONB column and backfill it from run_events (errors only).
    if "errors" not in review_cols:
        op.add_column("review_runs", sa.Column("errors", JSONB))
    if insp.has_table("run_events"):
        op.execute(
            sa.text(
                """
                UPDATE review_runs r
                SET errors = sub.arr
                FROM (
                    SELECT review_run_id,
                           jsonb_agg(
                               jsonb_strip_nulls(jsonb_build_object(
                                   'stage', stage,
                                   'context', context,
                                   'message', message,
                                   'traceback', traceback,
                                   'at', occurred_at
                               )) ORDER BY id
                           ) AS arr
                    FROM run_events
                    WHERE severity = 'error' AND review_run_id IS NOT NULL
                    GROUP BY review_run_id
                ) sub
                WHERE r.id = sub.review_run_id;
                """
            )
        )
        op.drop_table("run_events")

    if "warning_count" in review_cols:
        op.drop_column("review_runs", "warning_count")
