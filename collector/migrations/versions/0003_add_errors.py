"""add error_count + errors columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08

Adds per-run error capture: error_count (int, for monitoring/alerting) and
errors (JSONB, the structured error array the reviewer now sends). Both guarded
so the migration is a no-op if the columns already exist.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("review_runs")}
    if "error_count" not in existing:
        op.add_column("review_runs", sa.Column("error_count", sa.Integer))
    if "errors" not in existing:
        op.add_column("review_runs", sa.Column("errors", JSONB))


def downgrade() -> None:
    op.drop_column("review_runs", "errors")
    op.drop_column("review_runs", "error_count")
