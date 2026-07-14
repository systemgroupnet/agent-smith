import re
from typing import Optional

from pydantic import BaseModel, field_validator


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
