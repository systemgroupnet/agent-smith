"""add project_path to comment_scores

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-21

Adds the human-readable ``project_path`` column to ``comment_scores`` (the
GitLab ``project.path_with_namespace`` from the emoji webhook), alongside the
existing numeric ``project_id``, so scores are attributable to a project by
name — not just its opaque id. Idempotent-safe: skipped if the column already
exists.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("comment_scores"):
        return
    existing = {c["name"] for c in insp.get_columns("comment_scores")}
    if "project_path" not in existing:
        op.add_column("comment_scores", sa.Column("project_path", sa.String))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("comment_scores"):
        existing = {c["name"] for c in insp.get_columns("comment_scores")}
        if "project_path" in existing:
            op.drop_column("comment_scores", "project_path")
