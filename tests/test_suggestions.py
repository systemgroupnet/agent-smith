"""An Apply button only ever commits a fix that fits the line it replaces.

Production replies on posted comments: "nice catch but wrong suggestion"; "the
suggested change leads to an error"; "applying the code suggestion was wrong
syntactically"; "it causes syntax error in my query". Two causes:

* ``format_single_issue`` always emitted ```` ```suggestion:-0+0 ````, which
  replaces exactly **one** line with every line of the block — so a multi-line
  rewrite anchored on its first line left the old body and its closing brace in
  place, and Apply committed broken code.
* The comment reviewer re-emits ``suggestion`` freely and nothing put the
  reviewers' own back, so one finding's fix could land on another finding's line.

These tests hold the single-line rule, the anchor checks, the restore, and the
sanitizer the whole path rests on (which had no tests of its own).
"""

import pytest

from core.utils.comment_formatter import (
    anchor_suggestion, comment_body_has_text, diff_line_text, format_single_issue,
    restore_issue_suggestions, sanitize_suggestion,
)
from core.utils.gitlab_helpers import build_diff_position_map
from dto.general import CommentReviewResponse

APPLY = "```suggestion:-0+0"


def _issue(problem="nil deref on `order.Customer`", suggestion="", path="svc/order.go",
           line=42, severity="high"):
    return {"problem": problem, "suggestion": suggestion, "file_name": path,
            "line": line, "severity": severity}


def _selected(**fields):
    """What the ranking stage returns, through the real lenient model."""
    payload = {"selected_issues": [fields], "filtered_count": 0, "reasoning": "r"}
    return CommentReviewResponse.model_validate(payload).selected_issues


# ── sanitize_suggestion ──────────────────────────────────────────────────────

def test_the_models_own_fence_is_unwrapped_and_the_prose_around_it_dropped():
    raw = "Here's the fix:\n```go\nreturn nil, err\n```\nThis avoids the nil deref."
    assert sanitize_suggestion(raw) == ("return nil, err", True)


def test_an_echoed_suggestion_fence_is_unwrapped():
    assert sanitize_suggestion("```suggestion:-0+0\nx := 1\n```") == ("x := 1", True)


def test_an_unterminated_fence_keeps_what_follows_it():
    assert sanitize_suggestion("```go\nx := 1") == ("x := 1", True)


@pytest.mark.parametrize("lead_in", ["Suggested fix:", "Here's the fix:", "Replace with:",
                                     "Fix:", "Use this instead:"])
def test_a_lead_in_line_is_stripped(lead_in):
    assert sanitize_suggestion(f"{lead_in}\nreturn nil, err") == ("return nil, err", True)


def test_an_inline_lead_in_prefix_is_stripped():
    assert sanitize_suggestion("Change this to: x := compute()") == ("x := compute()", True)


def test_code_that_starts_like_a_lead_in_is_not_truncated():
    assert sanitize_suggestion("fix := compute()") == ("fix := compute()", True)


def test_the_line_number_gutter_is_stripped():
    raw = "12|\tif err != nil {\n13|\t\treturn err\n14|\t}"
    assert sanitize_suggestion(raw) == ("\tif err != nil {\n\t\treturn err\n\t}", True)


def test_a_partially_copied_gutter_is_still_stripped():
    raw = "12|\tx := 1\n\ty := 2"
    assert sanitize_suggestion(raw) == ("\tx := 1\n\ty := 2", True)


def test_the_first_lines_indentation_survives():
    """The block replaces the whole line, so a stripped indent would be committed."""
    assert sanitize_suggestion("\n\treturn nil, err\n") == ("\treturn nil, err", True)


@pytest.mark.parametrize("sql", [
    "ALTER TABLE orders ADD COLUMN status text",
    "DROP INDEX idx_orders_status",
    "SELECT id FROM orders WHERE status = $1",
])
def test_punctuation_free_sql_stays_code(sql):
    assert sanitize_suggestion(sql) == (sql, True)


def test_a_sentence_is_prose_and_never_applyable():
    text, is_code = sanitize_suggestion("You should check the error before using the value.")
    assert not is_code and text == "You should check the error before using the value."


