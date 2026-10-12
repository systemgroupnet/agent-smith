"""The only channel a finding travels on.

Every reviewer reports through `record_findings`, and the issue is normalised into
`ReviewIssue` shape there and then. Two properties are load-bearing: the
validation must be at least as forgiving as `ReviewIssue`'s (a tool call that
fail-validates loses the whole file's findings), and the sink doubles as the
coverage ledger, which is what distinguishes "reviewed and clean" from "never ran".
"""

import pytest

from core.agents.findings_sink import (
    MAX_FINDINGS_PER_FILE, FindingsSink, RecordedFinding, finding_file_name,
    make_record_findings_tool,
)
from core.utils.comment_formatter import extract_issues_for_review

LONG = "The call to Save() drops its returned error, so a failed write reports success."


def test_stores_findings_as_pipeline_shaped_dicts():
    sink = FindingsSink()
    sink.record("svc/order.go", [RecordedFinding(problem=LONG, line=42, severity="high")])
    issues = sink.findings["svc/order.go"]
    assert len(issues) == 1
    assert isinstance(issues[0], dict), "the pipeline reads dicts, not objects"
    assert issues[0]["line"] == 42
    assert issues[0]["file_name"] == "svc/order.go", "stamped from the call's file_path"


@pytest.mark.parametrize("raw,expected", [("42", 42), (42, 42), ("12-15", 12), ("n/a", None), ("", None)])
def test_line_coercion_matches_reviewissue(raw, expected):
    """This is the *first* validator a finding meets; a stricter copy loses findings."""
    assert RecordedFinding(problem="p", line=raw).line == expected


@pytest.mark.parametrize("raw,expected", [
    ("HIGH", "high"), ("critical", "high"), ("blocker", "high"),
    ("moderate", "medium"), ("warning", "medium"),
    ("nit", "low"), ("gibberish", "low"),
])
def test_severity_coercion(raw, expected):
    assert RecordedFinding(problem="p", severity=raw).severity == expected


def test_a_finding_with_no_text_is_rejected():
    sink = FindingsSink()
    sink.record("a.go", [RecordedFinding(problem="", line=9, severity="high")])
    assert sink.findings["a.go"] == [] and sink.rejected == 1


def test_an_empty_report_still_registers_the_file_as_reviewed():
    """The distinction the coverage check depends on."""
    sink = FindingsSink()
    sink.record("svc/clean.go", [])
    assert sink.reviewed_files == {"svc/clean.go"}
    assert sink.findings["svc/clean.go"] == []


def test_explicit_cross_file_finding_keeps_its_own_path():
    sink = FindingsSink()
    sink.record("svc/a.go", [RecordedFinding(problem=LONG, line=7, severity="high",
                                             file_name="svc/b.go")])
    assert sink.findings["svc/a.go"][0]["file_name"] == "svc/b.go"


def test_per_file_cap_is_enforced_and_counted():
    sink = FindingsSink()
    sink.record("f.go", [RecordedFinding(problem=f"p{i}", line=i, severity="high")
                         for i in range(MAX_FINDINGS_PER_FILE + 10)])
    assert len(sink.findings["f.go"]) == MAX_FINDINGS_PER_FILE
    assert sink.over_cap == 10, "truncation must never be silent"


def test_as_all_comments_feeds_the_existing_pipeline():
    sink = FindingsSink()
    sink.record("a.go", [RecordedFinding(problem=LONG, line=1, severity="high")])
    sink.record("b.go", [])
    sink.record("c.go", [RecordedFinding(problem=LONG, line=2, severity="medium")])
    all_comments = sink.as_all_comments()
    assert [p for p, _ in all_comments] == ["a.go", "b.go", "c.go"], "order preserved"
    assert len(extract_issues_for_review(all_comments, ("high",))) == 1
    assert len(extract_issues_for_review(all_comments, ("high", "medium"))) == 2


def test_the_tool_accepts_the_raw_dicts_a_model_emits():
    sink = FindingsSink()
    tool = make_record_findings_tool(sink)
    out = tool.invoke({"file_path": "x/y.go",
                       "findings": [{"problem": "boom", "line": 3, "severity": "high"}]})
    assert "Recorded 1" in out
    assert sink.findings["x/y.go"][0]["problem"] == "boom"


def test_an_empty_file_path_is_refused_with_guidance():
    sink = FindingsSink()
    msg = sink.record("", [RecordedFinding(problem=LONG, line=1)])
    assert "Rejected" in msg and sink.reviewed_files == set()


def test_a_checkout_rooted_path_is_the_same_file_as_the_plans_path():
    """The reviewers read from a filesystem rooted at the checkout, so a model that
    reports the path it read reports `/svc/pay.go` where the dispatch plan says
    `svc/pay.go`. Coverage is exact set membership, so before normalising, one live
    run reported `reviewed: {'/svc/pay.go'} | failed: ['svc/pay.go']` — the same file
    counted as both, which means a non-zero exit and a held marker for a file that
    was actually reviewed."""
    sink = FindingsSink()
    sink.record("/svc/pay.go", [RecordedFinding(problem="eligible is hardcoded", line=5,
                                                severity="high")])

    assert sink.reviewed_files == {"svc/pay.go"}
    assert [path for path, _issues in sink.as_all_comments()] == ["svc/pay.go"]


def test_the_other_spellings_of_the_same_path_also_collapse():
    sink = FindingsSink()
    for spelling in ("svc/pay.go", "./svc/pay.go", "/svc/pay.go"):
        sink.record(spelling, [RecordedFinding(problem="x", line=1, severity="low")])

    assert sink.reviewed_files == {"svc/pay.go"}, "one file, however it was spelled"
    assert len(sink.as_all_comments()) == 1


# ── a finding's own file_name ────────────────────────────────────────────────

#: Verbatim from this agent's review of its own MR, where the in-diff gate dropped
#: the finding as an unknown file.
PROSE_FILE_NAME = (
    "reviewer/core/prompts/comment_prompts.py, reviewer/core/prompts/common.py "
    "(VERIFY_BEFORE_REPORTING, check 4 has the carve-out this rule lacks; "
    "tests/test_severity.py asserts the current wording)"
)


@pytest.mark.parametrize("reported,expected", [
    # A plain path, bare or suffixed, passes untouched: the gate resolves those.
    ("svc/order.go", "svc/order.go"),
    ("order.go", "order.go"),
    ("", "svc/own.go"),
    (None, "svc/own.go"),
    # Prose: the first path it names, normalised.
    (PROSE_FILE_NAME, "reviewer/core/prompts/comment_prompts.py"),
    ("./svc/pay.go (the caller)", "svc/pay.go"),
    (".aid/service.md, rule 3", ".aid/service.md"),
    # Prose naming no path: the file under review, never a fragment of the prose.
    ("e.g. the caller, see 0.35 and v1.2", "svc/own.go"),
])
def test_a_findings_file_name_is_a_path(reported, expected):
    assert finding_file_name(reported, "svc/own.go") == expected


def test_a_prose_file_name_is_stored_as_the_path_it_names():
    sink = FindingsSink()
    sink.record("reviewer/core/prompts/common.py",
                [RecordedFinding(problem=LONG, line=123, severity="high",
                                 file_name=PROSE_FILE_NAME)])
    (issue,) = sink.findings["reviewer/core/prompts/common.py"]
    assert issue["file_name"] == "reviewer/core/prompts/comment_prompts.py"
