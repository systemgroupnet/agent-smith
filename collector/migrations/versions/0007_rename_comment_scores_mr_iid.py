"""rename comment_scores.mr_iid to mr_id

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-21

Renames ``comment_scores.mr_iid`` → ``mr_id`` so the MR identifier column is
named consistently with ``review_comments.mr_id``. Idempotent-safe: skipped if
the column has already been renamed (or the table is absent).
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("comment_scores"):
        return
    cols = {c["name"] for c in insp.get_columns("comment_scores")}
    if "mr_iid" in cols and "mr_id" not in cols:
        op.alter_column("comment_scores", "mr_iid", new_column_name="mr_id")


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("comment_scores"):
        return
    cols = {c["name"] for c in insp.get_columns("comment_scores")}
    if "mr_id" in cols and "mr_iid" not in cols:
        op.alter_column("comment_scores", "mr_id", new_column_name="mr_iid")
