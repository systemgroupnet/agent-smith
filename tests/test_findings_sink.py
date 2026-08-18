"""The only channel a finding travels on.

Every reviewer reports through `record_findings`, and the issue is normalised into
`ReviewIssue` shape there and then. Two properties are load-bearing: the
validation must be at least as forgiving as `ReviewIssue`'s (a tool call that
fail-validates loses the whole file's findings), and the sink doubles as the
coverage ledger, which is what distinguishes "reviewed and clean" from "never ran".
"""

import pytest

from core.agents.findings_sink import (
    MAX_FINDINGS_PER_FILE, FindingsSink, RecordedFinding, make_record_findings_tool,
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
