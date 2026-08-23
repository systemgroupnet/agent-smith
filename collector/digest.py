#!/usr/bin/env python3
"""Weekly engagement digest → Mattermost.

A dashboard nobody opens changes nothing. This is what closes the loop: once a
week the collector posts the closed week's result to a channel — the winners, the
points table, the badges people crossed, and the two numbers that say whether the
loop is working (how much of what the reviewer left got rated, and what grade the
reviewer earned for it).

The week is an **ISO week**, and the default is the week that has just *closed*
rather than the one in progress. A digest about a live week is a leaderboard
snapshot people argue with; a digest about a finished week is a result. Run it on
a Monday and it reports the week before, which is also why the awards are stored
per ISO week rather than computed over a rolling window — the announcement and
the dashboard have to still agree a year from now.

Usage::

    python digest.py                      # last completed ISO week, all projects
    python digest.py --week 2026-W34      # a specific week (regenerates exactly)
    python digest.py --current            # the week in progress, as a preview
    python digest.py --project mars/x     # scope to one project (default: all)
    python digest.py --dry-run            # print the message, post nothing

Configuration (environment):

    MATTERMOST_URL          e.g. https://mattermost.example.com
    MATTERMOST_TOKEN        bot account or personal access token
    MATTERMOST_CHANNEL_ID   target channel id (Channel → View Info)
    COLLECTOR_DATABASE_URL  the collector's PostgreSQL (as the service uses)

Deliberately no new dependency: one HTTP POST goes out through ``urllib`` from
the standard library rather than pulling ``requests`` into a service that needs
it for nothing else.

Never fails loudly enough to matter — a missing token, an unreachable Mattermost
or an empty week all exit non-zero with one line on stderr, and nothing about
this touches the ingest path. It is also **idempotent by construction**: it
settles the week's awards before reading them, and posting twice would produce
two identical messages rather than two different ones.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import engagement
import storage

# Mattermost renders a table only with a blank line before it, and collapses
# anything wider than the channel; four columns is about the practical limit on a
# phone, which is where most people will read this.
_MAX_LEADERBOARD_ROWS = 8


def last_completed_iso_week(today: date) -> str:
    """The ISO week before the one containing ``today``.

    Monday-safe: subtracting 7 days from any day of this week lands in the
    previous ISO week, whatever the weekday, which is what makes a Monday-morning
    cron report Monday-to-Sunday of the week just gone.
    """
    return engagement.iso_week_key(today - timedelta(days=7))


def week_span(iso_week: str) -> str:
    """``2026-W34`` → ``18–24 Aug 2026``, for the message header."""
    start = date.fromisoformat(engagement.week_start_date(iso_week))
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{start.day}–{end.day} {end:%b %Y}"
    return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """``1 reply`` / ``4 replies``. Trivial, and the digest is read by people."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _fmt_value(award_key: str, value) -> str:
    """A winning metric rendered for humans, per category."""
    if value is None:
        return "—"
    if award_key == "fastest":
        # Sub-hour medians are the *good* case here and read badly as "0.0 h".
        if value < 1 / 60:
            return "under a minute"
        if value < 1:
            return f"{value * 60:.0f} min median"
        return f"{value:.1f} h median"
    if award_key == "completionist":
        return f"{value:.0f}% covered"
    if award_key == "best_feedback":
        return _plural(int(value), "reply", "replies")
    if award_key == "top_rater":
        return _plural(int(value), "rating")
    return f"{value:.0f} pts"


def _who(username, user_name) -> str:
    """Display name with the handle, or just the handle when that is all we have."""
    if user_name and user_name != username:
        return f"{user_name} (@{username})"
    return f"@{username}"


