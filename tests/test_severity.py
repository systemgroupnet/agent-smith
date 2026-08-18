"""Severity calibration: one shared rubric, and an explicit demotion channel.

The first live run posted four comments, all high, two of them missing-test-coverage
essays. The cause was nine per-prompt severity ladders that had drifted — `html`
said "high: Accessibility violations", `test` said "high: Missing test coverage",
`service` listed "code duplication" among the highs. These tests hold the single
replacement rubric in place and cover the ranking stage's demotion channel.
"""

from core.agents.review_contexts import ReviewPromptContext
from core.prompts import (
    get_business_logic_reviewer_prompt, get_comment_reviewer_prompt,
    get_dataview_reviewer_prompt, get_html_reviewer_prompt, get_pbook_reviewer_prompt,
    get_scss_reviewer_prompt, get_semantic_reviewer_prompt, get_sql_reviewer_prompt,
    get_test_reviewer_prompt, get_typescript_reviewer_prompt,
)
from core.utils.comment_formatter import (
    apply_severity_corrections, get_severity, restore_issue_severities,
)
from dto.general import CommentReviewResponse, SeverityCorrection

import asyncio

import pytest

GETTERS = {
    "semantic": get_semantic_reviewer_prompt,
    "business": get_business_logic_reviewer_prompt,
    "sql": get_sql_reviewer_prompt,
    "test": get_test_reviewer_prompt,
    "typescript": get_typescript_reviewer_prompt,
    "html": get_html_reviewer_prompt,
    "scss": get_scss_reviewer_prompt,
    "pbook": get_pbook_reviewer_prompt,
    "dataview": get_dataview_reviewer_prompt,
}


def _issue(severity, path="a/x.go", line=5):
    return {"problem": "p", "suggestion": "", "file_name": path, "line": line,
            "severity": severity}


@pytest.mark.parametrize("name", sorted(GETTERS))
def test_every_reviewer_prompt_carries_exactly_one_rubric(name):
    text = GETTERS[name](ReviewPromptContext(), "")
    assert "Missing tests are medium" in text
    assert "Do not inflate" in text
    assert text.count("SEVERITY - assign it by consequence".replace("-", "\u2014")) <= 1
    assert text.count("assign it by consequence") == 1, "rubric must appear exactly once"


@pytest.mark.parametrize("name", sorted(GETTERS))
def test_the_drifted_ladders_are_gone(name):
    """These three lines produced every miscalibrated comment on the live run."""
    text = GETTERS[name](ReviewPromptContext(), "")
    assert "high**: Accessibility violations" not in text
    assert "high**: Missing test coverage" not in text
    assert "code duplication, performance issues" not in text


def test_the_comment_reviewer_is_told_how_to_correct_a_severity():
    text = get_comment_reviewer_prompt()
    assert "STEP 0b" in text
    assert "severity_corrections" in text
    assert "Only **lowering** a severity has any effect" in text
    assert "about three sentences" in text, "the live run produced 1000-char essays"


def test_a_demotion_below_the_level_drops_the_comment():
    """At the default verbosity 'this is actually a medium' means 'do not post it'."""
    kept, dropped = apply_severity_corrections(
        [_issue("high")],
        [SeverityCorrection(file_name="a/x.go", line=5, to_severity="medium",
                            why="missing tests")],
        ("high",))
    assert kept == []
    assert dropped[0][2:5] == ("high", "medium", "missing tests")


def test_the_same_demotion_is_posted_as_medium_when_medium_is_in_scope():
    kept, dropped = apply_severity_corrections(
        [_issue("high")],
        [SeverityCorrection(file_name="a/x.go", line=5, to_severity="medium", why="w")],
        ("high", "medium"))
    assert len(kept) == 1 and get_severity(kept[0]) == "medium" and not dropped


def test_a_promotion_is_ignored():
    """Inflation must not be reachable through the correction channel either."""
    kept, _ = apply_severity_corrections(
        [_issue("medium")],
        [SeverityCorrection(file_name="a/x.go", line=5, to_severity="high", why="w")],
        ("high", "medium"))
    assert get_severity(kept[0]) == "medium"


def test_a_correction_for_an_unknown_position_is_inert():
    kept, dropped = apply_severity_corrections(
        [_issue("high")],
        [SeverityCorrection(file_name="other/y.go", line=99, to_severity="low", why="w")],
        ("high",))
    assert len(kept) == 1 and not dropped


def test_the_basename_fallback_matches():
    kept, dropped = apply_severity_corrections(
        [_issue("high", path="deep/nested/x.go")],
        [SeverityCorrection(file_name="x.go", line=5, to_severity="low", why="style")],
        ("high",))
    assert kept == [] and len(dropped) == 1


def test_no_corrections_is_a_no_op():
    selected = [_issue("high"), _issue("high", path="b/y.go", line=9)]
    kept, dropped = apply_severity_corrections(selected, [], ("high",))
    assert kept == selected and not dropped


def test_restore_then_correct_is_what_makes_a_demotion_deliberate():
    """The model cannot demote by editing the field - the field is overwritten."""
    originals = [_issue("high")]
    selected = [_issue("low")]          # what a field-omitting response produces
    selected, changes = restore_issue_severities(selected, originals, ("high",))
    assert get_severity(selected[0]) == "high", "the accidental low must be undone"
    assert changes
    kept, dropped = apply_severity_corrections(
        selected,
        [SeverityCorrection(file_name="a/x.go", line=5, to_severity="medium", why="coverage")],
        ("high",))
    assert kept == [] and dropped[0][2:4] == ("high", "medium")


class _Fake:
    def __init__(self, response=None, boom=False):
        self.response, self.boom = response, boom

    async def run(self, text, config=None):
        if self.boom:
            raise RuntimeError("provider said no")
        return self.response


def test_the_call_site_hands_corrections_back(monkeypatch):
    import core.agents.file_reviewer as fr

    monkeypatch.setattr(fr, "create_comment_reviewer_agent", lambda **k: _Fake(
        CommentReviewResponse(
            selected_issues=[_issue("high")], filtered_count=0, reasoning="r",
            severity_corrections=[{"file_name": "a/x.go", "line": 5,
                                   "to_severity": "medium", "why": "tests"}])),
        raising=False)
    import core.agents.comment_reviewer_agent as cra
    monkeypatch.setattr(cra, "create_comment_reviewer_agent",
                        fr.create_comment_reviewer_agent, raising=False)
    selected, corrections, err = asyncio.run(fr.review_comments_with_agent([_issue("high")]))
    assert err is None and len(selected) == 1 and len(corrections) == 1
    assert corrections[0].to_severity == "medium"


def test_a_failed_ranking_stage_returns_no_corrections(monkeypatch):
    import core.agents.file_reviewer as fr
    import core.agents.comment_reviewer_agent as cra

    monkeypatch.setattr(fr, "create_comment_reviewer_agent",
                        lambda **k: _Fake(boom=True), raising=False)
    monkeypatch.setattr(cra, "create_comment_reviewer_agent",
                        fr.create_comment_reviewer_agent, raising=False)
    selected, corrections, err = asyncio.run(fr.review_comments_with_agent([_issue("high")]))
    assert err and corrections == []
    assert len(selected) == 1, "the deterministic fallback still selects"
