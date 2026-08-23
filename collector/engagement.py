"""Engagement layer: points, achievements and the weekly awards.

The rating loop only pays off if people actually rate, and the friction is never
effort — clicking a reaction on a comment already in front of you costs nothing —
it is salience. So this module exists to make the aggregate visible and worth
something: a live points balance, a durable badge ladder, and the queries the
Grafana panels and the weekly Mattermost digest are built from.

**The rule every weight here obeys: score the act of rating, never the value
given.** Rewarding "highest average awarded" would inflate the exact signal the
ratings exist to measure, so nothing below reads ``score``. For the same reason
there is no metric that rewards an author for *receiving* few findings — that
would pay people to open smaller merge requests.

Points
------
    rate a finding           2     + 1  someone else's MR      (peer review)
    rate a summary           1     + 1  within 24h of posting  (timeliness)
    first substantive reply  5     substantive = >= 15 chars, first in its thread
    any other reply          1

A reply outweighs a rating because writing one costs something and a click does
not. Only the *first* reply in a thread earns full points, and only if it says
more than "👍", because a reply is otherwise the cheapest action here to repeat
and points have a reward behind them. There is deliberately **no per-MR cap** on
ratings: rating all 30 findings on a large merge request is precisely the
behaviour worth paying for, and the anti-farm metric is coverage, which is
bounded at 100% per person and cannot be inflated by volume.

Points are **derived, never stored**. A revoked reaction has to take its points
back with it, and a materialized counter would get that wrong exactly when
somebody noticed. Achievements are the opposite — stored, and never revoked:
"your first 100 ratings" must not vanish when a dashboard window rolls past it,
and un-clicking a reaction a month later does not un-earn the threshold it
crossed at the time.

Every metric is expressed the same way, and it is the trick the whole module
turns on: ``_METRIC_EVENT_SQL[metric]`` returns the **ordered list of timestamps
at which that metric first reached 1, 2, 3, …**. From one query you therefore get
both the counter (the number of rows) and, for any threshold N, the moment it was
crossed (row N). That is what lets a backfill date a badge to when it was
genuinely earned rather than to the day the migration ran — without it, running
this over a year of history would stamp every badge with today and make the
"recently unlocked" panel meaningless.

No database views: every query lives here, in the collector, so the SQL ships and
versions with the code that reads it. The cost is that the Grafana panels carry
their own copy of the same CTE — see ``dashboards/README.md``; this module is the
source of truth, and a weight changed here must be changed there too.
"""

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text


@dataclass(frozen=True)
class Achievement:
    """One durable unlock: a counter, a threshold, and what to call it.

    ``metric`` names a key of ``_METRIC_EVENT_SQL``, which is what keeps the
    catalogue pure data — adding a badge is a tuple entry below, and adding a
    *kind* of badge is one extra query.
    """

    key: str
    emoji: str
    label: str
    metric: str
    threshold: int
    blurb: str


# The ladder. Ordered by metric then threshold for readability; nothing depends
# on the order.
ACHIEVEMENTS: tuple[Achievement, ...] = (
    # Volume — any rating, finding or summary. A rating is a rating when the
    # question is engagement; the two are only kept apart for quality averages.
    Achievement("first_rating", "🌱", "First Contact", "ratings", 1,
                "Rated your first review comment"),
    Achievement("ratings_10", "⭐", "Ten Rated", "ratings", 10,
                "Rated 10 review comments"),
    Achievement("ratings_50", "🌟", "Half Century", "ratings", 50,
                "Rated 50 review comments"),
    Achievement("ratings_100", "💯", "Century", "ratings", 100,
                "Rated 100 review comments"),
    Achievement("ratings_500", "🏛️", "Legend", "ratings", 500,
                "Rated 500 review comments"),
    # Written feedback — worth more than a click, and the only thing here that
    # tells the team *why* a finding was good or bad.
    Achievement("first_reply", "💬", "First Words", "replies", 1,
                "Replied to the reviewer for the first time"),
    Achievement("replies_10", "📝", "Conversationalist", "replies", 10,
                "Wrote 10 replies to review comments"),
    Achievement("replies_50", "🗣️", "Voice of the Team", "replies", 50,
                "Wrote 50 replies to review comments"),
    # Peer review — rating findings on someone else's MR, which nobody has to do.
    Achievement("peer_10", "🤝", "Good Neighbour", "peer_ratings", 10,
                "Rated 10 comments on other people's merge requests"),
    Achievement("peer_50", "🧭", "Community Pillar", "peer_ratings", 50,
                "Rated 50 comments on other people's merge requests"),
    # Timeliness — the same 24h window the speed bonus uses, deliberately: two
    # definitions of "fast" in one feature is one too many.
    Achievement("fast_10", "⚡", "Same-Day Service", "fast_ratings", 10,
                "Rated 10 comments within a day of them being posted"),
    Achievement("fast_50", "🚀", "Always On", "fast_ratings", 50,
                "Rated 50 comments within a day of them being posted"),
    # Consistency. Weekly rather than daily: nobody opens a merge request every
    # day, and a streak you lose by taking a weekend stops motivating anyone.
    Achievement("streak_4w", "🔥", "Month on Fire", "streak_weeks", 4,
                "Rated something 4 weeks running"),
    Achievement("streak_12w", "🧗", "Quarter Climber", "streak_weeks", 12,
                "Rated something 12 weeks running"),
    Achievement("streak_26w", "🏔️", "Half-Year Hero", "streak_weeks", 26,
                "Rated something 26 weeks running"),
    # Completeness on your own merge requests — the fairest metric in the
    # feature, because it is bounded at 100% and cannot be farmed by volume.
    Achievement("perfect_1", "🎯", "Clean Sweep", "perfect_mrs", 1,
                "Rated every finding on one of your own merge requests"),
    Achievement("perfect_5", "🏆", "Perfectionist", "perfect_mrs", 5,
                "Rated every finding on 5 of your own merge requests"),
    Achievement("perfect_20", "👑", "Untouchable", "perfect_mrs", 20,
                "Rated every finding on 20 of your own merge requests"),
)

