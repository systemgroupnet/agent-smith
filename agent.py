from pydantic import BaseModel
import os
import re
from typing import Optional
from urllib.parse import unquote, urlparse
from dotenv import load_dotenv

# Load environment variables and setup proxy
load_dotenv()
from core.config import setup_proxy
setup_proxy()

# Import LangGraph workflow
from core.workflow.graph import review_workflow
from core.workflow.state import create_initial_state


def parse_gitlab_mr_url(url: str) -> Optional[dict]:
    """
    Parse GitLab MR URL to extract project path and MR ID.
    
    Examples:
        https://gitlab.example.com/group/project/-/merge_requests/123
        https://gitlab.example.com/group/project/-/merge_requests/123/diffs
    
    Returns:
        Dictionary with 'domain', 'project_path', and 'mr_id' or None if parsing fails
    """
    # Decode URL-encoded characters (e.g., %2F -> /) to handle Windows/encoded URLs
    decoded_url = unquote(url)
    
    # Extract domain and path separately
    parsed = urlparse(decoded_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    
    # Extract project path and MR ID from the path
    # Path format: /group/project/-/merge_requests/123
    match = re.search(r'^/(.+?)/-/merge_requests/(\d+)', path)
    if not match:
        return None
    
    project_path = match.group(1)  # group/project (without leading /)
    mr_id = match.group(2)
    
    return {
        "domain": domain,
        "project_path": project_path,
        "mr_id": mr_id
    }


class WorkflowInput(BaseModel):
    """Input model for the review workflow."""
    input_as_text: str


async def run_workflow(workflow_input: WorkflowInput):
    """
    Run the code review workflow using LangGraph.
    
    Args:
        workflow_input: Input containing file information or GitLab MR URL
        
    Returns:
        Review result with issues found
    """
    # Try to extract project_path and gitlab_url from GitLab MR URL if input_as_text is a URL
    project_path: Optional[str] = None
    gitlab_url: Optional[str] = os.getenv("GITLAB_URL")
    
    # Check if input_as_text is a GitLab MR URL
    mr_info = parse_gitlab_mr_url(workflow_input.input_as_text)
    if mr_info:
        project_path = mr_info["project_path"]
        gitlab_url = mr_info["domain"]  # Full URL with protocol
        print(f"📦 Extracted project path from URL: {project_path}")
        print(f"🌐 Extracted GitLab URL: {gitlab_url}")
    else:
        # Fallback to environment variables if URL parsing fails
        project_path = os.getenv("GITLAB_PROJECT_PATH")
        if not gitlab_url:
            # If GITLAB_URL not set, try to construct from GITLAB_DOMAIN for backward compatibility
            gitlab_domain = os.getenv("GITLAB_DOMAIN")
            if gitlab_domain:
                gitlab_url = f"https://{gitlab_domain}"
                print(f"⚠️  Using GITLAB_DOMAIN (deprecated) - please use GITLAB_URL instead")
        
        if project_path:
            print(f"📦 Using project path from environment: {project_path}")
        if gitlab_url:
            print(f"🌐 Using GitLab URL from environment: {gitlab_url}")
        if not project_path and not gitlab_url:
            print("⚠️  Warning: No project path or GitLab URL found in URL or environment")
    
    # Create initial state
    initial_state = create_initial_state(
        input_text=workflow_input.input_as_text,
        gitlab_token=os.getenv("GITLAB_TOKEN"),
        gitlab_url=gitlab_url,
        project_path=project_path
    )
    
    # Run the workflow
    print("\n" + "="*60)
    print("🚀 Starting LangGraph Code Review Workflow")
    print("="*60)
    
    final_state = await review_workflow.ainvoke(initial_state)
    
    print("\n" + "="*60)
    print("✨ Workflow Complete")
    print("="*60 + "\n")
    
    # Return the output
    if final_state.get("error"):
        return {
            "error": final_state["error"]
        }
    
    output = final_state.get("output")
    if output:
        return {
            "output_text": output
        }
    
    # Fallback - return classification if no review was done
    return {
        "output_text": final_state.get("classification", {})
    }
