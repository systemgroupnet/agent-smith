"""
LangChain agents module for code review.
"""

from core.agents.comment_reviewer_agent import (
    CommentReviewerAgent,
    create_comment_reviewer_agent
)

__all__ = [
    "CommentReviewerAgent",
    "create_comment_reviewer_agent",
]