ACHIEVEMENTS_BY_KEY: dict[str, Achievement] = {a.key: a for a in ACHIEVEMENTS}

# Point totals → level. Descending, so the first match wins; the 0 floor means
# every known participant has a level rather than a NULL.
POINT_LEVELS: tuple[tuple[int, str, str], ...] = (
    (400, "💎", "Diamond"),
    (150, "🥇", "Gold"),
    (50, "🥈", "Silver"),
    (0, "🥉", "Bronze"),
)


def newly_unlocked(
    counters: dict[str, int], already_earned: set[str]
) -> list[Achievement]:
    """Which achievements the counters have crossed that aren't already held.

    Pure — no database, no clock — so the ladder is testable without a
    PostgreSQL. An unknown metric counts as 0 rather than raising: a catalogue
    entry whose query was never implemented should stay locked, not take down the
    webhook that evaluated it.
    """
    return [
        a
        for a in ACHIEVEMENTS
        if a.key not in already_earned and counters.get(a.metric, 0) >= a.threshold
    ]


def level_for(points: int) -> tuple[str, str]:
    """``(emoji, name)`` for a point total. Pure; mirrors POINT_LEVELS."""
    for floor, emoji, name in POINT_LEVELS:
        if points >= floor:
            return emoji, name
    return POINT_LEVELS[-1][1], POINT_LEVELS[-1][2]


# --- SQL -------------------------------------------------------------------
#
# Timestamps in this store are TEXT. The ones written by this service
# (``received_at``) are ``datetime.now(timezone.utc).isoformat()`` and are always
# parseable, so casting them is safe. GitLab's own ``awarded_at`` is text from
# another system and is deliberately never cast — one malformed value would take
# down every query that touched it.

# Rendered back out as strict ISO-8601 so a value read here can be written
# straight into a String column and re-parsed by anything downstream.
def _iso(expr: str) -> str:
    return (
        f"to_char(({expr}) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')"
        " || '+00:00'"
    )


# One row per (note_id, user_id): the rater's *latest* click, findings and
# summaries unified under ``kind``, joined to the artifact that was rated so the
# peer bonus has the MR author and the speed bonus has the posting time.
#
# This CTE is why the raw tables are never counted directly. ``comment_scores``
# is unique on (note_id, user_id, emoji), so a rater who clicks 3, reconsiders,
# clicks 4 and does not revoke the 3 leaves **two rows** — every COUNT(*) over it
# scores them twice and every AVG blends a rating they abandoned. Tolerable in a
# diagnostic panel; not tolerable once points buy something.
#
# Findings and summaries are unioned here because *engagement* counts a rating as
# a rating. Finding **quality** averages must keep reading ``comment_scores``
# directly — that is what migration 0010 separated them for.
_CANONICAL_CTE = """
WITH unified AS (
    SELECT 'finding'::text AS kind, note_id, project_path, mr_id,
           user_id, username, user_name, emoji, score, received_at
    FROM comment_scores
    UNION ALL
    SELECT 'summary'::text AS kind, note_id, project_path, mr_id,
           user_id, username, user_name, emoji, score, received_at
    FROM summary_scores
),
canonical AS (
    SELECT DISTINCT ON (note_id, user_id)
           kind, note_id, project_path, mr_id,
           user_id, username, user_name, emoji, score, received_at
    FROM unified
    -- latest click wins; emoji breaks a tie so the row is deterministic when two
    -- awards share a received_at (a webhook redelivery burst).
    ORDER BY note_id, user_id, received_at DESC, emoji DESC
),
scores AS (
    SELECT c.kind,
           c.note_id,
           c.project_path,
           c.mr_id,
           c.username,
           c.user_name,
           -- Present for *quality* reads only (the agent's own grade). No
           -- engagement weight anywhere in this module reads it: paying a rater
           -- for the number they picked would inflate the one signal the ratings
           -- exist to measure.
           c.score,
           c.received_at AS rated_at,
           COALESCE(rc.mr_author_username, rs.mr_author_username) AS mr_author_username,
           COALESCE(rc.received_at, rs.received_at)               AS artifact_posted_at
    FROM canonical c
    LEFT JOIN review_comments  rc ON c.kind = 'finding' AND rc.note_id = c.note_id
    LEFT JOIN review_summaries rs ON c.kind = 'summary' AND rs.note_id = c.note_id
)
"""

