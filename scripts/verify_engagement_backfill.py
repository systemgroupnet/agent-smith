"""Verify that migration 0011 credits history that already exists.

The real production path, in order:

  1. migrate to ``0010`` — the schema before the engagement feature
  2. insert ten weeks of ratings and replies with **historical** timestamps
  3. migrate to ``head`` — 0011 creates its tables and backfills
  4. assert badges are dated to the events that earned them rather than to today,
     that every active ISO week has its winners settled, that a second run awards
     nothing, and that a past week's digest still renders its points table

Timestamps are written directly here rather than through the webhook path (which
stamps ``now()``); that path is covered by ``verify_engagement.py``.

Not part of `pytest tests/`: it needs a database, and CI has none.

    docker run -d --name crg-verify-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=collector -p 55433:5432 postgres:18
    COLLECTOR_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55433/collector \
      python scripts/verify_engagement_backfill.py
    docker rm -f crg-verify-pg

**Destructive** — it drops and recreates the ``public`` schema. Point it at a
throwaway, never at the real collector.
"""
import os
import subprocess
import sys
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

DEFAULT_URL = "postgresql+psycopg://postgres:test@127.0.0.1:55433/collector"
URL = os.getenv("COLLECTOR_DATABASE_URL") or DEFAULT_URL
os.environ["COLLECTOR_DATABASE_URL"] = URL
COLLECTOR = str(Path(__file__).resolve().parent.parent / "collector")
sys.path.insert(0, COLLECTOR)

from sqlalchemy import create_engine, text  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: got {got!r}, want {want!r}")
    if not ok:
        FAIL.append(label)


def alembic(target):
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", "dummy", "upgrade", target],
        cwd=COLLECTOR, capture_output=True, text=True,
        env={**os.environ, "COLLECTOR_DATABASE_URL": URL},
    )
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit(f"alembic upgrade {target} failed")
    return r.stderr


eng = create_engine(URL, future=True)

# --- 1. wipe and migrate to the pre-feature schema -------------------------
with eng.begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
alembic("0010")
with eng.connect() as c:
    tables = {
        r[0]
        for r in c.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))
    }
check("achievements does not exist at 0010", "achievements" in tables, False)
check("review_comments does exist at 0010", "review_comments" in tables, True)

# --- 2. ten weeks of history ----------------------------------------------
PROJECT = "acme/shop"
# Anchor on a Monday so each iteration lands squarely in its own ISO week.
start = date(2026, 6, 1)  # a Monday
note = 500_000
rows_c, rows_s, rows_r = [], [], []

for week in range(10):
    day = start + timedelta(weeks=week)
    posted = datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc)
    rated = posted + timedelta(hours=3)
    # 3 findings per week on alice's MR; alice rates all 3, bob rates 1 (peer).
    for i in range(3):
        note += 1
        rows_c.append({
            "received_at": posted.isoformat(), "run_id": f"r{week}",
            "review_run_id": 1, "project_path": PROJECT, "mr_id": str(9000 + week),
            "note_id": note, "discussion_id": f"d{note}", "comment_type": "inline",
            "file": f"svc/f{i}.go", "line": 10 + i, "severity": "high",
            "model": "glm-5.3", "mr_author_username": "alice",
        })
        rows_s.append({
            "received_at": rated.isoformat(), "note_id": note, "project_id": 42,
            "project_path": PROJECT, "mr_id": 9000 + week, "user_id": 1,
            "username": "alice", "user_name": "Alice A", "emoji": "four",
            "score": 4, "awarded_at": rated.isoformat(),
        })
    rows_s.append({
        "received_at": rated.isoformat(), "note_id": note, "project_id": 42,
        "project_path": PROJECT, "mr_id": 9000 + week, "user_id": 2,
        "username": "bob", "user_name": "Bob B", "emoji": "five",
        "score": 5, "awarded_at": rated.isoformat(),
    })
    # bob writes one substantive reply a week
    rows_r.append({
        "received_at": rated.isoformat(), "note_id": 900_000 + week,
        "discussion_id": f"d{note}", "parent_note_id": note, "run_id": f"r{week}",
        "project_id": 42, "project_path": PROJECT, "mr_id": 9000 + week,
        "author_id": 2, "author_username": "bob", "author_name": "Bob B",
        "body": "This would have crashed on a nil order, good catch.",
        "note_type": "DiffNote", "url": "", "noted_at": rated.isoformat(),
        "updated_at": rated.isoformat(), "action": "create", "parent_kind": "finding",
    })

with eng.begin() as c:
    c.execute(text("""INSERT INTO review_runs
        (received_at, run_id, project_path, mr_id, status, raw_json)
        VALUES ('2026-06-01T09:00:00+00:00','r0',:p,'9000','success','{}')"""),
        {"p": PROJECT})
    c.execute(text("""INSERT INTO review_comments
        (received_at, run_id, review_run_id, project_path, mr_id, note_id,
         discussion_id, comment_type, file, line, severity, model,
         mr_author_username)
        VALUES (:received_at,:run_id,:review_run_id,:project_path,:mr_id,:note_id,
                :discussion_id,:comment_type,:file,:line,:severity,:model,
                :mr_author_username)"""), rows_c)
    c.execute(text("""INSERT INTO comment_scores
        (received_at, note_id, project_id, project_path, mr_id, user_id, username,
         user_name, emoji, score, awarded_at)
        VALUES (:received_at,:note_id,:project_id,:project_path,:mr_id,:user_id,
                :username,:user_name,:emoji,:score,:awarded_at)"""), rows_s)
    c.execute(text("""INSERT INTO comment_replies
        (received_at, note_id, discussion_id, parent_note_id, run_id, project_id,
         project_path, mr_id, author_id, author_username, author_name, body,
         note_type, url, noted_at, updated_at, action, parent_kind)
        VALUES (:received_at,:note_id,:discussion_id,:parent_note_id,:run_id,
                :project_id,:project_path,:mr_id,:author_id,:author_username,
                :author_name,:body,:note_type,:url,:noted_at,:updated_at,:action,
                :parent_kind)"""), rows_r)
