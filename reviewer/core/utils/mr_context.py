"""
Shared Merge Request Context

Builds a single "MR context" block once per merge request — MR title/description,
the full list of changed files (with change type), and the Jira ticket body — so
that every per-file reviewer agent can be told what else changed in the same MR.

This is the highest-value defense against:
  - false positives (a symbol reported as "undefined" that is actually defined in
    a sibling file changed in the same MR), and
  - missed cross-file bugs (a signature change in one file breaking a caller in
    another).

The block is computed a single time in main.process_mr and threaded through the
workflow into every agent's prompt (see core/prompts/common.format_mr_context_section).
"""

from typing import List, Optional


def build_mr_context(
    mr_title: str,
    mr_description: str,
    changed_paths: List[str],
    jira_ticket_body: str = "",
    jira_key: str = "",
) -> str:
    """Build the shared MR context block injected into every reviewer prompt.

    Args:
        mr_title: Merge request title.
        mr_description: Merge request description (may be empty).
        changed_paths: Pre-formatted "<change_type>: <path>" entries for every
            file changed in the MR (e.g. "modified: core/foo.go").
        jira_ticket_body: Jira ticket body text, or "" when unavailable.
        jira_key: Jira issue key (e.g. "GME-123"), or "" when none found.

    Returns:
        A formatted multi-line string.
    """
    lines: List[str] = []
    lines.append(f"Merge Request: {mr_title or '(untitled)'}")
    lines.append("Description:")
    lines.append(
        mr_description.strip() if mr_description and mr_description.strip() else "(none)"
    )
    lines.append("")
    lines.append(
        "Other files changed in this MR (review this file in light of these — symbols "
        "defined in these sibling files exist; flag changes that would break callers here):"
    )
    if changed_paths:
        for entry in changed_paths:
            lines.append(f"- {entry}")
    else:
        lines.append("- (no other files)")
    lines.append("")
    lines.append(f"Jira Ticket ({jira_key}):" if jira_key else "Jira Ticket:")
    lines.append(
        jira_ticket_body.strip()
        if jira_ticket_body and jira_ticket_body.strip()
        else "(not available)"
    )
    return "\n".join(lines)


async def fetch_jira_ticket_body(jira_key: Optional[str]) -> str:
    """Best-effort fetch of a Jira ticket body via the Atlassian MCP.

    Returns "" if there is no usable key, the Atlassian MCP is disabled, the
    jira_get_issue tool is unavailable, or anything goes wrong. Never raises —
    MCP failures are non-fatal, mirroring the rest of the codebase.

    Args:
        jira_key: Jira issue key extracted from the MR (e.g. "GME-123").

    Returns:
        The ticket body as a string, or "" when unavailable.
    """
    if not jira_key or jira_key == "N/A":
        return ""

    try:
        from core.MCP.toolkit_manager import get_mcp_toolkit_manager

        manager = get_mcp_toolkit_manager()
        if not manager.is_atlassian_enabled():
            return ""

        tools = manager.get_atlassian_tools()
        jira_tool = next((t for t in tools if t.name == "jira_get_issue"), None)
        if jira_tool is None:
            print("  ⚠️  [MR Context] jira_get_issue tool not available; skipping Jira body")
            return ""

        # Resolve the issue-key argument name from the tool schema (fallback: issue_key).
        arg_name = "issue_key"
        try:
            tool_args = getattr(jira_tool, "args", {}) or {}
            for candidate in ("issue_key", "issueKey", "key", "issue_id", "issueIdOrKey"):
                if candidate in tool_args:
                    arg_name = candidate
                    break
        except Exception:
            pass

        print(f"  📥 [MR Context] Fetching Jira ticket {jira_key} via MCP...")
        result = await jira_tool.ainvoke({arg_name: jira_key})
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        print(f"  ⚠️  [MR Context] Failed to fetch Jira ticket {jira_key}: {e}")
        return ""
