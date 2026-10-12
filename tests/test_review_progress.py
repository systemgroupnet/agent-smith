"""The progress record: which reviewer-file pairs a run can skip, and when the marker moves.

Production evidence behind it: 61% of runs ended on the overall timeout, a timeout
held the commit marker, and an MR whose first run never finished cleanly had no
marker at all — so every re-run was a full review from the same deterministic
starting point, re-finding and re-posting the same leading files and never reaching
the tail. One file failed the time budget in 27 runs out of 27 and pinned its MR's
marker forever.

What is asserted here is the contract that fixes that: a re-run reviews exactly
what is left, per *pair* rather than per file, keyed on content rather than commit;
a pair that keeps failing at the same content is given up on so the marker can
advance; and nothing about the record can make a run skip something it should not
— an unreadable record, an unreadable notes list or a foreign note all degrade to
"review it again".
"""

import asyncio
import dataclasses
import subprocess
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from core.agents import deep_reviewer
from core.agents.deep_reviewer import order_by_prior_failures, review_changed_files
from core.utils import gitlab_helpers
from core.utils.gitlab_helpers import (
    MARKER_TOKEN,
    format_review_marker,
    get_review_marker,
    upsert_review_marker,
)
from core.utils.repo_clone import tree_blobs
from core.utils.review_progress import (
    ProgressRecord,
    abandoned,
    covers,
    decide,
    decode,
    encode,
    is_outage,
    merge,
    pending_pairs,
    settle,
)

SVC =("svc/order.go", "service_reviewer")
BIZ = ("svc/order.go", "service_business_reviewer")


# ── encoding ────────────────────────────────────────────────────────────────


def test_a_record_survives_the_round_trip_through_a_note():
    """Including a path that would close an HTML comment if it were written raw."""
    record = ProgressRecord(head="abc123", verbosity=2)
    record.done[("weird/-->/path.go", "service_reviewer")] = "b" * 40
    record.done[("سرویس/فایل.go", "sql_reviewer")] = "c" * 40
    record.fail[SVC] = ("d" * 40, 1)

    comment = encode(record)
    assert comment.startswith("<!-- code-review-agent:progress:v1 ")
    assert comment.count("-->") == 1, "the payload must not be able to end the comment"

    back, error = decode(f"<!-- {MARKER_TOKEN}=abc -->\n{comment}\nsome visible text")
    assert error is None
    assert back == record


def test_no_record_is_an_empty_record_and_not_an_error():
    record, error = decode(f"<!-- {MARKER_TOKEN}=abc123 -->\n🤖 reviewed up to `abc123`.")
    assert record.is_empty() and error is None, "a pre-feature marker is not a fault"


def test_garbage_is_an_empty_record_with_a_reason():
    for body in (
        "<!-- code-review-agent:progress:v1 !!!not base64!!! -->",
        "<!-- code-review-agent:progress:v1 aGVsbG8= -->",  # base64, but not zlib
        "<!-- code-review-agent:progress:v2 eJyrrgUAAXUA+Q== -->",  # a newer format
        encode(ProgressRecord()).replace(":v1 ", ":v1 x"),
    ):
        record, error = decode(body)
        assert record.is_empty(), body
        assert error, f"an unreadable record must be reported, not passed off as absent: {body}"


def test_a_record_claiming_another_version_inside_a_v1_comment_is_refused():
    import base64
    import json
    import zlib

    payload = base64.urlsafe_b64encode(
        zlib.compress(json.dumps({"v": 9, "done": [["a.go", "x", "b"]]}).encode())
    ).decode()
    record, error = decode(f"<!-- code-review-agent:progress:v1 {payload} -->")
    assert record.is_empty() and "version" in error


# ── what a run skips ────────────────────────────────────────────────────────


def test_one_of_two_service_reviewers_done_dispatches_only_the_other():
    """The reason the record is per pair: a run that truncates between a service
    file's two reviewers has reviewed half of it."""
    blobs = {"svc/order.go": "b1"}
    record = ProgressRecord(verbosity=0, done={SVC: "b1"})
    assert pending_pairs([SVC, BIZ], record, blobs, verbosity=0) == [BIZ]


