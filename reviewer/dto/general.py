import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


def coerce_line_number(value):
    """Accept ints, numeric strings, or ranges like "12-15"/"12:3"; anything
    non-numeric (None, "", "N/A") becomes None rather than raising.

    Module-level so every model that takes a line number from an LLM shares one
    implementation. There is more than one such boundary now: a reviewer's
    ``ReviewIssue`` and, in deep-agent mode, the ``record_findings`` tool's
    argument schema — which is the *first* validator a finding meets, so a second,
    stricter copy there would fail the whole tool call (and lose the findings) on
    exactly the inputs this function exists to absorb.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        return int(match.group()) if match else None
    return None


#: Words models reach for instead of the three severities they were asked for.
#: An unrecognised severity is not harmless: ``extract_issues_for_review`` keeps
#: only the level's severities, so a genuine finding labelled "critical" is
#: filtered out before the in-diff gate and never posted — silently, since it is
#: counted in neither ``issues_high`` nor anything else. Mapping is a judgement
#: call, but a defensible one is better than the drop.
_SEVERITY_ALIASES = {
    "critical": "high", "blocker": "high", "fatal": "high", "severe": "high",
    "major": "high", "error": "high",
    "moderate": "medium", "med": "medium", "warning": "medium", "warn": "medium",
    "minor": "low", "info": "low", "nit": "low", "trivial": "low", "note": "low",
}


def coerce_severity(value) -> str:
    """Normalise a model-supplied severity to exactly ``high``/``medium``/``low``.

    Lowercased and trimmed first — the posting loop writes the raw string into
    ``posted_comments`` and from there into ``review_comments.severity``, so an
    uppercase ``HIGH`` would split every by-severity dashboard panel in two while
    still passing the lowercased comparisons in ``main.py``.
    """
    text = str(value or "").strip().lower()
    if text in ("high", "medium", "low"):
        return text
    return _SEVERITY_ALIASES.get(text, "low")


class ReviewIssue(BaseModel):
    # Lenient defaults: the LLM sometimes omits fields (notably `line` for
    # file-level findings). A single missing/None field must not fail-validate
    # the whole file's ReviewResponse and abort the review.
    problem: str = ""
    suggestion: str = ""
    file_name: str = ""
    line: Optional[int] = None
    severity: str = "low"  # "high", "medium", "low"

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value):
        return coerce_line_number(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value):
        return coerce_severity(value)


class ReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class CommentReviewInput(BaseModel):
    issues: list[ReviewIssue]


class SeverityCorrection(BaseModel):
    """One finding the comment reviewer judges mislabelled, with the right level.

    A **separate channel** rather than an edit to ``selected_issues[].severity``,
    and for the reason that field cannot be trusted: it defaults to ``"low"``, so a
    response that merely omits it relabels every finding it selected — which
    happened on three production runs and is why ``restore_issue_severities``
    overwrites the field wholesale. An explicit list cannot be produced by
    omission, so it can be honoured where the field cannot.

    Only **demotions** are applied. The observed failure is inflation (four posted
    comments, all high, two of them missing-test-coverage), and allowing promotion
    would reopen that path through a different door.
    """

    file_name: str = ""
    line: Optional[int] = None
    to_severity: str = ""
    why: str = ""

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value):
        return coerce_line_number(value)

    @field_validator("to_severity", mode="before")
    @classmethod
    def _coerce_to(cls, value):
        return coerce_severity(value) if value else ""

    @field_validator("file_name", "why", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)


class CommentReviewResponse(BaseModel):
    selected_issues: list[ReviewIssue]  # Top 10 most critical
    filtered_count: int  # Number of issues filtered out
    reasoning: str  # Brief explanation of selection criteria applied
    # Findings whose severity the reviewers got wrong. Empty is the normal answer.
    severity_corrections: List[SeverityCorrection] = Field(default_factory=list)

    @field_validator("severity_corrections", mode="before")
    @classmethod
    def _coerce_corrections(cls, value):
        if value is None:
            return []
        return value if isinstance(value, (list, tuple)) else []


class SqlRoutingDecision(BaseModel):
    """Whether one Go file also needs the SQL reviewer."""

    file_path: str = ""
    needs_sql_review: bool = False
    reason: str = ""

    @field_validator("needs_sql_review", mode="before")
    @classmethod
    def _coerce_bool(cls, value):
        """A model may answer "yes"/"true"/1 instead of a JSON boolean.

        Anything unrecognised is False — the conservative direction, since a
        spurious extra reviewer costs a whole review pass on a file that has no
        SQL in it, while a missed one costs a second opinion.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value or "").strip().lower() in ("true", "yes", "y", "1")

    @field_validator("file_path", "reason", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)


