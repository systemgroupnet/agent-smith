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


#: Severity marker prefixed to every posted comment. A severity is decided during
#: the review and then, until now, survived only in the analytics store — on the MR
#: itself a dozen findings rendered as a dozen identical bubbles with nothing saying
#: which one to read first. The colours match the "posted by severity" panels in
#: ``dashboards/run-stats.json`` (red / orange / blue) so a chart and a comment agree.
SEVERITY_BADGES = {
    "high": "🔴 High",
    "medium": "🟠 Medium",
    "low": "🔵 Low",
}


def severity_badge(issue: Any) -> str:
    """Return the ``🔴 High``-style marker for an issue, or ``""`` when unknown.

    Unknown rather than defaulted to low: this is the one place a human reads the
    severity, and labelling an unlabelled finding is worse than labelling nothing.
    Note this differs from :func:`get_severity`, whose ``"low"`` default exists to
    keep *counting* total — a count of unknowns is harmless, a wrong badge is not.
    """
    severity = (
        issue.get("severity") if isinstance(issue, dict) else getattr(issue, "severity", None)
    )
    return SEVERITY_BADGES.get(str(severity or "").strip().lower(), "")


#: Severity ordering, for resolving a collision and for picking a fallback.
_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


def _issue_field(issue: Any, name: str) -> Any:
    return issue.get(name) if isinstance(issue, dict) else getattr(issue, name, None)


def _set_severity(issue: Any, severity: str) -> None:
    if isinstance(issue, dict):
        issue["severity"] = severity
    else:
        setattr(issue, "severity", severity)


def restore_issue_severities(
    selected: list, originals: list, severities=("high",)
) -> Tuple[list, list]:
    """Put the reviewers' severity back over whatever the comment reviewer returned.

    Returns ``(selected, changes)`` where each change is ``(file_name, line, was,
    now)``. ``selected`` is the same list, its issues updated in place.

    The comment reviewer *synthesizes* ``selected_issues``, and
    ``ReviewIssue.severity`` defaults to ``"low"`` — so a model that omits the field
    silently turns every finding it picked into a low. Observed in production: three
    runs at the **default** verbosity, where ``extract_issues_for_review`` passes
    nothing but highs, posted five comments all recorded ``low`` (and it is
    per-response — when the field goes missing it goes missing for the whole array).
    That severity then reaches the analytics store, the dashboards, and — since the
    badge exists — the comment a human reads, where a 🔵 on a real high is a lie.

    The prompt asks the model to copy the field through; this does not depend on it.
    A severity is a fact the reviewers already established, and it is recoverable:
    the issue must keep its ``file_name``/``line`` to be posted at all, so matching
    on position gives the original back. Note ``low`` is never in ``severities`` at
    any verbosity level, so any low in the model's output is corrected by definition.
    """
    allowed = tuple(dict.fromkeys(
        str(s).strip().lower() for s in (severities or ()) if str(s).strip()
    ))
    if not allowed or not selected:
        return selected, []

    def _norm(value: Any) -> str:
        return str(value or "").strip().lower()

    def _rank(severity: str) -> int:
        return _SEVERITY_RANK.get(severity, 0)

    # A merged comment ("if multiple comments relate to the same problem, synthesize
    # them into one") inherits the most severe of the group, so a position that
    # carried several findings resolves to the highest rather than the first seen.
    by_pos: dict = {}
    by_base: dict = {}
    for issue in originals:
        severity = _norm(get_severity(issue))
        if severity not in allowed:
            continue
        file_name = str(_issue_field(issue, "file_name") or "")
        line = _issue_field(issue, "line")
        if not file_name:
            continue
        for index, key in ((by_pos, (file_name, line)), (by_base, (file_name.rsplit("/", 1)[-1], line))):
            if _rank(severity) > _rank(index.get(key, "")):
                index[key] = severity

    fallback = max(allowed, key=_rank)
    changes = []
    for issue in selected:
        current = _norm(get_severity(issue))
        file_name = str(_issue_field(issue, "file_name") or "")
        line = _issue_field(issue, "line")
        want = by_pos.get((file_name, line)) or by_base.get((file_name.rsplit("/", 1)[-1], line))
        if want is None:
            # No positional match: the model rewrote file_name or line. Keep its own
            # value when the level could have produced it, otherwise assume the most
            # severe severity in scope — the input is ordered highs-first and the
            # model is asked to rank by severity, so that is the likelier truth.
            want = current if current in allowed else fallback
        if want != current:
            changes.append((file_name, line, current or "<missing>", want))
            _set_severity(issue, want)
    return selected, changes


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

    # Format the comment. The heading carries the severity badge when there is one,
    # rather than adding a line of its own — an inline comment is read in a narrow
    # column next to the code and every extra line pushes the finding down.
    badge = severity_badge(issue)
    heading = f"**{badge} severity issue**" if badge else "**Issue:**"
    comment = f"{heading}\n{problem}\n\n"

    cleaned, is_code = sanitize_suggestion(suggestion)
    if cleaned is None:
        return comment
    if not is_code:
        # Prose survived where code was asked for. Post it as text — fencing it as
        # a suggestion would let a reviewer commit English into their source.
        return comment + f"**Suggested Fix:**\n{cleaned}"
    fence = "suggestion:-0+0" if applyable else ""
    return comment + f"**Suggested Fix:**\n```{fence}\n{cleaned}\n```"