def test_prose_wrapped_around_code_is_peeled_off():
    raw = "Consider returning early here\nif err != nil { return err }\nThis keeps it safe."
    assert sanitize_suggestion(raw) == ("if err != nil { return err }", True)


@pytest.mark.parametrize("value", [None, "", "   \n ", 42])
def test_nothing_usable_is_none(value):
    assert sanitize_suggestion(value) == (None, False)


# ── one line, or no Apply button ─────────────────────────────────────────────

def test_a_single_line_fix_is_applyable():
    body = format_single_issue(_issue(suggestion="return nil, err"), applyable=True)
    assert f"{APPLY}\nreturn nil, err\n```" in body


def test_a_multi_line_fix_is_a_plain_block_never_a_suggestion():
    """``-0+0`` would replace one line with three and leave the old body behind."""
    fix = "if order.Customer == nil {\n\treturn ErrNoCustomer\n}"
    body = format_single_issue(_issue(suggestion=fix), applyable=True)
    assert "suggestion:" not in body
    assert f"**Suggested Fix:**\n```\n{fix}\n```" in body


def test_a_multi_line_sql_string_is_not_applyable():
    fix = "SELECT id, status\nFROM orders\nWHERE status = $1"
    body = format_single_issue(_issue(suggestion=fix), applyable=True)
    assert "suggestion:" not in body and fix in body


def test_the_general_note_fallback_is_never_applyable():
    body = format_single_issue(_issue(suggestion="return nil, err"), applyable=False)
    assert "suggestion:" not in body and "```\nreturn nil, err\n```" in body


def test_a_multi_line_fix_renders_exactly_like_the_general_note_fallback():
    fix = "a := 1\nb := 2"
    assert (format_single_issue(_issue(suggestion=fix), applyable=True)
            == format_single_issue(_issue(suggestion=fix), applyable=False))


# ── anchor checks against the commented line ─────────────────────────────────

def test_a_no_op_fix_is_not_shown_at_all():
    body = format_single_issue(_issue(suggestion="x := compute( a,b )"), applyable=True,
                               original_line="\tx := compute( a,b )")
    assert "Suggested Fix" not in body and "nil deref" in body


def test_only_trailing_whitespace_is_ignored_by_the_no_op_check():
    assert anchor_suggestion("\t\treturn nil, err  ", "\t\treturn nil, err") == (None, False)


def test_a_real_change_on_the_line_stays_applyable():
    assert anchor_suggestion("\treturn nil, err", "\treturn nil, nil") == ("\treturn nil, err", True)


@pytest.mark.parametrize("original,fix", [
    # YAML: the key sits under the wrong parent; the fix *is* the indentation.
    ("  image: nginx:1.25", "    image: nginx:1.25"),
    # Python: a statement in the wrong block.
    ("        return result", "    return result"),
    # Go: a doubled space inside a string literal is a real bug.
    ('msg := "Hello  World"', 'msg := "Hello World"'),
])
def test_a_fix_that_only_changes_whitespace_is_still_a_fix(original, fix):
    """Comparing whitespace-collapsed text called each of these a no-op and hid it."""
    body = format_single_issue(_issue(suggestion=fix), applyable=True, original_line=original)
    assert f"{APPLY}\n{fix}\n```" in body


def test_a_fix_with_its_own_indentation_is_committed_as_written():
    """Forcing the line's indentation on it would undo an indentation fix."""
    assert anchor_suggestion("    return nil, err", "\t\treturn nil, nil") == ("    return nil, err", True)


def test_an_unindented_fix_gets_the_lines_indentation():
    """The model stripped it — a bare statement, or the indent lost in JSON."""
    assert anchor_suggestion("return nil, err", "    return nil, nil") == ("    return nil, err", True)
    body = format_single_issue(_issue(suggestion="return nil, err"), applyable=True,
                               original_line="\t\treturn nil, nil")
    assert f"{APPLY}\n\t\treturn nil, err\n```" in body


def test_an_unindented_copy_of_the_line_is_a_no_op():
    assert anchor_suggestion("return nil, err", "\t\treturn nil, err") == (None, False)


