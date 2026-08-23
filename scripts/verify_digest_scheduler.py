"""Verify the in-service weekly-digest scheduler against a real PostgreSQL.

Covers what `tests/test_engagement.py` cannot, because it needs a database and a
Mattermost:

  * the ``digest_runs`` claim really serialises — 8 threads race, one wins
  * a restart does not re-announce a week already dealt with
  * a **failed** week keeps its claim, so it can never be double-posted
  * an empty week is recorded and stays quiet
  * ``tick()`` short-circuits when nothing is due
  * a full tick on a week with activity posts and records the post id

Not part of `pytest tests/`: CI has no database and no channel to post to.

    docker run -d --name crg-verify-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=collector -p 55433:5432 postgres:18
    python scripts/verify_engagement.py          # seed some ratings first
    MATTERMOST_URL=... MATTERMOST_TOKEN=... MATTERMOST_CHANNEL_ID=... \
      python scripts/verify_digest_scheduler.py
    docker rm -f crg-verify-pg

Without the three MATTERMOST_* variables the posting sections are skipped and the
claim/bookkeeping checks still run.

**Destructive, and it posts.** It clears ``digest_runs`` and sends real messages to
whatever channel you give it — use a scratch channel, never the team's.
"""
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "postgresql+psycopg://postgres:test@127.0.0.1:55433/collector"
os.environ["COLLECTOR_DATABASE_URL"] = os.getenv("COLLECTOR_DATABASE_URL") or DEFAULT_URL
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import digest  # noqa: E402
import engagement  # noqa: E402
import scheduler  # noqa: E402
import storage  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: got {got!r}, want {want!r}")
    if not ok:
        FAIL.append(label)


storage.run_migrations()
# Re-runnable: the claims this script makes would otherwise be found already taken
# on a second run, which is the correct behaviour and a useless test.
with storage.get_engine().begin() as _c:
    from sqlalchemy import text as _clear
    _c.execute(_clear("DELETE FROM digest_runs"))
print("migrations applied (0012 included), digest_runs cleared\n")

WEEK = "2026-W34"

# --- 1. the claim serialises ----------------------------------------------
print("--- 8 threads race for the same week ---")
results = []
lock = threading.Lock()


def race():
    got = storage.claim_digest_week(WEEK)
    with lock:
        results.append(got)