# A rating that landed within a day of the comment being posted. Shared by the
# speed bonus and the Same-Day Service badge so "fast" means one thing.
_WITHIN_A_DAY = (
    "artifact_posted_at IS NOT NULL"
    " AND rated_at::timestamptz <= artifact_posted_at::timestamptz + interval '24 hours'"
)

# A rating on someone else's merge request. Shared by the peer bonus and the
# Good Neighbour badge.
_PEER = "mr_author_username IS NOT NULL AND mr_author_username <> username"

# Per metric: the ordered timestamps at which it first reached 1, 2, 3, … So the
# row count is the counter, and row N is when threshold N was crossed. Every
# query is scoped to one (:username, :project) pair.
_METRIC_EVENT_SQL: dict[str, str] = {
    "ratings": _CANONICAL_CTE + f"""
        SELECT {_iso('rated_at::timestamptz')} AS at
        FROM scores
        WHERE username = :username AND project_path = :project
        ORDER BY rated_at::timestamptz
    """,
    "peer_ratings": _CANONICAL_CTE + f"""
        SELECT {_iso('rated_at::timestamptz')} AS at
        FROM scores
        WHERE username = :username AND project_path = :project AND {_PEER}
        ORDER BY rated_at::timestamptz
    """,
    "fast_ratings": _CANONICAL_CTE + f"""
        SELECT {_iso('rated_at::timestamptz')} AS at
        FROM scores
        WHERE username = :username AND project_path = :project AND {_WITHIN_A_DAY}
        ORDER BY rated_at::timestamptz
    """,
    "replies": f"""
        SELECT {_iso('received_at::timestamptz')} AS at
        FROM comment_replies
        WHERE author_username = :username AND project_path = :project
        ORDER BY received_at::timestamptz
    """,
    # Longest run of consecutive ISO weeks containing at least one rating.
    # Gaps-and-islands: subtracting the row number from the week collapses each
    # consecutive run onto a single anchor date, which is then a GROUP BY. Taking
    # MIN(wk) per position across runs gives the *earliest* time a streak of that
    # length was ever reached — non-decreasing in n, so the list stays ordered.
    "streak_weeks": _CANONICAL_CTE + f"""
        , weeks AS (
            SELECT DISTINCT date_trunc('week', rated_at::timestamptz) AS wk
            FROM scores
            WHERE username = :username AND project_path = :project
        ),
        grouped AS (
            SELECT wk,
                   wk - (row_number() OVER (ORDER BY wk)) * interval '1 week' AS anchor
            FROM weeks
        ),
        ranked AS (
            SELECT wk, row_number() OVER (PARTITION BY anchor ORDER BY wk) AS n
            FROM grouped
        )
        SELECT {_iso('MIN(wk)')} AS at
        FROM ranked
        GROUP BY n
        ORDER BY n
    """,
    # Own merge requests where the author rated *every* finding themselves, dated
    # by the rating that completed each one. The ``cs.username = :username`` join
    # condition is what makes this un-giftable — a colleague rating your MR does
    # not earn you the badge. Two findings minimum, or a one-comment MR unlocks
    # it by accident.
    "perfect_mrs": _CANONICAL_CTE + f"""
        SELECT {_iso('MAX(cs.rated_at::timestamptz)')} AS at
        FROM review_comments rc
        LEFT JOIN scores cs
               ON cs.note_id = rc.note_id AND cs.username = :username
        WHERE rc.mr_author_username = :username
          AND rc.project_path = :project
        GROUP BY rc.mr_id
        HAVING COUNT(DISTINCT rc.note_id) >= 2
           AND COUNT(DISTINCT cs.note_id) = COUNT(DISTINCT rc.note_id)
        ORDER BY 1
    """,
}

