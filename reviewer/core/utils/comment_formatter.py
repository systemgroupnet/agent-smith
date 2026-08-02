"""
Comment Formatting Utilities

This module provides functions for formatting review comments and issues.

A posted comment wraps the issue's ``suggestion`` in a GitLab ```suggestion
block, which renders an "Apply suggestion" button: one click commits that text
into the file verbatim. The reviewer prompts (common.SUGGESTION_FIELD_RULE)
demand code only, but models still return prose ("Here's the fix: ..."), wrap
the code in their own markdown fence, or copy the ``NN|`` line-number gutter out
of the file content. Any of those would be committed into the source, so
``sanitize_suggestion`` below strips the recognizable damage and, when what
remains is not code at all, tells the caller to post it as plain text rather
than as an applyable suggestion.
"""

import re
from typing import Any, Optional, Tuple

# A markdown fence line with an optional info string — a language tag (```go) or
# a suggestion range the model echoed back (```suggestion:-0+0).
_FENCE_LINE = re.compile(r"^[ \t]*(?:```+|~~~+)[ \t]*[\w+#.:,-]*[ \t]*$")

# The NN| gutter that format_file_content_section prepends to the file content.
_GUTTER_LINE = re.compile(r"^[ \t]*\d+\|")

# Lead-ins models put on their own line before the code. Single words are allowed
# here only because the whole line has to match.
_LEAD_IN_LINE = re.compile(
    r"^[ \t]*(?:"
    r"here(?:'|’)?s(?:\s+is)?\s+the\s+(?:fix|correct(?:ed)?\s+code|suggestion|updated?\s+code)"
    r"|the\s+fix\s+is"
    r"|(?:suggested\s+)?fix(?:ed)?(?:\s+code)?"
    r"|correct(?:ed)?\s+code"
    r"|change\s+(?:this\s+)?to"
    r"|replace\s+(?:this\s+)?with"
    r"|use\s+(?:this\s+)?instead"
    r"|suggestion"
    r")[ \t]*[:.–—-]*[ \t]*$",
    re.IGNORECASE,
)

# The same lead-ins as an inline prefix ("Change this to: x := 1"). Restricted to
# multi-word phrases followed by a colon, so Go/TS code like `fix := compute()`
# is not mistaken for a lead-in and truncated.
_LEAD_IN_PREFIX = re.compile(
    r"^[ \t]*(?:"
    r"here(?:'|’)?s(?:\s+is)?\s+the\s+(?:fix|correct(?:ed)?\s+code|suggestion|updated?\s+code)"
    r"|the\s+fix\s+is"
    r"|suggested\s+fix"
    r"|fixed\s+code"
    r"|correct(?:ed)?\s+code"
    r"|change\s+(?:this\s+)?to"
    r"|replace\s+(?:this\s+)?with"
    r"|use\s+(?:this\s+)?instead"
    r")[ \t]*:[ \t]+(?=\S)",
    re.IGNORECASE,
)

# Punctuation that effectively never appears in an English sentence but is
# everywhere in code. One of these on a line means "this is code".
_CODE_CHARS = frozenset("{}[]()<>;=+*/\\|&^%$#@`\"'~")

# Words that mark a line as English rather than code. Deliberately narrow: SQL
# suggestions ("ALTER TABLE orders ADD COLUMN status text") are punctuation-free
# and must not be classified as prose.
_PROSE_WORDS = frozenset(
    {
        "the", "this", "that", "these", "those", "you", "your", "we", "should",
        "would", "could", "must", "need", "needs", "ensure", "ensures",
        "instead", "because", "avoid", "avoids", "consider", "please", "will",
        "it", "its", "there", "here", "which", "when", "before", "after",
        "since", "cannot", "does", "doesn't", "don't", "make", "makes",
    }
)


def _looks_like_prose(line: str) -> bool:
    """Is this line an English sentence fragment rather than a line of code?"""
    stripped = line.strip()
    if not stripped:
        return False  # blank lines are neutral — they belong to either
    if any(ch in _CODE_CHARS for ch in stripped):
        return False
    words = stripped.split()
    if len(words) < 3:
        return False  # `DROP INDEX idx_foo`, `return nil`
    if stripped.endswith((".", "!", "?")):
        return True
    return any(word.strip(",:").lower() in _PROSE_WORDS for word in words)


