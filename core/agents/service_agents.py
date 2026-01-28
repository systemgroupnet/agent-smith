"""
Service Agents - Semantic and Business Logic Reviewers with MCP Tools

This module provides two specialized agents for reviewing Go service files:
1. Semantic Reviewer - Technical code quality and Go best practices
2. Business Logic Reviewer - Business requirements fulfillment

Both agents now use MCP tools to automatically fetch external data (Jira, Confluence, GitLab)
as needed during the review process. The LLM chooses which tools to use.
"""

import asyncio
from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from dto.general import ReviewResponse


class ServiceContext(BaseModel):
    """Context for service review agents"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    package_name: Optional[str] = None
    module_name: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


# =============================================================================
# Agent Prompts
# =============================================================================

def get_semantic_reviewer_prompt(context: ServiceContext) -> str:
    """Get the system prompt for semantic reviewer with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.package_name:
        context_info.append(f"Package: {context.package_name}")
    if context.module_name:
        context_info.append(f"Module: {context.module_name}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are a senior Go engineer performing technical code review. Focus ONLY on code quality, correctness, and Go best practices. DO NOT review business logic.

CRITICAL AREAS TO REVIEW:
1. Correctness: Nil checks, race conditions, defer in loops, type assertions
2. Error Handling: Ignored errors, missing wrapping (fmt.Errorf with %w), transaction rollbacks
3. GORM/Database: Transaction management, N+1 queries (missing Preload), SQL injection, using db vs tx
4. Context: Missing ctx parameters, not propagating context, goroutine leaks
5. Performance: DB queries in loops, missing pagination, inefficient queries
6. Security: SQL injection, logging sensitive data, hardcoded secrets
7. Go Idioms: Early returns, variable shadowing, resource cleanup

SEVERITY LEVELS:
You MUST assign a severity level to each issue:
- **high**: Bugs, errors, data corruption, security flaws, panics, race conditions, missing error handling, transaction issues, N+1 queries, memory leaks, code duplication, performance issues that will cause real problems
- **medium**: Performance issues that might cause problems, suboptimal queries, missing error wrapping
- **low**: Style issues, naming improvements, refactoring opportunities

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical.

TOOLS AVAILABLE:
You have access to MCP tools for GitLab that can help you:
- Fetch file contents from the repository to understand context
- Look up related files or dependencies
- View merge request information

Use these tools if you need additional context to perform your review, but avoid unnecessary tool calls.

CONSTRAINTS:
- Be specific: exact line numbers and WHY it's wrong
- Provide actionable fixes with code examples
- Focus on changed code (Git Diff)
- NO comments on documentation, tests, or business logic

DATABASE CONTEXT: Postgres + GORM

RESPONSE: Return ReviewResponse with "issues" array. Each issue needs: problem (specific), suggestion (actionable with code), file_name, line (exact number), severity ("high"|"medium"|"low").

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


def get_business_logic_reviewer_prompt(context: ServiceContext) -> str:
    """Get the system prompt for business logic reviewer with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.package_name:
        context_info.append(f"Package: {context.package_name}")
    if context.module_name:
        context_info.append(f"Module: {context.module_name}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are a senior software architect reviewing business logic implementation. Focus ONLY on whether code correctly implements business requirements. DO NOT review code quality or technical aspects.

CRITICAL AREAS TO REVIEW:
1. Requirements Completeness: All features from task implemented, no missing functionality
2. Business Rules: Constraints validated, status workflow followed, calculation formulas correct
3. Data Validation: Required fields present, value ranges match specs, cross-field validations
4. Workflow: State transitions match specs, approval processes correct, proper order
5. Calculations: Math operations, tax/discount logic, totals, rounding per specs
6. Edge Cases: Boundary conditions, zero quantities, refunds/cancellations, defaults
7. Data Consistency: Related entities updated together, cascading operations, audit trail
8. Integrations: External systems called correctly, notification triggers match specs

SEVERITY LEVELS:
You MUST assign a severity level to each issue:
- **high**: Core functionality missing, wrong financial calculations, forbidden state transitions, required feature missing, important business rule not validated, workflow incorrect
- **medium**: Edge case not handled, validation missing from spec, minor rule inconsistency
- **low**: Style issues, uncommon edge cases, optional features

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical.

TOOLS AVAILABLE:
You have access to MCP tools for Jira and Confluence that can help you:
- Fetch Jira issue details to understand requirements
- Retrieve Confluence documentation and specifications
- Look up related tasks and documents

You also have GitLab tools to:
- Fetch related files from the repository
- View merge request context

Use these tools to gather the information you need to properly review business logic against requirements. You SHOULD use Jira/Confluence tools if a Jira task number is provided to understand the requirements.

CONSTRAINTS:
- ALWAYS reference specific requirement or task description section
- Compare code vs task description and related documents
- Don't assume requirements - use tools to fetch documentation
- Focus on changed code (Git Diff)
- NO technical/performance/error handling review

RESPONSE: Return ReviewResponse with "issues" array. Each issue needs: problem (with requirement reference), suggestion (with spec reference), file_name, line (exact number), severity ("high"|"medium"|"low").

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


# =============================================================================
# Agent Factories
# =============================================================================

async def create_semantic_reviewer_agent():
    """
    Create semantic code reviewer agent with GitLab MCP tools.
    
    The agent can use GitLab tools to fetch additional file context if needed.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab"],  # Only GitLab tools for semantic review
        model_name="gpt-4o",
        temperature=0.0,
        max_iterations=10,
        verbose=False,
    )


