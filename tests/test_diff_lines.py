"""The changed-line gate, and the diff that finally reaches each reviewer again.

About 6% of developer replies rejected a comment on code the merge request did not
change — "This was already there before this PR", "The code was only moved inside
the if block, so they appear as changes in the diff". Two causes, both covered here:
since eb3308c no reviewer was shown its diff at all (every prompt rendered an empty
Git Diff block), and the in-diff gate accepts unchanged context lines, which were a
median 58% of the lines findings passed it on.
"""

import asyncio
import re
from typing import Dict, Optional

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from core.agents.deep_reviewer import review_changed_files
from core.utils.diff_lines import (
    DEFAULT_CHANGED_LINE_RADIUS,
    MAX_PROMPT_DIFF_CHARS,
    apply_changed_line_gate,
    build_changed_line_index,
    changed_line_radius,
    clip_diff_for_prompt,
    normalize_line,
    parse_unified_diff,
)
from core.utils.gitlab_helpers import build_diff_position_map
from core.utils.review_verbosity import parse_verbosity

DEFAULT = parse_verbosity([]).severities
VV = parse_verbosity(["review::-vv"]).severities


# ── The parser ────────────────────────────────────────────────────────────────

SIMPLE = (
    "@@ -10,7 +10,7 @@ func Save(ctx context.Context) error {\n"
    " \tctx = withTimeout(ctx)\n"
    "-\terr := s.repo.Save(ctx, order)\n"
    "+\terr := s.repo.SaveWithAudit(ctx, order, auditor)\n"
    " \tif err != nil {\n"
    " \t\treturn err\n"
    " \t}\n"
    "-\tlog.Printf(\"saved order %d for customer\", order.ID)\n"
    " \treturn nil\n"
    " }\n"
)


def test_the_parser_numbers_added_removed_and_context_lines():
    parsed = parse_unified_diff(SIMPLE)
    assert parsed.added == {11: "\terr := s.repo.SaveWithAudit(ctx, order, auditor)"}
    assert parsed.context == {10: 10, 12: 12, 13: 13, 14: 14, 15: 16, 16: 17}
    removed = {dl.old_ln: dl.new_ln for dl in parsed.removed}
    # Each removal is anchored to the new-side line now standing where it was.
    assert removed == {11: 11, 15: 15}
    # ...and both neighbours of the gap are its anchor points.
    assert parsed.removal_anchors() == {10, 11, 14, 15}


def test_the_parser_ignores_what_is_not_a_line():
    diff = (
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "\\ No newline at end of file\n"
        "@@ this header does not parse @@\n"
        "+suspended until the next real hunk\n"
        "@@ -40 +40,2 @@\n"
        " forty\n"
        "+forty-one\n"
    )
    parsed = parse_unified_diff(diff)
    assert parsed.added == {1: "new", 41: "forty-one"}
    assert parsed.context == {40: 40}
    assert parse_unified_diff("").lines == ()


def _legacy_build_diff_position_map(diff_text: str) -> Dict[int, Optional[int]]:
    """The position map's parser as it was before the hunk walk was shared, verbatim."""
    positions: Dict[int, Optional[int]] = {}
    if not diff_text:
        return positions

    old_ln = new_ln = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
                in_hunk = True
            else:
                in_hunk = False
            continue
        if not in_hunk or raw == "":
            continue
        tag = raw[0]
        if tag == "+":
            positions[new_ln] = None
            new_ln += 1
        elif tag == "-":
            old_ln += 1
        elif tag == "\\":
            continue
        else:
            positions[new_ln] = old_ln
            old_ln += 1
            new_ln += 1
    return positions


PARITY_CASES = [
    "",
    SIMPLE,
    "@@ -0,0 +1,3 @@\n+package svc\n+\n+func New() {}\n",
    "@@ -1,3 +0,0 @@\n-package svc\n-\n-func Old() {}\n",
    "@@ -1,2 +1,2 @@\n-a\n+b\n\\ No newline at end of file\n",
    # A malformed header suspends the hunk; the next good one resumes it.
    "@@ -5,2 +5,2 @@\n c\n@@ bogus @@\n+lost\n@@ -9 +9 @@\n+found\n",
    # Overlapping hunks: the later line wins, in the order the diff states them.
    "@@ -1,2 +1,2 @@\n+x\n y\n@@ -1,2 +1,2 @@\n x\n+y\n",
    # Text before the first hunk, and an empty line inside one, are not lines.
    "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n",
    "no hunks at all\n+looks added\n",
]