def test_a_blank_original_line_has_no_indentation_to_impose():
    assert anchor_suggestion("\tdefer rows.Close()", "") == ("\tdefer rows.Close()", True)


def test_without_the_original_line_a_single_line_is_taken_as_is():
    assert anchor_suggestion("  x := 1", None) == ("  x := 1", True)


def test_a_multi_line_fix_is_never_applyable_whatever_the_line():
    assert anchor_suggestion("a\nb", "a") == ("a\nb", False)


# ── the commented line's text comes from the diff already in memory ──────────

DIFF = (
    "@@ -10,4 +10,5 @@ func Load() {\n"
    " \tctx := context.Background()\n"
    "-\trows, _ := db.Query(q)\n"
    "+\trows, err := db.Query(q)\n"
    "+\tif err != nil {\n"
    " \tdefer rows.Close()\n"
    "\\ No newline at end of file\n"
    "@@ -40,2 +41,2 @@\n"
    " \treturn out\n"
    "+}\n"
)


@pytest.mark.parametrize("line,text", [
    (10, "\tctx := context.Background()"),
    (11, "\trows, err := db.Query(q)"),
    (12, "\tif err != nil {"),
    (13, "\tdefer rows.Close()"),
    (41, "\treturn out"),
    (42, "}"),
])
def test_diff_line_text_reads_added_and_context_lines(line, text):
    assert diff_line_text(DIFF, line) == text


@pytest.mark.parametrize("line", [9, 14, 40, 43, None, "n/a"])
def test_a_line_outside_the_hunks_has_no_text(line):
    assert diff_line_text(DIFF, line) is None


@pytest.mark.parametrize("diff", [
    DIFF,
    "@@ -0,0 +1,2 @@\n+package svc\n+\n",                          # new file
    "@@ -1,2 +0,0 @@\n-package svc\n-\n",                          # deleted file
    "@@ -5,2 +5,2 @@\n c\n@@ bogus @@\n+lost\n@@ -9 +9 @@\n+found\n",  # bad header
    "@@ -1,2 +1,2 @@\n+x\n y\n@@ -1,2 +1,2 @@\n x\n+y\n",            # overlapping hunks
    "no hunks at all\n+looks added\n",
])
def test_diff_line_text_numbers_lines_exactly_as_the_position_map_does(diff):
    """The text must be of the line GitLab anchors the comment on — both are now
    read off the one parser, malformed diffs included."""
    positions = build_diff_position_map(diff)
    found = {n for n in range(0, 60) if diff_line_text(diff, n) is not None}
    assert found == set(positions)


def test_a_line_number_a_malformed_diff_repeats_reads_as_the_position_map_resolves_it():
    """Overlapping hunks: the later statement of line 1 wins, in both."""
    diff = "@@ -1,2 +1,2 @@\n+first\n y\n@@ -1,2 +1,2 @@\n second\n+y\n"
    assert build_diff_position_map(diff)[1] == 1, "context in the later hunk"
    assert diff_line_text(diff, 1) == "second"


# ── restore: the reviewers' own fix, by position ─────────────────────────────

def test_the_reviewers_suggestion_replaces_a_rewritten_one():
    selected = _selected(file_name="svc/order.go", line=42, problem="rewritten",
                         severity="high", suggestion="return ErrSomethingInvented")
    kept, restored, unverified, _ = restore_issue_suggestions(
        selected, [_issue(suggestion="return nil, ErrNoCustomer")])
    assert restored == [("svc/order.go", 42)] and not unverified
    assert kept[0].suggestion == "return nil, ErrNoCustomer"


def test_a_suggestion_invented_downstream_of_the_code_is_removed():
    """The original had no fix; the ranking stage never saw the code to write one."""
    selected = _selected(file_name="svc/order.go", line=42, problem="p",
                         suggestion="pbook.ErrOperationTemplateItemNotFound")
    kept, restored, _, _ = restore_issue_suggestions(selected, [_issue(suggestion="")])
    assert restored and kept[0].suggestion == ""
    assert "Suggested Fix" not in format_single_issue(kept[0])