# One row per points-earning event, with the breakdown alongside the total so a
# balance is always explainable back to the actions that produced it ("why do I
# have 47?"). Consumers filter on ``occurred_at`` and SUM(points).
POINTS_LEDGER_SQL = _CANONICAL_CTE + f"""
SELECT project_path,
       username,
       user_name,
       {_iso('rated_at::timestamptz')} AS occurred_at,
       CASE WHEN kind = 'finding' THEN 'rate_finding' ELSE 'rate_summary' END AS action,
       kind,
       mr_id,
       note_id,
       CASE WHEN kind = 'finding' THEN 2 ELSE 1 END AS base_points,
       CASE WHEN {_PEER} THEN 1 ELSE 0 END AS peer_bonus,
       CASE WHEN {_WITHIN_A_DAY} THEN 1 ELSE 0 END AS speed_bonus,
       (CASE WHEN kind = 'finding' THEN 2 ELSE 1 END)
       + (CASE WHEN {_PEER} THEN 1 ELSE 0 END)
       + (CASE WHEN {_WITHIN_A_DAY} THEN 1 ELSE 0 END) AS points
FROM scores
WHERE username IS NOT NULL

UNION ALL

SELECT project_path,
       username,
       user_name,
       occurred_at,
       'reply' AS action,
       kind,
       mr_id,
       note_id,
       points AS base_points,
       0 AS peer_bonus,
       0 AS speed_bonus,
       points
FROM (
    SELECT cr.project_path,
           cr.author_username AS username,
           cr.author_name     AS user_name,
           {_iso('cr.received_at::timestamptz')} AS occurred_at,
           COALESCE(cr.parent_kind, 'finding') AS kind,
           cr.mr_id,
           cr.note_id,
           -- Full value for the first substantive reply in a thread; a token
           -- acknowledgement, or a follow-up in a thread this person has already
           -- replied to, earns 1. The weight goes to the reply that carries the
           -- feedback rather than to the count of replies.
           CASE WHEN length(btrim(COALESCE(cr.body, ''))) >= 15
                     AND row_number() OVER (
                         PARTITION BY cr.author_username, cr.discussion_id
                         ORDER BY cr.received_at
                     ) = 1
                THEN 5 ELSE 1 END AS points
    FROM comment_replies cr
    WHERE cr.author_username IS NOT NULL
) replies
"""


# --- reads -----------------------------------------------------------------


def metric_events(conn, username: str, project_path: str) -> dict[str, list[str]]:
    """Every metric's threshold-crossing timestamps for one (user, project)."""
    params = {"username": username, "project": project_path}
    return {
        metric: [row[0] for row in conn.execute(text(sql), params)]
        for metric, sql in _METRIC_EVENT_SQL.items()
    }


def counters_from_events(events: dict[str, list[str]]) -> dict[str, int]:
    """Counter per metric — the length of its crossing list, by construction."""
    return {metric: len(times) for metric, times in events.items()}