def build_message(
    iso_week: str, scope: str = engagement.ALL_PROJECTS
) -> tuple[str, dict]:
    """Render the digest for one ISO week. Returns ``(markdown, payload_summary)``.

    **One report for everybody, not one per project.** A developer rating a
    review comment is engaging with the reviewer whichever repository it happened
    in, and splitting the announcement by project turns one small team into
    several smaller ones — two separate leaderboards nobody compares, and the same
    person congratulated twice. ``scope`` is ``engagement.ALL_PROJECTS`` by
    default; pass a real project path to report on just that one (what
    ``--project`` does).

    Reads through ``storage``/``engagement`` so the digest and the API cannot
    disagree about who won or what a rating is worth.
    """
    lines: list[str] = [
        f"### 🏆 Code Review Engagement — week {iso_week} ({week_span(iso_week)})",
    ]
    summary: dict = {"iso_week": iso_week, "scope": scope}
    anything = False

    with storage.get_engine().connect() as conn:
        stats = engagement.weekly_stats(conn, iso_week, scope)
        awards = engagement.list_weekly_awards(
            conn, project_path=scope, iso_week=iso_week
        )
        # Both bounds, in SQL. The window is the ISO week's Monday up to (not
        # including) the next one — filtering the result in Python instead
        # drops anyone active during the week *and* afterwards, which emptied
        # this table for every past week.
        week_from = date.fromisoformat(engagement.week_start_date(iso_week))
        points = engagement.points_leaderboard(
            conn,
            limit=_MAX_LEADERBOARD_ROWS,
            project_path=None if scope == engagement.ALL_PROJECTS else scope,
            since=f"{week_from}T00:00:00+00:00",
            until=f"{week_from + timedelta(days=7)}T00:00:00+00:00",
        )
        # A level is a *cumulative* standing, so it has to come from lifetime
        # points, not from the week's. Deriving it from the weekly total put
        # every single person on 🥉 Bronze in every digest forever — the
        # column read as "nobody is getting anywhere", in the one place the
        # ladder is supposed to mean something.
        lifetime = {
            row["username"]: int(row["points"] or 0)
            for row in engagement.points_leaderboard(
                conn, limit=1000,
                project_path=None if scope == engagement.ALL_PROJECTS else scope,
            )
        }
        badges = engagement.achievements_in_week(conn, iso_week, scope)

    # Rendering needs no connection, so it happens outside the block — and the
    # "was there anything at all?" guard reads as a plain condition rather than
    # the loop-and-continue this used to be when the report was per project.
    if awards or points or badges or stats.get("findings_posted"):
        anything = True
        summary["stats"] = stats
        summary["awards"] = len(awards)
        summary["badges"] = len(badges)

        lines.append("")

        # The two numbers the team can act on, first.
        coverage = stats.get("coverage_pct")
        grade = stats.get("agent_grade")
        bits = []
        if stats.get("findings_posted"):
            covered = (
                f"{coverage:.0f}%" if coverage is not None else "—"
            )
            bits.append(
                f"📊 **{covered}** of {stats['findings_posted']} findings rated"
            )
        if grade is not None:
            bits.append(f"🤖 reviewer scored **{float(grade):.2f}/5**")
        if stats.get("replies"):
            bits.append(
                f"💬 {_plural(int(stats['replies']), 'written reply', 'written replies')}"
            )
        if bits:
            lines.append(" · ".join(bits))

        if awards:
            lines.append("")
            lines.append("| | Award | Winner | |")
            lines.append("|:-:|---|---|---|")
            # Catalogue order, not database order: the same categories in the
            # same places every week is what makes the message skimmable.
            by_cat = {a["category"]: a for a in awards}
            for meta in engagement.WEEKLY_AWARDS:
                row = by_cat.get(meta.key)
                if not row:
                    continue
                lines.append(
                    f"| {meta.emoji} | **{meta.label}** | "
                    f"{_who(row['username'], row.get('user_name'))} | "
                    f"{_fmt_value(meta.key, row.get('value'))} |"
                )

        if points:
            lines.append("")
            lines.append("| # | Who | This week | Total | Level |")
            lines.append("|:-:|---|:-:|:-:|---|")
            for i, row in enumerate(points, start=1):
                total = lifetime.get(row["username"], int(row["points"] or 0))
                emoji, level = engagement.level_for(total)
                lines.append(
                    f"| {i} | {_who(row['username'], row.get('user_name'))} | "
                    f"**{row['points']}** | {total} | {emoji} {level} |"
                )

        if badges:
            lines.append("")
            lines.append("**🎖️ Unlocked this week**")
            for b in badges:
                label = b.get("label") or b["achievement_key"]
                lines.append(
                    f"- {b.get('emoji') or '🏅'} **{label}** — "
                    f"{_who(b['username'], b.get('user_name'))}"
                )

    if not anything:
        lines.append("")
        lines.append("_Nothing to report — no findings posted or rated this week._")

    lines.append("")
    lines.append(
        "_Rate a review comment by clicking 1️⃣–5️⃣ on it. "
        "Replies are worth the most — they tell the reviewer *why*._"
    )
    return "\n".join(lines), summary


def post_to_mattermost(message: str, channel_id: str) -> dict:
    """POST one message to a channel via the Mattermost v4 API.

    Stdlib only. Raises on a non-2xx so the caller can report it and exit
    non-zero — a digest that silently failed to send is worse than one that says
    it could not.
    """
    base = (os.getenv("MATTERMOST_URL") or "").rstrip("/")
    token = os.getenv("MATTERMOST_TOKEN") or ""
    if not base or not token:
        raise RuntimeError("MATTERMOST_URL and MATTERMOST_TOKEN are required")

    body = json.dumps({"channel_id": channel_id, "message": message}).encode()
    req = urllib.request.Request(
        f"{base}/api/v4/posts",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:400]
        # Never echo the token — the URL is safe to print, the header is not.
        raise RuntimeError(f"Mattermost returned {e.code}: {detail}") from None


