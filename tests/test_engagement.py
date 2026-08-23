"""Engagement layer: the ladder, the levels, the ISO weeks and the digest text.

Scope, deliberately: everything here is **pure**. No database, so CI needs no
PostgreSQL, and each test names one rule rather than one function. What that
cannot cover is whether the SQL is *correct* — the queries in
``collector/engagement.py`` are verified against a real PostgreSQL by driving the
webhook ingest paths end to end (see ``tests/README.md``); what these tests pin
down is the behaviour around them, and the design rules that would otherwise be
quietly violated by a future edit.

The most important test in the file is
``test_no_achievement_is_earned_by_the_score_a_rater_gave``: it is the one design
rule the whole feature rests on, and it is exactly the kind of thing somebody
adds a well-meaning badge for six months from now.
"""

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import digest
import engagement


# --- the ladder ------------------------------------------------------------


def test_crossing_a_threshold_unlocks_exactly_that_badge():
    unlocked = engagement.newly_unlocked({"ratings": 10}, already_earned=set())
    keys = {a.key for a in unlocked}
    assert keys == {"first_rating", "ratings_10"}, (
        "10 ratings crosses both the 1 and 10 thresholds and nothing above them"
    )


def test_a_badge_already_held_is_never_awarded_twice():
    counters = {"ratings": 50}
    held = {"first_rating", "ratings_10"}
    keys = {a.key for a in engagement.newly_unlocked(counters, held)}
    assert keys == {"ratings_50"}


def test_an_unknown_metric_leaves_its_badges_locked_rather_than_raising():
    """A catalogue entry whose query was never implemented must not take down the
    webhook that evaluated it — the rating is already committed by then."""
    counters = {}  # no metric produced any count at all
    assert engagement.newly_unlocked(counters, set()) == []


def test_every_achievement_metric_has_a_query_behind_it():
    metrics = {a.metric for a in engagement.ACHIEVEMENTS}
    missing = metrics - set(engagement._METRIC_EVENT_SQL)
    assert not missing, f"catalogue references metrics with no SQL: {missing}"


def test_achievement_keys_are_unique():
    keys = [a.key for a in engagement.ACHIEVEMENTS]
    assert len(keys) == len(set(keys))
    assert len(engagement.ACHIEVEMENTS_BY_KEY) == len(keys)


def test_no_achievement_is_earned_by_the_score_a_rater_gave():
    """The one rule the feature rests on.

    Rewarding a rater for the *value* they picked would inflate the exact signal
    the ratings exist to measure — the agent's quality. Every metric must be a
    function of the act (how many, how fast, how consistently, on whose MR), and
    no query behind one may read ``score``. A future "gave ten 5-stars" badge
    fails here, which is the point.
    """
    for metric, sql in engagement._METRIC_EVENT_SQL.items():
        # The shared CTE selects `score` for the agent's own grade, so strip it
        # and assert on the metric's own query. Word-bounded, or the CTE named
        # `scores` and the table `comment_scores` match a bare substring search.
        own = sql.replace(engagement._CANONICAL_CTE, "")
        assert not re.search(r"\bscore\b", own, re.I), (
            f"metric {metric!r} reads the rating value; engagement must score the "
            "act of rating, never the number given"
        )


def test_the_points_ledger_does_not_read_the_rating_value_either():
    # `score` appears in the shared CTE's column list (it is there for the
    # agent's own grade), so strip the CTE and assert on the arithmetic: no
    # weight in the ledger may branch on the value a rater picked.
    weights = engagement.POINTS_LEDGER_SQL.replace(engagement._CANONICAL_CTE, "")
    assert not re.search(r"\bscore\b", weights, re.I)


# --- levels ----------------------------------------------------------------


@pytest.mark.parametrize(
    "points,expected",
    [
        (0, "Bronze"),
        (49, "Bronze"),
        (50, "Silver"),
        (149, "Silver"),
        (150, "Gold"),
        (399, "Gold"),
        (400, "Diamond"),
        (10_000, "Diamond"),
    ],
)
def test_level_boundaries_are_inclusive_at_the_floor(points, expected):
    assert engagement.level_for(points)[1] == expected


def test_a_negative_total_still_has_a_level():
    """Not reachable today, but a level of ``None`` would render as a blank cell
    in the leaderboard rather than as an obvious bug."""
    assert engagement.level_for(-5)[1] == "Bronze"


# --- ISO weeks -------------------------------------------------------------


def test_iso_week_key_is_zero_padded_and_carries_the_year():
    assert engagement.iso_week_key(date(2026, 8, 23)) == "2026-W34"
    assert engagement.iso_week_key(date(2026, 1, 5)) == "2026-W02"


