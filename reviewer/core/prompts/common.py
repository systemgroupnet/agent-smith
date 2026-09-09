"""
Shared prompt helpers used across all reviewer prompts.
"""

from typing import Optional


# Shared across every reviewer prompt: the build already passed before the MR was
# opened, so nothing a compiler, type checker or parser would reject can actually
# be present in this code. Each reviewer sees exactly one file (plus the MR
# context block), which makes "undefined symbol" / "unimplemented function" /
# "unused variable" findings a systematic false positive: the definition or the
# call site simply lives in a file the model was never shown. That class is also
# invisible to the in-diff gate in main.py — the issue points at a real changed
# line, so it survives every deterministic filter and lands on the MR. It has to
# be suppressed at the prompt, and again at the comment-review stage
# (comment_prompts.py) for anything that slips through.
NO_COMPILE_ERRORS_RULE = (
    "## COMPILATION IS NOT YOUR CONCERN\n"
    "This code has already been compiled, built and run successfully by the "
    "developer. Anything a compiler, type checker, parser, build step or linter "
    "would reject therefore does NOT exist here. NEVER report:\n"
    "- Missing, unused or misordered imports\n"
    "- Unused variables, constants, parameters, functions, methods, types or "
    "fields — including \"declared but not used\"\n"
    "- Undefined, undeclared or missing symbols: a function, method, type, "
    "constant, variable, field or package you cannot find is defined somewhere "
    "you were not shown, NOT missing\n"
    "- Unimplemented / not-yet-implemented functions, methods or interfaces, or "
    "an interface that looks unsatisfied\n"
    "- Type errors, signature or argument mismatches, wrong return counts, "
    "conversion errors\n"
    "- Syntax errors, unbalanced brackets/tags, malformed declarations, or "
    "anything that would prevent the file from compiling, parsing, rendering or "
    "executing at all\n"
    "- Any claim of the form \"this will not compile\", \"this will fail to "
    "build\", \"undefined: X\", \"X is not defined\", \"X is never used\"\n"
    "Symbols defined in another file changed in this same merge request (see the "
    "Merge Request Context section) exist — do NOT report them as undefined or "
    "missing. You are shown one file at a time, so you can never conclude from "
    "what is in front of you that something is undefined, unused or "
    "unimplemented. If you are tempted to, look it up in the checkout — `grep` for "
    "where it is defined — and drop the finding when in doubt.\n"
    "Report ONLY problems that survive a successful build: runtime failures, "
    "logic errors, security flaws, data-integrity and performance problems, and "
    "design issues."
)