def participants(conn) -> list[dict[str, Any]]:
    """Every (username, project_path) pair that has ever rated or replied."""
    rows = conn.execute(
        text(
            """
            SELECT username, MAX(user_name) AS user_name, project_path
            FROM (
                SELECT username, user_name, project_path
                FROM comment_scores
                WHERE username IS NOT NULL AND project_path IS NOT NULL
                UNION ALL
                SELECT username, user_name, project_path
                FROM summary_scores
                WHERE username IS NOT NULL AND project_path IS NOT NULL
                UNION ALL
                SELECT author_username AS username,
                       author_name     AS user_name,
                       project_path
                FROM comment_replies
                WHERE author_username IS NOT NULL AND project_path IS NOT NULL
            ) everyone
            GROUP BY username, project_path
            ORDER BY username, project_path
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def points_leaderboard(
    conn,
    limit: int = 100,
    project_path: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Points per person, highest first, each total with its breakdown.

    ``since``/``until`` are ISO timestamps bounding ``occurred_at``; omit both for
    all-time. ``until`` is **exclusive**, so a caller reporting one week passes the
    following Monday and gets exactly that week.

    Both bounds belong in the SQL. Filtering a half-open result in Python — say by
    checking each row's ``last_active`` — drops anyone who was active during the
    window *and* again afterwards, because their last activity falls outside it.
    That is not hypothetical: it silently emptied the weekly digest's points table
    for every past week, while the stored award winners beside it stayed correct,
    so the message announced a "Most Points" winner above a leaderboard that did
    not contain them.
    """
    clauses = ["username IS NOT NULL"]
    params: dict[str, Any] = {"limit": limit}
    if project_path:
        clauses.append("project_path = :project")
        params["project"] = project_path
    if since:
        clauses.append("occurred_at::timestamptz >= CAST(:since AS timestamptz)")
        params["since"] = since
    if until:
        clauses.append("occurred_at::timestamptz < CAST(:until AS timestamptz)")
        params["until"] = until

    sql = f"""
        WITH ledger AS ({POINTS_LEDGER_SQL})
        SELECT username,
               MAX(user_name)                                 AS user_name,
               SUM(points)::int                               AS points,
               COUNT(*) FILTER (WHERE action <> 'reply')::int  AS ratings,
               COUNT(*) FILTER (WHERE action = 'reply')::int   AS replies,
               SUM(peer_bonus)::int                           AS peer_bonus,
               SUM(speed_bonus)::int                          AS speed_bonus,
               MAX(occurred_at)                               AS last_active
        FROM ledger
        WHERE {' AND '.join(clauses)}
        GROUP BY username
        ORDER BY points DESC, ratings DESC
        LIMIT :limit
    """
    rows = conn.execute(text(sql), params).mappings().all()

    out = []
    for row in rows:
        item = dict(row)
        emoji, name = level_for(int(item.get("points") or 0))
        item["level_emoji"] = emoji
        item["level"] = name
        out.append(item)
    return out


# --- writes ----------------------------------------------------------------


def evaluate(
    conn,
    username: Optional[str],
    user_name: Optional[str],
    project_path: Optional[str],
) -> list[str]:
    """Award any achievements ``username`` has newly earned on ``project_path``.

    Returns the keys awarded (empty when nothing crossed). Each row is dated to
    the event that *crossed* the threshold, not to now — which is what makes a
    backfill over a year of history produce a truthful timeline rather than a
    thousand badges stamped with the migration date.

    ``ON CONFLICT DO NOTHING`` rather than a read-then-write guard: two of the
    reviewer's comments can be rated in the same second, and the unique index is
    the only thing that actually serialises that race.
    """
    if not username or not project_path:
        return []

    held = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT achievement_key FROM achievements"
                " WHERE username = :username AND project_path = :project"
            ),
            {"username": username, "project": project_path},
        )
    }
    events = metric_events(conn, username, project_path)
    counters = counters_from_events(events)
    unlocked = newly_unlocked(counters, held)
    if not unlocked:
        return []

    rows = []
    for a in unlocked:
        times = events.get(a.metric) or []
        # Row N is when threshold N was crossed. The guard is belt-and-braces:
        # newly_unlocked only returns a badge whose counter (== len(times))
        # already reached the threshold.
        crossed_at = times[a.threshold - 1] if len(times) >= a.threshold else times[-1]
        rows.append(
            {
                "awarded_at": crossed_at,
                "username": username,
                "user_name": user_name,
                "project_path": project_path,
                "achievement_key": a.key,
                "progress_value": counters.get(a.metric, 0),
            }
        )

    conn.execute(
        text(
            """
            INSERT INTO achievements
                (awarded_at, username, user_name, project_path,
                 achievement_key, progress_value)
            VALUES
                (:awarded_at, :username, :user_name, :project_path,
                 :achievement_key, :progress_value)
            ON CONFLICT (username, achievement_key, project_path) DO NOTHING
            """
        ),
        rows,
    )
    return [a.key for a in unlocked]


def backfill(conn) -> dict[str, Any]:
    """Evaluate every participant against the whole ladder, over all history.

    Run from migration 0011 so the feature arrives with the past already
    credited: unlocks are otherwise only ever awarded on the ingest path, which
    would leave everyone who rated before the deploy at zero badges — and the
    people with the most ratings to their name would be the ones it looked
    broken for. Also the repair path, exposed as
    ``POST /api/achievements/recompute``.

    Idempotent, and safe to run repeatedly: awarding conflicts are ignored, and
    because each badge is dated by its crossing event rather than by the clock, a
    later re-run reproduces exactly the same timestamps.
    """
    people = participants(conn)
    awarded: dict[str, list[str]] = {}
    for row in people:
        keys = evaluate(conn, row["username"], row.get("user_name"), row["project_path"])
        if keys:
            awarded[f"{row['username']}@{row['project_path']}"] = keys
    return {
        "participants": len(people),
        "awarded_count": sum(len(v) for v in awarded.values()),
        "awarded": awarded,
    }


# ---------------------------------------------------------------------------
# Weekly awards
#
# Deliberately anchored to the **ISO week**, never to a rolling window. A "last 7
# days" leaderboard changes every time you look at it, which makes it useless as
# a record: nobody can be told they won, the same person can be top on Tuesday
# and second on Wednesday without anything happening, and two people reading the
# dashboard a day apart see different history. A named week — 2026-W34, Monday to
# Sunday — closes, gets a winner, and stays settled forever.
#
# ``to_char(ts, 'IYYY"-W"IW')`` is the bucket key. The ISO year is part of it on
# purpose: plain week numbers collide across years, and the last days of December
# belong to week 1 of the *following* ISO year (2026-12-31 is 2027-W01), so
# pairing IW with the calendar year would silently file that week twice.
#
# Winners are **stored**, unlike points. A week's result is a fact about a closed
# period and must not drift when someone revokes a reaction three months later —
# and the whole point of announcing it in Mattermost is that the announcement and
# the dashboard agree a year from now.
#
# One winner per (week, project, category), and five categories rather than one
# leaderboard: a single board motivates exactly one person and quietly tells
# everyone else not to bother, while five means most people can top something.
# ---------------------------------------------------------------------------