def test_the_iso_year_is_not_the_calendar_year_at_the_boundary():
    """1 Jan 2027 belongs to ISO week 53 of **2026**.

    This is why the bucket key is ``IYYY-IW`` and not the calendar year paired
    with a week number: that pairing would file this day as "2027, week 53",
    inventing a week that does not exist and splitting the real one in half.
    (2026 has 53 ISO weeks at all because it opens on a Thursday.)
    """
    assert engagement.iso_week_key(date(2027, 1, 1)) == "2026-W53"
    assert engagement.iso_week_key(date(2027, 1, 3)) == "2026-W53"
    assert engagement.iso_week_key(date(2027, 1, 4)) == "2027-W01"


def test_week_start_is_always_the_monday():
    for key in ("2026-W01", "2026-W34", "2027-W01", "2026-W53"):
        try:
            start = date.fromisoformat(engagement.week_start_date(key))
        except ValueError:
            continue  # 2026 has no W53; not every key is a real week
        assert start.weekday() == 0, f"{key} started on {start:%A}"


def test_week_key_and_week_start_round_trip():
    for day in (date(2026, 1, 1), date(2026, 6, 15), date(2026, 12, 28)):
        key = engagement.iso_week_key(day)
        start = date.fromisoformat(engagement.week_start_date(key))
        assert engagement.iso_week_key(start) == key


def test_weekly_award_categories_are_unique_and_catalogued():
    keys = [w.key for w in engagement.WEEKLY_AWARDS]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(engagement._WEEKLY_AWARD_SQL)


def test_every_weekly_award_query_is_scoped_to_one_week_and_project():
    """A category that forgot its ``:week`` filter would silently crown an
    all-time winner every week, and nobody would notice for months."""
    for key, sql in engagement._WEEKLY_AWARD_SQL.items():
        assert ":week" in sql, f"{key} does not filter by week"
        assert ":project" in sql, f"{key} does not filter by project"
        assert "LIMIT 1" in sql, f"{key} may return more than one winner"


# --- the digest ------------------------------------------------------------


def test_the_default_week_is_the_one_that_just_closed():
    """A digest about a live week is a snapshot people argue with; about a closed
    week it is a result. Run on any day, it reports the previous ISO week."""
    for day in (
        date(2026, 8, 24),  # Monday
        date(2026, 8, 26),  # Wednesday
        date(2026, 8, 30),  # Sunday
    ):
        assert digest.last_completed_iso_week(day) == "2026-W34"


def test_last_completed_week_crosses_the_year_boundary_correctly():
    assert digest.last_completed_iso_week(date(2027, 1, 4)) == "2026-W53"


def test_week_span_reads_as_a_date_range():
    assert digest.week_span("2026-W34") == "17–23 Aug 2026"


def test_week_span_names_both_months_when_the_week_straddles_them():
    span = digest.week_span("2026-W40")
    assert "Sep" in span and "Oct" in span, span


@pytest.mark.parametrize(
    "category,value,expected",
    [
        ("top_rater", 1, "1 rating"),
        ("top_rater", 12, "12 ratings"),
        ("best_feedback", 1, "1 reply"),
        ("best_feedback", 4, "4 replies"),
        ("completionist", 87.4, "87% covered"),
        ("most_points", 31, "31 pts"),
        ("fastest", 0.004, "under a minute"),
        ("fastest", 0.5, "30 min median"),
        ("fastest", 6.25, "6.2 h median"),
    ],
)
def test_award_values_are_rendered_per_category(category, value, expected):
    assert digest._fmt_value(category, value) == expected


def test_a_missing_award_value_renders_as_a_dash_not_a_crash():
    assert digest._fmt_value("top_rater", None) == "—"


def test_the_winner_line_falls_back_to_the_handle_when_there_is_no_display_name():
    assert digest._who("alice", "Alice A") == "Alice A (@alice)"
    assert digest._who("alice", None) == "@alice"
    assert digest._who("alice", "alice") == "@alice"


def test_the_digest_never_prints_the_mattermost_token(monkeypatch, capsys):
    """The token is a write credential for the channel. A failure has to be
    reportable without leaking it — the URL is safe to print, the header is not.
    """
    monkeypatch.setenv("MATTERMOST_URL", "https://example.invalid")
    monkeypatch.setenv("MATTERMOST_TOKEN", "s3cret-token-value")
    with pytest.raises(Exception) as excinfo:
        digest.post_to_mattermost("hello", "chan-id")
    assert "s3cret-token-value" not in str(excinfo.value)


def test_posting_requires_both_a_url_and_a_token(monkeypatch):
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    monkeypatch.setenv("MATTERMOST_TOKEN", "t")
    with pytest.raises(RuntimeError, match="MATTERMOST_URL"):
        digest.post_to_mattermost("hello", "chan-id")