print(f"seeded {len(rows_c)} findings, {len(rows_s)} ratings, "
      f"{len(rows_r)} replies across 10 weeks (Jun–Aug 2026)\n")

# --- 3. migrate to head — the backfill ------------------------------------
stderr = alembic("head")
print("migration output:")
for line in stderr.splitlines():
    if "[0011]" in line:
        print("   ", line)
print()

# --- 4. assertions --------------------------------------------------------
with eng.connect() as c:
    ach = c.execute(text(
        "SELECT username, achievement_key, awarded_at, progress_value "
        "FROM achievements ORDER BY username, awarded_at")).mappings().all()
    weeks = c.execute(text(
        "SELECT iso_week, COUNT(*) AS n FROM weekly_awards "
        "GROUP BY iso_week ORDER BY iso_week")).mappings().all()

print("achievements backfilled:")
for a in ach:
    print(f"   {a['username']:6} {a['achievement_key']:14} "
          f"@ {a['awarded_at'][:10]}  (counter {a['progress_value']})")
print("\nweekly awards per ISO week:")
print("   " + ", ".join(f"{w['iso_week']}:{w['n']}" for w in weeks))
print()

by_user = {}
for a in ach:
    by_user.setdefault(a["username"], {})[a["achievement_key"]] = a

check("alice earned first_rating", "first_rating" in by_user["alice"], True)
check("alice earned ratings_10 (30 ratings over 10 weeks)",
      "ratings_10" in by_user["alice"], True)
check("alice earned streak_4w (10 consecutive weeks)",
      "streak_4w" in by_user["alice"], True)
check("alice did NOT earn streak_12w (only 10 weeks)",
      "streak_12w" in by_user["alice"], False)
check("alice earned perfect_5 (rated all 3 findings on 10 own MRs)",
      "perfect_5" in by_user["alice"], True)
check("bob earned peer_10 (10 peer ratings)", "peer_10" in by_user["bob"], True)
check("bob earned replies_10", "replies_10" in by_user["bob"], True)
check("bob earned no perfect_1 (no MRs of his own)",
      "perfect_1" in by_user["bob"], False)

# The headline requirement: dated to the past, not to migration day.
today = datetime.now(timezone.utc).date().isoformat()
first = by_user["alice"]["first_rating"]["awarded_at"][:10]
check("first_rating is dated to the first week of history, not today",
      first, "2026-06-01")
check("first_rating is not stamped with today", first == today, False)

tenth = by_user["alice"]["ratings_10"]["awarded_at"][:10]
check("ratings_10 crossed in the 4th week of history (3 findings/week)",
      tenth, "2026-06-22")
check("streak_4w is dated to the 4th consecutive week",
      by_user["alice"]["streak_4w"]["awarded_at"][:10], "2026-06-22")

check("every one of the 10 active weeks was settled", len(weeks), 10)
check("each week has all 5 categories", {w["n"] for w in weeks}, {5})

# --- 5. re-running the backfill changes nothing ---------------------------
import engagement  # noqa: E402

with eng.begin() as c:
    again = engagement.backfill(c)
    engagement.backfill_weekly_awards(c)
with eng.connect() as c:
    n_ach = c.execute(text("SELECT COUNT(*) FROM achievements")).scalar()
    n_awd = c.execute(text("SELECT COUNT(*) FROM weekly_awards")).scalar()
    stamps = c.execute(text(
        "SELECT awarded_at FROM achievements WHERE username='alice' "
        "AND achievement_key='first_rating'")).scalar()
check("re-running the backfill awards nothing new", again["awarded_count"], 0)
check("achievement count unchanged", n_ach, len(ach))
check("weekly award count unchanged", n_awd, sum(w["n"] for w in weeks))
check("timestamps are reproducible, not clock-based", stamps[:10], "2026-06-01")

# --- 6. a past week's digest must contain its points table -----------------
# Regression: the leaderboard was summed from the week start with no upper bound
# and then filtered in Python on each row's last_active, so anyone active in the
# week *and* later was dropped — which emptied the table for every past week while
# the stored award winners beside it stayed correct. The digest then announced a
# "Most Points" winner above a leaderboard that did not contain them.
import digest as digest_mod  # noqa: E402

for week in ("2026-W25", "2026-W30"):
    message, _ = digest_mod.build_message(week, [PROJECT])
    has_points_table = "| # | Who | Points | Level |" in message
    check(f"{week} digest renders a points table", has_points_table, True)

    # And the announced Most Points winner is the leaderboard's top row.
    lines = message.splitlines()
    winner = next(
        (l.split("|")[3].strip() for l in lines if "**Most Points**" in l), None
    )
    top = next(
        (l.split("|")[2].strip() for i, l in enumerate(lines)
         if l.startswith("| 1 |")), None
    )
    check(f"{week} Most Points winner is on the leaderboard", winner == top, True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("✅ migration backfill verified over 10 weeks of pre-existing history")
