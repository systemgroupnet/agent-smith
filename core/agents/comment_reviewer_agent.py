"""
Comment Reviewer Agent

This module provides an agent that filters and ranks high-severity review comments,
returning only the top 10 most critical issues.
"""

from typing import List
from core.agents.base_agent import BaseReviewAgent
from core.prompts import get_comment_reviewer_prompt
from dto.general import CommentReviewResponse, ReviewIssue


class CommentReviewerAgent(BaseReviewAgent[CommentReviewResponse]):
    """
    Agent that filters and ranks high-severity review comments.
    
    Filters out:
    - Style/formatting suggestions
    - Documentation-related comments
    - Minor refactoring suggestions
    - Restrictive/negative comments (e.g., "don't use X") without strong rationale
    
    Returns: Top 10 most critical issues ranked by importance
    """
    
    def __init__(
        self,
        model_name: str = None,
        temperature: float = 0.0,
    ):
        """
        Initialize comment reviewer agent.
        
        Args:
            model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
            temperature: Model temperature (0.0 = deterministic)
        """
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            output_model=CommentReviewResponse
        )
    
    def get_system_prompt(self) -> str:
        """
        Get the system prompt for the comment reviewer agent.
        
        Returns:
            System prompt string with filtering and ranking instructions
        """
        return get_comment_reviewer_prompt()


def create_comment_reviewer_agent(
    model_name: str = None,
    temperature: float = 0.0,
) -> CommentReviewerAgent:
    """
    Factory function to create a comment reviewer agent.
    
    Args:
        model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
        temperature: Model temperature (0.0 = deterministic)
    
    Returns:
        Configured comment reviewer agent
    """
    return CommentReviewerAgent(
        model_name=model_name,
        temperature=temperature
    )