def test_a_changed_file_is_reviewed_again():
    record = ProgressRecord(verbosity=0, done={SVC: "old"}, fail={BIZ: ("old", 1)})
    blobs = {"svc/order.go": "new"}
    assert pending_pairs([SVC, BIZ], record, blobs, verbosity=0) == [SVC, BIZ]

    merged = merge(record, completed=[], failed=[BIZ], blobs=blobs,
                   current_paths=["svc/order.go"], verbosity=0)
    assert SVC not in merged.done, "an entry for old content is dropped"
    assert merged.fail[BIZ] == ("new", 1), "the count restarts on new content"


def test_an_unknown_blob_never_skips():
    record = ProgressRecord(verbosity=0, done={SVC: "b1"})
    assert pending_pairs([SVC], record, None, verbosity=0) == [SVC]
    assert pending_pairs([SVC], record, {}, verbosity=0) == [SVC]


def test_a_record_from_a_quieter_level_is_not_trusted():
    """Reviewers record every severity but a run posts only its level's — a pair done
    at the default level had its mediums discarded, so -vv must look again."""
    blobs = {"svc/order.go": "b1"}
    quiet = ProgressRecord(verbosity=0, done={SVC: "b1"})
    loud = ProgressRecord(verbosity=2, done={SVC: "b1"})
    assert pending_pairs([SVC], quiet, blobs, verbosity=2) == [SVC]
    assert pending_pairs([SVC], loud, blobs, verbosity=0) == [], "louder covers quieter"

    merged = merge(quiet, completed=[], failed=[], blobs=blobs,
                   current_paths=["svc/order.go"], verbosity=2)
    assert not merged.done, "kept under the louder label it would claim coverage it lacks"


def test_a_vv_record_is_not_trusted_at_v():
    """Levels are not nested: -vv ranks highs and mediums down to 30, -v keeps every
    high. A high -vv ranked out was never posted, so -v must look again — which a
    comparison of level numbers (2 >= 1) used to wave through."""
    blobs = {"svc/order.go": "b1"}
    vv = ProgressRecord(verbosity=2, done={SVC: "b1"})
    assert pending_pairs([SVC], vv, blobs, verbosity=1) == [SVC]
    merged = merge(vv, completed=[], failed=[], blobs=blobs,
                   current_paths=["svc/order.go"], verbosity=1)
    assert not merged.done


def test_which_level_covers_which():
    """Derived from the level definitions; spelled out here so a change to a level
    that silently changes what a record is trusted for fails loudly."""
    expected = {          # recorded level -> the levels it covers
        0: {0},           # default: ranked highs, 10 — not -v's every high, 30
        1: {0, 1},        # -v: every high, 30
        2: {0, 2},        # -vv: ranked highs + mediums, 30 — not -v, not -vvv
        3: {0, 1, 2, 3},  # -vvv: every high and medium, 30 — covers everything
    }
    for recorded, covered in expected.items():
        for current in range(4):
            assert covers(recorded, current) == (current in covered), (recorded, current)
    assert not covers(9, 0), "a level this reviewer does not define covers nothing"

    blobs = {"svc/order.go": "b1"}
    vvv = ProgressRecord(verbosity=3, done={SVC: "b1"})
    default = ProgressRecord(verbosity=0, done={SVC: "b1"})
    for level in range(4):
        assert pending_pairs([SVC], vvv, blobs, verbosity=level) == [], level
        same = ProgressRecord(verbosity=level, done={SVC: "b1"})
        assert pending_pairs([SVC], same, blobs, verbosity=level) == [], level
    assert pending_pairs([SVC], default, blobs, verbosity=1) == [SVC], (
        "the default level posted ten ranked highs; -v posts every one")


def test_a_file_that_left_the_mr_drops_out_of_the_record():
    record = ProgressRecord(done={SVC: "b1"}, fail={BIZ: ("b1", 1)})
    merged = merge(record, completed=[], failed=[], blobs={"svc/order.go": "b1"},
                   current_paths=["other.go"], verbosity=0)
    assert merged.is_empty()


