"""
LangGraph Workflow for Code Review

This module implements the main LangGraph workflow that orchestrates
all review agents based on file type classification.
"""

from typing import Literal
from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig

from core.workflow.state import (
    ReviewState,
    StarterResponse,
    update_state_with_classification,
    add_review_to_state,
    merge_reviews,
    set_output,
    set_error,
)
from dto.general import ReviewResponse

# Import static classifier
from core.utils.file_classifier import static_classifier

# Tracing helper: keep the inherited thread_id while naming each agent run
from core.config.tracing import merge_thread_into_config

# Import all agents
from core.agents.service_agents import (
    ServiceContext,
    run_combined_service_review,
)
from core.agents.sql_agent import SqlContext, run_sql_review
from core.agents.test_agent import TestContext, run_test_review
from core.agents.pbook_agent import PbookContext, run_pbook_review
from core.agents.dataview_agent import DataviewContext, run_dataview_review
from core.agents.typescript_agent import TypescriptContext, run_typescript_review
from core.agents.html_agent import HtmlContext, run_html_review
from core.agents.scss_agent import ScssContext, run_scss_review


# =============================================================================
# Graph Nodes
# =============================================================================

async def classify_node(state: ReviewState) -> ReviewState:
    """
    Classification node - uses static file classifier to classify file type.
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with classification
    """
    print("\n" + "="*60)
    print("🔍 Step 1: Static File Classification")
    print("="*60)
    
    try:
        # Run static classifier with file_path
        result = await static_classifier.run(
            state["input_text"], 
            file_path=state.get("file_path")
        )
        
        # Update state with classification
        state = update_state_with_classification(state, result)
        
        print(f"✅ Classified as: {state['file_type']}")
        print(f"   Module: {state['module_name']}, Package: {state['package_name']}")
        
        return state
    except Exception as e:
        print(f"❌ Classification failed: {e}")
        state = set_error(state, f"Classification error: {str(e)}")
        return state


