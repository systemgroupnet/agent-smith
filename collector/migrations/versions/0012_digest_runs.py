"""digest_runs — one row per ISO week the weekly digest has dealt with

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23

The weekly digest is scheduled **inside the collector** (a daemon thread, see
``scheduler.py``) rather than by a crontab on the host. That is the right place
for it — the service already holds the data and the credentials, and a schedule
that ships with the code cannot be forgotten during a host migration — but it
moves two problems into the process that cron solved for free: *has this week
already been posted?* and *is another worker posting it right now?*

This table answers both, and the unique index on ``iso_week`` is the whole
mechanism. A worker claims a week with ``INSERT … ON CONFLICT DO NOTHING``; at
most one insert can succeed, so exactly one claimant posts however many workers,
threads or replicas are running. The service runs ``-w 1`` today, which makes the
race theoretical — but the cost of being right about it is one index, and the
failure mode it prevents is the team getting the same announcement twice.

**A claim is never released, even on failure**, which is a deliberate choice of
*at most once* over *at least once*. A post that fails after the request reached
Mattermost is indistinguishable from one that failed before it, so releasing the
claim risks announcing the same week twice, while keeping it risks missing a week.
A duplicate is embarrassing and permanent in a channel; a missed week is one
command away (``python digest.py --week 2026-W34``). The row keeps ``status`` and
``error`` so a missed week is visible rather than silent.

``status`` values:
  ``posted``  — announced; ``post_id`` is the Mattermost post
  ``skipped`` — the week had no activity worth announcing (a weekly "nothing to
                report" message is how a digest gets muted, so it stays quiet)
  ``failed``  — claimed and could not be posted; ``error`` says why
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("digest_runs"):
        op.create_table(
            "digest_runs",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            # The ISO week the digest reports on ("2026-W34"), not the week it
            # was sent in. Unique — this is the claim.
            sa.Column("iso_week", sa.String, nullable=False),
            sa.Column("claimed_at", sa.String, nullable=False),
            sa.Column("posted_at", sa.String),
            sa.Column("status", sa.String, nullable=False),
            sa.Column("channel_id", sa.String),
            sa.Column("post_id", sa.String),
            sa.Column("error", sa.String),
        )
        op.create_index(
            "uq_digest_runs_iso_week", "digest_runs", ["iso_week"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("digest_runs"):
        op.drop_table("digest_runs")
