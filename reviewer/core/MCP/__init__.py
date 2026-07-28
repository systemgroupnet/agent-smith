"""
MCP (Model Context Protocol) integration.

All MCP access goes through the centralized ``MCPToolkitManager`` in
``core.MCP.toolkit_manager``, which connects to the GitLab, Atlassian and
cocoindex MCP servers and exposes their tools to the review agents (via
``langchain-mcp-adapters``).
"""