# Postgres format string for the ISO year-week bucket. Doubled quotes inside
# because it is embedded in a Python string that is itself embedded in SQL.
_WEEK_KEY = "'IYYY\"-W\"IW'"


@dataclass(frozen=True)
class WeeklyAward:
    """One weekly category: what it measures and how to say who won."""

    key: str
    emoji: str
    label: str
    blurb: str
    unit: str          # rendered after the value in the digest
    lower_is_better: bool = False


WEEKLY_AWARDS: tuple[WeeklyAward, ...] = (
    WeeklyAward("top_rater", "🏅", "Top Rater",
                "Rated the most review comments this week", "ratings"),
    WeeklyAward("most_points", "🔥", "Most Points",
                "Earned the most engagement points this week", "pts"),
    WeeklyAward("best_feedback", "💬", "Best Feedback",
                "Wrote the most substantive replies this week", "replies"),
    WeeklyAward("fastest", "⚡", "Fastest Draw",
                "Shortest median time from a comment being posted to rating it",
                "h median", lower_is_better=True),
    WeeklyAward("completionist", "🎯", "Completionist",
                "Rated the highest share of the findings on their own merge requests",
                "% covered"),
)

WEEKLY_AWARDS_BY_KEY: dict[str, WeeklyAward] = {w.key: w for w in WEEKLY_AWARDS}


# Each query returns the single winner for (:week, :project) as
# (username, user_name, value). Ties break on username so a recompute is stable.
_WEEKLY_AWARD_SQL: dict[str, str] = {
    "top_rater": _CANONICAL_CTE + f"""
        SELECT username, MAX(user_name) AS user_name, COUNT(*)::numeric AS value
        FROM scores
        WHERE project_path = :project
          AND username IS NOT NULL
          AND to_char(rated_at::timestamptz, {_WEEK_KEY}) = :week
        GROUP BY username
        ORDER BY value DESC, username
        LIMIT 1
    """,
    # Median rather than mean: one comment rated three weeks late would otherwise
    # sink an otherwise prompt week. Three ratings minimum, or a single lucky
    # click wins the category.
    "fastest": _CANONICAL_CTE + f"""
        SELECT username,
               MAX(user_name) AS user_name,
               (percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY GREATEST(0, EXTRACT(EPOCH FROM (
                       rated_at::timestamptz - artifact_posted_at::timestamptz
                   )))
               ) / 3600.0)::numeric AS value
        FROM scores
        WHERE project_path = :project
          AND username IS NOT NULL
          AND artifact_posted_at IS NOT NULL
          AND to_char(rated_at::timestamptz, {_WEEK_KEY}) = :week
        GROUP BY username
        HAVING COUNT(*) >= 3
        ORDER BY value ASC, username
        LIMIT 1
    """,
    # Substantive only — the same >= 15 character rule the points ledger uses, so
    # "a reply" means one thing across the feature.
    "best_feedback": f"""
        SELECT author_username AS username,
               MAX(author_name) AS user_name,
               COUNT(*)::numeric AS value
        FROM comment_replies
        WHERE project_path = :project
          AND author_username IS NOT NULL
          AND length(btrim(COALESCE(body, ''))) >= 15
          AND to_char(received_at::timestamptz, {_WEEK_KEY}) = :week
        GROUP BY author_username
        ORDER BY value DESC, username
        LIMIT 1
    """,
    # Coverage of the findings posted on your *own* merge requests that week, and
    # only ratings by you count — the point of the category is that you read what
    # the reviewer left you. Bounded at 100%, so unlike volume it cannot be
    # farmed; three findings minimum so a one-comment week is not a free win.
    "completionist": _CANONICAL_CTE + f"""
        SELECT rc.mr_author_username AS username,
               NULL::text AS user_name,
               (100.0 * COUNT(DISTINCT cs.note_id)
                      / COUNT(DISTINCT rc.note_id))::numeric AS value
        FROM review_comments rc
        LEFT JOIN scores cs
               ON cs.note_id = rc.note_id
              AND cs.username = rc.mr_author_username
        WHERE rc.project_path = :project
          AND rc.mr_author_username IS NOT NULL
          AND to_char(rc.received_at::timestamptz, {_WEEK_KEY}) = :week
        GROUP BY rc.mr_author_username
        HAVING COUNT(DISTINCT rc.note_id) >= 3
        ORDER BY value DESC, username
        LIMIT 1
    """,
    "most_points": f"""
        WITH ledger AS ({POINTS_LEDGER_SQL})
        SELECT username, MAX(user_name) AS user_name, SUM(points)::numeric AS value
        FROM ledger
        WHERE project_path = :project
          AND username IS NOT NULL
          AND to_char(occurred_at::timestamptz, {_WEEK_KEY}) = :week
        GROUP BY username
        ORDER BY value DESC, username
        LIMIT 1
    """,
}


