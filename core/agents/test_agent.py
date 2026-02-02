"""
Test Reviewer Agent - Review Go test files with Gherkin BDD steps and MCP Tools

This module provides test review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from dto.general import ReviewResponse


class TestContext(BaseModel):
    """Context for test review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


def get_test_reviewer_prompt(context: TestContext) -> str:
    """Get test reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert test reviewer for the application. Review Go test files with Gherkin BDD step implementations.

## Critical Review Areas

### 1. Test Structure
- Proper step definition format
- Complete scenario coverage
- Clear step descriptions

### 2. Test Implementation
- Correct use of godog/BDD framework
- Proper context handling in steps
- Error handling in test steps

### 3. Test Data
- Proper test data setup and cleanup
- No hardcoded values that should be parameterized
- Realistic test scenarios

### 4. Assertions
- Comprehensive assertions
- Clear error messages
- Edge case coverage

### 5. Code Quality
- No code duplication in steps
- Proper helper functions
- Clean test organization

## Severity Levels
- **high**: Missing test coverage for critical features, incorrect assertions, broken test logic
- **medium**: Suboptimal test structure, missing edge cases
- **low**: Style issues, naming improvements

**CRITICAL**: For high severity issues, explain the impact on test reliability.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch the source code being tested from the repository
- Look up Jira issues to understand feature requirements being tested
- View Confluence documentation about testing standards
- Understand test scenarios and expected behaviors

Use these tools if you need additional context to perform your review, but avoid unnecessary tool calls.

=== Context Information ===
{context_section}

=== File Content ===
```
{context.content}
```

=== Git Diff ===
```
{context.diff}
```"""


async def create_test_reviewer_agent():
    """
    Create test reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="gpt-4o",
        temperature=0.0,
        max_iterations=10,
        verbose=False,
    )


async def run_test_review(context: TestContext) -> ReviewResponse:
    """
    Run test review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about the tests.
    
    Args:
        context: Test context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running test review...")
    
    try:
        # Create agent with tools
        agent = await create_test_reviewer_agent()
        
        # Build input with context
        input_text = get_test_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Test review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Test review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