def test_a_pair_is_abandoned_after_two_failures_at_the_same_content():
    """The 27-of-27 file: it must stop pinning the marker."""
    blobs = {"svc/order.go": "b1", "svc/pay.go": "p1"}
    plan = [SVC, ("svc/pay.go", "service_reviewer")]
    paths = list(blobs)
    first = settle(ProgressRecord(), plan, completed=[("svc/pay.go", "service_reviewer")],
                   failed=[SVC], blobs=blobs, current_paths=paths,
                   verbosity=0, head="h1", max_failures=2)
    assert first.record.fail[SVC] == ("b1", 1)
    assert not first.settled and first.pending == [SVC]

    second = settle(first.record, plan, completed=[], failed=[SVC], blobs=blobs,
                    current_paths=paths, verbosity=0, head="h1", max_failures=2)
    assert second.newly_abandoned == {SVC}
    assert second.settled, "every pair is now done or given up on — the marker advances"

    nxt = decide(plan, second.record, blobs, verbosity=0, max_failures=2)
    assert nxt.abandoned == {SVC} and nxt.skip == set(plan)

    assert abandoned(second.record, {**blobs, "svc/order.go": "b2"}, 2) == set(), (
        "changing the file gives it a fresh start")
    assert abandoned(second.record, blobs, 0) == set(), "0 disables abandonment"


def test_a_run_that_completed_nothing_across_many_files_charges_nobody():
    """Two runs against a provider too slow to finish anything would otherwise
    abandon every pair and advance the marker over an MR nobody read."""
    plan = [(f"f{i}.go", "service_reviewer") for i in range(3)]
    blobs = {path: "b" for path, _ in plan}
    out = settle(ProgressRecord(), plan, completed=[], failed=plan, blobs=blobs,
                 current_paths=list(blobs), verbosity=0, head="h", max_failures=2)
    assert out.outage and not out.record.fail and not out.settled


def _settle_twice(completed, failed, plan):
    blobs = {path: "b" for path, _ in plan}
    record, outs = ProgressRecord(), []
    for _ in range(2):
        out = settle(record, plan, completed=completed, failed=failed, blobs=blobs,
                     current_paths=list(blobs), verbosity=0, head="h", max_failures=2)
        record = out.record
        outs.append(out)
    return outs


def test_a_slow_provider_that_finishes_only_the_small_files_charges_nobody():
    """The guard used to need zero completions, so two tiny files finishing was
    enough to charge the eight that timed out — and two such runs abandoned all
    eight and advanced the marker over code nobody read."""
    plan = [(f"f{i}.go", "service_reviewer") for i in range(10)]
    for out in _settle_twice(completed=plan[:2], failed=plan[2:], plan=plan):
        assert out.outage
        assert not out.record.fail and not out.newly_abandoned
        assert not out.settled and len(out.pending) == 8


def test_a_lone_failing_file_among_many_completed_is_still_charged_and_given_up_on():
    """The case abandonment exists for must survive the wider outage guard — both
    beside nine completions and when it is all a re-run has left to dispatch."""
    plan = [(f"f{i}.go", "service_reviewer") for i in range(10)]
    first, second = _settle_twice(completed=plan[:9], failed=plan[9:], plan=plan)
    assert not first.outage and first.record.fail[plan[9]] == ("b", 1)
    assert second.newly_abandoned == {plan[9]} and second.settled

    blobs = {path: "b" for path, _ in plan}
    rerun = settle(first.record, plan, completed=[], failed=plan[9:], blobs=blobs,
                   current_paths=list(blobs), verbosity=0, head="h", max_failures=2)
    assert not rerun.outage and rerun.newly_abandoned == {plan[9]} and rerun.settled


def test_the_outage_guard_needs_both_many_files_and_more_failures_than_completions():
    def pairs(*paths):
        return [(p, "service_reviewer") for p in paths]

    assert is_outage(pairs("a"), pairs("x", "y", "z", "w"))
    assert not is_outage(pairs("a", "b", "c"), pairs("x", "y", "z")), "a tie is charged"
    two_service_files = [(p, r) for p in ("x", "y")
                         for r in ("service_reviewer", "service_business_reviewer")]
    assert not is_outage([], two_service_files), (
        "below the file floor even with nothing completed — a short tail is charged")


def test_never_failed_files_go_first_and_a_files_reviewers_stay_together():
    plan = [("x.go", "service_reviewer"), ("x.go", "service_business_reviewer"),
            ("y.go", "service_reviewer"), ("y.go", "service_business_reviewer"),
            ("z.go", "sql_reviewer"), ("x.go", "sql_reviewer")]
    ordered = order_by_prior_failures(plan, {("y.go", "service_reviewer"): 2,
                                             ("z.go", "sql_reviewer"): 1})
    assert ordered == [
        ("x.go", "service_reviewer"), ("x.go", "service_business_reviewer"),
        ("x.go", "sql_reviewer"),
        ("z.go", "sql_reviewer"),
        ("y.go", "service_reviewer"), ("y.go", "service_business_reviewer"),
    ]