@dataclass
class DigestOutcome:
    """What one digest attempt did. ``status`` is the vocabulary the
    ``digest_runs`` table records, plus ``dry_run`` which is never stored."""

    iso_week: str
    status: str  # posted | empty | error | dry_run
    message: str = ""
    post_id: Optional[str] = None
    channel_id: Optional[str] = None
    error: Optional[str] = None


def run_digest(
    iso_week: str,
    project: Optional[str] = None,
    channel: Optional[str] = None,
    dry_run: bool = False,
    refresh: bool = True,
) -> DigestOutcome:
    """Build one week's digest and post it. Shared by the CLI and the scheduler.

    Returns rather than raises, so the scheduler can record an outcome without a
    try/except around a function that might have already posted. Deliberately does
    **not** touch ``digest_runs``: the claim belongs to the scheduler, and a manual
    ``--week`` re-post has to work regardless of what that table says — that is the
    recovery path for a week whose claim is stuck.
    """
    scope = project or engagement.ALL_PROJECTS

    # Settle the week before reporting it, so a missed scheduled run or a rating
    # that arrived late cannot leave the announcement disagreeing with the
    # dashboard. Idempotent — the awards upsert per (week, project, category).
    if refresh:
        try:
            storage.refresh_weekly_awards(iso_week=iso_week, project_path=project)
        except Exception as e:  # noqa: BLE001 — report the week we can compute
            print(f"digest: warning: could not settle {iso_week}: {e}",
                  file=sys.stderr)

    try:
        message, summary = build_message(iso_week, scope)
    except Exception as e:  # noqa: BLE001
        return DigestOutcome(iso_week, "error", error=f"could not build: {e}")

    if dry_run:
        return DigestOutcome(iso_week, "dry_run", message=message)

    # A week nobody touched gets no message. A weekly "nothing to report" post is
    # how a digest channel gets muted, and muting it costs more than the silence.
    if not summary.get("stats"):
        return DigestOutcome(iso_week, "empty", message=message,
                             error="no activity in this week")

    target = channel or os.getenv("MATTERMOST_CHANNEL_ID") or ""
    if not target:
        return DigestOutcome(
            iso_week, "error",
            error="MATTERMOST_CHANNEL_ID is required (or pass --channel)",
        )

    try:
        result = post_to_mattermost(message, target)
    except Exception as e:  # noqa: BLE001
        return DigestOutcome(iso_week, "error", message=message,
                             channel_id=target, error=str(e))

    return DigestOutcome(iso_week, "posted", message=message,
                         post_id=result.get("id"), channel_id=target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--week", help="ISO week to report, e.g. 2026-W34")
    parser.add_argument(
        "--current",
        action="store_true",
        help="report the week in progress instead of the last completed one",
    )
    parser.add_argument("--project", help="limit to one project path")
    parser.add_argument("--channel", help="override MATTERMOST_CHANNEL_ID")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the message, post nothing"
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip re-settling the week's awards before reading them",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="post even when the week has no activity (default: stay quiet)",
    )
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    if args.week:
        iso_week = args.week
    elif args.current:
        iso_week = engagement.iso_week_key(today)
    else:
        iso_week = last_completed_iso_week(today)

    outcome = run_digest(
        iso_week,
        project=args.project,
        channel=args.channel,
        dry_run=args.dry_run,
        refresh=not args.no_refresh,
    )

    if outcome.status == "dry_run":
        print(outcome.message)
        return 0

    if outcome.status == "empty":
        # An empty week is not a failure of the digest, but on the command line
        # the operator asked for it explicitly, so say what happened. --force
        # posts it anyway (useful for a first announcement).
        if args.force and outcome.message:
            target = args.channel or os.getenv("MATTERMOST_CHANNEL_ID") or ""
            if not target:
                print("digest: MATTERMOST_CHANNEL_ID is required", file=sys.stderr)
                return 1
            try:
                result = post_to_mattermost(outcome.message, target)
            except Exception as e:  # noqa: BLE001
                print(f"digest: {e}", file=sys.stderr)
                return 1
            storage.mark_digest_posted(iso_week, target, result.get("id"))
            print(f"digest: posted (forced) empty week {iso_week} to {target}")
            return 0
        print(f"digest: nothing to announce for {iso_week} ({outcome.error})",
              file=sys.stderr)
        return 0

    if outcome.status == "error":
        print(f"digest: {outcome.error}", file=sys.stderr)
        return 1

    # Record a hand-run post too, so the scheduler does not later announce the
    # same week and the digest history stays true.
    storage.mark_digest_posted(iso_week, outcome.channel_id or "", outcome.post_id)
    print(f"digest: posted week {iso_week} to channel {outcome.channel_id} "
          f"(post id {outcome.post_id or '?'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
