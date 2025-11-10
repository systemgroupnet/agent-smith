"""
MCP (Model Context Protocol) Clients

This package contains MCP client modules for interacting with various services:
- Atlassian MCP: For Jira and Confluence integration
- GitLab MCP: For GitLab integration
"""

from .atlassian_mcp_client import (
    is_mcp_atlassian_enabled,
    get_jira_issue_via_mcp,
    get_confluence_page_via_mcp,
    call_atlassian_mcp_tool,
    get_mcp_atlassian_server_params
)

from .gitlab_mcp_client import (
    is_mcp_gitlab_enabled,
    get_gitlab_mr_info_via_mcp,
    get_gitlab_mr_changes_via_mcp,
    get_gitlab_file_content_via_mcp,
    post_gitlab_comment_via_mcp,
    call_gitlab_mcp_tool,
    get_mcp_gitlab_server_params
)

__all__ = [
    # Atlassian MCP
    "is_mcp_atlassian_enabled",
    "get_jira_issue_via_mcp",
    "get_confluence_page_via_mcp",
    "call_atlassian_mcp_tool",
    "get_mcp_atlassian_server_params",
    # GitLab MCP
    "is_mcp_gitlab_enabled",
    "get_gitlab_mr_info_via_mcp",
    "get_gitlab_mr_changes_via_mcp",
    "get_gitlab_file_content_via_mcp",
    "post_gitlab_comment_via_mcp",
    "call_gitlab_mcp_tool",
    "get_mcp_gitlab_server_params",
]

