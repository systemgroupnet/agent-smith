"""
Markdown Content Loader Utility

This module loads markdown files from GitLab repository:
1. Root .aid/{agent_type}.md (project-wide instructions)
2. Module-specific .aid/{agent_type}.md (module-specific instructions)

Markdown content is merged with module content appended after root content.
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
    Load markdown content for a specific agent type.
    
    This function:
    1. Extracts root directory from file_path
    2. Fetches root .aid/{agent_type}.md
    3. Fetches module .aid/{agent_type}.md from {module_name}/.aid/ (if module_name provided)
    4. Merges content (module appended after root with separator)
    5. Returns raw markdown content
    
    Args:
        agent_type: Type of agent (e.g., "service", "sql", "dataview")
        file_path: Path to the file being reviewed
        module_name: Module name (e.g., "auth", "sales")
        gitlab_url: GitLab instance URL
        project_path: Project path in GitLab
        source_branch: Branch to fetch from
        gitlab_token: GitLab access token
        
    Returns:
        Raw markdown content for the agent
    """
    print(f"  📋 Loading markdown content for agent type: {agent_type}")
    
    # Extract root directory from file path
    root_dir = extract_root_directory(file_path)
    if not root_dir:
        print(f"  ⚠️  Warning: Could not extract root directory from file path: {file_path}")
        return ""
    
    print(f"  📁 Root directory: {root_dir}")
    
    # Construct paths to markdown files
    root_md_path = f"{root_dir}/.aid/{agent_type}.md"
    module_md_path = None

    if module_name and "unknown" not in module_name:
        module_md_path = f"{root_dir}/{module_name}/.aid/{agent_type}.md"
    
    if module_md_path:
        print(f"  📁 Module markdown path: {module_md_path}")
    
    # Fetch root markdown content
    root_content = await fetch_md_file(
        root_md_path,
        gitlab_url,
        project_path,
        source_branch,
        gitlab_token
    )
    
    # Fetch module markdown content if module_name provided
    module_content = None
    if module_md_path:
        module_content = await fetch_md_file(
            module_md_path,
            gitlab_url,
            project_path,
            source_branch,
            gitlab_token
        )
    
    # Merge content
    merged_content = merge_md_content(root_content or "", module_content or "")
    
    # Log result
    if merged_content:
        print(f"  ✅ Loaded markdown content for {agent_type}")
    else:
        print(f"  ℹ️  No markdown content found for {agent_type}")
    
    return merged_content