def _unwrap_fence(text: str) -> str:
    """Return the body of the first fenced block, or ``text`` if there is none.

    Also drops anything outside the fence, which is where models put their
    explanation ("Here's the fix:" before, "This avoids the nil deref." after).
    An unterminated fence (truncated output) keeps everything after the opener.
    """
    lines = text.splitlines()
    opener = next((i for i, line in enumerate(lines) if _FENCE_LINE.match(line)), None)
    if opener is None:
        return text
    closer = next(
        (i for i in range(opener + 1, len(lines)) if _FENCE_LINE.match(lines[i])),
        len(lines),
    )
    body = "\n".join(lines[opener + 1 : closer])
    # A fence that opens on the last line (or encloses nothing) would extract to
    # empty and silently discard the suggestion — keep the original instead and
    # let the head/tail peel below drop the stray fence markers.
    return body if body.strip() else text


def _strip_gutter(lines: list) -> list:
    """Drop the ``NN|`` prefix when the copied text is carrying the gutter.

    Applied when at least half the non-blank lines match, which catches a
    partially-copied gutter too. A real line of code that begins with digits
    followed by ``|`` is close to nonexistent in any of the reviewed languages,
    and committing a stray gutter into someone's file is the worse failure.
    """
    candidates = [line for line in lines if line.strip()]
    if not candidates:
        return lines
    matched = sum(1 for line in candidates if _GUTTER_LINE.match(line))
    if matched * 2 < len(candidates):
        return lines
    return [re.sub(r"^[ \t]*\d+\|", "", line) if line.strip() else line for line in lines]


def sanitize_suggestion(suggestion: Any) -> Tuple[Optional[str], bool]:
    """Clean a model-authored ``suggestion`` for use in a GitLab suggestion block.

    Returns ``(text, is_code)``:
    - ``(code, True)``  — safe to wrap in ```suggestion and offer as applyable.
    - ``(prose, False)`` — no usable code; post as plain text so the reviewer's
      point survives without an Apply button that would corrupt the file.
    - ``(None, False)`` — nothing left worth posting.
    """
    if not isinstance(suggestion, str) or not suggestion.strip():
        return None, False

    # Only newlines are trimmed here: the first line's leading whitespace is the
    # code's indentation, and the block replaces the whole line, so stripping it
    # would apply a dedented line into the file.
    text = _unwrap_fence(suggestion.strip("\n"))
    lines = _strip_gutter(text.splitlines())
    # Fence markers anywhere are markup, never content — a mid-text fence just
    # means the model emitted more than one block.
    lines = [line for line in lines if not _FENCE_LINE.match(line)]
    unpeeled = list(lines)

    # Peel prose and lead-ins off the top, then off the bottom, so an explanation
    # wrapped around the code goes away but the code itself is untouched.
    while lines and (
        not lines[0].strip()
        or _LEAD_IN_LINE.match(lines[0])
        or _looks_like_prose(lines[0])
    ):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _looks_like_prose(lines[-1])):
        lines.pop()

    if lines:
        lines[0] = _LEAD_IN_PREFIX.sub("", lines[0])

    code = "\n".join(lines).strip("\n")
    if code.strip():
        return code, True

    # Everything was prose. Keep it (minus the lead-ins and markup) as plain
    # text: an unhelpful comment beats a suggestion that commits English into
    # the file.
    fallback = "\n".join(
        _LEAD_IN_PREFIX.sub("", line)
        for line in unpeeled
        if not _LEAD_IN_LINE.match(line)
    ).strip()
    return (fallback, False) if fallback else (None, False)


def get_severity(issue: Any) -> str:
    """
    Extract severity from an issue (handles both dict and object formats).
    
    Args:
        issue: Review issue (dict or object)
    
    Returns:
        Severity string ("high", "medium", "low")
    """
    return issue.get("severity", "low") if isinstance(issue, dict) else issue.severity


