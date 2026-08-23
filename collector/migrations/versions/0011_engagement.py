"""achievements table, backfilled from the whole rating history

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-23

Adds the one piece of the engagement layer that is *stored*. Points, coverage,
streaks and the weekly awards are all computed on demand from queries in
``collector/engagement.py`` — right for a live balance, because a revoked
reaction has to take its points back with it, and a materialized counter would
get that wrong exactly when somebody noticed.

A milestone is the opposite. "Your first 100 ratings" must not vanish when a
dashboard window rolls past it, and un-clicking a reaction a month later does not
un-earn the threshold it crossed at the time. So an unlock is written once, with
the counter value that earned it, and never revoked.

Scoped per ``(username, achievement_key, project_path)``: every dashboard panel
in this repo filters on ``$project``, so a row with no project could not honour
the picker. The cost is that someone active in three repos earns the entry-level
badges three times — accepted, because a per-project ladder is also the more
meaningful one for a person who only works in one of them.

**The migration backfills the entire history**, which is the point of doing it
here rather than leaving it to the first webhook. Unlocks are otherwise only
awarded on the ingest path, so a deploy would leave everyone who rated before it
sitting at zero badges — and the people with the most ratings to their name would
be exactly the ones it looked broken for. The backfill is not a one-off trick
either: ``engagement.evaluate`` dates every row to the event that *crossed* the
threshold, so running it over a year of data reproduces the real timeline instead
of stamping a thousand badges with today's date and drowning the "recently
unlocked" panel. It is idempotent, and re-running it (``POST
/api/achievements/recompute``) yields byte-identical timestamps.

The backfill is deliberately **non-fatal**: a failure warns and leaves the table
empty rather than aborting the migration, because the schema change is what the
service needs to start and the data credit is recoverable with one API call. A
migration that refuses to complete over a badge would take the collector down
with it.
"""
import sys

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("achievements"):
        op.create_table(
            "achievements",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            # When the threshold was *crossed*, derived from the rating or reply
            # that crossed it — not when the row was written. See the module
            # docstring of engagement.py for why that distinction earns its keep.
            sa.Column("awarded_at", sa.String, nullable=False),
            sa.Column("username", sa.String, nullable=False),
            sa.Column("user_name", sa.String),
            sa.Column("project_path", sa.String, nullable=False),
            sa.Column("achievement_key", sa.String, nullable=False),
            # The counter's value when the unlock was recorded. Kept because a
            # backfill can cross several thresholds from one event stream, and
            # "you were at 118 when Century landed" is the only way to tell that
            # apart from a live unlock at exactly 100.
            sa.Column("progress_value", sa.Integer),
        )
        op.create_index(
            "uq_achievements_user_key_project",
            "achievements",
            ["username", "achievement_key", "project_path"],
            unique=True,
        )
        op.create_index("idx_achievements_username", "achievements", ["username"])
        op.create_index("idx_achievements_project", "achievements", ["project_path"])

    if not insp.has_table("weekly_awards"):
        op.create_table(
            "weekly_awards",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            # ISO year + week ("2026-W34"). The year is part of the key because
            # plain week numbers collide across years, and the last days of
            # December belong to week 1 of the *following* ISO year.
            sa.Column("iso_week", sa.String, nullable=False),
            # That week's Monday, as an ISO date. Grafana's $__timeFilter cannot
            # filter on "2026-W34", and a panel that silently ignores the time
            # picker is the failure mode dashboards/README.md warns about.
            sa.Column("week_start", sa.String, nullable=False),
            sa.Column("project_path", sa.String, nullable=False),
            sa.Column("category", sa.String, nullable=False),
            sa.Column("username", sa.String),
            sa.Column("user_name", sa.String),
            sa.Column("value", sa.Float),
            sa.Column("computed_at", sa.String),
        )
        op.create_index(
            "uq_weekly_awards_week_project_category",
            "weekly_awards",
            ["iso_week", "project_path", "category"],
            unique=True,
        )
        op.create_index(
            "idx_weekly_awards_week_start", "weekly_awards", ["week_start"]
        )
        op.create_index("idx_weekly_awards_project", "weekly_awards", ["project_path"])

    # Credit the past. Imported here rather than at module scope so a failure to
    # import cannot stop the schema change itself, and so the catalogue that runs
    # is the one shipping with the code rather than a copy frozen into this file.
    try:
        import engagement  # noqa: PLC0415 — see above

        badges = engagement.backfill(bind)
        weeks = engagement.backfill_weekly_awards(bind)
        print(
            f"[0011] achievements backfilled: {badges['awarded_count']} unlock(s) "
            f"for {badges['participants']} participant(s); "
            f"weekly awards: {weeks['awards']} winner(s) across {weeks['weeks']} "
            "project-week(s)",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001 — never block a deploy over a badge
        print(
            f"[0011] WARNING: engagement backfill skipped ({type(e).__name__}: {e}). "
            "Run POST /api/achievements/recompute to credit existing raters and "
            "settle past weeks.",
            file=sys.stderr,
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in ("weekly_awards", "achievements"):
        if insp.has_table(table):
            op.drop_table(table)
