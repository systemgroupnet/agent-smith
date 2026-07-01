from pydantic import BaseModel


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class ReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class CommentReviewInput(BaseModel):
    issues: list[ReviewIssue]


class CommentReviewResponse(BaseModel):
    selected_issues: list[ReviewIssue]  # Top 10 most critical
    filtered_count: int  # Number of issues filtered out
    reasoning: str  # Brief explanation of selection criteria applied