def test_blob_ids_come_from_the_checkout(git_origin):
    origin, _tip, _marker = git_origin
    blobs = tree_blobs(str(origin))
    expected = subprocess.run(["git", "hash-object", "f1.go"], cwd=origin,
                              capture_output=True, text=True).stdout.strip()
    assert blobs["f1.go"] == expected
    assert set(blobs) >= {"m1.txt", "f4.go"}


def test_blob_ids_from_a_directory_that_is_not_a_checkout_are_none(tmp_path):
    assert tree_blobs(str(tmp_path)) is None


# ── per-pair completion in the review pass ──────────────────────────────────


class Recorder(BaseChatModel):
    """Records one finding for whatever file it was asked about, then answers.

    Business-logic reviewers (recognised by a stubbed prompt) answer in prose instead
    when ``silent_business`` is set. ``delay`` slows the first call of every pair.
    ``raise_on`` names paths whose reviewer raises instead, the way a context-length
    400 does. ``asked`` collects the path of every pair that reached a model — typed
    ``Any`` so pydantic keeps the caller's list rather than validating a copy of it.
    """

    asked: Any = None
    delay: float = 0.0
    silent_business: bool = False
    raise_on: Any = None

    @property
    def _llm_type(self):
        return "recorder"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("async only")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        human = next(m for m in messages if getattr(m, "type", "") == "human")
        path = str(human.content).replace("Review", "", 1).strip()
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        recorded = any(isinstance(m, ToolMessage) and m.name == "record_findings"
                       for m in messages)
        if recorded:
            message = AIMessage(content="done")
        elif self.silent_business and "BUSINESS-STUB" in system:
            message = AIMessage(content="Looks fine to me.")
        else:
            if self.asked is not None:
                self.asked.append(path)
            if path in (self.raise_on or ()):
                raise ValueError("Error code: 400 - maximum context length exceeded")
            if self.delay:
                await asyncio.sleep(self.delay)
            message = AIMessage(content="", tool_calls=[{
                "name": "record_findings", "id": "r",
                "args": {"file_path": path, "findings": [
                    {"problem": f"bug in {path}", "line": 1, "severity": "high"}]}}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def _stub_business_prompt(monkeypatch):
    spec = deep_reviewer._SPECS_BY_NAME["service_business_reviewer"]
    monkeypatch.setitem(deep_reviewer._SPECS_BY_NAME, "service_business_reviewer",
                        dataclasses.replace(spec, prompt_getter=lambda ctx, instr: "BUSINESS-STUB"))


def test_one_reviewer_recording_does_not_complete_the_others_pair(checkout, monkeypatch):
    """The sink keys on the file, so before per-pair tracking the semantic reviewer
    recording made the whole file look reviewed while the business half never ran."""
    _stub_business_prompt(monkeypatch)
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch=None, model=Recorder(silent_business=True), provider_policy=False))

    assert result.failed_files == [], "file-level: something reported on it"
    assert result.completed_pairs == {SVC}
    assert result.failed_pairs == {BIZ}, "per pair: the business half did not"
    assert set(result.plan_pairs) == {SVC, BIZ}


def test_a_skipped_sql_second_opinion_stays_in_the_plan_but_is_not_dispatched(checkout):
    asked: list = []
    sql = ("svc/order.go", "sql_reviewer")
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch=None, sql_paths=["svc/order.go"], model=Recorder(asked=asked),
        provider_policy=False, skip_pairs={sql}))

    assert sql in result.plan_pairs, "skipped after add_sql_dispatches, not before"
    assert result.completed_pairs == {SVC, BIZ}
    assert sql not in result.completed_pairs | result.failed_pairs | result.never_dispatched


SQL_FILES = [(f"m/{name}.sql", "sql") for name in "abcd"]


