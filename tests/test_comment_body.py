"""A posted comment always says something.

Production symptom: comments arriving on a merge request whose entire body was the
severity badge —

    **🔴 High severity issue**

— a red dot next to someone's diff that costs them a click and tells them nothing.

The cause is the same door the all-``low`` severity bug came through. The comment
reviewer *synthesizes* ``selected_issues``, ``ReviewIssue.problem`` defaults to
``""``, and nothing between that stage and the POST looked at the text. Findings are
guarded at their source (``FindingsSink.record`` rejects a finding with no
description) and the review summary is guarded by ``ReviewSummary.is_empty``; the
ranking stage's output was the one unguarded boundary, and it is the only one that
*rewrites* the text.

These tests hold the recovery in place, and its one asymmetry against the severity
restore: a severity has a defensible fallback, a description does not.
"""

from core.agents.review_contexts import ReviewPromptContext
from core.prompts import (
    get_business_logic_reviewer_prompt, get_comment_reviewer_prompt,
    get_dataview_reviewer_prompt, get_e2e_reviewer_prompt, get_html_reviewer_prompt,
    get_pbook_reviewer_prompt, get_rules_reviewer_prompt,
    get_scss_reviewer_prompt, get_semantic_reviewer_prompt, get_sql_reviewer_prompt,
    get_test_reviewer_prompt, get_typescript_reviewer_prompt,
)
from core.utils.comment_formatter import (
    comment_body_has_text, format_single_issue, restore_issue_problems,
    restore_issue_severities,
)
from core.agents.findings_sink import FindingsSink, RecordedFinding
from dto.general import CommentReviewResponse, ReviewSummary

import pytest


def _orig(problem, path="svc/order.go", line=42, severity="high"):
    return {"problem": problem, "suggestion": "", "file_name": path, "line": line,
            "severity": severity}


def _selected(**fields):
    """What the ranking stage returns, through the real lenient model."""
    payload = {"selected_issues": [fields], "filtered_count": 0, "reasoning": "r"}
    return CommentReviewResponse.model_validate(payload).selected_issues


# ── the symptom ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("problem", ["", "   ", "\n\t "])
def test_an_empty_problem_renders_a_comment_that_is_only_a_badge(problem):
    """The bug, stated as a fact about the formatter — this is what got posted."""
    body = format_single_issue(_orig(problem), applyable=True)
    assert body.strip() == "**🔴 High severity issue**"


def test_a_suggestion_does_not_rescue_an_empty_problem():
    """A fix block with no explanation is a patch someone is asked to trust blind."""
    issue = _orig("")
    issue["suggestion"] = "return nil, err"
    body = format_single_issue(issue, applyable=True)
    assert "severity issue" in body and "return nil, err" in body
    # Nothing between the badge and the fix says what is wrong.
    head = body.split("**Suggested Fix:**")[0]
    assert head.replace("**🔴 High severity issue**", "").strip() == ""


# ── the recovery ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("problem", ["", "   "])
def test_the_reviewers_text_comes_back_on_a_positional_match(problem):
    selected = _selected(file_name="svc/order.go", line=42, problem=problem)
    kept, restored, dropped = restore_issue_problems(
        selected, [_orig("nil deref on `Order.Customer`")])
    assert not dropped and restored == [("svc/order.go", 42)]
    assert kept[0].problem == "nil deref on `Order.Customer`"
    assert "nil deref" in format_single_issue(kept[0])


def test_a_bare_basename_still_matches():
    """Same fallback the severity restore has: the model may drop the directory."""
    selected = _selected(file_name="order.go", line=42, problem="")
    kept, restored, _ = restore_issue_problems(
        selected, [_orig("error swallowed", path="deep/svc/order.go")])
    assert restored and kept[0].problem == "error swallowed"


def test_a_merged_comment_inherits_the_most_severe_findings_text():
    """Two findings on one line: the text must match the badge the reader sees."""
    selected = _selected(file_name="svc/order.go", line=42, problem="")
    kept, _, _ = restore_issue_problems(selected, [
        _orig("cosmetic naming", severity="medium"),
        _orig("nil deref crashes checkout", severity="high"),
    ])
    assert kept[0].problem == "nil deref crashes checkout"


def test_the_first_finding_wins_a_tie():
    """Input is ordered highs-first in file order, so earliest is what was read first."""
    selected = _selected(file_name="svc/order.go", line=42, problem="")
    kept, _, _ = restore_issue_problems(
        selected, [_orig("first"), _orig("second")])
    assert kept[0].problem == "first"


# ── the asymmetry against the severity restore ───────────────────────────────

def test_an_unrecoverable_empty_problem_is_dropped_not_invented():
    """restore_issue_severities falls back to the most severe level in scope,
    because *some* badge has to be printed. There is no equivalent for text, so
    the comment goes rather than getting a sentence nobody wrote."""
    selected = _selected(file_name="ghost.go", line=1, problem="")
    kept, restored, dropped = restore_issue_problems(selected, [_orig("unrelated")])
    assert kept == [] and not restored and dropped == [("ghost.go", 1)]


def test_a_findings_severity_is_still_invented_where_its_text_is_not():
    """The two backstops differ on purpose; pin both halves in one place."""
    selected = _selected(file_name="rewritten.go", line=99, problem="")
    kept, _ = restore_issue_severities(selected, [_orig("t")], ("high",))
    assert kept[0].severity == "high", "a badge has to be printed, so it is guessed"
    kept, _, dropped = restore_issue_problems(kept, [_orig("t")])
    assert kept == [] and dropped, "a description is not guessed"


