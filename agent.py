from pydantic import BaseModel
import os
from dotenv import load_dotenv

# Load environment variables and setup proxy
load_dotenv()
from core.config import setup_proxy
setup_proxy()

# Import LangGraph workflow
from core.workflow.graph import review_workflow
from core.workflow.state import create_initial_state


class WorkflowInput(BaseModel):
    """Input model for the review workflow."""
    input_as_text: str


async def run_workflow(workflow_input: WorkflowInput):
    """
    Run the code review workflow using LangGraph.
    
    Args:
        workflow_input: Input containing file information
        
    Returns:
        Review result with issues found
    """
    # Create initial state
    initial_state = create_initial_state(
        input_text=workflow_input.input_as_text,
        gitlab_token=os.getenv("GITLAB_TOKEN"),
        gitlab_domain=os.getenv("GITLAB_DOMAIN", "gitlab.example.com"),
        project_path=os.getenv("GITLAB_PROJECT_PATH")
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