def test_iso_week_key_accepts_a_datetime_as_well_as_a_date():
    """Callers pass both: the awards path hands it a ``date``, while anything
    reading a stored timestamp hands it a ``datetime``."""
    stamp = datetime(2026, 8, 23, 14, 30, tzinfo=timezone.utc)
    assert engagement.iso_week_key(stamp) == "2026-W34"


# --- the in-service scheduler ----------------------------------------------
#
# The digest is scheduled by the collector itself rather than by a host crontab,
# so the "is it due?" decision is code and needs pinning down. next_due_week is
# pure, which is the point of its signature.

import scheduler  # noqa: E402


TEHRAN = ZoneInfo("Asia/Tehran")
UTC = ZoneInfo("UTC")


def _at(y, m, d, hh, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def test_nothing_is_due_before_the_firing_moment():
    # Monday 2026-08-24, 08:59 — one minute early.
    assert scheduler.next_due_week(_at(2026, 8, 24, 8, 59), day=0, hour=9) is None


def test_the_closed_week_is_due_from_the_firing_moment():
    assert scheduler.next_due_week(_at(2026, 8, 24, 9, 0), day=0, hour=9) == "2026-W34"


def test_the_same_week_stays_due_all_week_so_a_late_start_still_posts():
    """The claim table stops a second post, not the clock. A service that comes
    back on Thursday must still find Monday's digest outstanding."""
    for day in (24, 25, 27, 30):  # Mon → Sun of the same ISO week
        assert scheduler.next_due_week(
            _at(2026, 8, day, 12), day=0, hour=9
        ) == "2026-W34"


def test_a_later_firing_day_delays_the_announcement_not_the_week_reported():
    """DIGEST_DAY=wed still reports Monday-to-Sunday of the previous week."""
    assert scheduler.next_due_week(_at(2026, 8, 24, 12), day=2, hour=9) is None
    assert scheduler.next_due_week(_at(2026, 8, 26, 9), day=2, hour=9) == "2026-W34"


def test_the_week_boundary_is_read_in_the_schedule_timezone():
    """The bug this guards: deriving the week from UTC while comparing against a
    local firing moment shifts the reported week by one either side of
    Sunday/Monday midnight. 03:00 Tehran on Monday is still *Sunday* in UTC, so a
    UTC-derived week boundary would report the wrong week.
    """
    monday_early_tehran = _at(2026, 8, 24, 3, 0, tz=TEHRAN)
    assert monday_early_tehran.astimezone(UTC).date().isoformat() == "2026-08-23"
    # Fires at 02:00 local, so it is due; and the week must be the one that closed
    # on Sunday *local* time.
    assert scheduler.next_due_week(monday_early_tehran, day=0, hour=2) == "2026-W34"


def test_due_week_crosses_the_year_boundary():
    # Monday 4 Jan 2027 is the start of 2027-W01, so the closed week is 2026-W53.
    assert scheduler.next_due_week(_at(2027, 1, 4, 9), day=0, hour=9) == "2026-W53"


@pytest.mark.parametrize(
    "raw,expected",
    [("mon", 0), ("Mon", 0), ("monday", 0), ("sun", 6), ("3", 3), ("6", 6),
     ("", 0), (None, 0), ("nonsense", 0), ("9", 2)],
)
def test_day_parsing_is_lenient(raw, expected):
    """A typo in a cadence setting should delay the digest at worst, never stop
    the service from starting."""
    assert scheduler.parse_day(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("9", 9), ("0", 0), ("23", 23), ("09:30", 9), ("", 9), (None, 9),
     ("24", 9), ("-1", 9), ("half past nine", 9)],
)
def test_hour_parsing_is_lenient(raw, expected):
    assert scheduler.parse_hour(raw) == expected


def test_an_unknown_timezone_falls_back_to_utc_rather_than_raising():
    assert scheduler.resolve_timezone("Mars/Olympus_Mons").key == "UTC"
    assert scheduler.resolve_timezone("").key == "UTC"
    assert scheduler.resolve_timezone("Asia/Tehran").key == "Asia/Tehran"


def test_the_credentials_are_the_opt_in(monkeypatch):
    for key in ("MATTERMOST_URL", "MATTERMOST_TOKEN", "MATTERMOST_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DIGEST_ENABLED", raising=False)
    assert scheduler.enabled() is False

    monkeypatch.setenv("MATTERMOST_URL", "https://example.invalid")
    monkeypatch.setenv("MATTERMOST_TOKEN", "t")
    monkeypatch.setenv("MATTERMOST_CHANNEL_ID", "c")
    assert scheduler.enabled() is True