def extract_issues_for_review(all_comments: list, severities=("high",)) -> list:
    """
    Extract the issues of interest from all reviewed files, in file order.

    Args:
        all_comments: List of tuples (file_path, issues) where issues is a list of issue objects
        severities: Severities to keep. Default ``("high",)`` — the reviewer's
            terse default, where mediums and lows are counted into analytics and
            then dropped. A verbosity label widens this (see
            ``core/utils/review_verbosity.py``), and widening it here is what first
            lets a medium reach the in-diff gate at all.

    Returns:
        List of matching issues across all files, highest severity first so a
        later cap cuts mediums before highs.
    """
    wanted = {str(s).strip().lower() for s in severities}
    by_severity: dict = {s: [] for s in ("high", "medium", "low")}
    for _file_path, issues in all_comments:
        for issue in issues:
            severity = (get_severity(issue) or "").strip().lower()
            if severity in wanted:
                by_severity.setdefault(severity, []).append(issue)
    # Ordered high → medium → low on purpose: every downstream truncation takes a
    # prefix, so ordering by severity here means a cap sheds the least important
    # findings rather than whichever files happened to be reviewed last.
    return by_severity["high"] + by_severity["medium"] + by_severity["low"]




# ── Deterministic stand-in for the comment reviewer ──────────────────────────
# The comment reviewer does two jobs no other stage does: it caps the list at the
# top ~10, and its STEP 0 purge is the last chance to drop a compile-time claim
# before posting. When it fails (a provider block that even the fallback provider
# can't serve), both are lost — the old behavior was to pass every high-severity
# issue straight through, uncapped and unpurged, which is the *worst* possible
# degradation: a 30-finding MR gets flooded, and the class of false positive the
# purge exists for goes out to a human. So the fallback is bounded here instead.

#: Ceiling on a fallback selection. The same ~10 the prompt asks for, since it is
#: a property of what a human will read, not of how the list was produced.
MAX_FALLBACK_COMMENTS = 10

# Phrases that can only be a compiler / type-checker / parser complaint. The MR's
# code was built and run before the MR was opened, so none of these can be true of
# it — see NO_COMPILE_ERRORS_RULE, which asks every reviewer not to emit them.
#
# Deliberately narrow, and matched against the problem text only (the suggestion
# is code, where these words appear legitimately). Phrases that a genuine runtime
# finding might reasonably use are left out on purpose: "type mismatch" fits a real
# JSON-decoding bug, and "missing field" fits a real serialization bug, so neither
# is here. This runs *only* on the fallback path, where the LLM purge is already
# gone — it is not a second filter layered on the normal path, where a false
# positive here would silently drop a real defect the model had chosen to keep.
_COMPILE_TIME_SIGNATURES = (
    "will not compile",
    "won't compile",
    "will fail to compile",
    "does not compile",
    "doesn't compile",
    "fails to compile",
    "compilation error",
    "compilation will fail",
    "compile error",
    "compile-time error",
    "syntax error",
    "undefined symbol",
    "undefined variable",
    "undefined function",
    "undefined method",
    "undefined reference",
    "undeclared",
    "is not defined",
    "declared and not used",
    "declared but not used",
    "unused variable",
    "unused import",
    "unused parameter",
    "does not implement",
    "unimplemented method",
    "unsatisfied interface",
    "signature mismatch",
)


def looks_compile_time(issue: Any) -> bool:
    """True when an issue's problem text is a compile-time claim.

    Used by :func:`fallback_comment_selection`; see ``_COMPILE_TIME_SIGNATURES``
    for why the match is deliberately narrow.
    """
    problem = issue.get("problem", "") if isinstance(issue, dict) else getattr(issue, "problem", "")
    text = str(problem or "").lower()
    return any(sig in text for sig in _COMPILE_TIME_SIGNATURES)