# Two distinct findings on one line — two service sub-agents routinely do this.
NIL_FIX = "if order == nil { return ErrNotFound }"
SQL_FIX = 'db.Where("name = ?", name)'
NIL_CHECK = _issue(
    "`order` is nil when the lookup misses, and `order.Customer` dereferences it: "
    "a nil-pointer panic on every unknown order id.",
    suggestion=NIL_FIX, path="svc/a.go", line=10)
SQL_INJECTION = _issue(
    "`name` is concatenated into the SQL string, so a crafted name injects SQL. "
    "Use a bound parameter.",
    suggestion=SQL_FIX, path="svc/a.go", line=10)
# What the comment reviewer is told to produce: two plain sentences, reworded.
NIL_REWORDED = "A missed lookup leaves `order` nil, so reading `order.Customer` panics."
SQL_REWORDED = "Building the SQL string from `name` lets a caller inject SQL."


def _two_selected(nil_suggestion, sql_suggestion, nil_text=NIL_REWORDED, sql_text=SQL_REWORDED):
    payload = {"selected_issues": [
        {"file_name": "svc/a.go", "line": 10, "severity": "high",
         "problem": nil_text, "suggestion": nil_suggestion},
        {"file_name": "svc/a.go", "line": 10, "severity": "high",
         "problem": sql_text, "suggestion": sql_suggestion},
    ], "filtered_count": 0, "reasoning": "r"}
    return CommentReviewResponse.model_validate(payload).selected_issues


def test_reworded_findings_sharing_a_line_each_get_their_own_fix():
    """Both used to get the nil-check fix, the most severe (first) finding's — so
    Apply under the SQL-injection comment committed a nil check."""
    selected = _two_selected("if order == nil {return ErrNotFound}", "")
    kept, restored, unverified, _ = restore_issue_suggestions(
        selected, [dict(NIL_CHECK), dict(SQL_INJECTION)])
    assert [i.suggestion for i in kept] == [NIL_FIX, SQL_FIX]
    assert not unverified
    sql_body = format_single_issue(kept[1], applyable=True)
    assert f"{APPLY}\n{SQL_FIX}\n```" in sql_body and "order == nil" not in sql_body


def test_one_findings_fix_copied_onto_another_never_gets_a_button():
    """The model carried the nil-check fix onto both: the one whose text is the
    nil check keeps it; the SQL comment's text and fix disagree, so it is posted
    without Apply rather than with either finding's fix guessed."""
    selected = _two_selected(NIL_FIX, NIL_FIX)
    kept, _, unverified, _ = restore_issue_suggestions(
        selected, [dict(NIL_CHECK), dict(SQL_INJECTION)])
    assert kept[0].suggestion == NIL_FIX
    assert unverified == [("svc/a.go", 10)]
    assert kept[1].suggestion == NIL_FIX, "the model's text is kept, as text"
    assert "suggestion:" not in format_single_issue(kept[1], applyable=False)


def test_rewordings_that_match_neither_finding_are_not_guessed():
    """A merged or generic rewrite reads like neither; the most severe finding's
    fix is *not* the fallback — a wrong badge is a mislabel, a wrong fix is code."""
    selected = _two_selected("", "x := 1", nil_text="Two problems on this line.",
                             sql_text="This line needs attention before merge.")
    kept, restored, unverified, _ = restore_issue_suggestions(
        selected, [dict(NIL_CHECK), dict(SQL_INJECTION)])
    assert [i.suggestion for i in kept] == ["", "x := 1"]
    assert not restored and unverified == [("svc/a.go", 10)]


def test_one_findings_fix_never_goes_to_two_different_comments():
    """One finding on the line, two comments there with different text: the one
    passed through verbatim is that finding; the other could be anything."""
    other = "Building the SQL string from `name` lets a caller inject SQL."
    selected = [dict(NIL_CHECK, suggestion=""),
                dict(NIL_CHECK, problem=other, suggestion="db.Exec(q)")]
    kept, _, unverified, unverified_issues = restore_issue_suggestions(
        selected, [dict(NIL_CHECK)]
    )
    assert kept[0]["suggestion"] == NIL_FIX
    assert kept[1]["suggestion"] == "db.Exec(q)" and unverified == [("svc/a.go", 10)]
    # Both comments sit on svc/a.go:10. Only the one nothing vouches for loses the
    # Apply button: the caller keys on these objects, not on the shared position.
    assert len(unverified_issues) == 1 and unverified_issues[0] is kept[1]


