"""comment_replies table (human replies to the agent's review comments)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-25

Adds ``comment_replies`` — one row per human reply posted **in the thread of a
review comment the agent left**. Fed by GitLab's comment ("Note Hook") webhook,
which the collector now accepts alongside the emoji hook on
``POST /api/webhooks/gitlab``.

A reply is recognised by its ``discussion_id`` matching a tracked
``review_comments.discussion_id`` — GitLab gives every note in a thread the same
discussion id, so this ties the reply to the exact agent comment it answers
(``parent_note_id``) without needing any GitLab API call. Unique on ``note_id``
so webhook redeliveries and edits upsert instead of duplicating.

Guarded so the migration is safe to re-run.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("comment_replies"):
        op.create_table(
            "comment_replies",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("note_id", sa.BigInteger, nullable=False),
            sa.Column("discussion_id", sa.String),
            sa.Column("parent_note_id", sa.BigInteger),
            sa.Column("run_id", sa.String),
            sa.Column("project_id", sa.Integer),
            sa.Column("project_path", sa.String),
            sa.Column("mr_id", sa.Integer),
            sa.Column("author_id", sa.Integer),
            sa.Column("author_username", sa.String),
            sa.Column("author_name", sa.String),
            sa.Column("body", sa.String),
            sa.Column("note_type", sa.String),
            sa.Column("url", sa.String),
            sa.Column("noted_at", sa.String),
            sa.Column("updated_at", sa.String),
            sa.Column("action", sa.String),
        )
        op.create_index(
            "idx_comment_replies_note_id", "comment_replies", ["note_id"], unique=True
        )
        op.create_index(
            "idx_comment_replies_discussion_id", "comment_replies", ["discussion_id"]
        )
        op.create_index(
            "idx_comment_replies_parent_note_id", "comment_replies", ["parent_note_id"]
        )
        op.create_index("idx_comment_replies_run_id", "comment_replies", ["run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if insp.has_table("comment_replies"):
        op.drop_table("comment_replies")
