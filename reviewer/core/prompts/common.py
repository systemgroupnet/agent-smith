"""
Shared prompt helpers used across all reviewer prompts.
"""

from typing import Optional


def format_code_index_guidance(source_branch: Optional[str] = None) -> str:
    """Guidance shared across all reviewer prompts: which MCP to use for what.

    Two things to get across:

    1. Which server to ask. Files *changed* in this MR must come from the GitLab
       MCP (that reads the merge request's own blobs, i.e. the new version).
       Old/unchanged files and the rest of the codebase come from the cocoindex
       code-index MCP (semantic ``search`` plus exact ``ripgrep``).

    2. Which branch to ask about. Both cocoindex tools take an optional
       ``branch`` argument and **default to the server's checked-out base
       branch** when it is omitted. For a review that default is wrong: it
       returns the pre-merge-request state of the code, so a symbol the MR added
       looks missing and a signature the MR changed looks unchanged — exactly the
       false positives the MR context block exists to prevent. Passing the MR
       source branch makes the index overlay that branch's changes, so lookups
       reflect the code as it will be after merge.

    3. How to shape the arguments. Two constraints get violated on essentially
       every run: ``paths``/``languages``/``globs`` are JSON **arrays**, and
       ``context_lines`` is capped at 20. A violation is a server-side pydantic
       error, so the call returns nothing useful — in one observed run *every*
       cocoindex call in the whole MR failed on those two mistakes alone.
       ``toolkit_manager._repair_tool_args`` now fixes both before the call goes
       out, but saying it here saves the round trip.

    The branch is interpolated as a literal so the model can copy it verbatim
    rather than infer it. cocoindex's ``search`` requires the ref to already
    exist in the server's clone (``ripgrep`` fetches on demand), hence the
    retry-without-branch instruction — a branch the index has never seen should
    degrade to a base-branch lookup, not to no lookup at all.
    """
    lines = [
        "For files that have changes in this merge request, use the GitLab MCP "
        "tools (so you read the new version of the file). For old/unchanged files "
        "and existing code in the rest of the codebase, use the cocoindex code "
        "index MCP (`search` for semantic lookups, `ripgrep` for exact text) to "
        "look them up for context.",
    ]
    if source_branch:
        lines.append(
            f'ALWAYS pass `branch="{source_branch}"` to every cocoindex tool call. '
            "That is this merge request's source branch. Without it the index "
            "answers from the default branch, which does not contain this merge "
            "request's changes, and you will draw the wrong conclusion about code "
            "the merge request touched. If a call reports that the branch or ref "
            "is unknown, retry the same call once without `branch` and treat the "
            "result as the pre-merge-request state of the code."
        )
    else:
        lines.append(
            "The cocoindex tools take an optional `branch` argument and otherwise "
            "answer from the default branch, which does not contain this merge "
            "request's changes. Pass the merge request's source branch whenever "
            "you know it."
        )
    lines.append(
        "Respect the cocoindex argument types or the call is rejected and you get "
        'nothing back: `paths`, `languages` and `globs` are arrays — `paths=["a/b"]`, '
        'never `paths="a/b"` — and `context_lines` must be between 0 and 20 (ask for '
        "20 if you want maximum context, not 50 or 100)."
    )
    return " ".join(lines)


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
    "unimplemented. If you are tempted to, look it up with the code index tools "
    "instead, and drop the finding when in doubt.\n"
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