def iso_week_key(day) -> str:
    """``date``/``datetime`` → ``2026-W34``, matching the SQL bucket exactly."""
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_start_date(iso_week: str) -> str:
    """``2026-W34`` → ``2026-08-17`` (the Monday). ISO 8601 date, as text.

    Stored alongside every award row so Grafana has something castable to filter
    and sort on — ``$__timeFilter`` cannot work on ``2026-W34``, and a dashboard
    panel that silently ignores the time picker is the bug this repo's README
    warns about.
    """
    from datetime import date

    year_s, week_s = iso_week.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1).isoformat()


def active_weeks(conn) -> list[dict[str, str]]:
    """Every (project_path, iso_week) that saw a rating or a reply.

    The set of weeks worth computing awards for. Derived from the data rather
    than generated as a calendar: a week nobody rated in has no winners, and a
    row saying so would be noise in every panel and every digest.
    """
    rows = conn.execute(
        text(
            f"""
            SELECT DISTINCT project_path,
                   to_char(received_at::timestamptz, {_WEEK_KEY}) AS iso_week
            FROM (
                SELECT project_path, received_at FROM comment_scores
                 WHERE project_path IS NOT NULL
                UNION ALL
                SELECT project_path, received_at FROM summary_scores
                 WHERE project_path IS NOT NULL
                UNION ALL
                SELECT project_path, received_at FROM comment_replies
                 WHERE project_path IS NOT NULL
            ) activity
            ORDER BY project_path, iso_week
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def compute_weekly_awards(
    conn, iso_week: str, project_path: str, computed_at: Optional[str] = None
) -> list[dict[str, Any]]:
    """Decide and store every category's winner for one (week, project).

    Upserted on ``(iso_week, project_path, category)``: recomputing a week
    replaces its result rather than stacking a second winner, which is what makes
    it safe to re-run the current week every day and settle it when it closes.
    A category nobody qualified for stores no row — an empty slot in the digest is
    honest, and a placeholder winner is not.
    """
    from datetime import datetime, timezone

    stamp = computed_at or datetime.now(timezone.utc).isoformat()
    params = {"week": iso_week, "project": project_path}
    winners: list[dict[str, Any]] = []

    for award in WEEKLY_AWARDS:
        row = conn.execute(
            text(_WEEKLY_AWARD_SQL[award.key]), params
        ).mappings().first()
        if row is None or not row.get("username"):
            continue
        winners.append(
            {
                "iso_week": iso_week,
                "week_start": week_start_date(iso_week),
                "project_path": project_path,
                "category": award.key,
                "username": row["username"],
                "user_name": row.get("user_name"),
                "value": float(row["value"]) if row["value"] is not None else None,
                "computed_at": stamp,
            }
        )

    if winners:
        conn.execute(
            text(
                """
                INSERT INTO weekly_awards
                    (iso_week, week_start, project_path, category,
                     username, user_name, value, computed_at)
                VALUES
                    (:iso_week, :week_start, :project_path, :category,
                     :username, :user_name, :value, :computed_at)
                ON CONFLICT (iso_week, project_path, category) DO UPDATE SET
                    username    = EXCLUDED.username,
                    user_name   = EXCLUDED.user_name,
                    value       = EXCLUDED.value,
                    week_start  = EXCLUDED.week_start,
                    computed_at = EXCLUDED.computed_at
                """
            ),
            winners,
        )
    return winners


def backfill_weekly_awards(conn) -> dict[str, Any]:
    """Compute awards for every week that has ever had activity, per project.

    Run from migration 0011, so the feature arrives with its whole history
    settled instead of starting from an empty board — the first Mattermost digest
    can then say "here is week 34, and here is how it compares to 33". Idempotent
    (every week upserts), so it doubles as the repair path when a scheduled run
    is missed.
    """
    weeks = active_weeks(conn)
    total = 0
    for row in weeks:
        total += len(compute_weekly_awards(conn, row["iso_week"], row["project_path"]))
    return {"weeks": len(weeks), "awards": total}


def list_weekly_awards(
    conn,
    limit: int = 200,
    project_path: Optional[str] = None,
    iso_week: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Stored weekly winners, most recent week first, enriched from the catalogue."""
    clauses = ["1 = 1"]
    params: dict[str, Any] = {"limit": limit}
    if project_path:
        clauses.append("project_path = :project")
        params["project"] = project_path
    if iso_week:
        clauses.append("iso_week = :week")
        params["week"] = iso_week

    rows = conn.execute(
        text(
            f"""
            SELECT iso_week, week_start, project_path, category,
                   username, user_name, value, computed_at
            FROM weekly_awards
            WHERE {' AND '.join(clauses)}
            ORDER BY week_start DESC, category
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    out = []
    for row in rows:
        item = dict(row)
        meta = WEEKLY_AWARDS_BY_KEY.get(item.get("category", ""))
        item["emoji"] = meta.emoji if meta else None
        item["label"] = meta.label if meta else None
        item["unit"] = meta.unit if meta else None
        item["blurb"] = meta.blurb if meta else None
        out.append(item)
    return out


def weekly_stats(conn, iso_week: str, project_path: str) -> dict[str, Any]:
    """Team-level numbers for one (ISO week, project) — the digest's headline.

    Coverage is the fair figure to lead with: it is bounded at 100%, so unlike a
    rating count it cannot be moved by one enthusiastic person, and it answers the
    only question the team can act on — *did we tell the reviewer what we thought
    of what it left us?* The denominator is findings **posted** that week, so a
    quiet week with everything rated reads as 100% rather than as failure.

    ``agent_grade`` is the mirror image, and the reason the loop is worth running
    at all: the average rating the findings themselves earned. It reads
    ``kind = 'finding'`` only — pooling summary ratings into it would answer "was
    the description useful" and "was the defect real" with one number, which is
    what migration 0010 exists to prevent.
    """
    row = conn.execute(
        text(
            f"""
            WITH scoped AS ({_CANONICAL_CTE} SELECT * FROM scores),
            rated AS (
                SELECT * FROM scoped
                WHERE project_path = :project
                  AND to_char(rated_at::timestamptz, {_WEEK_KEY}) = :week
            ),
            posted AS (
                SELECT rc.note_id
                FROM review_comments rc
                WHERE rc.project_path = :project
                  AND to_char(rc.received_at::timestamptz, {_WEEK_KEY}) = :week
            )
            SELECT
              (SELECT COUNT(*) FROM rated)                          AS ratings,
              (SELECT COUNT(DISTINCT username) FROM rated)          AS raters,
              (SELECT COUNT(*) FROM posted)                         AS findings_posted,
              (SELECT COUNT(*) FROM posted p
                WHERE EXISTS (SELECT 1 FROM scoped s
                               WHERE s.note_id = p.note_id))        AS findings_rated,
              (SELECT ROUND(AVG(score)::numeric, 2) FROM rated
                WHERE kind = 'finding')                             AS agent_grade,
              (SELECT COUNT(*) FROM comment_replies
                WHERE project_path = :project
                  AND length(btrim(COALESCE(body, ''))) >= 15
                  AND to_char(received_at::timestamptz, {_WEEK_KEY}) = :week
              )                                                     AS replies
            """
        ),
        {"week": iso_week, "project": project_path},
    ).mappings().first()

    stats = dict(row) if row else {}
    posted = int(stats.get("findings_posted") or 0)
    rated = int(stats.get("findings_rated") or 0)
    stats["coverage_pct"] = round(100.0 * rated / posted, 1) if posted else None
    stats["iso_week"] = iso_week
    stats["project_path"] = project_path
    return stats


def achievements_in_week(
    conn, iso_week: str, project_path: Optional[str] = None
) -> list[dict[str, Any]]:
    """Badges whose threshold was crossed during one ISO week.

    Reads ``awarded_at``, which is the *crossing* time rather than the write
    time — so this is truthful even for weeks settled by the migration backfill,
    and a digest for any past week can be regenerated exactly.
    """
    clauses = [f"to_char(awarded_at::timestamptz, {_WEEK_KEY}) = :week"]
    params: dict[str, Any] = {"week": iso_week}
    if project_path:
        clauses.append("project_path = :project")
        params["project"] = project_path

    rows = conn.execute(
        text(
            f"""
            SELECT username, user_name, project_path, achievement_key,
                   progress_value, awarded_at
            FROM achievements
            WHERE {' AND '.join(clauses)}
            ORDER BY awarded_at
            """
        ),
        params,
    ).mappings().all()

    out = []
    for row in rows:
        item = dict(row)
        meta = ACHIEVEMENTS_BY_KEY.get(item.get("achievement_key", ""))
        item["emoji"] = meta.emoji if meta else None
        item["label"] = meta.label if meta else None
        out.append(item)
    return out