async def review_service_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """
    Service review node - runs semantic and business logic reviewers in parallel.
    
    Args:
        state: Current workflow state
        
    Returns:
        Updated state with review results
    """
    print("\n" + "="*60)
    print("🔍 Step 2: Service File Review (Parallel)")
    print("="*60)
    
    try:
        # Create service context
        context = ServiceContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            package_name=state.get("package_name"),
            module_name=state.get("module_name"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        # Run combined review (semantic + business logic in parallel)
        result = await run_combined_service_review(
            context, config=merge_thread_into_config(config, run_name="service_review")
        )
        
        # Set output directly
        state = set_output(state, result)
        
        print(f"\n✅ Service review complete: {len(result.issues)} total issues")
        
        return state
    except Exception as e:
        print(f"❌ Service review failed: {e}")
        state = set_error(state, f"Service review error: {str(e)}")
        return state


async def review_sql_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """SQL review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: SQL File Review")
    print("="*60)
    
    try:
        context = SqlContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_sql_review(
            context, config=merge_thread_into_config(config, run_name="sql_review")
        )
        state = set_output(state, result)
        
        print(f"✅ SQL review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ SQL review failed: {e}")
        state = set_error(state, f"SQL review error: {str(e)}")
        return state


async def review_test_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """Test review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: Test File Review")
    print("="*60)
    
    try:
        context = TestContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_test_review(
            context, config=merge_thread_into_config(config, run_name="test_review")
        )
        state = set_output(state, result)
        
        print(f"✅ Test review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ Test review failed: {e}")
        state = set_error(state, f"Test review error: {str(e)}")
        return state


async def review_pbook_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """Pbook review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: Pbook File Review")
    print("="*60)
    
    try:
        context = PbookContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_pbook_review(
            context, config=merge_thread_into_config(config, run_name="pbook_review")
        )
        state = set_output(state, result)
        
        print(f"✅ Pbook review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ Pbook review failed: {e}")
        state = set_error(state, f"Pbook review error: {str(e)}")
        return state


async def review_dataview_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """Dataview review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: Dataview File Review")
    print("="*60)
    
    try:
        context = DataviewContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            package_name=state.get("package_name"),
            module_name=state.get("module_name"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_dataview_review(
            context, config=merge_thread_into_config(config, run_name="dataview_review")
        )
        state = set_output(state, result)
        
        print(f"✅ Dataview review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ Dataview review failed: {e}")
        state = set_error(state, f"Dataview review error: {str(e)}")
        return state


async def review_typescript_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """TypeScript review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: TypeScript File Review")
    print("="*60)
    
    try:
        context = TypescriptContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_typescript_review(
            context, config=merge_thread_into_config(config, run_name="typescript_review")
        )
        state = set_output(state, result)
        
        print(f"✅ TypeScript review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ TypeScript review failed: {e}")
        state = set_error(state, f"TypeScript review error: {str(e)}")
        return state


async def review_html_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """HTML review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: HTML File Review")
    print("="*60)
    
    try:
        context = HtmlContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_html_review(
            context, config=merge_thread_into_config(config, run_name="html_review")
        )
        state = set_output(state, result)
        
        print(f"✅ HTML review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ HTML review failed: {e}")
        state = set_error(state, f"HTML review error: {str(e)}")
        return state


async def review_scss_node(state: ReviewState, config: RunnableConfig) -> ReviewState:
    """SCSS review node."""
    print("\n" + "="*60)
    print("🔍 Step 2: SCSS File Review")
    print("="*60)
    
    try:
        context = ScssContext(
            content=state["content"],
            diff=state["diff"],
            file_name=state["file_name"],
            file_path=state.get("file_path"),
            jira_task_number=state.get("jira_task_number"),
            gitlab_token=state.get("gitlab_token"),
            gitlab_url=state.get("gitlab_url"),
            source_branch=state.get("source_branch"),
            project_path=state.get("project_path"),
            mr_context=state.get("mr_context"),
        )
        
        result = await run_scss_review(
            context, config=merge_thread_into_config(config, run_name="scss_review")
        )
        state = set_output(state, result)
        
        print(f"✅ SCSS review complete: {len(result.issues)} issues")
        return state
    except Exception as e:
        print(f"❌ SCSS review failed: {e}")
        state = set_error(state, f"SCSS review error: {str(e)}")
        return state


# =============================================================================
# Routing Function
# =============================================================================

def route_by_file_type(state: ReviewState) -> str:
    """
    Route to appropriate reviewer based on file type.
    
    Args:
        state: Current workflow state
        
    Returns:
        Name of next node to execute
    """
    if state.get("error"):
        return END
    
    file_type = state.get("file_type", "unknown")
    
    routing_map = {
        "service": "review_service",
        "validation": "review_service",  # validation files use service reviewer
        "sql": "review_sql",
        "test": "review_test",
        "pbook": "review_pbook",
        "dataview": "review_dataview",
        "typescript": "review_typescript",
        "html": "review_html",
        "scss": "review_scss",
    }
    
    next_node = routing_map.get(file_type, END)
    print(f"\n🔀 Routing to: {next_node}")
    
    return next_node


# =============================================================================
# Build Graph
# =============================================================================

def create_review_workflow() -> StateGraph:
    """
    Create the LangGraph workflow for code review.
    
    Returns:
        Compiled StateGraph workflow
    """
    # Create graph
    workflow = StateGraph(ReviewState)
    
    # Add nodes
    workflow.add_node("classify", classify_node)
    workflow.add_node("review_service", review_service_node)
    workflow.add_node("review_sql", review_sql_node)
    workflow.add_node("review_test", review_test_node)
    workflow.add_node("review_pbook", review_pbook_node)
    workflow.add_node("review_dataview", review_dataview_node)
    workflow.add_node("review_typescript", review_typescript_node)
    workflow.add_node("review_html", review_html_node)
    workflow.add_node("review_scss", review_scss_node)
    
    # Set entry point
    workflow.set_entry_point("classify")
    
    # Add conditional edges from classify node
    workflow.add_conditional_edges(
        "classify",
        route_by_file_type,
        {
            "review_service": "review_service",
            "review_sql": "review_sql",
            "review_test": "review_test",
            "review_pbook": "review_pbook",
            "review_dataview": "review_dataview",
            "review_typescript": "review_typescript",
            "review_html": "review_html",
            "review_scss": "review_scss",
            END: END,
        }
    )
    
    # All review nodes end the workflow
    workflow.add_edge("review_service", END)
    workflow.add_edge("review_sql", END)
    workflow.add_edge("review_test", END)
    workflow.add_edge("review_pbook", END)
    workflow.add_edge("review_dataview", END)
    workflow.add_edge("review_typescript", END)
    workflow.add_edge("review_html", END)
    workflow.add_edge("review_scss", END)
    
    # Compile and return
    return workflow.compile()


# Create global workflow instance
review_workflow = create_review_workflow()

