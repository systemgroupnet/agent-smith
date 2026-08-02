import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


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
        """Accept ints, numeric strings, or ranges like "12-15"/"12:3"; anything
        non-numeric (None, "", "N/A") becomes None rather than raising."""
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


class ReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class CommentReviewInput(BaseModel):
    issues: list[ReviewIssue]


class CommentReviewResponse(BaseModel):
    selected_issues: list[ReviewIssue]  # Top 10 most critical
    filtered_count: int  # Number of issues filtered out
    reasoning: str  # Brief explanation of selection criteria applied


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