# Shared across every reviewer prompt: what may go in an issue's `suggestion`.
# The posted comment wraps this field in a GitLab ```suggestion block, so a human
# clicking "Apply suggestion" commits the field's text into the file verbatim —
# any prose, lead-in, fence or line-number gutter that leaks through is written
# into their source. core/utils/comment_formatter.py sanitizes what it can, but
# it can only salvage recognizable damage; the field has to arrive clean.
#: The one severity ladder, shared by every reviewer.
#:
#: It replaced nine per-prompt ladders that had drifted apart — the same failure
#: `NO_COMPILE_ERRORS_RULE` and `SUGGESTION_FIELD_RULE` were created to fix. The
#: drift was not cosmetic: the service reviewer's list ended with "code
#: duplication" as **high**, which makes a refactor suggestion outrank a missing
#: rollback, and no ladder said what is *not* high, so nothing pushed back on
#: inflation.
#:
#: Severity is load-bearing here in a way a reviewer cannot see. At the default
#: verbosity `extract_issues_for_review` collects **only highs**, so a finding
#: labelled medium is counted and then discarded — which creates a standing
#: incentive to label everything high in order to be heard. Observed on a live run:
#: four posted comments, all high, two of them "this file's error branches are
#: untested". Real points, not high severity, and they occupied slots in a
#: ten-comment budget.
#:
#: So the rubric names the exclusions explicitly and states the incentive out loud,
#: because a model that understands why inflation is self-defeating calibrates
#: better than one merely told a list of words.
SEVERITY_RUBRIC = (
    "## SEVERITY — assign it by consequence, not by how much you dislike the code\n\n"
    "Use exactly one of `high`, `medium`, `low`. The test is simple: **would you "
    "block the merge for this?**\n\n"
    "**high** — it can hurt in production. Wrong results or wrong money; data loss "
    "or corruption; a security hole (injection, authorization bypass, secret "
    "exposure, unsafe deserialization); a crash, panic or nil dereference on a "
    "reachable path; a race or deadlock; an unbounded loop, query or allocation; a "
    "query per row in a request path; a write with no rollback on failure; a "
    "silently swallowed error that loses data. If you cannot name the consequence "
    "in one clause, it is not high.\n\n"
    "**medium** — it should be fixed, but you would not block the merge for it. "
    "Missing or thin test coverage, including untested error branches. An edge case "
    "that is unhandled but unlikely. A validation gap with no security or "
    "correctness consequence. Accessibility on a shipped UI — unless it makes a "
    "primary action genuinely unusable with a keyboard or screen reader, which is "
    "high. A performance concern outside a hot path. Missing error wrapping or "
    "context.\n\n"
    "**low** — naming, formatting, structure, duplication, documentation, "
    "\"consider extracting this\", anything you would raise as a preference.\n\n"
    "Three rules about this:\n"
    "1. **Do not inflate.** Only high-severity findings are posted at the default "
    "verbosity, so labelling a medium as high is how findings get seen — and it is "
    "self-defeating: the comment budget is about ten, so every inflated finding "
    "takes a slot from a genuine one, and a reader who finds two style notes among "
    "your highs stops trusting the rest. A correctly-labelled medium that goes "
    "unposted today is worth more than a fake high.\n"
    "2. **Missing tests are medium.** However thorough your analysis of which "
    "branches are uncovered, absent tests are not a production defect. The "
    "exception is a test that asserts something false, which is high.\n"
    "3. **Name the consequence in the `problem` text for every high.** Not "
    "\"this is critical\" — what actually goes wrong, for whom, and when."
)


SUGGESTION_FIELD_RULE = (
    "**IMPORTANT**: the `suggestion` field is applied to the file *verbatim* — it "
    "is rendered as a GitLab suggestion block and a reviewer clicks a button to "
    "commit it. It MUST contain ONLY the replacement code:\n"
    "- NO explanation, description, or lead-in — never `Here's the fix:`, "
    "`Change this to:`, `Use this instead:`, and no commentary after the code. "
    "Explain the reasoning in `problem` instead.\n"
    "- NO markdown code fences or backticks around the code.\n"
    "- NO line-number gutter: the `NN|` prefixes in the file content above are "
    "not part of the file, so never copy them into the code.\n"
    "- Keep the exact indentation the code needs in the file, and make it a "
    "drop-in replacement for the line the issue points at (a self-contained "
    "rewrite of that line, not a diff or a fragment).\n"
    "- If you have no concrete code to offer, leave `suggestion` EMPTY. An empty "
    "suggestion is correct and expected; a prose description in this field is a "
    "defect, because it would be committed into the file as code."
)


# Shared across every reviewer prompt: how to write a symbol inside `problem`.
# A finding is read in a narrow column beside the diff, and GitLab renders
# `like this` as code — which is what separates the subject of the sentence from
# the prose around it. comment_formatter.emphasize_symbols wraps what is left,
# but deliberately only matches shapes that cannot be an English word (paths and
# `Do()`-style calls); a bare type or field name is never guessed at, so those
# only ever get backticks if the model writes them. Hence the prompt half.
# Unlike the summary's rule this does NOT ban code fences: quoting the offending
# lines in a ``` block is a legitimate and useful thing for a finding to do.
SYMBOL_FORMATTING_RULE = (
    "**Every path and symbol in `problem` goes in backticks.** Wrap file paths, "
    "file names, directories, functions, methods, types, fields, columns, "
    "endpoints and env vars in single backticks: `internal/app/order/order.go`, "
    "`CalculateTax()`, `Order.TotalAmount`, `POST /api/orders`, `MAX_RETRIES`. "
    "GitLab renders them as code, and that is what lets a reader's eye find the "
    "subject of the sentence in a comment read beside the diff. A bare `order.go` "
    "sitting in prose is a miss, not a style preference. Quoting the offending "
    "lines in a ``` code fence is fine and often helpful — this rule is about the "
    "symbols named in the surrounding sentences."
)


