"""
Comment Reviewer Agent

This module provides an agent that filters and ranks high-severity review comments,
returning only the top 10 most critical issues.
"""

from typing import List, Optional
from core.agents.base_agent import BaseReviewAgent
from core.config.review_profile import ProfileError, get_profile
from core.prompts import get_comment_reviewer_prompt
from core.utils.review_verbosity import DEFAULT, Verbosity
from dto.general import CommentReviewResponse, ReviewIssue


#: The review-profile key whose house rules this stage reads (plus the profile's ``all``).
PROFILE_KEY = "comment_reviewer"


def profile_instructions() -> str:
    """The active profile's house rules for the ranking stage.

    The profile is loaded and validated before the first LLM call (``main.py``
    records a broken one as a ``config`` error and stops), so in a run this is a
    read of the cached object. Outside a run — web mode with a changed env, a test —
    a profile that fails to load costs this stage its house rules, not its ranking:
    the conventions are an addendum to a prompt that works without them, and failing
    here would drop every finding into the deterministic fallback.
    """
    try:
        return get_profile().instructions_for(PROFILE_KEY)
    except ProfileError as err:
        print(f"  ⚠️  Comment reviewer runs without house rules: {err}")
        return ""


class CommentReviewerAgent(BaseReviewAgent[CommentReviewResponse]):
    """
    Agent that filters and ranks the review comments before they are posted.

    Filters out:
    - Compile-time claims (STEP 0 of the prompt — false by construction, since the
      MR's code was built and run before the MR was opened)
    - Style/formatting suggestions
    - Documentation-related comments
    - Minor refactoring suggestions
    - Restrictive/negative comments (e.g., "don't use X") without strong rationale

    Returns: the issues worth posting, within the budget its ``verbosity`` sets —
    by default the top ~10 highs, ranked; at a raised level, everything that
    qualifies. The filters above apply at **every** level; verbosity moves the
    budget, not the bar.
    """

    def __init__(
        self,
        model_name: str = None,
        temperature: float = 0.0,
        verbosity: Optional[Verbosity] = None,
        instructions: Optional[str] = None,
    ):
        """
        Initialize comment reviewer agent.

        Args:
            model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
            temperature: Model temperature (0.0 = deterministic)
            verbosity: The MR's verbosity level (see core/utils/review_verbosity.py).
                Defaults to the terse default. Must be set before ``super().__init__``,
                which builds the prompt.
            instructions: House rules for the prompt's *Additional Instructions*.
                ``None`` reads them from the review profile; ``""`` means none.
        """
        self.verbosity = verbosity or DEFAULT
        self.instructions = profile_instructions() if instructions is None else instructions
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
        return get_comment_reviewer_prompt(self.verbosity, instructions=self.instructions)


def create_comment_reviewer_agent(
    model_name: str = None,
    temperature: float = 0.0,
    verbosity: Optional[Verbosity] = None,
    instructions: Optional[str] = None,
) -> CommentReviewerAgent:
    """
    Factory function to create a comment reviewer agent.

    Args:
        model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
        temperature: Model temperature (0.0 = deterministic)
        verbosity: The MR's verbosity level; defaults to the terse default.
        instructions: House rules; ``None`` (the default) reads the review profile.

    Returns:
        Configured comment reviewer agent
    """
    return CommentReviewerAgent(
        model_name=model_name,
        temperature=temperature,
        verbosity=verbosity,
        instructions=instructions,
    )