@pytest.mark.parametrize("diff", PARITY_CASES)
def test_the_position_map_is_unchanged_by_sharing_the_parser(diff):
    """build_diff_position_map had no tests and decides every inline position, so the
    refactor onto the shared parser is held to the old implementation, case by case."""
    assert build_diff_position_map(diff) == _legacy_build_diff_position_map(diff)


# ── Moved code ────────────────────────────────────────────────────────────────

REINDENT = (
    "@@ -10,13 +10,15 @@ func Load(ctx context.Context) error {\n"
    " \tctx = withTimeout(ctx)\n"                                            # 10
    "-\trows, err := s.db.QueryContext(ctx, query, args...)\n"
    "-\tif err != nil {\n"
    "-\t\treturn err\n"
    "-\t}\n"
    "-\tdefer rows.Close()\n"
    "-\tfor rows.Next() {\n"
    "-\t\tif err := rows.Scan(&order.ID, &order.Status); err != nil {\n"
    "-\t\t\treturn err\n"
    "-\t\t}\n"
    "-\t}\n"
    "+\tif s.featureEnabled {\n"                                              # 11 NEW
    "+\t\trows, err := s.db.QueryContext(ctx, query, args...)\n"              # 12
    "+\t\tif err != nil {\n"                                                 # 13
    "+\t\t\treturn err\n"                                                    # 14
    "+\t\t}\n"                                                               # 15
    "+\t\tdefer rows.Close()\n"                                              # 16
    "+\t\tfor rows.Next() {\n"                                               # 17
    "+\t\t\tif err := rows.Scan(&order.ID, &order.Status); err != nil {\n"   # 18
    "+\t\t\t\treturn err\n"                                                  # 19
    "+\t\t\t}\n"                                                             # 20
    "+\t\t}\n"                                                               # 21
    "+\t}\n"                                                                 # 22 NEW
    " \treturn nil\n"                                                        # 23
    " }\n"                                                                   # 24
)


def test_code_reindented_into_a_new_if_is_moved_and_the_if_is_new():
    """'The code was only moved inside the if block, so they appear as changes.'"""
    lines = build_changed_line_index({"svc/load.go": REINDENT})["svc/load.go"]
    assert 11 in lines.new, "the `if` header is what the author wrote"
    assert set(range(12, 22)) <= lines.moved, "the body only changed indentation"
    assert 22 in lines.new, "the `}` closing the new `if` was not carried from anywhere"
    assert not lines.anchors, "removals that reappear elsewhere are not removals"


def test_short_lines_inside_a_moved_block_move_with_it():
    """`if err != nil {` and `return err` carry no evidence on their own, but they
    sit in a block that lines up with the removed one line for line."""
    lines = build_changed_line_index({"svc/load.go": REINDENT})["svc/load.go"]
    assert {13, 14} <= lines.moved


DELETED_FILE = (
    "@@ -1,7 +0,0 @@\n"
    "-package legacy\n"
    "-\n"
    "-func ComputeInvoiceTotal(items []Item) (total int64) {\n"
    "-\tfor _, item := range items {\n"
    "-\t\ttotal += item.UnitPrice * item.Quantity\n"
    "-\t}\n"
    "-\treturn total\n"
)

RECEIVING_FILE = (
    "@@ -3,4 +3,10 @@ import \"time\"\n"
    " \n"
    " type Invoice struct{ Items []Item }\n"
    " \n"
    "+func ComputeInvoiceTotal(items []Item) (total int64) {\n"
    "+\tfor _, item := range items {\n"
    "+\t\ttotal += item.UnitPrice * item.Quantity\n"
    "+\t}\n"
    "+\treturn total\n"
    "+}\n"
    " func (i Invoice) Due() time.Time { return dueDate() }\n"
)


def test_code_moved_between_files_through_a_deleted_file_is_moved():
    index = build_changed_line_index(
        {"legacy/total.go": DELETED_FILE, "billing/invoice.go": RECEIVING_FILE})
    receiving = index["billing/invoice.go"]
    assert {6, 7, 8, 9, 10} <= receiving.moved
    assert 11 in receiving.new, "the deleted file never had a closing brace there"
    assert not index["legacy/total.go"].anchors, "nothing was really deleted, it moved"


def test_a_lone_closing_brace_is_not_a_move():
    """Every Go change removes and adds a `}` somewhere — matching text is not a move."""
    index = build_changed_line_index({
        "a.go": "@@ -1,4 +1,2 @@\n func a() {\n-\tif legacyMode {\n-\t}\n }\n",
        "b.go": "@@ -1,2 +1,3 @@\n func b() {\n+\t}\n }\n",
    })
    assert 2 in index["b.go"].new and not index["b.go"].moved


