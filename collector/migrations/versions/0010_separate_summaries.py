"""separate review summaries and their ratings from findings

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-02

The reviewer now posts a one-off **review summary** comment on each MR (the
orientation note a human reads before the findings), and that comment can be
rated with the same :one:..:five: scale as a finding.

A summary and a finding are different artifacts, and their ratings answer
different questions — "was this description useful" vs "was this defect real".
Storing both in ``review_comments``/``comment_scores`` (discriminated by a
``comment_type``) would have silently changed the meaning of every existing
count and average the moment the first summary was rated, and kept working only
as long as every future query remembered a filter. So they are separated at
**write time** instead:

* ``review_summaries`` — one row per posted summary comment.
* ``summary_scores``   — human ratings of summaries, same shape as comment_scores.
* ``comment_replies.parent_kind`` — "finding" | "summary", so thread replies stay
  attributable without pooling the two.

Consequence, and the point of the exercise: every pre-existing query over
``review_comments`` and ``comment_scores`` remains correct with **no edits** —
those tables continue to mean findings only.

No backfill: nothing in the store predates the summary feature, so there is
nothing to move. ``comment_replies.parent_kind`` is NULL for rows written before
this migration, which means "finding" in practice (summaries did not exist yet)
but is left NULL rather than asserted.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("review_summaries"):
        op.create_table(
            "review_summaries",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("run_id", sa.String),
            sa.Column("review_run_id", sa.Integer),
            sa.Column("project_path", sa.String),
            sa.Column("mr_id", sa.String),
            sa.Column("note_id", sa.BigInteger, nullable=False),
            sa.Column("discussion_id", sa.String),
            sa.Column("model", sa.String),
            sa.Column("mr_author_username", sa.String),
        )
        op.create_index("idx_review_summaries_run_id", "review_summaries", ["run_id"])
        op.create_index(
            "idx_review_summaries_note_id", "review_summaries", ["note_id"], unique=True
        )
        op.create_index(
            "idx_review_summaries_project_mr", "review_summaries", ["project_path", "mr_id"]
        )

    if not insp.has_table("summary_scores"):
        op.create_table(
            "summary_scores",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("received_at", sa.String, nullable=False),
            sa.Column("note_id", sa.BigInteger, nullable=False),
            sa.Column("project_id", sa.Integer),
            sa.Column("project_path", sa.String),
            sa.Column("mr_id", sa.Integer),
            sa.Column("user_id", sa.Integer),
            sa.Column("username", sa.String),
            sa.Column("user_name", sa.String),
            sa.Column("emoji", sa.String),
            sa.Column("score", sa.Integer),
            sa.Column("awarded_at", sa.String),
        )
        op.create_index("idx_summary_scores_note_id", "summary_scores", ["note_id"])
        op.create_index(
            "idx_summary_scores_unique",
            "summary_scores",
            ["note_id", "user_id", "emoji"],
            unique=True,
        )
        op.create_index("idx_summary_scores_project", "summary_scores", ["project_path"])

    if insp.has_table("comment_replies"):
        cols = {c["name"] for c in insp.get_columns("comment_replies")}
        if "parent_kind" not in cols:
            op.add_column("comment_replies", sa.Column("parent_kind", sa.String))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("comment_replies"):
        cols = {c["name"] for c in insp.get_columns("comment_replies")}
        if "parent_kind" in cols:
            op.drop_column("comment_replies", "parent_kind")
    for table in ("summary_scores", "review_summaries"):
        if insp.has_table(table):
            op.drop_table(table)
