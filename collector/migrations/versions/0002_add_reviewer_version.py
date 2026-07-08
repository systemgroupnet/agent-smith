"""add reviewer_version column

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-08

Adds the reviewer_version column (the reviewer build/tag that produced the
event). Idempotent-safe: skipped if the column already exists (e.g. a table
created by the pre-Alembic create_all path that already had it).
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("review_runs")}
    if "reviewer_version" not in existing:
        op.add_column("review_runs", sa.Column("reviewer_version", sa.String))


def downgrade() -> None:
    op.drop_column("review_runs", "reviewer_version")