def _review_and_settle(checkout, record, files, raise_on, blobs):
    """One run the way `main.py` does it: decide, review, settle on `failed_pairs`."""
    plan = [(path, "sql_reviewer") for path, _ in files]
    decision = decide(plan, record, blobs, verbosity=0, max_failures=2)
    asked: list = []
    result = asyncio.run(review_changed_files(
        classified=files, clone_root=str(checkout), source_branch=None,
        model=Recorder(asked=asked, raise_on=raise_on), provider_policy=False,
        skip_pairs=decision.skip, failure_counts=decision.failure_counts))
    out = settle(record, result.plan_pairs, result.completed_pairs, result.failed_pairs,
                 blobs, list(blobs), verbosity=0, head="h", max_failures=2)
    return result, out, asked


def test_a_reviewer_that_raises_on_one_file_every_run_is_given_up_on(checkout):
    """A raised pair used to be retried forever without being counted — a
    context-length 400 on one file held the marker on every run, the problem the
    record exists to remove. It is charged like a timeout now, and kept apart."""
    files = SQL_FILES[:3]
    blobs = {path: f"blob-{path}" for path, _ in files}
    bad = ("m/c.sql", "sql_reviewer")

    first, first_out, _ = _review_and_settle(checkout, ProgressRecord(), files,
                                             {"m/c.sql"}, blobs)
    assert first.errored_pairs == {bad} and bad in first.failed_pairs, (
        "raised pairs are failures, and still distinguishable as raised")
    assert not first_out.outage and first_out.record.fail[bad] == ("blob-m/c.sql", 1)
    assert not first_out.settled

    second, second_out, asked = _review_and_settle(checkout, first_out.record, files,
                                                   {"m/c.sql"}, blobs)
    assert asked == ["m/c.sql"], "only what is left is dispatched"
    assert second_out.newly_abandoned == {bad}
    assert second_out.settled, "given up on — the marker can advance"


def test_a_run_where_most_reviewers_raise_is_an_outage_and_charges_nobody(checkout):
    files = SQL_FILES
    blobs = {path: f"blob-{path}" for path, _ in files}
    raising = {"m/b.sql", "m/c.sql", "m/d.sql"}

    result, out, _ = _review_and_settle(checkout, ProgressRecord(), files, raising, blobs)
    assert {path for path, _ in result.errored_pairs} == raising
    assert result.completed_pairs == {("m/a.sql", "sql_reviewer")}
    assert out.outage and not out.record.fail, "a provider fault is nobody's file"
    assert ("m/a.sql", "sql_reviewer") in out.record.done and not out.settled


# ── the marker note ─────────────────────────────────────────────────────────


class FakeGitLab:
    """An MR's notes, in memory, newest-updated first like the real list call."""

    def __init__(self, user_id=7, notes=None, list_fails=False, user_fails=False):
        self.user_id = user_id
        self.notes = list(notes or [])
        self.list_fails = list_fails
        self.user_fails = user_fails
        self.writes: list = []

    def list_merge_request_notes(self, project_path, mr_iid):
        if self.list_fails:
            return None
        return [dict(n) for n in self.notes]

    def get_current_user(self):
        return None if self.user_fails else {"id": self.user_id, "username": "bot"}

    def create_merge_request_note(self, project_path, mr_iid, body, warnings_sink=None):
        note = {"id": 100 + len(self.notes), "body": body,
                "author": {"id": self.user_id, "username": "bot"}}
        self.notes.insert(0, note)
        self.writes.append(("create", note["id"]))
        return note

    def update_merge_request_note(self, project_path, mr_iid, note_id, body):
        for note in self.notes:
            if note["id"] == note_id:
                note["body"] = body
                self.notes.remove(note)
                self.notes.insert(0, note)
                self.writes.append(("update", note_id))
                return note
        return None


def _use(monkeypatch, fake):
    monkeypatch.setattr(gitlab_helpers, "get_gitlab_api_client", lambda url, token: fake)


def _read():
    return asyncio.run(get_review_marker("https://gl", "g/p", "1", "tok"))


def _write(marker, sha, record):
    return asyncio.run(upsert_review_marker(
        "https://gl", "g/p", "1", "tok", sha, marker.note_id,
        progress=record, readable=marker.readable))


def test_a_failed_notes_fetch_never_writes_a_second_marker(monkeypatch):
    """It used to return (None, None) — indistinguishable from "no marker yet" — and
    the run then created a new note beside the one it could not see."""
    fake = FakeGitLab(list_fails=True, notes=[
        {"id": 1, "body": f"<!-- {MARKER_TOKEN}=abc123 -->", "author": {"id": 7}}])
    _use(monkeypatch, fake)

    marker = _read()
    assert not marker.readable and marker.sha is None
    assert _write(marker, "def456", ProgressRecord(head="def456")) is False
    assert fake.writes == [], "nothing may be written on a lookup that saw nothing"