def format_single_issue(issue: Any, applyable: bool = True) -> str:
    """
    Format a single issue for GitLab comment.

    Args:
        issue: Review issue (dict or object)
        applyable: True when this comment will be posted on a diff line, where
            GitLab honors a ```suggestion block and renders "Apply suggestion".
            False for the general-note fallback — a note off the diff cannot be
            applied, so the code is shown as a plain block instead of dangling an
            inert Apply affordance.

    Returns:
        Formatted comment string, with GitLab suggestion syntax when the
        suggestion is code that can safely be committed as-is.
    """
    # Handle both dict and object formats
    problem = issue.get("problem", "") if isinstance(issue, dict) else issue.problem
    suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else getattr(issue, "suggestion", "")

    # Format the comment
    comment = f"**Issue:**\n{problem}\n\n"

    cleaned, is_code = sanitize_suggestion(suggestion)
    if cleaned is None:
        return comment
    if not is_code:
        # Prose survived where code was asked for. Post it as text — fencing it as
        # a suggestion would let a reviewer commit English into their source.
        return comment + f"**Suggested Fix:**\n{cleaned}"
    fence = "suggestion:-0+0" if applyable else ""
    return comment + f"**Suggested Fix:**\n```{fence}\n{cleaned}\n```"


def extract_high_severity_issues(all_comments: list) -> list:
    """
    Extract high-severity issues from all comments.
    
    Args:
        all_comments: List of tuples (file_path, issues) where issues is a list of issue objects
        
    Returns:
        List of high-severity issues across all files
    """
    all_high_severity_issues = []
    for file_path, issues in all_comments:
        high_issues = [issue for issue in issues if get_severity(issue) == "high"]
        all_high_severity_issues.extend(high_issues)

    return all_high_severity_issues


# Hard caps on the review summary. The prompt states these too, but a prompt is a
# request and this is the last writer before the comment is posted — the same
# belt-and-braces the suggestion field gets (SUGGESTION_FIELD_RULE +
# sanitize_suggestion). A summary nobody reads is worth nothing, and the failure
# mode of a chatty model is a wall of text that gets scrolled past, so length is
# enforced rather than asked for.
MAX_SUMMARY_OVERVIEW_CHARS = 400
MAX_SUMMARY_KEY_CHANGES = 5
MAX_SUMMARY_REVIEW_FOCUS = 3
# Per-entry cap is the loosest of the three on purpose: the *count* caps above do
# most of the length work, and clipping an entry mid-thought ("…the most
# failure-prone…") destroys the very information the entry existed to convey —
# worse than one slightly longer line. Worst case overall is still ~2k chars.
MAX_SUMMARY_ENTRY_CHARS = 220


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` at a word boundary, with an ellipsis when trimmed."""
    text = " ".join(str(text).split())  # collapse newlines/runs so entries stay one line
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut or text[:limit]}…"


def _summary_entries(items, max_items: int) -> list:
    """Cap an entry list by count and per-entry length. Entries are ordered
    most-significant-first by the prompt, so dropping the tail loses the least."""
    out = []
    for item in items or []:
        text = _clip(item, MAX_SUMMARY_ENTRY_CHARS)
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def format_review_summary(summary, effort: Optional[int] = None) -> str:
    """Render a ``ReviewSummary`` as the MR's orientation comment.

    Deliberately plain markdown with no ```suggestion fences: this comment is
    context, not a finding, so it must never render an "Apply suggestion" button
    that would commit prose into someone's source (see sanitize_suggestion).

    Sections with nothing in them are omitted rather than left as empty headings.
    """
    parts = ["**🤖 Review Summary** — what this MR does and where to look. "
             "Not a review; findings are posted separately.\n"]

    overview = _clip(getattr(summary, "overview", "") or "", MAX_SUMMARY_OVERVIEW_CHARS)
    if overview:
        parts.append(f"\n{overview}\n")

    key_changes = _summary_entries(getattr(summary, "key_changes", None), MAX_SUMMARY_KEY_CHANGES)
    if key_changes:
        parts.append("\n**What changed**\n")
        parts.extend(f"- {item}\n" for item in key_changes)

    review_focus = _summary_entries(getattr(summary, "review_focus", None), MAX_SUMMARY_REVIEW_FOCUS)
    if review_focus:
        parts.append("\n**Where to focus**\n")
        parts.extend(f"- {item}\n" for item in review_focus)

    if effort is not None:
        parts.append(f"\n_Estimated review effort: **{effort}/5**_\n")

    return "".join(parts)