def fallback_comment_selection(
    issues: list, limit: Optional[int] = None
) -> Tuple[list, list, list]:
    """Pick comments to post when the comment reviewer agent could not run.

    Returns ``(selected, purged, dropped)``: the issues to post, those removed as
    compile-time claims, and those cut by the ``limit`` (the active verbosity
    level's cap, or ``MAX_FALLBACK_COMMENTS`` when not given).

    The cut is **arbitrary** — input order, i.e. severity then file order — because
    without the model there is nothing left to rank by: every issue here is already
    past the in-diff gate. That is stated rather than dressed up, and the caller
    reports both lists so a truncation is never silent.
    """
    if limit is None:
        limit = MAX_FALLBACK_COMMENTS
    limit = max(0, limit)
    purged = [i for i in issues if looks_compile_time(i)]
    kept = [i for i in issues if not looks_compile_time(i)]
    return kept[:limit], purged, kept[limit:]


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


# ── Inline code in the review summary ────────────────────────────────────────
# The summary's whole value is in the paths and symbols it names, and it is read
# by someone skimming for ~20 seconds. GitLab renders `like this` as code, which
# is what separates "the tax is computed in order.go" from the prose around it.
# The prompt requires backticks (rule 6); this is the deterministic half, because
# a prompt is a request and the model drops them often enough to notice on a real
# MR — the same belt-and-braces as the suggestion field and the summary caps.

#: Regions that are already markup and must be left exactly as they are: an inline
#: code span (already done), a markdown link (wrapping part of the target breaks
#: the link) and a bare URL (a slash-separated run inside one is not a repo path).
_PROTECTED_SPAN = re.compile(r"`[^`]*`|\[[^\]]*\]\([^)]*\)|<?https?://[^\s>)]+>?")

#: Extensions the reviewers classify, plus the config/doc files an MR touches.
_SOURCE_EXTENSIONS = (
    "go", "ts", "tsx", "js", "jsx", "vue", "py", "sql", "html", "scss", "css",
    "json", "yml", "yaml", "md", "sh", "proto", "toml", "ini", "xml", "cs", "java",
)
_EXT_ALTERNATION = "|".join(_SOURCE_EXTENSIONS)

#: What gets wrapped — only the two shapes that cannot be an English word: something
#: path-shaped and something call-shaped. A bare CamelCase or snake_case identifier
#: is deliberately *not* matched: guessing wrong there puts backticks around a word
#: in the middle of a sentence, which is worse than leaving a symbol plain.
#: ``ts`` precedes ``tsx`` in the alternation and the trailing lookahead rejects the
#: leftover, so ``x.tsx`` backtracks to the longer extension instead of matching
#: ``x.ts`` and failing — and ``main.gopher`` matches nothing at all.
_BARE_SYMBOL = re.compile(
    r"(?<![\w/.`~-])("
    r"(?:[\w.@+-]+/){2,}[\w.@+-]+"                        # three or more segments
    rf"|[\w.@+-]+/[\w.@+-]*\.(?:{_EXT_ALTERNATION})"      # dir/file.go
    rf"|[\w@+-]+\.(?:{_EXT_ALTERNATION})"                 # file.go
    r"|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\(\)"               # Do(), svc.Do()
    r")(?![\w/])"
)


def emphasize_symbols(text: str) -> str:
    """Backtick bare paths and calls so GitLab renders them as code.

    Existing inline code, markdown links and URLs are passed through untouched.
    """
    if not text:
        return text
    pieces, last = [], 0
    for span in _PROTECTED_SPAN.finditer(text):
        pieces.append(_BARE_SYMBOL.sub(r"`\1`", text[last:span.start()]))
        pieces.append(span.group(0))
        last = span.end()
    pieces.append(_BARE_SYMBOL.sub(r"`\1`", text[last:]))
    return "".join(pieces)


def _balance_backticks(text: str) -> str:
    """Close a code span the length cap cut in half.

    ``_clip`` truncates at a word boundary and does not know about markup, so a
    long entry could end mid-span and leave one stray backtick — which GitLab
    renders as a literal backtick and then treats the *next* one as an opener,
    turning the rest of the comment into code.
    """
    return text if text.count("`") % 2 == 0 else f"{text}`"


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` at a word boundary, with an ellipsis when trimmed."""
    text = " ".join(str(text).split())  # collapse newlines/runs so entries stay one line
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut or text[:limit]}…"


def _summary_prose(text: Any, limit: int) -> str:
    """Cap a summary field, then make its paths and symbols render as code.

    Order matters: clip first so a backtick this adds can never be the one the cap
    cuts, then balance whatever the model itself left open, then wrap.
    """
    return emphasize_symbols(_balance_backticks(_clip(text, limit)))


def _summary_entries(items, max_items: int) -> list:
    """Cap an entry list by count and per-entry length. Entries are ordered
    most-significant-first by the prompt, so dropping the tail loses the least."""
    out = []
    for item in items or []:
        text = _summary_prose(item, MAX_SUMMARY_ENTRY_CHARS)
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

    overview = _summary_prose(getattr(summary, "overview", "") or "", MAX_SUMMARY_OVERVIEW_CHARS)
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