def test_a_short_line_alone_is_never_evidence_of_a_move():
    index = build_changed_line_index({
        "a.go": "@@ -1,2 +1,1 @@\n x\n-\treturn nil\n",
        "b.go": "@@ -1,1 +1,2 @@\n y\n+\treturn nil\n",
    })
    assert 2 in index["b.go"].new and not index["b.go"].moved
    assert index["a.go"].anchors == {1, 2}, "and its removal is a real one"


def test_a_long_line_matched_alone_is_a_move():
    query = ('query := "SELECT o.id, o.status, o.total, c.name FROM orders o '
             'JOIN customers c ON c.id = o.customer_id WHERE o.id = $1"')
    index = build_changed_line_index({
        "a.go": f"@@ -1,2 +1,1 @@\n x\n-\t{query}\n",
        "b.go": f"@@ -1,1 +1,2 @@\n y\n+\t\t{query.replace(', ', ',  ')}\n",
    })
    assert index["b.go"].moved == {2}, "re-indented and re-spaced is still the same line"
    assert not index["a.go"].anchors


def test_a_mid_length_line_alone_is_not_a_move():
    """Distinctive enough for git to colour, not enough to cost a finding."""
    index = build_changed_line_index({
        "a.go": "@@ -1,2 +1,1 @@\n x\n-\tlog.Printf(\"order %d saved for customer %d\", o.ID, c.ID)\n",
        "b.go": "@@ -1,1 +1,2 @@\n y\n+\t\tlog.Printf(\"order %d saved for customer %d\", o.ID, c.ID)\n",
    })
    assert 2 in index["b.go"].new and not index["b.go"].moved


UNRELATED_DELETION = (
    "@@ -20,10 +20,3 @@ func (s *Store) Count(ctx context.Context) (int, error) {\n"
    " \treturn s.count(ctx)\n"
    " }\n"
    " \n"
    "-func (s *Store) LoadLegacy(ctx context.Context, id int64) (*Order, error) {\n"
    "-\torder, err := s.legacy.Find(ctx, id)\n"
    "-\tif err != nil {\n"
    "-\t\treturn nil, err\n"
    "-\t}\n"
    "-\treturn order, nil\n"
    "-}\n"
)

NEW_FUNCTION_WITH_THE_SAME_ERR_BLOCK = (
    "@@ -0,0 +1,10 @@\n"
    "+package svc\n"                                                                # 1
    "+\n"                                                                           # 2
    "+func (s *Store) CreateOrder(ctx context.Context, o *Order) (*Order, error) {\n"  # 3
    "+\ttx := s.db.Begin()\n"                                                       # 4
    "+\terr := tx.Create(o).Error\n"                                                # 5
    "+\tif err != nil {\n"                                                          # 6
    "+\t\treturn nil, err\n"                                                        # 7
    "+\t}\n"                                                                        # 8
    "+\treturn o, tx.Commit().Error\n"                                              # 9
    "+}\n"                                                                          # 10
)


def test_boilerplate_deleted_elsewhere_does_not_make_new_code_moved():
    """`if err != nil { return nil, err }` is exactly 20 alphanumerics — git's bar.
    Deleting an unrelated function that ends the same way used to make the new
    function's error path "moved", and its high ("returns without tx.Rollback()")
    was demoted out of the default verbosity and never posted."""
    diffs = {"svc/legacy.go": UNRELATED_DELETION,
             "svc/order.go": NEW_FUNCTION_WITH_THE_SAME_ERR_BLOCK}
    lines = build_changed_line_index(diffs)["svc/order.go"]
    assert {6, 7, 8} <= lines.new and not lines.moved

    issue = _issue(7, path="svc/order.go")
    kept, demotions = _gate([issue], diffs, severities=DEFAULT)
    assert kept == [issue] and issue["severity"] == "high" and demotions == []


def test_whitespace_is_not_part_of_a_line_s_identity():
    assert normalize_line("\t\tName   string `json:\"name\"`") == 'Name string `json:"name"`'


# ── The gate ──────────────────────────────────────────────────────────────────

GATE_DIFF = (
    "@@ -1,12 +1,13 @@\n"
    " package svc\n"                                           # 1
    " \n"                                                      # 2
    " func Save(ctx context.Context, o Order) error {\n"       # 3
    " \tif o.ID == 0 {\n"                                      # 4
    " \t\treturn ErrNoID\n"                                    # 5
    " \t}\n"                                                   # 6
    " \tctx = withTimeout(ctx)\n"                              # 7
    "+\tauditTrail.Record(ctx, \"order.save\", o.ID)\n"        # 8  NEW
    " \tif err := s.repo.Save(ctx, o); err != nil {\n"         # 9
    " \t\treturn err\n"                                        # 10
    " \t}\n"                                                   # 11
    " \treturn nil\n"                                          # 12
    " }\n"                                                     # 13
)