async def create_business_logic_reviewer_agent():
    """
    Create business logic reviewer agent with Jira, Confluence, and GitLab MCP tools.
    
    The agent can use:
    - Jira tools to fetch task/requirement information
    - Confluence tools to fetch documentation
    - GitLab tools to fetch related files
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="gpt-4o",
        temperature=0.0,
        max_iterations=15,
        verbose=False,
    )


# =============================================================================
# Review Functions
# =============================================================================

async def run_semantic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run semantic code review with MCP tools.
    
    The agent will automatically use GitLab tools if it needs additional
    context about the codebase.
    
    Args:
        context: Service context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running semantic code review...")
    
    try:
        # Create agent with tools
        agent = await create_semantic_reviewer_agent()
        
        # Build input with context
        input_text = get_semantic_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Semantic review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Semantic review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])


async def run_business_logic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run business logic review with MCP tools.
    
    The agent will automatically use Jira/Confluence tools to fetch
    requirements and documentation as needed.
    
    Args:
        context: Service context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running business logic review...")
    
    try:
        # Create agent with tools
        agent = await create_business_logic_reviewer_agent()
        
        # Build input with context
        input_text = get_business_logic_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Business logic review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Business logic review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])


async def run_combined_service_review(context: ServiceContext) -> ReviewResponse:
    """
    Run both semantic and business logic reviews in parallel.
    
    Both agents will automatically use MCP tools as needed:
    - Semantic reviewer: Uses GitLab tools for code context
    - Business logic reviewer: Uses Jira/Confluence/GitLab tools for requirements
    
    Args:
        context: Service context with file information
        
    Returns:
        Combined ReviewResponse with all issues
    """
    print(f"\n📝 Starting combined service review (parallel)")
    print(f"   File: {context.file_name}")
    
    try:
        # Run both reviews in parallel
        semantic_task = run_semantic_review(context)
        business_task = run_business_logic_review(context)
        
        semantic_result, business_result = await asyncio.gather(
            semantic_task,
            business_task,
            return_exceptions=True
        )
        
        # Handle exceptions
        if isinstance(semantic_result, Exception):
            print(f"  ⚠️  Semantic review exception: {semantic_result}")
            semantic_result = ReviewResponse(issues=[])
        
        if isinstance(business_result, Exception):
            print(f"  ⚠️  Business logic review exception: {business_result}")
            business_result = ReviewResponse(issues=[])
        
        # Combine results
        all_issues = semantic_result.issues + business_result.issues
        
        print(f"\n✅ Combined review complete:")
        print(f"   - Semantic issues: {len(semantic_result.issues)}")
        print(f"   - Business logic issues: {len(business_result.issues)}")
        print(f"   - Total issues: {len(all_issues)}")
        
        return ReviewResponse(issues=all_issues)
        
    except Exception as e:
        print(f"  ❌ Combined review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        return ReviewResponse(issues=[])



