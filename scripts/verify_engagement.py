"""Verify the engagement layer against a real PostgreSQL.

Drives the **real** ingest paths (``insert_event``, ``record_webhook_event``), so
the canonical-score dedupe, the points ledger, the achievement awards and the
ISO-week awards are all exercised through the code a GitLab webhook actually
reaches — not through hand-inserted rows.

Not part of `pytest tests/`: it needs a database, and CI has none. Run it by hand
when changing any SQL in ``collector/engagement.py`` (see `tests/README.md`).

    docker run -d --name crg-verify-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=collector -p 55433:5432 postgres:18
    COLLECTOR_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:55433/collector \
      python scripts/verify_engagement.py
    docker rm -f crg-verify-pg

**Destructive** — it writes into whatever database you point it at. Point it at a
throwaway, never at the real collector.
"""
import os
import sys
from pathlib import Path

DEFAULT_URL = "postgresql+psycopg://postgres:test@127.0.0.1:55433/collector"
os.environ["COLLECTOR_DATABASE_URL"] = (
    os.getenv("COLLECTOR_DATABASE_URL") or DEFAULT_URL
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import engagement  # noqa: E402
import storage  # noqa: E402
from sqlalchemy import text  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: got {got!r}, want {want!r}")
    if not ok:
        FAIL.append(label)


storage.run_migrations()
print("migrations applied\n")

# --- a run posting 4 findings + a summary on alice's MR ----------------------
storage.insert_event({
    "schema_version": "1",
    "run_id": "run-1",
    "project_path": "acme/shop",
    "mr_id": "12017",
    "model": "glm-5.3",
    "status": "success",
    "mr_author_username": "alice",
    "posted_comments": [
        {"note_id": 1001, "discussion_id": "d1", "comment_type": "inline",
         "file": "a.go", "line": 10, "severity": "high", "model": "glm-5.3"},
        {"note_id": 1002, "discussion_id": "d2", "comment_type": "inline",
         "file": "b.go", "line": 20, "severity": "high", "model": "glm-5.3"},
        {"note_id": 1003, "discussion_id": "d3", "comment_type": "inline",
         "file": "c.go", "line": 30, "severity": "medium", "model": "glm-5.3"},
        {"note_id": 1004, "discussion_id": "d4", "comment_type": "note",
         "file": "d.go", "line": 40, "severity": "high", "model": "glm-5.3"},
    ],
    "posted_summary": {"note_id": 2001, "comment_type": "note", "model": "glm-5.3"},
})
# a second MR, bob's, 2 findings
storage.insert_event({
    "schema_version": "1",
    "run_id": "run-2",
    "project_path": "acme/shop",
    "mr_id": "12018",
    "model": "glm-5.3",
    "status": "success",
    "mr_author_username": "bob",
    "posted_comments": [
        {"note_id": 1101, "discussion_id": "e1", "comment_type": "inline",
         "file": "x.go", "line": 1, "severity": "high", "model": "glm-5.3"},
        {"note_id": 1102, "discussion_id": "e2", "comment_type": "inline",
         "file": "y.go", "line": 2, "severity": "high", "model": "glm-5.3"},
    ],
})
print("2 runs ingested (4+1 artifacts on alice's MR, 2 on bob's)\n")


def emoji_event(note_id, emoji, user_id, username, name, mr_iid, author_id=999,
                action="award"):
    return {
        "object_kind": "emoji",
        "event_type": action,
        "project_id": 42,
        "project": {"path_with_namespace": "acme/shop"},
        "user": {"id": user_id, "username": username, "name": name},
        "note": {"author_id": author_id},
        "merge_request": {"iid": mr_iid},
        "object_attributes": {
            "awardable_type": "Note",
            "awardable_id": note_id,
            "name": emoji,
            "action": action,
            "created_at": "2026-08-20T10:00:00Z",
        },
    }


def note_event(note_id, discussion_id, body, user_id, username, name, mr_iid):
    return {
        "object_kind": "note",
        "project_id": 42,
        "project": {"path_with_namespace": "acme/shop"},
        "user": {"id": user_id, "username": username, "name": name},
        "merge_request": {"iid": mr_iid},
        "object_attributes": {
            "id": note_id,
            "noteable_type": "MergeRequest",
            "discussion_id": discussion_id,
            "note": body,
            "type": "DiffNote",
            "action": "create",
            "created_at": "2026-08-20T11:00:00Z",
        },
    }


# --- alice rates all 4 of her own findings + the summary ---------------------
for nid, emo in ((1001, "four"), (1002, "five"), (1003, "three"), (1004, "two")):
    storage.record_webhook_event(emoji_event(nid, emo, 1, "alice", "Alice A", 12017))
storage.record_webhook_event(emoji_event(2001, "five", 1, "alice", "Alice A", 12017))

# the multi-emoji case: alice reconsiders 1003 (three → four) without revoking
storage.record_webhook_event(emoji_event(1003, "four", 1, "alice", "Alice A", 12017))

print("--- the canonical CTE dedupes the double-click ---")
with storage.get_engine().connect() as c:
    raw = c.execute(text(
        "SELECT COUNT(*) FROM comment_scores WHERE username='alice'")).scalar()
    ev = engagement.metric_events(c, "alice", "acme/shop")
check("raw comment_scores rows for alice (4 findings + 1 double-click)", raw, 5)
check("counted ratings for alice (4 findings + 1 summary, deduped)",
      len(ev["ratings"]), 5)

print("\n--- summary rating routed to summary_scores, not comment_scores ---")
with storage.get_engine().connect() as c:
    in_summary = c.execute(text(
        "SELECT COUNT(*) FROM summary_scores WHERE note_id=2001")).scalar()
    in_comments = c.execute(text(
        "SELECT COUNT(*) FROM comment_scores WHERE note_id=2001")).scalar()
check("summary rating in summary_scores", in_summary, 1)
check("summary rating NOT in comment_scores", in_comments, 0)

# --- bob rates one of alice's findings (peer) and replies -------------------
storage.record_webhook_event(emoji_event(1001, "five", 2, "bob", "Bob B", 12017))
r = storage.record_webhook_event(note_event(
    5001, "d1", "Good catch, this would have crashed on nil orders.",
    2, "bob", "Bob B", 12017))
check("bob's reply recorded", r.get("status"), "recorded")
# a token follow-up in the same thread — should earn 1, not 5
storage.record_webhook_event(note_event(5002, "d1", "thanks", 2, "bob", "Bob B", 12017))
# a reply in a thread we don't own → ignored
r2 = storage.record_webhook_event(note_event(
    5003, "unknown-discussion", "unrelated chatter", 2, "bob", "Bob B", 12017))
check("reply outside our threads ignored", r2.get("status"), "ignored")

print("\n--- points ledger ---")
with storage.get_engine().connect() as c:
    rows = c.execute(text(
        f"""SELECT username, action, base_points, peer_bonus, speed_bonus, points
            FROM ({engagement.POINTS_LEDGER_SQL}) l
            ORDER BY username, action, note_id"""
    )).mappings().all()
    for row in rows:
        print("   ", dict(row))

pts = {r["username"]: r for r in storage.list_engagement_points()}
# alice: 4 findings x2 = 8, +1 summary = 1, no peer bonus (own MR),
# speed bonus on all 5 (rated within 24h of ingest) = +5  → 14
check("alice points", pts["alice"]["points"], 14)
check("alice ratings counted", pts["alice"]["ratings"], 5)
# bob: 1 finding rating = 2 +1 peer +1 speed = 4; substantive first reply = 5;
# token follow-up in same thread = 1  → 10
check("bob points", pts["bob"]["points"], 10)
check("bob replies counted", pts["bob"]["replies"], 2)
check("alice level", pts["alice"]["level"], "Bronze")

print("\n--- achievements ---")
ach = storage.list_achievements()
by_user = {}
for a in ach:
    by_user.setdefault(a["username"], set()).add(a["achievement_key"])
print("   alice:", sorted(by_user.get("alice", [])))
print("   bob:  ", sorted(by_user.get("bob", [])))
check("alice got first_rating", "first_rating" in by_user.get("alice", set()), True)
check("alice got fast_10? (only 5 ratings)",
      "fast_10" in by_user.get("alice", set()), False)
# alice rated all 4 findings on her own MR herself → Clean Sweep
check("alice got perfect_1 (clean sweep on own MR)",
      "perfect_1" in by_user.get("alice", set()), True)
check("bob got first_reply", "first_reply" in by_user.get("bob", set()), True)
check("bob did NOT get perfect_1 (his own MR is unrated)",
      "perfect_1" in by_user.get("bob", set()), False)
check("bob got no peer_10 yet (1 peer rating)",
      "peer_10" in by_user.get("bob", set()), False)

print("\n--- idempotency: re-fire the same rating, and recompute ---")
before = len(storage.list_achievements())
storage.record_webhook_event(emoji_event(1001, "four", 1, "alice", "Alice A", 12017))
rec = storage.recompute_achievements()
after = len(storage.list_achievements())
check("recompute awards nothing new", after, before)
check("recompute saw both participants", rec["participants"], 2)

print("\n--- revoke removes the rating and its points ---")
storage.record_webhook_event(
    emoji_event(1002, "five", 1, "alice", "Alice A", 12017, action="revoke"))
pts2 = {r["username"]: r for r in storage.list_engagement_points()}
check("alice points drop after revoke", pts2["alice"]["points"], 11)
check("alice keeps her badges after revoke (ratchet)",
      "first_rating" in {a["achievement_key"] for a in storage.list_achievements()
                         if a["username"] == "alice"}, True)

print("\n--- self-award (reviewer's own seed) still ignored ---")
r3 = storage.record_webhook_event(
    emoji_event(1101, "five", 999, "aid-bot", "AID Bot", 12018, author_id=999))
check("self-award ignored", r3.get("status"), "ignored")

print("\n--- windowed points (since) ---")
w = storage.list_engagement_points(since="2099-01-01T00:00:00+00:00")
check("future window yields nobody", len(w), 0)

print("\n--- weekly awards, keyed on the ISO week ---")
import datetime as _dt
this_week = engagement.iso_week_key(_dt.datetime.now(_dt.timezone.utc))
res = storage.refresh_weekly_awards()
print("   refresh:", res)
rows = storage.list_weekly_awards()
for r in rows:
    print(f"   {r['iso_week']} {r['emoji']} {r['label']:14} "
          f"{r['username']:6} {r['value']} {r['unit']}")
cats = {r["category"]: r for r in rows if r["iso_week"] == this_week}
check("this week's awards were settled", bool(cats), True)
check("top rater is alice (5 ratings vs bob's 1)",
      cats.get("top_rater", {}).get("username"), "alice")
check("best feedback is bob (only substantive replier)",
      cats.get("best_feedback", {}).get("username"), "bob")
check("week key format", this_week[:5] + this_week[5], this_week[:6])
check("week_start is that week's Monday",
      _dt.date.fromisoformat(rows[0]["week_start"]).weekday(), 0)
check("fastest needs 3+ ratings, so bob (1) cannot win it",
      cats.get("fastest", {}).get("username"), "alice")
# completionist: alice has 4 findings on her MR but revoked one rating → 3/4
check("completionist is alice", cats.get("completionist", {}).get("username"), "alice")

print("\n--- awards are idempotent per ISO week ---")
before = len(storage.list_weekly_awards())
storage.refresh_weekly_awards(iso_week=this_week)
storage.refresh_weekly_awards()
check("re-settling adds no duplicate rows", len(storage.list_weekly_awards()), before)

print("\n--- badges are dated to the crossing event, not to now ---")
ach_rows = {a["achievement_key"]: a for a in storage.list_achievements()
            if a["username"] == "alice"}
fr = ach_rows.get("first_rating", {})
print("   first_rating awarded_at:", fr.get("awarded_at"),
      "progress:", fr.get("progress_value"))
check("first_rating has a crossing timestamp", bool(fr.get("awarded_at")), True)
check("first_rating progress_value is the counter at unlock",
      fr.get("progress_value") is not None, True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("✅ all engagement checks passed")