threads = [threading.Thread(target=race) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("exactly one claimant wins", sum(1 for r in results if r), 1)
check("the other seven are refused", sum(1 for r in results if not r), 7)

with storage.get_engine().connect() as c:
    from sqlalchemy import text
    n = c.execute(text("SELECT COUNT(*) FROM digest_runs WHERE iso_week=:w"),
                  {"w": WEEK}).scalar()
check("one row in digest_runs", n, 1)

# --- 2. a claimed week is never re-claimed (restart safety) ---------------
print("\n--- restart: the same week is not claimed again ---")
check("re-claiming the week is refused", storage.claim_digest_week(WEEK), False)
storage.mark_digest_posted(WEEK, "chan-1", "post-abc")
check("still refused after being marked posted",
      storage.claim_digest_week(WEEK), False)
row = storage.digest_week_status(WEEK)
check("status recorded as posted", row["status"], "posted")
check("post id recorded", row["post_id"], "post-abc")

# --- 3. a failed week keeps its claim (at most once) ----------------------
print("\n--- a failure keeps the claim, so it cannot be double-posted ---")
FAILED = "2026-W33"
check("claiming a fresh week succeeds", storage.claim_digest_week(FAILED), True)
storage.mark_digest_failed(FAILED, "Mattermost returned 403: forbidden")
check("failed week is NOT re-claimable", storage.claim_digest_week(FAILED), False)
row = storage.digest_week_status(FAILED)
check("status recorded as failed", row["status"], "failed")
check("error recorded", "403" in (row["error"] or ""), True)
check("claimed_at survives the outcome write", bool(row["claimed_at"]), True)

# --- 4. an empty week is recorded and stays quiet -------------------------
print("\n--- an empty week is recorded, not announced ---")
EMPTY = "2026-W20"
storage.claim_digest_week(EMPTY)
storage.mark_digest_skipped(EMPTY, "no activity in this week")
row = storage.digest_week_status(EMPTY)
check("status recorded as skipped", row["status"], "skipped")
check("no post id for a skipped week", row["post_id"], None)

# run_digest on a genuinely empty week must report empty, not post
out = digest.run_digest("2026-W02", dry_run=False)
check("run_digest reports an empty week as empty", out.status, "empty")
check("and produced no post id", out.post_id, None)

# --- 5. tick() does nothing when nothing is due --------------------------
print("\n--- tick with nothing due ---")
os.environ["DIGEST_TIMEZONE"] = "UTC"
os.environ["DIGEST_DAY"] = "mon"
os.environ["DIGEST_HOUR"] = "9"
now = datetime.now(timezone.utc)
this_week = engagement.iso_week_key(now)
# The due week right now is last week; claim it so the tick has nothing to do.
last_week = scheduler.next_due_week(now, day=0, hour=9)
if last_week:
    storage.claim_digest_week(last_week)
    storage.mark_digest_skipped(last_week, "pre-claimed by the test")
check("tick returns None when the due week is already dealt with",
      scheduler.tick(), None)

# --- 6. a real tick, end to end -----------------------------------------
print("\n--- a real tick posts the due week ---")
# Free the due week and give the scheduler a firing moment that has passed.
if last_week:
    with storage.get_engine().begin() as c:
        from sqlalchemy import text as _t
        c.execute(_t("DELETE FROM digest_runs WHERE iso_week=:w"), {"w": last_week})
os.environ["DIGEST_HOUR"] = "0"
if os.getenv("MATTERMOST_TOKEN") and os.getenv("MATTERMOST_CHANNEL_ID"):
    outcome = scheduler.tick()
    if outcome is None:
        print("   (nothing was due — check the clock)")
    else:
        check("tick posted the due week", outcome.status in ("posted", "empty"), True)
        row = storage.digest_week_status(outcome.iso_week)
        check("outcome recorded in digest_runs", row["status"] in ("posted", "skipped"),
              True)
        print(f"   week {outcome.iso_week} → {outcome.status} "
              f"(post {outcome.post_id})")
        check("a second tick does not re-post it", scheduler.tick(), None)
else:
    print("   skipped — no MATTERMOST_TOKEN/CHANNEL_ID in the environment")

# --- 7. a tick on a week that HAS activity really posts ------------------
#
# The week is *discovered* rather than assumed to be the current one: which weeks
# hold data depends on which seeder ran last (verify_engagement.py writes the
# current week, verify_engagement_backfill.py writes ten weeks in the past), and a
# check that only passes in one of those orders is worse than no check.
print("\n--- a tick on a week with activity posts and records it ---")
with storage.get_engine().connect() as c:
    live_weeks = sorted({row["iso_week"] for row in engagement.active_weeks(c)})
target = live_weeks[-1] if live_weeks else None

if not (os.getenv("MATTERMOST_TOKEN") and os.getenv("MATTERMOST_CHANNEL_ID")):
    print("   skipped — no credentials")
elif target is None:
    print("   skipped — no week in this database has any activity; run "
          "scripts/verify_engagement.py first")
else:
    print(f"   using {target}, which has activity")
    with storage.get_engine().begin() as c:
        from sqlalchemy import text as _t2
        c.execute(_t2("DELETE FROM digest_runs WHERE iso_week=:w"), {"w": target})
    real_due = scheduler.next_due_week
    scheduler.next_due_week = lambda *a, **k: target
    try:
        outcome = scheduler.tick()
        check("tick posted", outcome.status if outcome else None, "posted")
        check("a post id came back", bool(outcome and outcome.post_id), True)
        row = storage.digest_week_status(target)
        check("digest_runs says posted", row["status"], "posted")
        check("channel recorded", row["channel_id"],
              os.getenv("MATTERMOST_CHANNEL_ID"))
        print(f"   posted {target} → post id {outcome.post_id}")
        check("an immediate second tick is refused by the claim",
              scheduler.tick(), None)
    finally:
        scheduler.next_due_week = real_due

print()
if FAIL:
    print(f"❌ {len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("✅ scheduler verified end to end")
