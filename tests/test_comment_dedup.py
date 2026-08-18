"""Duplicate suppression — the only stage that reads what is already on the MR.

Everything upstream judges a finding on its own merits. This asks whether it has
already been said, and it must fail toward *posting*: a duplicate costs a reader
two seconds, a dropped finding is a defect that ships.
"""

import asyncio

import core.agents.dedup_agent as da
from core.agents.dedup_agent import _finish, _read_model_drops, deduplicate_comments
from core.utils.comment_dedup import (
    apply_drops, build_dedup_input, collect_existing_comments, deterministic_duplicates,
    normalize_comment_text, resolve_drops, target_candidate,
)
from dto.general import CommentDedupResponse, ReviewIssue

LONG = ("The call to `Save()` ignores its returned error, so a failed write is silently "
        "treated as a success and the caller commits the transaction anyway.")


def _notes():
    return [
        {"id": 1, "system": True, "body": "assigned to @bob", "author": {"username": "bob"}},
        {"id": 2, "body": "<!-- code-review-agent:last-reviewed-sha=abc --> reviewed",
         "author": {"username": "bot"}},
        {"id": 3, "body": "<!-- code-review-agent:review-summary -->\nSummary",
         "author": {"username": "bot"}},
        {"id": 4, "body": "**🔴 High severity issue**\n" + LONG, "author": {"username": "bot"},
         "position": {"new_path": "svc/order.go", "new_line": 42}, "resolved": False},
        {"id": 5, "body": "", "author": {"username": "alice"}},
        {"id": 6, "body": "we do this deliberately", "author": {"username": "alice"},
         "position": {"new_path": "svc/order.go", "new_line": 42}, "resolved": True},
    ]


def test_normalisation_bridges_posted_and_candidate_markup():
    """A posted note went through format_single_issue; the candidate did not."""
    candidate = normalize_comment_text(LONG + "\n```go\nx := 1\n```")
    posted = normalize_comment_text("**🔴 High severity issue**\n" + LONG)
    assert "```" not in candidate and "x 1" not in candidate, "fences dropped"
    assert candidate in posted, "the same text must match across both renderings"


def test_bookkeeping_and_system_notes_are_excluded():
    existing = collect_existing_comments(_notes())
    assert [n["note_id"] for n in existing] == [4, 6], "marker, summary, system, empty all out"
    assert existing[0]["file"] == "svc/order.go" and existing[0]["line"] == 42
    assert existing[1]["resolved"] is True, "a resolved thread is still a duplicate"


def test_pre_pass_matches_textual_identity_only():
    existing = collect_existing_comments(_notes())
    candidates = [
        ReviewIssue(problem=LONG, file_name="svc/order.go", line=42, severity="high"),
        ReviewIssue(problem=LONG.replace("`", ""), file_name="order.go", line=42, severity="high"),
        ReviewIssue(problem=LONG, file_name="svc/other.go", line=42, severity="high"),
        {"problem": "Unbounded loop over the result set", "file_name": "svc/order.go",
         "line": 90, "severity": "high"},
    ]
    drops = deterministic_duplicates(candidates, existing)
    assert drops[0][0] == "existing:4", "same text already posted at this position"
    assert drops[1][0] == "candidate:0", "same text as an earlier candidate"
    assert 2 not in drops, "same class at a DIFFERENT file is not a duplicate"
    assert 3 not in drops


def test_resolve_drops_never_empties_a_group():
    """A model handed a drop list emits '0 duplicates 1' and '1 duplicates 0' easily."""
    assert resolve_drops({0: 1, 1: 0}) == {1}, "mutual duplicates must leave one posted"
    assert resolve_drops({2: 1, 1: 2, 5: None}) == {2, 5}
    assert resolve_drops({0: 1, 1: 2}) == {0, 1}, "a chain ending on a survivor drops both"


def test_target_parsing_is_lenient_but_bounded():
    assert target_candidate("candidate:3", 5) == 3
    assert target_candidate("candidate 3", 5) == 3
    assert target_candidate("existing:99", 5) is None, "anchored outside the batch"
    assert target_candidate("candidate:9", 5) is None, "out of range"
    assert target_candidate("", 5) is None


def test_malformed_drop_entries_are_discarded_not_guessed():
    candidates = [{"problem": "p", "file_name": "a.go", "line": i, "severity": "high"}
                  for i in range(4)]
    response = CommentDedupResponse(duplicates=[
        {"candidate_id": "2", "duplicate_of": "candidate:3", "reason": "same"},
        {"candidate_id": 99, "duplicate_of": "candidate:0"},   # out of range
        {"candidate_id": "abc", "duplicate_of": "x"},          # unreadable -> -1
        {"candidate_id": 1, "duplicate_of": "candidate:1"},    # self-reference
    ])
    assert set(_read_model_drops(response, candidates, {}, 2)) == {2}