def _gate(issues, diffs, severities=DEFAULT, radius=1, review_diffs=None):
    index = build_changed_line_index(diffs, review_diffs=review_diffs)
    return apply_changed_line_gate(
        issues, index, lambda issue: issue["file_name"], severities, radius)


def _issue(line, severity="high", path="svc/save.go"):
    return {"problem": "p", "suggestion": "", "file_name": path, "line": line,
            "severity": severity}


def test_a_finding_on_a_new_line_stands():
    issue = _issue(8)
    kept, demotions = _gate([issue], {"svc/save.go": GATE_DIFF})
    assert kept == [issue] and issue["severity"] == "high" and demotions == []


def test_a_finding_one_line_from_a_change_stands_at_the_default_radius():
    assert DEFAULT_CHANGED_LINE_RADIUS == 1
    kept, demotions = _gate([_issue(9), _issue(7)], {"svc/save.go": GATE_DIFF})
    assert len(kept) == 2 and demotions == []


def test_a_context_line_three_away_is_dropped_by_default_and_a_medium_at_vv():
    kept, demotions = _gate([_issue(5)], {"svc/save.go": GATE_DIFF}, severities=DEFAULT)
    assert kept == []
    (d,) = demotions
    assert (d.stage, d.was, d.now, d.dropped, d.distance) == (
        "issue_demoted_context_line", "high", "medium", True, 3)
    assert "dropped" in d.describe(1, "default")

    issue = _issue(5)
    kept, demotions = _gate([issue], {"svc/save.go": GATE_DIFF}, severities=VV)
    assert kept == [issue] and issue["severity"] == "medium"
    assert demotions[0].stage == "issue_demoted_context_line" and not demotions[0].dropped


def test_a_medium_on_unchanged_code_is_demoted_out_of_scope_at_vv():
    kept, demotions = _gate([_issue(12, "medium")], {"svc/save.go": GATE_DIFF}, severities=VV)
    assert kept == [] and demotions[0].now == "low" and demotions[0].dropped


def test_a_finding_on_moved_code_is_demoted_as_moved():
    issues = [_issue(14, path="svc/load.go"), _issue(13, path="svc/load.go")]
    kept, demotions = _gate(issues, {"svc/load.go": REINDENT}, severities=VV)
    assert {d.stage for d in demotions} == {"issue_demoted_moved_code"}
    assert [i["severity"] for i in kept] == ["medium", "medium"]
    assert "moved" in demotions[0].describe(1, "review::-vv")

    kept, demotions = _gate([_issue(13, path="svc/load.go")], {"svc/load.go": REINDENT})
    assert kept == [] and demotions[0].stage == "issue_demoted_moved_code"


def test_moved_lines_next_to_what_the_author_wrote_still_stand():
    """The first line of the re-indented body sits next to the new `if` header."""
    kept, demotions = _gate([_issue(12, path="svc/load.go")], {"svc/load.go": REINDENT})
    assert len(kept) == 1 and demotions == []


def test_a_finding_beside_a_genuine_removal_stands():
    """'The audit call is gone' can only be anchored beside the gap it left."""
    diff = (
        "@@ -1,7 +1,6 @@\n"
        " package svc\n"
        " \n"
        " func Save(ctx context.Context, o Order) error {\n"
        "-\tauditTrail.Record(ctx, \"order.save\", o.ID)\n"
        " \treturn s.repo.Save(ctx, o)\n"
        " }\n"
        " \n"
    )
    kept, demotions = _gate([_issue(3), _issue(4)], {"svc/save.go": diff}, radius=0)
    assert len(kept) == 2 and demotions == []
    kept, demotions = _gate([_issue(1)], {"svc/save.go": diff}, radius=1)
    assert kept == [] and demotions[0].stage == "issue_demoted_context_line"


def test_demoted_findings_sort_after_the_highs_they_were():
    """Every later cap takes a prefix, so a demoted finding must not hold a high's slot."""
    issues = [_issue(5), _issue(8)]
    kept, _ = _gate(issues, {"svc/save.go": GATE_DIFF}, severities=VV)
    assert [(i["line"], i["severity"]) for i in kept] == [(8, "high"), (5, "medium")]


def test_the_gate_fails_open():
    unknown = _issue(5, path="not/indexed.go")
    no_line = _issue(None)
    kept, demotions = _gate([unknown, no_line], {"svc/save.go": GATE_DIFF})
    assert kept == [unknown, no_line] and demotions == []


