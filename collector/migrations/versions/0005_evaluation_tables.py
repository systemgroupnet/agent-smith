"""review-comment evaluation tables (review_comments + comment_scores)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-18

Adds the two tables that power the comment-evaluation loop:

* ``review_comments`` — one row per review comment the reviewer posted to a MR,
  from the run event's ``posted_comments`` array. Keyed to the run and, via the
  GitLab ``note_id`` (a unique index), to any reaction scores.
* ``comment_scores`` — one row per human 1–5 rating, gathered from GitLab's emoji
  webhook. Unique on (note_id, user_id, emoji) so webhook redeliveries upsert
  rather than duplicate.

Both steps are guarded so the migration is safe to re-run.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("review_comments"):
        op.create_table(
            "review_comments",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("run_id", sa.String),
            sa.Column("review_run_id", sa.Integer),
            sa.Column("project_path", sa.String),
            sa.Column("mr_id", sa.String),
            sa.Column("note_id", sa.BigInteger, nullable=False),
            sa.Column("discussion_id", sa.String),
            sa.Column("comment_type", sa.String),
            sa.Column("file", sa.String),
            sa.Column("line", sa.Integer),
            sa.Column("severity", sa.String),
            sa.Column("mr_author_username", sa.String),
        )
        op.create_index("idx_review_comments_run_id", "review_comments", ["run_id"])
        op.create_index(
            "idx_review_comments_note_id", "review_comments", ["note_id"], unique=True
        )
        op.create_index(
            "idx_review_comments_project_mr", "review_comments", ["project_path", "mr_id"]
        )

    if not insp.has_table("comment_scores"):
        op.create_table(
            "comment_scores",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("note_id", sa.BigInteger, nullable=False),
            sa.Column("project_id", sa.Integer),
            sa.Column("mr_iid", sa.Integer),
            sa.Column("user_id", sa.Integer),
            sa.Column("username", sa.String),
            sa.Column("user_name", sa.String),
            sa.Column("emoji", sa.String),
            sa.Column("score", sa.Integer),
            sa.Column("awarded_at", sa.String),
        )
        op.create_index("idx_comment_scores_note_id", "comment_scores", ["note_id"])
        op.create_index(
            "uq_comment_scores_note_user_emoji",
            "comment_scores",
            ["note_id", "user_id", "emoji"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if insp.has_table("comment_scores"):
        op.drop_table("comment_scores")
    if insp.has_table("review_comments"):
        op.drop_table("review_comments")
