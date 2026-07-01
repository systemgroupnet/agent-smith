"""
File Review Utilities

This module provides functions for reviewing files using the LangGraph workflow
and filtering comments using the comment reviewer agent.
"""

import asyncio
import json
import time
from typing import Optional, List
from langchain_core.runnables import RunnableConfig
from core.workflow.graph import review_workflow
from core.workflow.state import create_initial_state


async def review_file_with_workflow(
    file_path: str,
    content: str,
    diff: str,
    merge_request_title: str,
    target_branch: str,
    source_branch: str,
    gitlab_token: Optional[str],
    gitlab_url: Optional[str],
    project_path: Optional[str],
    mr_context: Optional[str] = None,
    config: Optional[RunnableConfig] = None,
) -> Optional[list]:
    """Review a file using LangGraph workflow.

    ``config`` carries the LangSmith thread metadata (thread_id) so this file's
    trace and every nested agent/LLM/tool run is grouped into the MR's thread.
    """
    
    async def _run_workflow() -> Optional[list]:
        """Inner function to run the workflow in isolation."""
        input_text = f"""
file_path: {file_path}
content:
{content}
diff:
{diff}
merge_request_title: {merge_request_title}
target_branch: {target_branch}
source_branch: {source_branch}
"""
        
        # Create initial state
        initial_state = create_initial_state(
            input_text=input_text,
            file_path=file_path,
            content=content,
            diff=diff,
            merge_request_title=merge_request_title,
            target_branch=target_branch,
            source_branch=source_branch,
            gitlab_token=gitlab_token,
            gitlab_url=gitlab_url,
            project_path=project_path,
            mr_context=mr_context
        )
        
        # Run workflow (config carries the LangSmith thread metadata, which LangGraph
        # forwards into each node's RunnableConfig)
        final_state = await review_workflow.ainvoke(initial_state, config=config)
        
        # Extract issues from output
        if final_state.get("error"):
            # Surface the workflow error so the file is recorded as errored and the
            # run exits non-zero, instead of masking it as "no issues found".
            raise RuntimeError(f"Workflow error: {final_state['error']}")

        output = final_state.get("output")
        if output and isinstance(output, dict):
            issues = output.get("issues", [])
            return issues
        
        return []
    
    # Run workflow in a shielded task to isolate from MCP cancel scopes
    task = asyncio.create_task(_run_workflow())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as e:
        # If cancelled (e.g., by lingering MCP cancel scope), wait for task and clear cancellation
        cancel_msg = str(e)
        try:
            # Wait for the task to complete anyway
            result = await task
            # Clear cancellation state so the main loop can continue
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            return result
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            print(f"  ⚠️  Workflow cancelled while reviewing {file_path}: {cancel_msg}")
            return None
        except Exception as inner_e:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            print(f"  ⚠️  Workflow error after cancel for {file_path}: {inner_e}")
            raise
    except Exception as e:
        # Genuine review failure (not an MCP cancel-scope race) — propagate so the
        # caller records this file as errored and the run exits non-zero.
        print(f"Error reviewing file {file_path}: {e}")
        raise


async def review_comments_with_agent(
    issues: list, config: Optional[RunnableConfig] = None
) -> list:
    """
    Filter and rank high-severity comments using the comment reviewer agent.

    Args:
        issues: List of high-severity ReviewIssue objects/dicts
        config: Optional LangSmith run config carrying the MR thread metadata

    Returns:
        List of top 10 most critical issues
    """
    if not issues:
        return []
    
    from core.agents.comment_reviewer_agent import create_comment_reviewer_agent
    
    start_time = time.time()
    print(f"  🤖 Running comment reviewer agent on {len(issues)} high-severity issues...")
    
    try:
        # Create the agent
        agent = create_comment_reviewer_agent()
        
        # Convert issues to proper format if needed
        issues_list = []
        for issue in issues:
            if isinstance(issue, dict):
                issues_list.append(issue)
            else:
                issues_list.append(issue.model_dump())
        
        # Create input as JSON string
        input_text = json.dumps({"issues": issues_list}, indent=2)
        
        # Run the agent
        result = await agent.run(input_text, config=config)
        
        elapsed_time = time.time() - start_time
        print(f"  ✅ Agent selected {len(result.selected_issues)} critical issues (⏱️  {elapsed_time:.2f}s)")
        print(f"  📊 Filtered out {result.filtered_count} less critical issues")
        print(f"  💡 Reasoning: {result.reasoning}")
        
        # Return selected issues as dicts
        return result.selected_issues
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"  ⚠️  Error running comment reviewer agent after {elapsed_time:.2f}s: {e}")
        print(f"  ℹ️  Falling back to all {len(issues)} issues")
        return issues