# Shared across every reviewer prompt, and interpolated LAST of the shared rules:
# a finding is only worth what its `problem` text says, and an empty one is worth
# less than nothing. `ReviewIssue.problem` defaults to `""` — lenient on purpose,
# so one omitted field cannot fail-validate a whole file's response — which means
# a finding recorded without a description survives every stage and renders as a
# comment whose entire body is the severity badge. Observed on real merge
# requests, via the comment reviewer's synthesis rather than this end; but the
# reviewers are the first writer of the text, and the cheapest place to refuse an
# empty finding is before it exists. `FindingsSink.record` rejects one
# deterministically and the posting loop refuses to post a body with no text —
# this is the layer that stops the model spending a call on one at all.
EMPTY_FINDING_RULE = (
    "**Before you call `record_findings`, re-read every finding and drop any whose "
    "`problem` field is empty, blank, or does not actually state what is wrong.** "
    "The `problem` text *is* the comment a human reads: it becomes the whole body "
    "of an inline comment on their merge request. A finding recorded without it "
    "posts a bare severity badge next to their code — it costs them a click, tells "
    "them nothing, and spends one of the few comment slots the merge request has. "
    "A `suggestion` does not stand in for it: that field is code applied to the "
    "file, and a fix with no explanation is a change someone is asked to trust "
    "blind. So state the defect in `problem` or do not record the finding — "
    "**`record_findings` with an empty list is a correct and expected answer**, and "
    "it is how you report a file you reviewed and found clean. Never pad a report "
    "to look thorough: a file with nothing wrong in it is a real result, and an "
    "empty finding is not a smaller version of a real one."
)


# Explains the line-number gutter that format_file_content_section prepends to
# every line of the reviewed file.
LINE_NUMBER_GUTTER_NOTE = (
    "Every line below is prefixed with `<line number>|`. That gutter is NOT part "
    "of the file: never copy it into a `suggestion` and never count it as source "
    "columns. Report the gutter number as an issue's `line` — it is the same "
    "numbering as the new side of the Git Diff (its `+` and context lines), which "
    "is what the merge request comments are anchored to."
)


def format_file_content_section(content: Optional[str]) -> str:
    """Render the reviewed file as a fenced block with a 1-based line-number gutter.

    GitLab returns the file as raw text with no line numbers, yet every reviewer
    is asked to report an exact `line` per issue — leaving the model to count
    rows down from a diff hunk header. A miscount is not a cosmetic error: the
    in-diff gate in main.py resolves each issue's (file_name, line) against the
    diff hunks and *drops* the ones that miss, so the finding is lost. Numbering
    the lines turns that counting task into a lookup.

    The numbers are those of the file on the MR source branch, i.e. the diff's
    new side, which is the side GitLab positions inline comments against.
    """
    if not content:
        return "(file content unavailable)"

    lines = content.splitlines()
    if not lines:
        return "(file is empty)"

    width = len(str(len(lines)))
    numbered = "\n".join(
        f"{number:>{width}}|{line}" for number, line in enumerate(lines, start=1)
    )
    return f"{LINE_NUMBER_GUTTER_NOTE}\n```\n{numbered}\n```"


def format_mr_context_section(mr_context: Optional[str]) -> str:
    """Render the shared MR-level context block for a reviewer prompt.

    `mr_context` is a preformatted block built once per MR by
    core.utils.mr_context.build_mr_context and threaded through the workflow
    state into every agent. Returns a short placeholder when no MR context is
    available (e.g. classifier-only or standalone runs).
    """
    if mr_context and mr_context.strip():
        return mr_context.strip()
    return "No MR-level context available."