def test_an_empty_digest_enabled_means_enabled(monkeypatch):
    """Deliberately the opposite of the reviewer's four flags, which read "" as
    off — here the credentials already cover the unconfigured case, so an empty
    value must not silently disable a configured digest."""
    monkeypatch.setenv("MATTERMOST_URL", "https://example.invalid")
    monkeypatch.setenv("MATTERMOST_TOKEN", "t")
    monkeypatch.setenv("MATTERMOST_CHANNEL_ID", "c")
    monkeypatch.setenv("DIGEST_ENABLED", "")
    assert scheduler.enabled() is True


@pytest.mark.parametrize("flag", ["false", "0", "no", "off", "OFF", " false "])
def test_digest_enabled_switches_it_off_explicitly(monkeypatch, flag):
    monkeypatch.setenv("MATTERMOST_URL", "https://example.invalid")
    monkeypatch.setenv("MATTERMOST_TOKEN", "t")
    monkeypatch.setenv("MATTERMOST_CHANNEL_ID", "c")
    monkeypatch.setenv("DIGEST_ENABLED", flag)
    assert scheduler.enabled() is False


def test_the_scheduler_does_not_start_when_unconfigured(monkeypatch):
    """Importing main() in a deployment with no Mattermost settings must not spawn
    a thread that then fails every tick."""
    for key in ("MATTERMOST_URL", "MATTERMOST_TOKEN", "MATTERMOST_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    assert scheduler.start() is False


def test_a_tick_with_nothing_due_touches_no_database(monkeypatch):
    """next_due_week returning None must short-circuit *before* the claim, or an
    unconfigured local run would hit PostgreSQL every 15 minutes."""
    monkeypatch.setenv("DIGEST_TIMEZONE", "UTC")
    monkeypatch.setenv("DIGEST_DAY", "mon")
    monkeypatch.setenv("DIGEST_HOUR", "9")

    def explode(*a, **k):  # pragma: no cover — must never be called
        raise AssertionError("claim_digest_week was reached with nothing due")

    monkeypatch.setattr(scheduler.storage, "claim_digest_week", explode)
    monkeypatch.setattr(
        scheduler, "next_due_week", lambda *a, **k: None
    )
    assert scheduler.tick() is None


@pytest.mark.parametrize(
    "hour_raw,minute_raw,expected",
    [
        ("11:45", None, 45),      # the HH:MM form people actually write
        ("11", "45", 45),         # explicit minute
        ("11:45", "0", 0),        # explicit minute wins over the HH:MM form
        ("9", None, 0),
        ("", None, 0),
        (None, None, 0),
        ("11:60", None, 0),       # out of range
        ("11:aa", None, 0),       # unparseable
        ("11:05", None, 5),
    ],
)
def test_minute_parsing(hour_raw, minute_raw, expected):
    """``DIGEST_HOUR=11:45`` must mean a quarter to twelve. Reading it as 11:00 is
    the kind of wrong nobody notices for a month."""
    assert scheduler.parse_minute(hour_raw, minute_raw) == expected


def test_the_firing_minute_is_honoured():
    # Monday 11:44 — one minute early, nothing due.
    assert scheduler.next_due_week(
        _at(2026, 8, 24, 11, 44), day=0, hour=11, minute=45
    ) is None
    assert scheduler.next_due_week(
        _at(2026, 8, 24, 11, 45), day=0, hour=11, minute=45
    ) == "2026-W34"


def test_a_sunday_schedule_reports_a_week_that_is_already_seven_days_old():
    """Documented consequence, pinned so nobody "fixes" it by accident.

    ISO weeks end on Sunday midnight, so at 11:45 on a Sunday the week you are
    standing in has not closed — the last *completed* week is the one before it.
    Sunday 30 Aug therefore reports 17–23 Aug, not 24–30 Aug. Monday is the
    setting that announces a week the morning after it closes.
    """
    sunday = _at(2026, 8, 30, 11, 45)          # last day of 2026-W35
    assert engagement.iso_week_key(sunday) == "2026-W35"
    assert scheduler.next_due_week(sunday, day=6, hour=11, minute=45) == "2026-W34"

    monday = _at(2026, 8, 31, 11, 45)          # first day of 2026-W36
    assert scheduler.next_due_week(monday, day=0, hour=11, minute=45) == "2026-W35"


def test_the_digest_points_table_shows_a_cumulative_level():
    """A level is a standing, not a weekly score.

    Regression: the digest derived the level from the week's points, so every
    person landed on 🥉 Bronze in every digest forever — a rewards ladder that
    reads as "nobody is getting anywhere". The Level column must come from
    lifetime points, alongside the week's own total.
    """
    import inspect

    source = inspect.getsource(digest.build_message)
    assert "lifetime" in source, "the points table must consult lifetime totals"
    assert "This week" in source and "Total" in source, (
        "the table should show the week's points and the lifetime total separately"
    )
    # And the level itself must be derived from the lifetime figure, not the row.
    assert "engagement.level_for(total)" in source