def test_an_incremental_run_judges_new_by_the_commits_under_review():
    """Line 8 was added by an earlier, already-reviewed commit: in the compare diff of
    the new commits it is context. A file the compare did not return falls back to
    its MR diff."""
    compare = (
        "@@ -6,4 +6,5 @@\n"
        " \t}\n"
        " \tctx = withTimeout(ctx)\n"
        " \tauditTrail.Record(ctx, \"order.save\", o.ID)\n"
        " \tif err := s.repo.Save(ctx, o); err != nil {\n"
        "+\t\tmetrics.SaveFailures.Inc()\n"
    )
    diffs = {"svc/save.go": GATE_DIFF, "svc/other.go": GATE_DIFF.replace("save", "other")}
    kept, demotions = _gate(
        [_issue(8), _issue(10), _issue(8, path="svc/other.go")], diffs,
        review_diffs={"svc/save.go": compare})
    assert [d.line for d in demotions] == [8]
    assert {(i["file_name"], i["line"]) for i in kept} == {
        ("svc/save.go", 10), ("svc/other.go", 8)}


@pytest.mark.parametrize("raw, expected", [
    (None, 1), ("", 1), ("0", 0), ("3", 3), ("-2", 1), ("one", 1), (" 2 ", 2),
])
def test_the_radius_is_configurable(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("REVIEW_CHANGED_LINE_RADIUS", raising=False)
    else:
        monkeypatch.setenv("REVIEW_CHANGED_LINE_RADIUS", raw)
    assert changed_line_radius() == expected


def test_radius_zero_keeps_only_the_changed_lines():
    kept, demotions = _gate([_issue(8), _issue(9)], {"svc/save.go": GATE_DIFF}, radius=0)
    assert [i["line"] for i in kept] == [8] and [d.line for d in demotions] == [9]


# ── The diff in the reviewer's prompt ─────────────────────────────────────────


def test_a_short_diff_is_shown_whole():
    assert clip_diff_for_prompt(SIMPLE) == SIMPLE.rstrip("\n")
    assert clip_diff_for_prompt("") == ""


def test_a_long_diff_is_cut_at_a_line_and_says_what_it_left_out():
    hunk = "@@ -{n},2 +{n},2 @@ func f{n}() {{\n context\n-old line {n}\n+new line {n}\n"
    diff = "".join(hunk.format(n=n) for n in range(1, 5000, 10))
    clipped = clip_diff_for_prompt(diff)
    body, note = clipped.rsplit("\n", 1)
    assert len(body) <= MAX_PROMPT_DIFF_CHARS
    assert diff.startswith(body + "\n"), "cut at a line boundary"
    omitted = re.findall(r"@@ -\d+,2 \+\d+,2 @@", diff[len(body):])
    assert note.startswith("… [diff truncated:")
    assert f"{len(omitted)} more hunk(s) not shown: {omitted[0]}, " in note
    assert "func f" not in note, "only the line ranges, not the hunk's function context"
    assert f"and {len(omitted) - 30} more" in note, "the list itself is bounded"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)


class PromptRecorder(BaseChatModel):
    """Records the system prompt it is given, then records a clean review."""

    prompts: list = []

    @property
    def _llm_type(self):
        return "prompt-recorder"

    def bind_tools(self, tools, **kwargs):
        return self

    def _step(self, messages):
        self.prompts.extend(_text(m.content) for m in messages if isinstance(m, SystemMessage))
        if not any(isinstance(m, ToolMessage) and m.name == "record_findings" for m in messages):
            return AIMessage(content="", tool_calls=[{
                "name": "record_findings", "id": "r",
                "args": {"file_path": "svc/order.go", "findings": []}}])
        return AIMessage(content="done")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._step(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._step(messages))])


def test_a_reviewer_is_shown_its_own_diff(checkout):
    """Regression for eb3308c: every prompt rendered an empty Git Diff block, so no
    reviewer knew which lines the merge request had changed."""
    diff = ("@@ -3,3 +3,4 @@\n func Save() error {\n"
            "+\tauditTrail.Record(\"order.save\")\n \treturn nil\n }\n")
    model = PromptRecorder(prompts=[])
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=model,
        concurrency=1, per_file_timeout_s=30, provider_policy=False,
        diffs={"svc/order.go": diff}))
    assert result.failed_files == []
    assert model.prompts, "the reviewer never saw a system prompt"
    for prompt in model.prompts:
        assert "=== Git Diff ===" in prompt
        shown = prompt.split("=== Git Diff ===", 1)[1]
        assert '+\tauditTrail.Record("order.save")' in shown, "the diff block is empty"
        assert "File Path: svc/order.go" in prompt
