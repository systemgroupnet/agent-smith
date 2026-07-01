"""
LangGraph State Schema for Code Review Workflow

This module defines the state structure used throughout the LangGraph workflow.
The state is shared across all nodes and contains all information needed for the review process.
"""

from typing import TypedDict, Optional, List, Dict, Any
from pydantic import BaseModel


# Import existing Pydantic models
from dto.general import ReviewResponse, ReviewIssue


class StarterResponse(BaseModel):
    """Classification response from starter agent"""
    file_name: str
    file_type: str
    package_name: str
    module_name: str
    content: str
    diff: str
    target_branch: str
    source_branch: str
    jira_task_number: str


class ReviewState(TypedDict):
    """
    LangGraph state for the code review workflow.
    
    This state is passed between all nodes in the graph and accumulates
    information as the workflow progresses.
    """
    # Input data
    input_text: str  # Raw input text
    file_path: Optional[str]
    content: str
    diff: str
    merge_request_title: Optional[str]
    target_branch: Optional[str]
    source_branch: Optional[str]
    mr_context: Optional[str]  # Shared MR-level context (description, changed files, Jira body)

    # Classification results
    file_type: Optional[str]
    classification: Optional[Dict[str, Any]]  # StarterResponse as dict
    
    # Context data
    jira_task_number: Optional[str]
    package_name: Optional[str]
    module_name: Optional[str]
    file_name: Optional[str]
    
    # External context (Jira, Confluence, GitLab)
    task_description: Optional[str]
    related_documents: Optional[str]
    package_context: Optional[str]
    
    # GitLab/MCP configuration
    gitlab_token: Optional[str]
    gitlab_url: Optional[str]  # Full URL with protocol (e.g., https://gitlab.example.com)
    project_path: Optional[str]
    
    # Review results
    reviews: List[Dict[str, Any]]  # List of ReviewResponse as dicts
    final_issues: List[Dict[str, Any]]  # List of ReviewIssue as dicts
    
    # Output
    output: Optional[Dict[str, Any]]  # Final output as dict
    
    # Workflow control
    error: Optional[str]  # Error message if any
    needs_parallel_review: bool  # Flag for service/dataview files needing parallel review


def create_initial_state(input_text: str, **kwargs) -> ReviewState:
    """
    Create initial state for the workflow.
    
    Args:
        input_text: The raw input text containing file info
        **kwargs: Additional state fields
    
    Returns:
        Initial ReviewState
    """
    state: ReviewState = {
        "input_text": input_text,
        "file_path": kwargs.get("file_path"),
        "content": kwargs.get("content", ""),
        "diff": kwargs.get("diff", ""),
        "merge_request_title": kwargs.get("merge_request_title"),
        "target_branch": kwargs.get("target_branch"),
        "source_branch": kwargs.get("source_branch"),
        "mr_context": kwargs.get("mr_context"),
        "file_type": None,
        "classification": None,
        "jira_task_number": None,
        "package_name": None,
        "module_name": None,
        "file_name": None,
        "task_description": None,
        "related_documents": None,
        "package_context": None,
        "gitlab_token": kwargs.get("gitlab_token"),
        "gitlab_url": kwargs.get("gitlab_url"),
        "project_path": kwargs.get("project_path"),
        "reviews": [],
        "final_issues": [],
        "output": None,
        "error": None,
        "needs_parallel_review": False,
    }
    return state


def update_state_with_classification(state: ReviewState, classification: StarterResponse) -> ReviewState:
    """
    Update state with classification results.
    
    Args:
        state: Current state
        classification: Classification response from starter agent
    
    Returns:
        Updated state
    """
    state["file_type"] = classification.file_type
    state["classification"] = classification.model_dump()
    state["file_name"] = classification.file_name
    state["package_name"] = classification.package_name
    state["module_name"] = classification.module_name
    state["jira_task_number"] = classification.jira_task_number
    state["content"] = classification.content
    state["diff"] = classification.diff
    state["target_branch"] = classification.target_branch
    state["source_branch"] = classification.source_branch
    
    # Set flag for parallel review (service, dataview)
    state["needs_parallel_review"] = classification.file_type in ["service", "dataview"]
    
    return state


def add_review_to_state(state: ReviewState, review: ReviewResponse) -> ReviewState:
    """
    Add a review result to the state.
    
    Args:
        state: Current state
        review: Review response from an agent
    
    Returns:
        Updated state
    """
    state["reviews"].append(review.model_dump())
    return state


def merge_reviews(state: ReviewState) -> ReviewState:
    """
    Merge all reviews in state into final_issues.
    
    Args:
        state: Current state with multiple reviews
    
    Returns:
        Updated state with merged issues
    """
    all_issues = []
    for review in state["reviews"]:
        all_issues.extend(review.get("issues", []))
    
    state["final_issues"] = all_issues
    return state


def set_output(state: ReviewState, output: ReviewResponse) -> ReviewState:
    """
    Set final output in state.
    
    Args:
        state: Current state
        output: Final review response
    
    Returns:
        Updated state with output
    """
    state["output"] = output.model_dump()
    state["final_issues"] = [issue.model_dump() for issue in output.issues]
    return state


def set_error(state: ReviewState, error: str) -> ReviewState:
    """
    Set error message in state.
    
    Args:
        state: Current state
        error: Error message
    
    Returns:
        Updated state with error
    """
    state["error"] = error
    return state