def test_the_same_finding_emitted_twice_gets_its_fix_twice():
    """Identical text is the same finding, not a contest (dedup drops the repeat)."""
    selected = _two_selected("", "", sql_text=NIL_REWORDED)
    kept, _, unverified, _ = restore_issue_suggestions(selected, [dict(NIL_CHECK)])
    assert [i.suggestion for i in kept] == [NIL_FIX, NIL_FIX] and not unverified


def test_a_comment_passed_through_verbatim_keeps_its_own_fix():
    """The deterministic fallback posts the reviewers' own findings, and two
    service sub-agents routinely flag the same line: each keeps its fix rather
    than both getting the most severe neighbour's."""
    first = _issue("nil deref", suggestion="if o == nil { return nil }")
    second = _issue("error swallowed", suggestion="return nil, err")
    selected = [dict(first), dict(second)]
    kept, restored, unverified, _ = restore_issue_suggestions(selected, [first, second])
    assert [i["suggestion"] for i in kept] == [first["suggestion"], second["suggestion"]]
    assert not restored and not unverified


def test_a_bare_basename_still_matches():
    selected = _selected(file_name="order.go", line=42, problem="p", suggestion="x")
    kept, restored, _, _ = restore_issue_suggestions(
        selected, [_issue(path="deep/svc/order.go", suggestion="return nil, err")])
    assert restored and kept[0].suggestion == "return nil, err"


def test_a_whitespace_only_difference_is_put_back_without_a_warning():
    selected = _selected(file_name="svc/order.go", line=42, problem="p",
                         suggestion="return nil, err")
    kept, restored, _, _ = restore_issue_suggestions(
        selected, [_issue(suggestion="\treturn nil, err")])
    assert kept[0].suggestion == "\treturn nil, err" and not restored


def test_an_unchanged_suggestion_is_not_a_restore():
    selected = _selected(file_name="svc/order.go", line=42, problem="p",
                         suggestion="return nil, err")
    _, restored, unverified, _ = restore_issue_suggestions(
        selected, [_issue(suggestion="return nil, err")])
    assert not restored and not unverified


def test_an_unmatched_position_keeps_the_text_but_loses_the_button():
    selected = _selected(file_name="ghost.go", line=7, problem="p", severity="high",
                         suggestion="return nil, err")
    kept, restored, unverified, _ = restore_issue_suggestions(selected, [_issue()])
    assert kept[0].suggestion == "return nil, err" and not restored
    assert unverified == [("ghost.go", 7)]
    # What the posting loop does with it: applyable only when the position is vouched for.
    body = format_single_issue(kept[0], applyable=("ghost.go", 7) not in set(unverified))
    assert "suggestion:" not in body and "```\nreturn nil, err\n```" in body


def test_an_unmatched_comment_with_no_suggestion_is_not_reported():
    selected = _selected(file_name="ghost.go", line=7, problem="p", suggestion="")
    _, _, unverified, _ = restore_issue_suggestions(selected, [_issue()])
    assert unverified == []


def test_an_empty_selection_is_a_no_op():
    assert restore_issue_suggestions([], [_issue()]) == ([], [], [], [])


# ── the final cross-check still passes these bodies ──────────────────────────

@pytest.mark.parametrize("suggestion,kwargs", [
    ("return nil, err", {"applyable": True}),
    ("a := 1\nb := 2", {"applyable": True}),
    ("return nil, err", {"applyable": False}),
    ("x := 1", {"applyable": True, "original_line": "\tx := 1"}),   # no-op dropped
])
def test_comment_body_has_text_still_passes(suggestion, kwargs):
    assert comment_body_has_text(format_single_issue(_issue(suggestion=suggestion), **kwargs))