def test_dropping_everything_with_an_empty_mr_is_impossible():
    """With nothing posted, every duplicate is between candidates — one must survive."""
    candidates = [{"problem": "p", "file_name": "a.go", "line": i, "severity": "high"}
                  for i in range(4)]
    response = CommentDedupResponse(duplicates=[
        {"candidate_id": i, "duplicate_of": "existing:1"} for i in range(4)])
    assert _read_model_drops(response, candidates, {}, 0) == {}, "discard the response whole"
    assert len(_read_model_drops(response, candidates, {}, 3)) == 4, "valid when the MR has comments"


def test_apply_drops_preserves_order_and_identity():
    candidates = [{"problem": f"p{i}", "file_name": "a.go", "line": i} for i in range(3)]
    kept, records = apply_drops(candidates, {1: ("candidate:0", "r")})
    assert kept[0] is candidates[0] and kept[1] is candidates[2]
    assert records[0]["issue"] is candidates[1]


def test_payload_states_the_no_existing_comments_case():
    candidates = [ReviewIssue(problem=LONG, file_name="a.go", line=1, severity="high")]
    payload, omitted = build_dedup_input(candidates, [])
    assert "must be between two candidates" in payload and omitted == 0


class _Fake:
    def __init__(self, response=None, boom=False):
        self.response, self.boom, self.seen = response, boom, None

    async def run(self, payload, config=None):
        self.seen = payload
        if self.boom:
            raise RuntimeError("provider said no")
        return self.response


def _candidates():
    return [
        ReviewIssue(problem=LONG, file_name="api/handler.go", line=10, severity="high"),
        ReviewIssue(problem="The retry loop has no backoff and no ceiling at all here.",
                    file_name="api/client.go", line=55, severity="high"),
        {"problem": LONG + " (restated)", "file_name": "api/handler.go", "line": 10,
         "severity": "high"},
    ]


def test_flags_only_what_the_agent_names(monkeypatch):
    agent = _Fake(CommentDedupResponse(
        duplicates=[{"candidate_id": 2, "duplicate_of": "candidate:0", "reason": "same defect"}],
        reasoning="one restatement"))
    monkeypatch.setattr(da, "create_comment_dedup_agent", lambda *a, **k: agent)
    kept, dropped, err = asyncio.run(deduplicate_comments(_candidates(), []))
    assert err is None and len(kept) == 2 and len(dropped) == 1
    assert dropped[0]["index"] == 2


def test_agent_failure_keeps_the_exact_matches_and_posts_the_rest(monkeypatch):
    existing = [{"note_id": 7, "author": "bot", "body": LONG, "file": "api/handler.go",
                 "line": 10, "resolved": False}]
    monkeypatch.setattr(da, "create_comment_dedup_agent", lambda *a, **k: _Fake(boom=True))
    kept, dropped, err = asyncio.run(deduplicate_comments(_candidates(), existing))
    assert err and err.startswith("comment dedup agent failed")
    assert [d["duplicate_of"] for d in dropped] == ["existing:7"], "textual identity still stands"
    assert len(kept) == 2


def test_a_none_response_does_not_crash_the_run(monkeypatch):
    """with_structured_output yields None when the model skips the schema tool.

    This crashed a real run at the last stage before posting.
    """
    monkeypatch.setattr(da, "create_comment_dedup_agent", lambda *a, **k: _Fake(None))
    kept, dropped, err = asyncio.run(deduplicate_comments(_candidates(), []))
    assert err and "no structured output" in err
    assert len(kept) >= 1, "must fail toward posting"


def test_single_candidate_and_empty_mr_short_circuits(monkeypatch):
    called = []

    class Boom(_Fake):
        async def run(self, payload, config=None):
            called.append(1)
            raise AssertionError("must not be called")

    monkeypatch.setattr(da, "create_comment_dedup_agent", lambda *a, **k: Boom())
    one = [ReviewIssue(problem=LONG, file_name="a.go", line=1, severity="high")]
    kept, dropped, err = asyncio.run(deduplicate_comments(one, []))
    assert kept == one and not dropped and err is None and not called


def test_empty_candidate_list_is_a_no_op():
    kept, dropped, err = asyncio.run(deduplicate_comments([], [{"note_id": 1, "body": "x"}]))
    assert kept == [] and dropped == [] and err is None
