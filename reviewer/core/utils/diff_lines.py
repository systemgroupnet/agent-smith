"""Which lines did a merge request actually change?

Pure, dependency-light parsing of GitLab's unified diffs into the three kinds of
line a review finding can sit on, plus the judgement the in-diff gate cannot make:

* **added** lines (``+``), by new-side line number;
* **removals**, anchored to the new-side lines bordering the gap they left;
* **context** lines, the unchanged neighbours a hunk carries for orientation.

The in-diff gate (``main.py``) accepts any line GitLab can anchor an inline
comment to, and that includes context. Measured on this repository, a median 58%
of the lines findings passed that gate on were unchanged context — and developers
reject exactly those comments: "this was already there before this PR". On top of
that, code a merge request merely *moved* (re-indented into a new ``if``, carried
across files) shows up as ``+`` lines that say nothing about what the author wrote.
:func:`apply_changed_line_gate` demotes a finding that sits on neither a new line
nor near one.

Moved code is detected the way ``git diff --color-moved=blocks`` detects it: an
added line whose whitespace-normalised text equals a removed line's is a
*candidate*, consecutive candidates that line up with consecutive removed lines
form a **block**, and a block counts as moved only when it carries at least
:data:`MOVED_MIN_ALNUM` alphanumeric characters — four times git's bar, because
here a false move costs a finding. A very long line can therefore be a block on
its own, while ``}``, ``return nil`` or a whole ``if err != nil`` return is never
evidence of a move by itself — it is moved only as part of a block that is.
Rename detection is out of scope: a renamed file's diff already shows only its
real edits.
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.utils.comment_formatter import get_severity

#: Same pattern the position map has always parsed hunk headers with.
_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ADDED, REMOVED, CONTEXT = "+", "-", " "

#: A block of moved lines must carry this many alphanumeric characters to count.
#: Below it, matching text is coincidence, not a move.
#:
#: Four times git's ``COLOR_MOVED_MIN_ALNUM_COUNT`` (20), because the two uses
#: price a mistake differently: git only colours a line, while here a "moved"
#: line demotes a finding, which at the default verbosity means it is not posted.
#: At 20 the Go error return — ``if err != nil {`` / ``return nil, err`` / ``}``,
#: exactly 20 — counted as moved whenever the MR deleted the same three lines
#: anywhere, so a high on a new function's error path ("returns without
#: ``tx.Rollback()``") was demoted and, by default, dropped. Boilerplate is
#: short, and the commonest idioms
#: all sit well under 80: that block is 20, with a ``tx.Rollback()`` in it 30,
#: ``ctx, cancel := context.WithTimeout(…)`` plus ``defer cancel()`` 52. Code
#: that is really carried — a four-line function body, a query plus its row loop
#: re-indented into a new ``if`` — is 100 and up. A short block that did move
#: stays new and keeps its severity: the cheaper of the two errors.
#:
#: A second rule, "at least two lines that are not boilerplate", was considered
#: and left out: 80 alphanumerics already exclude every idiom above, and a single
#: line long enough to reach 80 on its own (a long query string, a long log call)
#: is not boilerplate — demanding a second line would only stop finding real moves.
MOVED_MIN_ALNUM = 80

#: A line matching more removed lines than this may extend a moved block but not
#: start one. Without the bound, ``}`` and blank lines — thousands of each on a
#: large merge request — make block search quadratic. Such a line is still found
#: at the head of a block by walking back from the first distinctive line.
MAX_BLOCK_START_CANDIDATES = 256

#: Characters of one file's diff carried into its reviewer's prompt — the same
#: per-file cap the summary input uses, so one generated file cannot crowd out
#: the instructions around it.
MAX_PROMPT_DIFF_CHARS = 12_000

#: Omitted hunk headers listed after a truncated diff, so a reviewer still knows
#: which line ranges changed in the part it was not shown.
MAX_OMITTED_HUNKS_LISTED = 30

CHANGED_LINE_RADIUS_ENV = "REVIEW_CHANGED_LINE_RADIUS"
DEFAULT_CHANGED_LINE_RADIUS = 1

#: One level down. ``low`` has nowhere to go and leaves scope at every level.
_DEMOTE = {"high": "medium", "medium": "low", "low": ""}
_RANK = {"high": 3, "medium": 2, "low": 1}


# ── Parsing ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiffLine:
    """One body line of a hunk.

    ``new_ln`` of a removed line is the new-side line now standing where it was —
    the line after the gap — which is what a removal is anchored to.
    """

    kind: str
    old_ln: Optional[int]
    new_ln: int
    text: str


@dataclass(frozen=True)
class ParsedDiff:
    """A file's diff as an ordered list of hunk body lines."""

    lines: Tuple[DiffLine, ...] = ()

    @property
    def added(self) -> Dict[int, str]:
        """``{new_line: text}`` for every ``+`` line."""
        return {dl.new_ln: dl.text for dl in self.lines if dl.kind == ADDED}

    @property
    def removed(self) -> List[DiffLine]:
        return [dl for dl in self.lines if dl.kind == REMOVED]

    @property
    def context(self) -> Dict[int, int]:
        """``{new_line: old_line}`` for every unchanged line the hunks carry."""
        return {dl.new_ln: dl.old_ln for dl in self.lines if dl.kind == CONTEXT}

    def removal_anchors(self) -> Set[int]:
        """New-side lines bordering a removal: the one before the gap and the one after.

        Both, because a finding about deleted code ("the nil check is gone") is
        anchored to whichever neighbour the reviewer picked, and GitLab can only
        place it on the new side.
        """
        return _anchors(dl for dl in self.lines if dl.kind == REMOVED)

    def positions(self) -> Dict[int, Optional[int]]:
        """``{new_line: old_line}`` with ``None`` for an added line — the inline
        positions GitLab accepts. Built in diff order, so a malformed diff that
        repeats a line number resolves exactly as the original parser did."""
        out: Dict[int, Optional[int]] = {}
        for dl in self.lines:
            if dl.kind == ADDED:
                out[dl.new_ln] = None
            elif dl.kind == CONTEXT:
                out[dl.new_ln] = dl.old_ln
        return out

    @property
    def new_text(self) -> Dict[int, str]:
        """``{new_line: text}`` for every line :meth:`positions` returns — what an
        inline comment on that line sits on. Same order and the same resolution of
        a repeated number, so the text is always of the line GitLab anchors to."""
        return {dl.new_ln: dl.text for dl in self.lines if dl.kind in (ADDED, CONTEXT)}

    def added_runs(self) -> List[List[DiffLine]]:
        """Added lines that are adjacent in the new file."""
        return _runs(self.lines, ADDED)

    def removed_runs(self) -> List[List[DiffLine]]:
        """Removed lines that were adjacent in the old file."""
        return _runs(self.lines, REMOVED)


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    """Parse a GitLab unified diff (the ``diff`` field of an MR change).

    Line accounting is exactly the in-diff gate's: a header that does not parse
    suspends the hunk until the next one that does, an empty string is skipped,
    and ``\\ No newline at end of file`` is not a line.
    """
    out: List[DiffLine] = []
    if not diff_text:
        return ParsedDiff()

    old_ln = new_ln = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.startswith("@@"):
            m = _HUNK_HEADER.match(raw)
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
            out.append(DiffLine(ADDED, None, new_ln, raw[1:]))
            new_ln += 1
        elif tag == "-":
            out.append(DiffLine(REMOVED, old_ln, new_ln, raw[1:]))
            old_ln += 1
        elif tag == "\\":
            continue
        else:
            out.append(DiffLine(CONTEXT, old_ln, new_ln, raw[1:]))
            old_ln += 1
            new_ln += 1
    return ParsedDiff(tuple(out))


