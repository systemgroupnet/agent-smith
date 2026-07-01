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
        - get_typescript_reviewer_prompt
        - get_scss_reviewer_prompt
        - get_html_reviewer_prompt
        - get_pbook_reviewer_prompt
        - get_dataview_reviewer_prompt
    
    Classification Prompts:
        - get_starter_agent_prompt
    
    Comment Review Prompts:
        - get_comment_reviewer_prompt
"""

from .service_prompts import (
    get_semantic_reviewer_prompt,
    get_business_logic_reviewer_prompt,
)
from .sql_prompts import get_sql_reviewer_prompt
from .test_prompts import get_test_reviewer_prompt
from .typescript_prompts import get_typescript_reviewer_prompt
from .scss_prompts import get_scss_reviewer_prompt
from .html_prompts import get_html_reviewer_prompt
from .pbook_prompts import get_pbook_reviewer_prompt
from .dataview_prompts import get_dataview_reviewer_prompt
from .starter_prompts import get_starter_agent_prompt
from .comment_prompts import get_comment_reviewer_prompt


__all__ = [
    # Service prompts
    "get_semantic_reviewer_prompt",
    "get_business_logic_reviewer_prompt",
    # File type specific prompts
    "get_sql_reviewer_prompt",
    "get_test_reviewer_prompt",
    "get_typescript_reviewer_prompt",
    "get_scss_reviewer_prompt",
    "get_html_reviewer_prompt",
    "get_pbook_reviewer_prompt",
    "get_dataview_reviewer_prompt",
    # Classification prompts
    "get_starter_agent_prompt",
    # Comment review prompts
    "get_comment_reviewer_prompt",
]