# ── what must not change ─────────────────────────────────────────────────────

def test_a_comment_with_text_is_untouched():
    selected = _selected(file_name="svc/order.go", line=42, problem="real text")
    kept, restored, dropped = restore_issue_problems(selected, [_orig("something else")])
    assert not restored and not dropped
    assert kept[0].problem == "real text", "the model's own synthesis is authoritative"


def test_no_originals_still_drops_rather_than_posting_an_empty_comment():
    kept, _, dropped = restore_issue_problems(
        _selected(file_name="svc/order.go", line=42, problem=""), [])
    assert kept == [] and dropped


def test_an_empty_selection_is_a_no_op():
    assert restore_issue_problems([], [_orig("t")]) == ([], [], [])


# ── the other two boundaries, which were already guarded ─────────────────────

def test_the_sink_still_refuses_a_finding_with_no_description():
    """The reviewers' own channel is the first guard and must stay one, or the
    positional recovery above would have nothing to recover from."""
    sink = FindingsSink()
    sink.record("svc/order.go", [
        RecordedFinding(problem="   ", severity="high", line=1),
        RecordedFinding(problem="real", severity="high", line=2),
    ])
    assert sink.rejected == 1
    assert [f["problem"] for f in sink.findings["svc/order.go"]] == ["real"]


def test_an_all_empty_summary_is_never_posted():
    assert ReviewSummary().is_empty()
    assert ReviewSummary(overview="  ").is_empty()
    assert not ReviewSummary(key_changes=["added a thing"]).is_empty()


def test_the_comment_reviewer_is_told_the_field_is_mandatory():
    text = get_comment_reviewer_prompt()
    assert "**This field is never empty**" in text
    assert "**Every issue in `selected_issues` must carry one.**" in text


# ── the final cross-check: the artifact, not a field ─────────────────────────
#
# Every guard above protects one stage against one known failure. This one asks
# the reader's own question — does the thing about to be posted contain a
# sentence — so it holds for a stage nobody has thought of yet.

@pytest.mark.parametrize("problem", ["", "   ", "\n\t "])
def test_a_badge_only_body_is_refused(problem):
    assert not comment_body_has_text(format_single_issue(_orig(problem)))


def test_a_body_with_a_sentence_is_allowed():
    assert comment_body_has_text(format_single_issue(_orig("nil deref on checkout")))


def test_the_check_is_narrow_enough_to_keep_a_fix_with_no_prose():
    """A suggestion *is* information. Discarding a real fix to enforce a
    formatting rule trades a shipped defect for a tidied comment — that case is
    handled upstream by restore_issue_problems, where it can be handled properly."""
    issue = _orig("")
    issue["suggestion"] = "return nil, err"
    assert comment_body_has_text(format_single_issue(issue))


def test_nothing_at_all_is_refused():
    assert not comment_body_has_text("")
    assert not comment_body_has_text(None)
    assert not comment_body_has_text("**🔴 High severity issue**")


# ── the reviewers are told at their own end, too ─────────────────────────────

REVIEWER_PROMPTS = {
    "semantic": get_semantic_reviewer_prompt,
    "business": get_business_logic_reviewer_prompt,
    "sql": get_sql_reviewer_prompt,
    "test": get_test_reviewer_prompt,
    "e2e": get_e2e_reviewer_prompt,
    "rules": get_rules_reviewer_prompt,
    "typescript": get_typescript_reviewer_prompt,
    "html": get_html_reviewer_prompt,
    "scss": get_scss_reviewer_prompt,
    "pbook": get_pbook_reviewer_prompt,
    "dataview": get_dataview_reviewer_prompt,
}


@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_every_reviewer_is_told_to_drop_a_finding_with_no_description(name):
    """Nine prompts, one rule, exactly once each — the same no-drift guarantee
    SEVERITY_RUBRIC and NO_COMPILE_ERRORS_RULE get, and for the same reason: the
    nine drifted ladders they replaced are why this file's sibling exists."""
    text = REVIEWER_PROMPTS[name](ReviewPromptContext(), "")
    assert text.count("re-read every finding and drop any whose") == 1


@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_no_reviewer_is_left_able_to_read_padding_as_thoroughness(name):
    """The rule has to say what to do *instead*, or "never record an empty
    finding" reads as "find something to say about every file"."""
    text = REVIEWER_PROMPTS[name](ReviewPromptContext(), "")
    assert "`record_findings` with an empty list is a correct and expected answer" in text
    assert "Never pad a report" in text


# ── plain language: the text has to be readable, not just present ────────────

@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_every_reviewer_is_told_to_write_plain_english(name):
    text = REVIEWER_PROMPTS[name](ReviewPromptContext(), "")
    assert text.count("**Write `problem` in plain, simple English.**") == 1


def test_the_comment_reviewer_rewrites_into_plain_english():
    """It is the last writer before posting, so a rule only the reviewers hear
    is undone by a ranking stage that copies dense input verbatim."""
    text = get_comment_reviewer_prompt()
    assert text.count("**Write `problem` in plain, simple English.**") == 1
    assert "rewrite dense or wordy input into plain English" in text


def test_simple_is_not_allowed_to_mean_vague():
    from core.prompts.common import PLAIN_LANGUAGE_RULE
    assert "Simple means easy to read, not less precise." in PLAIN_LANGUAGE_RULE
    assert "keep every symbol in backticks" in PLAIN_LANGUAGE_RULE