def _runs(lines: Iterable[DiffLine], kind: str) -> List[List[DiffLine]]:
    """Maximal stretches of one kind that are consecutive on their own side.

    Consecutive numbering is the right test: a context line consumes a number on
    both sides and so breaks a run, while a ``-`` line between two ``+`` lines does
    not, since the two added lines are still neighbours in the new file.
    """
    runs: List[List[DiffLine]] = []
    current: List[DiffLine] = []
    prev: Optional[int] = None
    for dl in lines:
        if dl.kind != kind:
            continue
        num = dl.new_ln if kind == ADDED else dl.old_ln
        if current and prev is not None and num == prev + 1:
            current.append(dl)
        else:
            if current:
                runs.append(current)
            current = [dl]
        prev = num
    if current:
        runs.append(current)
    return runs


def _anchors(removed: Iterable[DiffLine]) -> Set[int]:
    anchors: Set[int] = set()
    for dl in removed:
        anchors.update(n for n in (dl.new_ln - 1, dl.new_ln) if n > 0)
    return anchors


def diffs_by_path(changes: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """``{path: diff}`` keyed the way the position maps are (new path, else old)."""
    out: Dict[str, str] = {}
    for change in changes or ():
        path = change.get("new_path") or change.get("old_path")
        if path:
            out[path] = change.get("diff", "") or ""
    return out


# ── Moved-code detection ──────────────────────────────────────────────────────


def normalize_line(text: str) -> str:
    """Whitespace-insensitive identity: re-indenting or re-aligning is not a change."""
    return " ".join((text or "").split())


def _alnum(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


class _RunPool:
    """The other side's runs, indexed by normalised text."""

    def __init__(self, runs: Iterable[Sequence[str]]) -> None:
        self.runs: List[Sequence[str]] = list(runs)
        self.index: Dict[str, List[Tuple[int, int]]] = {}
        for r, run in enumerate(self.runs):
            for k, text in enumerate(run):
                self.index.setdefault(text, []).append((r, k))

    def text_at(self, r: int, k: int) -> Optional[str]:
        run = self.runs[r]
        return run[k] if 0 <= k < len(run) else None

    def positions(self, text: str) -> List[Tuple[int, int]]:
        return self.index.get(text, [])


def _moved_indices(run: Sequence[str], pool: _RunPool) -> Set[int]:
    """Indices of ``run`` that belong to a moved block, greedily, like git.

    A block grows while each next line matches the line after its counterpart on
    the other side; when it stops matching, the block is closed (kept if it carries
    :data:`MOVED_MIN_ALNUM` alphanumerics) and a new one may start on the spot.
    """
    moved: Set[int] = set()
    block: List[int] = []
    cands: Set[Tuple[int, int]] = set()
    free_from = 0  # first index not already inside a closed block

    def close() -> None:
        nonlocal free_from
        if block:
            if sum(_alnum(run[i]) for i in block) >= MOVED_MIN_ALNUM:
                moved.update(block)
            free_from = block[-1] + 1
            block.clear()

    for i, text in enumerate(run):
        if cands:
            nxt = {(r, k + 1) for r, k in cands if pool.text_at(r, k + 1) == text}
            if nxt:
                block.append(i)
                cands = nxt
                continue
            close()
            cands = set()
        starts = pool.positions(text)
        if not starts or len(starts) > MAX_BLOCK_START_CANDIDATES:
            continue
        cands = set(starts)
        block.append(i)
        # Lines too common to start a block (a blank, a `}`) may still lead one:
        # walk back over them while the other side lines up.
        back = cands
        j = i - 1
        while j >= free_from:
            back = {(r, k - 1) for r, k in back if pool.text_at(r, k - 1) == run[j]}
            if not back:
                break
            block.insert(0, j)
            j -= 1
    close()
    return moved


@dataclass(frozen=True)
class ChangedLines:
    """One file's lines as the changed-line gate sees them."""

    #: Added lines that are neither moved nor blank — what the author wrote.
    new: FrozenSet[int] = frozenset()
    #: Added lines that are code carried from elsewhere in the merge request.
    moved: FrozenSet[int] = frozenset()
    #: New-side lines bordering a genuine (not moved, not blank) removal.
    anchors: FrozenSet[int] = frozenset()
    _points: Tuple[int, ...] = field(default=(), repr=False)

    @classmethod
    def build(cls, new: Set[int], moved: Set[int], anchors: Set[int]) -> "ChangedLines":
        return cls(frozenset(new), frozenset(moved), frozenset(anchors),
                   tuple(sorted(set(new) | set(anchors))))

    def distance_to_change(self, line: int) -> Optional[int]:
        """Lines between ``line`` and the nearest new line or removal; ``None`` when
        the file has neither (everything it adds was moved)."""
        points = self._points
        if not points:
            return None
        at = bisect.bisect_left(points, line)
        near = [abs(points[i] - line) for i in (at - 1, at) if 0 <= i < len(points)]
        return min(near)


def build_changed_line_index(
    diffs: Mapping[str, str],
    review_diffs: Optional[Mapping[str, str]] = None,
) -> Dict[str, ChangedLines]:
    """Classify every changed file's lines, with moves detected across the whole MR.

    Args:
        diffs: ``{path: diff}`` for **every** change in the merge request, deleted
            files included — code moves between files, and a deleted file is where
            moved code most often comes from.
        review_diffs: On an incremental run, ``{path: diff}`` of the commits under
            review (the compare API's diffs). A file present here is classified
            from this diff — "new" means new in *these* commits — and falls back
            to its MR diff otherwise. Both number lines on the head side, which is
            what findings and the position maps use. Both diffs' lines are
            evidence of a move.
    """
    parsed = {p: parse_unified_diff(d or "") for p, d in (diffs or {}).items()}
    reviewed = {p: parse_unified_diff(d) for p, d in (review_diffs or {}).items() if d}
    sources = list(parsed.values()) + list(reviewed.values())

    removed_pool = _RunPool(
        [normalize_line(dl.text) for dl in run] for pd in sources for run in pd.removed_runs()
    )
    added_pool = _RunPool(
        [normalize_line(dl.text) for dl in run] for pd in sources for run in pd.added_runs()
    )

    index: Dict[str, ChangedLines] = {}
    for path, pd in {**parsed, **reviewed}.items():
        moved: Set[int] = set()
        for run in pd.added_runs():
            texts = [normalize_line(dl.text) for dl in run]
            moved.update(run[i].new_ln for i in _moved_indices(texts, removed_pool))
        moved_out: Set[int] = set()
        for run in pd.removed_runs():
            texts = [normalize_line(dl.text) for dl in run]
            moved_out.update(run[i].old_ln for i in _moved_indices(texts, added_pool))
        new = {ln for ln, text in pd.added.items() if ln not in moved and text.strip()}
        anchors = _anchors(
            dl for dl in pd.removed if dl.old_ln not in moved_out and dl.text.strip()
        )
        index[path] = ChangedLines.build(new, moved, anchors)
    return index


# ── The changed-line gate ─────────────────────────────────────────────────────


def changed_line_radius() -> int:
    """``REVIEW_CHANGED_LINE_RADIUS``: how far from a change a finding may sit.

    ``0`` is allowed (only the changed lines themselves and the lines bordering a
    removal); a negative or unparseable value means the default rather than a
    startup failure, like every other numeric knob here.
    """
    raw = (os.getenv(CHANGED_LINE_RADIUS_ENV) or "").strip()
    if not raw:
        return DEFAULT_CHANGED_LINE_RADIUS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        print(f"  ⚠️  [config] Ignoring invalid {CHANGED_LINE_RADIUS_ENV}={raw!r}; "
              f"using default {DEFAULT_CHANGED_LINE_RADIUS}")
        return DEFAULT_CHANGED_LINE_RADIUS
    return value


@dataclass(frozen=True)
class LineDemotion:
    """One finding the gate demoted, for the run's warnings."""

    file_name: str
    line: int
    was: str
    now: str
    #: True when the line is moved code; False for an unchanged (context) line.
    moved: bool
    #: Lines to the nearest change, or ``None`` when the file has none.
    distance: Optional[int]
    #: True when the demoted severity is outside the run's verbosity: not posted.
    dropped: bool

    @property
    def stage(self) -> str:
        return "issue_demoted_moved_code" if self.moved else "issue_demoted_context_line"

    def describe(self, radius: int, verbosity_name: str = "default") -> str:
        where = f"{self.file_name}:{self.line}"
        if self.moved:
            what = f"{where} is code this MR moved or re-indented, not code it wrote"
        else:
            what = f"{where} is an unchanged line"
        if self.distance is None:
            near = "the file has no new line or removal"
        else:
            near = f"the nearest change is {self.distance} line(s) away (radius {radius})"
        now = self.now or "below low"
        outcome = (
            f"out of scope for verbosity {verbosity_name}, dropped before comment filtering"
            if self.dropped else "kept at the lower severity"
        )
        return f"{what}; {near}. Demoted {self.was} → {now}, {outcome}"


def _field(issue: Any, name: str) -> Any:
    return issue.get(name) if isinstance(issue, dict) else getattr(issue, name, None)


def _set_severity(issue: Any, severity: str) -> None:
    if isinstance(issue, dict):
        issue["severity"] = severity
    else:
        setattr(issue, "severity", severity)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_changed_line_gate(
    issues: Sequence[Any],
    index: Mapping[str, ChangedLines],
    real_path_of: Callable[[Any], Optional[str]],
    severities: Iterable[str] = ("high",),
    radius: int = DEFAULT_CHANGED_LINE_RADIUS,
) -> Tuple[list, List[LineDemotion]]:
    """Demote findings on code the merge request did not change.

    A finding stays as it is when its line is a new line, or within ``radius``
    lines of one or of a removal. Otherwise it is demoted one level — the same
    semantics as the comment reviewer's ``severity_corrections``: a demotion that
    leaves the level's ``severities`` is a drop, so at the default verbosity a high
    on unchanged code is not posted, while at ``review::-vv`` it posts as a medium.

    Demoted, not dropped outright, because the line *is* in the diff and the
    finding may be real; it is the claim to the author's attention that weakens.
    Fails open: an issue whose file or line cannot be resolved passes untouched.

    Returns ``(kept, demotions)``. ``kept`` is re-ordered high → medium → low
    (stably), preserving the invariant every later cap relies on: truncation sheds
    the least severe first. Kept issues are updated in place.
    """
    allowed = {str(s).strip().lower() for s in (severities or ()) if str(s).strip()}
    kept: list = []
    demotions: List[LineDemotion] = []
    for issue in issues:
        path = real_path_of(issue)
        lines = index.get(path) if path else None
        line = _as_int(_field(issue, "line"))
        was = str(get_severity(issue) or "").strip().lower()
        if lines is None or line is None or was not in _DEMOTE:
            kept.append(issue)
            continue
        distance = lines.distance_to_change(line)
        if distance is not None and distance <= radius:
            kept.append(issue)
            continue
        now = _DEMOTE[was]
        dropped = now not in allowed
        demotions.append(LineDemotion(
            file_name=str(_field(issue, "file_name") or path),
            line=line,
            was=was,
            now=now,
            moved=line in lines.moved,
            distance=distance,
            dropped=dropped,
        ))
        if not dropped:
            _set_severity(issue, now)
            kept.append(issue)
    kept.sort(key=lambda i: -_RANK.get(str(get_severity(i) or "").strip().lower(), 0))
    return kept, demotions


# ── The diff a reviewer is shown ──────────────────────────────────────────────


def clip_diff_for_prompt(diff: str, limit: int = MAX_PROMPT_DIFF_CHARS) -> str:
    """A file's diff for its reviewer's prompt, cut at a line boundary.

    A cut diff says so, and lists the hunk headers it left out: the reviewer can
    read the rest of the file from the checkout, but only the headers say which
    line ranges the merge request changed there.
    """
    text = (diff or "").rstrip("\n")
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    shown = text[: cut if cut > 0 else limit]
    headers = [
        m.group(0)
        for m in (_HUNK_HEADER.match(ln) for ln in text[len(shown):].split("\n"))
        if m
    ]
    note = f"… [diff truncated: {len(shown)} of {len(text)} characters shown"
    if headers:
        listed = ", ".join(headers[:MAX_OMITTED_HUNKS_LISTED])
        more = len(headers) - MAX_OMITTED_HUNKS_LISTED
        note += f"; {len(headers)} more hunk(s) not shown: {listed}"
        if more > 0:
            note += f" and {more} more"
    note += ". Read the file from the checkout for the changed lines not shown.]"
    return f"{shown}\n{note}"