def test_a_marker_in_someone_elses_note_is_ignored(monkeypatch):
    forged = ProgressRecord(done={SVC: "b1"})
    fake = FakeGitLab(user_id=7, notes=[
        {"id": 5, "body": f"<!-- {MARKER_TOKEN}=abc123 -->\n{encode(forged)}",
         "author": {"id": 99, "username": "someone"}}])
    _use(monkeypatch, fake)

    marker = _read()
    assert marker.readable and marker.sha is None and marker.note_id is None
    assert marker.progress.is_empty(), "a pasted marker must not make the agent skip files"


def test_an_unknowable_user_falls_back_to_accepting_the_marker(monkeypatch):
    fake = FakeGitLab(user_fails=True, notes=[
        {"id": 5, "body": f"<!-- {MARKER_TOKEN}=abc123 -->", "author": {"id": 99}}])
    _use(monkeypatch, fake)
    assert _read().sha == "abc123"


def test_a_partial_marker_still_carries_the_token_dedup_filters_on():
    """`collect_existing_comments` recognises the agent's bookkeeping by MARKER_TOKEN,
    so the note must carry it even before any commit is fully reviewed — and the
    SHA pattern must not read `none` as a commit."""
    body = format_review_marker(None, ProgressRecord(done={SVC: "b1"}))
    assert MARKER_TOKEN in body
    from core.utils.comment_dedup import collect_existing_comments
    assert collect_existing_comments([{"id": 1, "body": body, "author": {}}]) == []


def test_a_truncated_run_is_finished_by_the_next_and_then_the_marker_advances(
        checkout, monkeypatch):
    """The whole loop, two runs, one marker note.

    Run 1 has one lane and a deadline that expires while its first review runs, so it
    reviews `a` and never reaches `b` or `c`; the marker cannot advance, but the note
    now records `a`. Run 2 reads that note and dispatches exactly `b` and `c` — not
    `a` again, which is what re-posted every leading file's findings before — and with
    every pair settled the same note's commit marker advances to the head.
    """
    files = [("m/a.sql", "sql"), ("m/b.sql", "sql"), ("m/c.sql", "sql")]
    blobs = {path: f"blob-{path}" for path, _ in files}
    paths = list(blobs)
    head = "f" * 40
    fake = FakeGitLab()
    _use(monkeypatch, fake)

    def run(asked, *, deadline=None, delay=0.0):
        marker = _read()
        plan = [(path, "sql_reviewer") for path, _ in files]
        decision = decide(plan, marker.progress, blobs, verbosity=0, max_failures=2)
        result = asyncio.run(review_changed_files(
            classified=files, clone_root=str(checkout), source_branch=None,
            model=Recorder(asked=asked, delay=delay), concurrency=1, deadline=deadline,
            provider_policy=False, skip_pairs=decision.skip,
            failure_counts=decision.failure_counts))
        out = settle(marker.progress, result.plan_pairs, result.completed_pairs,
                     result.failed_pairs, blobs, paths, verbosity=0, head=head,
                     max_failures=2)
        assert _write(marker, head if out.settled else marker.sha, out.record)
        return result, out

    first_asked: list = []
    first, first_out = run(first_asked, deadline=time.monotonic() + 0.5, delay=0.8)
    assert first.truncated
    assert first_asked == ["m/a.sql"]
    assert first.never_dispatched == {("m/b.sql", "sql_reviewer"), ("m/c.sql", "sql_reviewer")}
    assert not first_out.settled
    marker = _read()
    assert marker.sha is None, "the commit marker holds: the MR is not covered yet"
    assert ("m/a.sql", "sql_reviewer") in marker.progress.done

    second_asked: list = []
    second, second_out = run(second_asked)
    assert sorted(second_asked) == ["m/b.sql", "m/c.sql"], "exactly the remainder"
    assert not second.truncated and second.failed_files == []
    assert second_out.settled

    marker = _read()
    assert marker.sha == head, "every pair settled — the marker advances"
    assert [kind for kind, _ in fake.writes] == ["create", "update"], (
        "one note, written in place: never a second marker")
