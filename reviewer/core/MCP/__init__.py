"""
MCP (Model Context Protocol) Clients

This package contains MCP client modules for interacting with various services:
- Atlassian MCP: For Jira and Confluence integration (via langchain-mcp)
- GitLab MCP: For GitLab integration (via langchain-mcp)

The clients now expose tools directly through langchain-mcp for seamless
LangChain integration with HTTP/SSE transport.
"""

from .atlassian_mcp_client import (
    is_mcp_atlassian_enabled,
    get_atlassian_mcp_tools,
    close_atlassian_mcp_toolkit,
    close_atlassian_mcp_session,  # backward compatibility
)

from .gitlab_mcp_client import (
    is_mcp_gitlab_enabled,
    get_gitlab_mcp_tools,
    close_gitlab_mcp_toolkit,
    close_gitlab_mcp_session,  # backward compatibility
)

__all__ = [
    # Atlassian MCP - New LangChain integration
    "is_mcp_atlassian_enabled",
    "get_atlassian_mcp_tools",  # Main function for LangChain integration
    "close_atlassian_mcp_toolkit",
    "close_atlassian_mcp_session",
    # GitLab MCP - New LangChain integration
    "is_mcp_gitlab_enabled",
    "get_gitlab_mcp_tools",  # Main function for LangChain integration
    "close_gitlab_mcp_toolkit",
    "close_gitlab_mcp_session",
]

