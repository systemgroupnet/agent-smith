from pydantic import BaseModel


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class ReviewResponse(BaseModel):
    issues: list[ReviewIssue]
