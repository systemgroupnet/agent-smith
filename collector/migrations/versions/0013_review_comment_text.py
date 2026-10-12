"""add the finding's text and file type to review_comments

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-11

``comment_replies`` holds what humans answered back to the agent, but
``review_comments`` stored only *where* each comment was (file, line, severity,
model) — never what it said. A reply like "no, this is checked in the caller"
could be read but not what it rejected, which is exactly what is needed to mine
repeated rejections into rules for the reviewers.

Adds:
* ``review_comments.problem`` (text) — the finding's text as the reviewer
  formatted it into the comment (clipped reviewer-side to 2000 characters). The
  finding, not the rendered body: the body's suggestion block is the reviewed
  repository's source verbatim.
* ``review_comments.file_type`` (text) — the review profile's classification of
  the comment's file (``service``, ``sql``, ...), i.e. which ``.aid/<type>.md`` a
  lesson drawn from it belongs in.

Both nullable with **no backfill**: rows written before this migration never
carried the text, and the posted note on GitLab is the only copy. Read a NULL as
"pre-0013", not "the comment was empty". Idempotent-safe: each column is skipped
if it already exists.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


_ADDITIONS = (
    ("review_comments", "problem", lambda: sa.Column("problem", sa.Text)),
    ("review_comments", "file_type", lambda: sa.Column("file_type", sa.String)),
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
