"""Duplicate suppression for the comments a run is about to post.

Every earlier stage in the pipeline judges a finding **on its own**: a reviewer
sees one file, the in-diff gate asks only "is this line in the diff", and the
comment reviewer ranks a flat list it was handed in one batch. Nothing asks the
question a human asks immediately — *has this already been said?* Two things
make that question matter here:

* **Within a run.** A service file is reviewed by two sub-agents (semantic and
  business-logic) and neighbouring files often carry the same defect, so the
  same concern arrives twice in different words. The comment reviewer is asked
  to merge related comments, but it is ranking and purging at the same time and
  routinely does not.
* **Across runs, and against humans.** The review is *incremental*: a re-run
  reviews the new commits and knows nothing about what the previous run already
  posted, so an unfixed finding on a still-changed line is posted again on every
  push. And a human reviewer who already raised the point gets it repeated back
  at them by the agent.

This module is the deterministic half of the answer (normalisation, the
existing-comment view, the exact-match pre-pass, and the arithmetic that turns a
drop list into a kept list); the semantic half is the agent in
``core/agents/dedup_agent.py``. Same belt-and-braces shape as
``SUGGESTION_FIELD_RULE``/``sanitize_suggestion`` and the verbosity cap: the
prompt asks, and code decides.

**Everything here fails open.** A drop is only ever honoured when it is
positively justified — an unreadable id, an unparseable target, or a group of
candidates that all claim to duplicate each other resolves to *posting* the
comment. Losing a real defect to a dedup bug is strictly worse than the
duplicate this exists to remove.
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core.utils.comment_formatter import get_severity


#: Existing MR comments shown to the agent. An MR under active review has tens
#: of comments, not hundreds of *relevant* ones, and the list is spent
#: same-file-first (see :func:`select_existing_for_prompt`) so the cap sheds the
#: notes least likely to duplicate anything.
MAX_EXISTING_NOTES = 60
#: Per-comment body budget. A human's comment is a sentence or two; the agent's
#: own is a heading, a paragraph and a suggestion block, and the paragraph — the
#: part that can duplicate something — comes first.
MAX_NOTE_BODY_CHARS = 700
#: Total characters of existing-comment text in one prompt.
EXISTING_NOTES_CHAR_BUDGET = 30_000
#: A candidate's problem text must be at least this long before a containment
#: match counts. Below it, "missing error handling" is inside half the comments
#: on the MR and containment stops being evidence of anything.
MIN_CONTAINMENT_CHARS = 60


# Markdown that the *same* text carries differently in a candidate issue and in
# a posted note — the note went through format_single_issue (badge heading,
# backticked symbols, suggestion fence) while the candidate has not — so it is
# stripped from both before they are compared.
_FENCED_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_MARKUP = re.compile(r"[`*_~#>\[\]()]+")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_comment_text(text: Any) -> str:
    """Reduce a comment to comparable words: no markup, no case, no punctuation.

    Code fences go entirely — a suggestion block is a *derivative* of the
    finding, and two comments about the same defect can carry very different
    fixes while two comments about different defects can carry the same one.
    """
    raw = str(text or "")
    raw = _HTML_COMMENT.sub(" ", raw)
    raw = _FENCED_BLOCK.sub(" ", raw)
    raw = _MD_MARKUP.sub(" ", raw.lower())
    return _NON_WORD.sub(" ", raw).strip()


def _field(issue: Any, name: str, default: Any = None) -> Any:
    """Read a field off an issue that may be a dict or a ``ReviewIssue``.

    Both shapes reach the comment pipeline — the comment reviewer returns
    objects, its deterministic fallback returns the raw dicts — and assuming
    either one has crashed production before.
    """
    value = issue.get(name, default) if isinstance(issue, dict) else getattr(issue, name, default)
    return default if value is None else value


def _basename(path: Any) -> str:
    return str(path or "").strip().rsplit("/", 1)[-1].lower()


def collect_existing_comments(notes: Optional[Sequence[dict]]) -> List[dict]:
    """Turn GitLab's notes list into the comments a dedup check should consider.

    Keeps **both** human and agent comments, and thread replies: a human's "we
    do this on purpose" under last week's finding is exactly the signal that
    re-posting it is noise. Drops GitLab's own system notes (label/assignee
    chatter, which is not a review comment) and the agent's two bookkeeping
    notes, found by their hidden markers — re-raising a finding because it
    resembles the "reviewed up to commit …" marker would be absurd.

    Each entry: ``{note_id, author, body, file, line, resolved}``.
    """
    # Imported here rather than at module scope: gitlab_helpers imports the
    # comment formatter, and this module is imported from main.py alongside it.
    from core.utils.gitlab_helpers import MARKER_TOKEN, SUMMARY_MARKER_TOKEN

    collected: List[dict] = []
    for note in notes or []:
        if not isinstance(note, dict) or note.get("system"):
            continue
        body = (note.get("body") or "").strip()
        if not body:
            continue
        if MARKER_TOKEN in body or SUMMARY_MARKER_TOKEN in body:
            continue
        position = note.get("position") or {}
        author = (note.get("author") or {}).get("username") or "unknown"
        collected.append({
            "note_id": note.get("id"),
            "author": author,
            "body": body,
            "file": position.get("new_path") or position.get("old_path") or "",
            "line": position.get("new_line") or position.get("old_line"),
            # Only a diff thread is resolvable; None means "not applicable".
            "resolved": note.get("resolved"),
        })
    return collected


def select_existing_for_prompt(
    existing: Sequence[dict],
    candidates: Sequence[Any],
    max_notes: int = MAX_EXISTING_NOTES,
    char_budget: int = EXISTING_NOTES_CHAR_BUDGET,
) -> Tuple[List[dict], int]:
    """Pick the existing comments worth showing the agent, most relevant first.

    Relevance is "does it sit on a file this batch has a finding on" — a comment
    on a file nobody is commenting on cannot be duplicated by this batch. Within
    each group GitLab's own ordering is kept (notes come back most-recently-
    updated first), so what the cap sheds is old chatter on unrelated files.

    Returns ``(selected, omitted_count)`` — the count is stated in the payload
    so the model knows it is judging against a partial view.
    """
    candidate_files = {_basename(_field(c, "file_name", "")) for c in candidates}
    candidate_files.discard("")

    same_file = [n for n in existing if _basename(n.get("file")) in candidate_files]
    other = [n for n in existing if _basename(n.get("file")) not in candidate_files]
    ranked = same_file + other

    selected: List[dict] = []
    spent = 0
    for note in ranked:
        if len(selected) >= max_notes or spent >= char_budget:
            break
        body = condense_body(note.get("body", ""))
        spent += len(body)
        selected.append({**note, "body": body})
    return selected, len(ranked) - len(selected)


def condense_body(body: str) -> str:
    """Trim a note body to what a duplicate check needs.

    Fenced blocks are replaced by a placeholder rather than dropped silently:
    the agent's own comments end in a ```suggestion block that is often longer
    than the finding, and a human quoting a stack trace can bury one sentence
    under fifty lines — but a body that is *nothing but* code should not look
    like an empty comment.
    """
    text = _HTML_COMMENT.sub("", str(body or "")).strip()
    text = _FENCED_BLOCK.sub("[code block]", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_NOTE_BODY_CHARS:
        text = text[:MAX_NOTE_BODY_CHARS].rstrip() + " …[truncated]"
    return text


def build_dedup_input(candidates: Sequence[Any], existing: Sequence[dict]) -> Tuple[str, int]:
    """Render the agent's input: the candidates by id, then the MR's comments.

    Returns ``(payload, omitted_existing)``.
    """
    shown, omitted = select_existing_for_prompt(existing, candidates)

    parts: List[str] = [
        f"=== CANDIDATE COMMENTS ABOUT TO BE POSTED ({len(candidates)}) ===",
        "Each is identified by the [id] in front of it. Refer to candidates by that id.",
        "",
    ]
    for index, issue in enumerate(candidates):
        file_name = _field(issue, "file_name", "") or "(unknown file)"
        line = _field(issue, "line")
        location = f"{file_name}:{line}" if line is not None else file_name
        parts.append(f"[{index}] ({get_severity(issue)}) {location}")
        parts.append(f"    {str(_field(issue, 'problem', '')).strip() or '(no text)'}")
        parts.append("")

    if shown:
        parts.append(f"=== COMMENTS ALREADY ON THIS MERGE REQUEST ({len(shown)}) ===")
        parts.append(
            "Written by humans reviewing the MR and by earlier runs of this agent. "
            "Refer to them by note id."
        )
        parts.append("")
        for note in shown:
            location = ""
            if note.get("file"):
                location = f" on {note['file']}"
                if note.get("line") is not None:
                    location += f":{note['line']}"
            state = " [thread resolved]" if note.get("resolved") is True else ""
            parts.append(f"(note {note.get('note_id')}, by @{note.get('author')}{location}){state}")
            parts.append(f"    {note.get('body', '')}")
            parts.append("")
        if omitted:
            parts.append(
                f"[{omitted} older comment(s) on other files were omitted to fit the size "
                f"budget. Judge only against what you were shown.]"
            )
    else:
        parts.append("=== COMMENTS ALREADY ON THIS MERGE REQUEST (0) ===")
        parts.append(
            "None — nothing has been posted on this MR yet, so every duplicate you find "
            "must be between two candidates."
        )

    return "\n".join(parts), omitted


def deterministic_duplicates(
    candidates: Sequence[Any], existing: Sequence[dict]
) -> Dict[int, Tuple[str, str]]:
    """Find the duplicates that need no judgement, before any LLM call.

    Two narrow rules, both requiring the *same position*:

    * an earlier candidate with the same normalised problem text — the same
      finding reported twice by two reviewers of the same file;
    * an existing comment whose body contains the candidate's whole normalised
      problem text — in practice this is the agent's own comment from a previous
      run, where the text is not merely similar but literally the same.

    Returns ``{candidate_index: (duplicate_of, reason)}``. Narrow on purpose:
    everything short of a textual identity is the agent's job, and a wrong drop
    here costs a real finding.
    """
    found: Dict[int, Tuple[str, str]] = {}

    seen: Dict[Tuple[str, Any, str], int] = {}
    for index, issue in enumerate(candidates):
        key = (
            _basename(_field(issue, "file_name", "")),
            _field(issue, "line"),
            normalize_comment_text(_field(issue, "problem", "")),
        )
        if not key[2]:
            continue
        if key in seen:
            found[index] = (
                f"candidate:{seen[key]}",
                "identical text at the same position as an earlier candidate",
            )
        else:
            seen[key] = index

    for index, issue in enumerate(candidates):
        if index in found:
            continue
        problem = normalize_comment_text(_field(issue, "problem", ""))
        if len(problem) < MIN_CONTAINMENT_CHARS:
            continue
        file_base = _basename(_field(issue, "file_name", ""))
        line = _field(issue, "line")
        for note in existing:
            if _basename(note.get("file")) != file_base or note.get("line") != line:
                continue
            if problem in normalize_comment_text(note.get("body", "")):
                found[index] = (
                    f"existing:{note.get('note_id')}",
                    "the same text is already posted at this position",
                )
                break

    return found


def target_candidate(duplicate_of: str, total: int) -> Optional[int]:
    """The candidate id a drop is anchored to, or None when it isn't one.

    None covers both "anchored on an existing MR comment" (fine — that anchor is
    outside this batch and cannot itself be dropped) and "unparseable", which
    :func:`resolve_drops` treats the same way. An out-of-range id is None too.
    """
    match = re.search(r"candidate\s*[:#]?\s*(\d+)", str(duplicate_of or ""), re.IGNORECASE)
    if not match:
        return None
    index = int(match.group(1))
    return index if 0 <= index < total else None


def resolve_drops(targets: Dict[int, Optional[int]]) -> Set[int]:
    """Honour a set of drops without ever emptying a group of duplicates.

    ``targets`` maps a dropped candidate to the candidate it duplicates (None
    when it duplicates an existing MR comment). A group of candidates that all
    point at each other — "0 duplicates 1" and "1 duplicates 0", which a model
    asked for a drop list produces easily — would otherwise delete the finding
    entirely. So every drop chain is followed to its anchor, and a chain that
    closes on itself rescues its lowest-numbered member to be the one posted
    (lowest = highest ranked, since the comment reviewer returns its list most
    critical first).
    """
    dropped = set(targets)
    while True:
        rescued: Optional[int] = None
        for start in sorted(dropped):
            node: Optional[int] = start
            seen: Set[int] = set()
            while node is not None and node in dropped and node not in seen:
                seen.add(node)
                node = targets.get(node)
            if node is not None and node in seen:
                # The chain closed without reaching a surviving candidate.
                rescued = min(seen)
                break
        if rescued is None:
            return dropped
        dropped.discard(rescued)


def apply_drops(
    candidates: Sequence[Any], drops: Dict[int, Tuple[str, str]]
) -> Tuple[List[Any], List[dict]]:
    """Split the candidates into the ones to post and the ones dropped.

    Returns ``(kept, dropped_records)``; each record is
    ``{index, issue, duplicate_of, reason}`` in candidate order, which is what
    the caller turns into one analytics warning per drop.
    """
    kept: List[Any] = []
    records: List[dict] = []
    for index, issue in enumerate(candidates):
        if index in drops:
            duplicate_of, reason = drops[index]
            records.append({
                "index": index,
                "issue": issue,
                "duplicate_of": duplicate_of,
                "reason": reason,
            })
        else:
            kept.append(issue)
    return kept, records


def describe_issue(issue: Any) -> str:
    """``file:line`` for a candidate, for logs and analytics context."""
    file_name = _field(issue, "file_name", "") or "?"
    line = _field(issue, "line")
    return f"{file_name}:{line}" if line is not None else str(file_name)
