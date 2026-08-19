"""
MCP (Model Context Protocol) integration.

All MCP access goes through the centralized ``MCPToolkitManager`` in
``core.MCP.toolkit_manager``, which connects to the GitLab and Atlassian MCP
servers via ``langchain-mcp-adapters``. Only the Atlassian tools have a consumer
today: ``core/utils/mr_context.py`` reads the merge request's Jira ticket. The
reviewers read a checkout of the source branch instead of asking a server.
"""
