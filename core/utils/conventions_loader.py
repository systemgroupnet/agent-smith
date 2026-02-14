"""
Conventions Loader Utility

This module loads convention YAML files from GitLab repository:
1. Root conventions.yml (project-wide conventions)
2. Module-specific conventions.yml (module-specific conventions)

Conventions are organized by agent type (service, sql, dataview, etc.)
and merged with module conventions overriding root conventions.
"""

import yaml
from typing import Optional, Dict, Any
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


def parse_and_extract_conventions(yaml_content: str, agent_type: str) -> list:
    """
    Parse YAML content and extract conventions for a specific agent type.
    
    Expected YAML structure:
    ```yaml
    service:
      - Convention 1
      - Convention 2
    sql:
      - Convention 1
    ```
    
    Args:
        yaml_content: YAML file content as string
        agent_type: Agent type (e.g., "service", "sql", "dataview")
        
    Returns:
        List of conventions for the agent type, or empty list if not found
    """
    if not yaml_content or not yaml_content.strip():
        return []
    
    try:
        data = yaml.safe_load(yaml_content)
        
        if not data or not isinstance(data, dict):
            return []
        
        # Get conventions for this agent type
        conventions = data.get(agent_type, [])
        
        # Ensure it's a list
        if isinstance(conventions, list):
            return conventions
        elif conventions:
            # If it's a single string, wrap in list
            return [str(conventions)]
        else:
            return []
            
    except yaml.YAMLError as e:
        print(f"  ⚠️  Warning: Failed to parse YAML: {e}")
        return []
    except Exception as e:
        print(f"  ⚠️  Warning: Unexpected error parsing YAML: {e}")
        return []


def merge_conventions(root_conventions: list, module_conventions: list) -> list:
    """
    Merge root and module conventions.
    
    Module conventions are appended after root conventions.
    
    Args:
        root_conventions: Conventions from root conventions.yml
        module_conventions: Conventions from module conventions.yml
        
    Returns:
        Combined list of conventions
    """
    result = []
    
    if root_conventions:
        result.extend(root_conventions)
    
    if module_conventions:
        result.extend(module_conventions)
    
    return result


def format_conventions_text(conventions: list) -> str:
    """
    Format conventions list as readable text for agent prompts.
    
    Args:
        conventions: List of convention strings
        
    Returns:
        Formatted conventions text
    """
    if not conventions:
        return "No specific conventions defined for this file type."
    
    # Format as numbered list
    formatted = "\n".join([f"{i+1}. {conv}" for i, conv in enumerate(conventions)])
    return formatted


async def fetch_yaml_file(
    file_path: str,
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str
) -> Optional[str]:
    """
    Fetch YAML file content from GitLab repository.
    
    Args:
        file_path: Path to YAML file in repository
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
            print(f"  ✅ Loaded conventions from: {file_path}")
            return content
        else:
            print(f"  ℹ️  Conventions file not found: {file_path}")
            return None
            
    except Exception as e:
        print(f"  ⚠️  Warning: Could not fetch {file_path}: {e}")
        return None


async def load_conventions(
    agent_type: str,
    file_path: str,
    module_name: Optional[str],
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str
) -> str:
    """
    Load conventions for a specific agent type.
    
    This function:
    1. Extracts root directory from file_path
    2. Fetches root conventions.yml
    3. Fetches module conventions.yml (if module_name provided)
    4. Merges conventions (module overrides root)
    5. Returns formatted conventions text
    
    Args:
        agent_type: Type of agent (e.g., "service", "sql", "dataview")
        file_path: Path to the file being reviewed
        module_name: Module name (e.g., "auth", "sales")
        gitlab_url: GitLab instance URL
        project_path: Project path in GitLab
        source_branch: Branch to fetch from
        gitlab_token: GitLab access token
        
    Returns:
        Formatted conventions text for the agent
    """
    print(f"  📋 Loading conventions for agent type: {agent_type}")
    
    # Extract root directory from file path
    root_dir = extract_root_directory(file_path)
    if not root_dir:
        print(f"  ⚠️  Warning: Could not extract root directory from file path: {file_path}")
        return "No conventions available (could not determine root directory)."
    
    print(f"  📁 Root directory: {root_dir}")
    
    # Construct paths to conventions files
    root_conventions_path = f"{root_dir}/conventions.yml"
    module_conventions_path = None
    if module_name:
        module_conventions_path = f"{root_dir}/{module_name}/conventions.yml"
        print(f"  📁 Module directory: {root_dir}/{module_name}")
    
    # Fetch root conventions
    root_content = await fetch_yaml_file(
        root_conventions_path,
        gitlab_url,
        project_path,
        source_branch,
        gitlab_token
    )
    
    root_conventions = parse_and_extract_conventions(
        root_content or "",
        agent_type
    )
    
    # Fetch module conventions if module_name provided
    module_conventions = []
    if module_conventions_path:
        module_content = await fetch_yaml_file(
            module_conventions_path,
            gitlab_url,
            project_path,
            source_branch,
            gitlab_token
        )
        
        module_conventions = parse_and_extract_conventions(
            module_content or "",
            agent_type
        )
    
    # Merge conventions
    all_conventions = merge_conventions(root_conventions, module_conventions)
    
    # Format and return
    if all_conventions:
        print(f"  ✅ Loaded {len(all_conventions)} convention(s) for {agent_type}")
    else:
        print(f"  ℹ️  No conventions found for {agent_type}")
    
    return format_conventions_text(all_conventions)

