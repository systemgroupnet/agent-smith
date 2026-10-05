"""
Centralized System Prompts Module

This module contains all system prompts for the code review agents,
extracted from individual agent files for better maintainability and version control.

Exports:
    Service Agent Prompts:
        - get_semantic_reviewer_prompt
        - get_business_logic_reviewer_prompt
    
    File Type Specific Prompts:
        - get_sql_reviewer_prompt
        - get_test_reviewer_prompt
        - get_e2e_reviewer_prompt
        - get_typescript_reviewer_prompt
        - get_scss_reviewer_prompt
        - get_html_reviewer_prompt
        - get_pbook_reviewer_prompt
        - get_dataview_reviewer_prompt
    
    Comment Review Prompts:
        - get_comment_reviewer_prompt
        - get_comment_dedup_prompt

    Whole-MR Prompts:
        - get_review_summary_prompt
"""

from .service_prompts import (
    get_semantic_reviewer_prompt,
    get_business_logic_reviewer_prompt,
)
from .sql_prompts import get_sql_reviewer_prompt
from .test_prompts import get_test_reviewer_prompt
from .e2e_prompts import get_e2e_reviewer_prompt
from .typescript_prompts import get_typescript_reviewer_prompt
from .scss_prompts import get_scss_reviewer_prompt
from .html_prompts import get_html_reviewer_prompt
from .pbook_prompts import get_pbook_reviewer_prompt
from .dataview_prompts import get_dataview_reviewer_prompt
from .comment_prompts import get_comment_reviewer_prompt
from .dedup_prompts import get_comment_dedup_prompt
from .route_prompts import get_sql_router_prompt
from .summary_prompts import get_review_summary_prompt
from .effort_prompts import get_effort_estimator_prompt


__all__ = [
    # Service prompts
    "get_semantic_reviewer_prompt",
    "get_business_logic_reviewer_prompt",
    # File type specific prompts
    "get_sql_reviewer_prompt",
    "get_test_reviewer_prompt",
    "get_e2e_reviewer_prompt",
    "get_typescript_reviewer_prompt",
    "get_scss_reviewer_prompt",
    "get_html_reviewer_prompt",
    "get_pbook_reviewer_prompt",
    "get_dataview_reviewer_prompt",
    # Comment review prompts
    "get_comment_reviewer_prompt",
    "get_comment_dedup_prompt",
    "get_sql_router_prompt",
    # Effort estimation prompts
    "get_effort_estimator_prompt",
]
