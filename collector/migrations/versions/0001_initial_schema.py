"""initial review_runs schema

Revision ID: 0001
Revises:
Create Date: 2026-07-08

Creates the base review_runs table. Guarded to no-op if the table already
exists, so a database created by the pre-Alembic collector (via create_all) is
adopted cleanly — Alembic simply records this revision and the follow-up
migration (0002) adds any newer columns without dropping existing data.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TEXT_FIELDS = (
    "schema_version",
    "run_id",
    "timestamp",
    "project_path",
    "mr_id",
    "mr_url",
    "mr_title",
    "source_branch",
    "target_branch",
    "head_sha",
    "last_reviewed_sha",
    "review_type",
    "model",
    "status",
)
_INT_FIELDS = (
    "files_changed_total",
    "files_reviewed",
    "files_failed",
    "files_skipped",
    "issues_total",
    "issues_high",
    "issues_medium",
    "issues_low",
    "comments_selected",
    "comments_posted",
    "comments_failed",
)


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("review_runs"):
        # Pre-Alembic table already present; adopt it (0002 adds new columns).
        return

    op.create_table(
        "review_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("received_at", sa.String, nullable=False),
        sa.Column("duration_seconds", sa.Float),
        *[sa.Column(name, sa.String) for name in _TEXT_FIELDS],
        *[sa.Column(name, sa.Integer) for name in _INT_FIELDS],
        sa.Column("raw_json", JSONB, nullable=False),
    )
    op.create_index(
        "idx_review_runs_project_mr", "review_runs", ["project_path", "mr_id"]
    )
    op.create_index("idx_review_runs_timestamp", "review_runs", ["timestamp"])


def downgrade() -> None:
    op.drop_index("idx_review_runs_timestamp", table_name="review_runs")
    op.drop_index("idx_review_runs_project_mr", table_name="review_runs")
    op.drop_table("review_runs")