class SqlRoutingResponse(BaseModel):
    """Output of the routing classifier: which Go files also go to the SQL reviewer.

    Empty by default so a degenerate response routes nothing extra, leaving the
    static assignment exactly as computed.
    """

    decisions: List[SqlRoutingDecision] = Field(default_factory=list)

    @field_validator("decisions", mode="before")
    @classmethod
    def _coerce_decisions(cls, value):
        if value is None:
            return []
        return value if isinstance(value, (list, tuple)) else []


class DuplicateComment(BaseModel):
    """One candidate comment the dedup agent judged redundant.

    Deliberately a *pointer*, never a rewritten comment: the agent names the
    candidate by its input id and says what it duplicates, and the caller filters
    its own authoritative list by that id. The comment reviewer synthesizes its
    output and that is exactly why ``restore_issue_severities`` and the posting
    loop's position safety net exist — a second synthesizing stage would need the
    same repairs. An id can't corrupt text, severity or position.
    """

    # Index of the duplicate candidate in the input list. -1 = unusable.
    candidate_id: int = -1
    # What it duplicates: "existing:<note_id>" (a comment already on the MR) or
    # "candidate:<id>" (another comment in this batch, the one being kept).
    duplicate_of: str = ""
    reason: str = ""

    @field_validator("candidate_id", mode="before")
    @classmethod
    def _coerce_candidate_id(cls, value):
        """Same leniency as ``ReviewIssue.line``: a model that answers "3" or
        "[3]" means 3, and anything unreadable becomes -1, which the caller
        ignores rather than turning into an accidental drop of candidate 0."""
        if value is None or isinstance(value, bool):
            return -1
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        match = re.search(r"-?\d+", str(value))
        return int(match.group()) if match else -1

    @field_validator("duplicate_of", "reason", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)


class CommentDedupResponse(BaseModel):
    """Output of the dedup agent: which candidates to drop, and why.

    ``duplicates`` defaults to empty so every degenerate response — an empty
    object, a missing field, a model that answers with prose — drops nothing.
    The failure mode of this stage is therefore "a duplicate slips through",
    which is the status quo it exists to improve on, never "a real finding is
    silently discarded".
    """

    duplicates: List[DuplicateComment] = Field(default_factory=list)
    reasoning: str = ""

    @field_validator("duplicates", mode="before")
    @classmethod
    def _coerce_duplicates(cls, value):
        if value is None:
            return []
        return value if isinstance(value, (list, tuple)) else []


class EffortEstimate(BaseModel):
    """How hard the whole MR is for a HUMAN reviewer, 1 (trivial) .. 5 (very hard).

    Lenient like the other outputs: a model that returns a string, a float, or an
    out-of-range number is coerced/clamped into 1..5 rather than fail-validating."""
    effort: int = 3
    reasoning: str = ""

    @field_validator("effort", mode="before")
    @classmethod
    def _clamp_effort(cls, value):
        if isinstance(value, bool):
            n = 3
        elif isinstance(value, (int, float)):
            n = int(value)
        else:
            match = re.search(r"[1-5]", str(value) if value is not None else "")
            n = int(match.group()) if match else 3
        return max(1, min(5, n))


class ReviewSummary(BaseModel):
    """Human-facing orientation for a merge request, posted once as a comment so
    a reviewer knows what they are about to read before they read it.

    Not a review: it reports what the MR *does*, never whether it is correct.
    Findings are ``ReviewIssue``\\ s and go through the normal comment pipeline.

    Lenient like the other model outputs — a model that returns a bare string
    where a list is declared gets it wrapped rather than fail-validating the
    whole summary and losing the pass.
    """

    # One short paragraph: what this MR is about, in plain language.
    overview: str = ""
    # The concrete features added / bugs fixed / refactors done.
    key_changes: List[str] = Field(default_factory=list)
    # Where a human should actually spend their attention, and why.
    review_focus: List[str] = Field(default_factory=list)

    @field_validator("key_changes", "review_focus", mode="before")
    @classmethod
    def _coerce_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            # A single string, or a newline/bullet list the model wrote as prose.
            lines = [
                re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip()
                for ln in value.splitlines()
            ]
            return [ln for ln in lines if ln]
        if isinstance(value, (list, tuple)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value)]

    @field_validator("overview", mode="before")
    @classmethod
    def _coerce_overview(cls, value):
        return "" if value is None else str(value)

    def is_empty(self) -> bool:
        """True when the model returned nothing usable — the caller skips posting
        rather than leaving an empty comment on someone's MR."""
        return not (self.overview.strip() or self.key_changes or self.review_focus)
