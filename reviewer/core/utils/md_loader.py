"""
Markdown Content Loader Utility

This module loads per-agent instruction markdown from the reviewed GitLab repo,
in two tiers, both derived from the reviewed file's path:

1. Repo-root ``.aid/{agent_type}.md`` — project-wide instructions kept in the
   repository's top-level ``.aid/`` (e.g. acme-shop keeps ``html.md`` /
   ``scss.md`` there). Fetched from the *real* repo root, not the file's first
   path segment.
2. Module ``{module}/.aid/{agent_type}.md`` — instructions for the file's
   top-level module (e.g. ``procurement/.aid/service.md``). The module is the
   file's first path segment (an explicit ``module_name`` overrides it).

Content is merged with the module tier appended after the root tier, so
module-specific guidance is read last (wins) while still inheriting the
project-wide baseline. Both tiers are optional — a missing file is non-fatal.
"""

from typing import Optional
from core.utils.gitlab_helpers import get_gitlab_api_client


def extract_root_directory(file_path: str) -> str:
    """
    Extract the root directory from a file path.
    
    For example:
    - "acme-shop/auth/services/user.go" -> "acme-shop"
    - "crm/sales/models.go" -> "crm"
    
    Args:
        file_path: Full file path
        
    Returns:
        Root directory name (first path segment)
    """
    if not file_path:
        return ""
    
    parts = file_path.split("/")
    return parts[0] if parts else ""


def merge_md_content(root_content: str, module_content: str) -> str:
    """
    Merge root and module markdown content.
    
    Module content is appended after root content with a separator.
    
    Args:
        root_content: Content from root .aid/{agent_type}.md
        module_content: Content from module .aid/{agent_type}.md
        
    Returns:
        Combined markdown content
    """
    result = []
    
    if root_content and root_content.strip():
        result.append(root_content.strip())
    
    if module_content and module_content.strip():
        result.append(module_content.strip())
    
    if not result:
        return ""
    
    # Join with separator
    return "\n\n---\n\n".join(result)


async def fetch_md_file(
    file_path: str,
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str
) -> Optional[str]:
    """
    Fetch markdown file content from GitLab repository.
    
    Args:
        file_path: Path to markdown file in repository
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        source_branch: Branch name
        gitlab_token: GitLab access token
        
    Returns:
        File content as string, or None if file doesn't exist or error occurred
    """
    try:
        # Use GitLab API client
        api_client = get_gitlab_api_client(gitlab_url, gitlab_token)
        content = api_client.get_file_content(project_path, file_path, source_branch)
        
        if content:
            print(f"  ✅ Loaded markdown content from: {file_path}")
            return content
        else:
            print(f"  ℹ️  Markdown file not found: {file_path}")
            return None
            
    except Exception as e:
        print(f"  ⚠️  Warning: Could not fetch {file_path}: {e}")
        return None


async def load_md_content(
    agent_type: str,
    file_path: str,
    module_name: Optional[str],
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str
) -> str:
    """
    Load markdown instructions for a specific agent type from the reviewed repo.

    Fetches two tiers and merges them (module appended after root, so
    module-specific guidance is read last):
    1. Repo-root ``.aid/{agent_type}.md`` (project-wide baseline).
    2. Module ``{module}/.aid/{agent_type}.md``, where ``module`` is the explicit
       ``module_name`` when valid, otherwise the file's first path segment.

    Both tiers are optional; a file that resolves to neither yields "" (the
    reviewer then simply runs without extra instructions). This deliberately does
    NOT prefix the root tier with the file's first segment (the previous behavior
    never consulted the true repo root) and never doubles the module segment.

    Args:
        agent_type: Type of agent (e.g., "service", "sql", "dataview")
        file_path: Path to the file being reviewed
        module_name: Explicit module override; when falsy/"N/A"/unknown the
            module is derived from the file path's first segment
        gitlab_url: GitLab instance URL
        project_path: Project path in GitLab
        source_branch: Branch to fetch from
        gitlab_token: GitLab access token

    Returns:
        Raw merged markdown content for the agent ("" when nothing was found)
    """
    print(f"  📋 Loading markdown content for agent type: {agent_type}")

    if not file_path:
        print(f"  ⚠️  Warning: empty file path; cannot locate .aid instructions")
        return ""

    # Tier 1: project-wide instructions at the repository root (".aid/").
    root_md_path = f".aid/{agent_type}.md"

    # Tier 2: the file's top-level module (".../{module}/.aid/"). Prefer an
    # explicitly-passed module_name; otherwise use the first path segment. A file
    # sitting directly at the repo root (no directory) has no module tier.
    parts = [p for p in file_path.split("/") if p]
    module_dir: Optional[str] = None
    if module_name and module_name != "N/A" and "unknown" not in module_name.lower():
        module_dir = module_name
    elif len(parts) >= 2:
        module_dir = parts[0]

    module_md_path = f"{module_dir}/.aid/{agent_type}.md" if module_dir else None

    print(f"  📁 Repo-root instructions: {root_md_path}")
    if module_md_path:
        print(f"  📁 Module instructions:    {module_md_path}")

    # Fetch repo-root instructions.
    root_content = await fetch_md_file(
        root_md_path,
        gitlab_url,
        project_path,
        source_branch,
        gitlab_token
    )

    # Fetch module instructions (if this file has a module tier).
    module_content = None
    if module_md_path:
        module_content = await fetch_md_file(
            module_md_path,
            gitlab_url,
            project_path,
            source_branch,
            gitlab_token
        )

    # Merge content (module appended after root with a separator).
    merged_content = merge_md_content(root_content or "", module_content or "")

    # Log result
    if merged_content:
        print(f"  ✅ Loaded markdown content for {agent_type}")
    else:
        print(f"  ℹ️  No markdown content found for {agent_type}")

    return merged_content
