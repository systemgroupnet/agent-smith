"""
Service Agents - Semantic and Business Logic Reviewers

This module provides two specialized agents for reviewing Go service files:
1. Semantic Reviewer - Technical code quality and Go best practices
2. Business Logic Reviewer - Business requirements fulfillment

Both agents work with a ServiceContext that includes external data (Jira, Confluence, GitLab).
"""

import os
import re
import requests
import asyncio
import traceback
from typing import Optional, Dict, Any
from urllib.parse import quote
from pydantic import BaseModel

from openai import OpenAI

from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse

# Import Atlassian MCP client
from core.MCP.atlassian_mcp_client import (
    is_mcp_atlassian_enabled,
    get_jira_issue_via_mcp,
    get_confluence_page_via_mcp
)


class ServiceContext(BaseModel):
    """Context for service review agents"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    package_name: Optional[str] = None
    module_name: Optional[str] = None
    jira_task_number: Optional[str] = None
    task_description: str = ""
    related_documents: str = ""
    package_context: str = ""
    gitlab_token: Optional[str] = None
    gitlab_domain: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


# =============================================================================
# Helper Functions for External Data Fetching
# =============================================================================

def translate_to_english(text: str) -> str:
    """
    Translate text to English using OpenAI.
    
    Args:
        text: Text to translate (may be in Persian or English)
        
    Returns:
        Translated text in English
    """
    if not text or len(text.strip()) == 0:
        return ""
    
    # Check if text is already in English (basic heuristic)
    # If text contains mostly ASCII characters, assume it's English
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    if ascii_chars / len(text) > 0.9:
        return text
    
    try:
        client = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        )
        
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Translate the following text to English. If it's already in English, return it as is."},
                {"role": "user", "content": text}
            ],
            temperature=0.0
        )
        
        return response.choices[0].message.content
    except Exception as e:
        print(f"  ⚠️  Translation failed: {e}")
        return text


async def get_task_description_async(jira_task_number: Optional[str]) -> str:
    """Fetch Jira task description using MCP."""
    if not jira_task_number:
        return ""
    
    print(f"  📋 Fetching Jira task description via MCP: {jira_task_number}")
    
    try:
        issue_data = await get_jira_issue_via_mcp(jira_task_number)
        if not issue_data:
            return ""
        
        description = issue_data.get("fields", {}).get("description", "")
        summary = issue_data.get("fields", {}).get("summary", "")
        
        combined_text = ""
        if description and summary:
            combined_text = f"Task: {summary}\n\nDescription:\n{description}"
        elif summary:
            combined_text = f"Task: {summary}"
        elif description:
            combined_text = description
        else:
            return ""
        
        translated_text = translate_to_english(combined_text)
        return translated_text
    except Exception as e:
        print(f"  ❌ MCP fetch failed: {e}")
        return ""


async def get_related_documents_async(jira_task_number: Optional[str]) -> str:
    """Fetch related Confluence documents using MCP."""
    if not jira_task_number:
        return ""
    
    print(f"  📚 Fetching related Confluence documents via MCP for: {jira_task_number}")
    
    try:
        issue_data = await get_jira_issue_via_mcp(jira_task_number)
        if not issue_data:
            return ""
        
        confluence_content = []
        issue_links = issue_data.get("fields", {}).get("issuelinks", [])
        
        # Process issue links to find Confluence pages
        for link in issue_links:
            outward_issue = link.get("outwardIssue")
            inward_issue = link.get("inwardIssue")
            
            for issue in [outward_issue, inward_issue]:
                if issue:
                    # Check if this is a Confluence link
                    # (Implementation depends on your Jira/Confluence setup)
                    pass
        
        if confluence_content:
            combined = "\n\n---\n\n".join(confluence_content)
            translated = translate_to_english(combined)
            return translated
        
        return ""
    except Exception as e:
        print(f"  ❌ Confluence fetch failed: {e}")
        return ""


def get_gitlab_file_content(context: ServiceContext, file_path: str) -> str:
    """Fetch file content from GitLab."""
    if not context.gitlab_token or not context.gitlab_domain or not context.project_path:
        return ""
    
    try:
        encoded_project_path = quote(context.project_path, safe='')
        encoded_file_path = quote(file_path, safe='')
        branch = context.source_branch or "main"
        
        url = f"https://{context.gitlab_domain}/api/v4/projects/{encoded_project_path}/repository/files/{encoded_file_path}/raw"
        headers = {"PRIVATE-TOKEN": context.gitlab_token}
        params = {"ref": branch}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            return response.text
        return ""
    except Exception as e:
        print(f"  ⚠️  Error fetching {file_path}: {e}")
        return ""


def get_package_context(context: ServiceContext) -> str:
    """
    Gather package context including dependencies and structure.
    Simplified version that doesn't fetch all files to avoid context overflow.
    """
    if not context.file_path:
        return ""
    
    context_parts = []
    
    # Extract imports from current file
    if context.content:
        imports = extract_imports_from_go_file(context.content)
        if imports:
            context_parts.append("=== Current File Imports ===\n" + "\n".join(imports[:20]))
    
    result = "\n".join(context_parts) if context_parts else ""
    return result


def extract_imports_from_go_file(content: str) -> list[str]:
    """Extract import statements from Go file content."""
    if not content:
        return []
    
    imports = []
    
    # Match single import: import "package"
    single_imports = re.findall(r'import\s+"([^"]+)"', content)
    imports.extend(single_imports)
    
    # Match multiple imports: import ( ... )
    multi_import_match = re.search(r'import\s+\((.*?)\)', content, re.DOTALL)
    if multi_import_match:
        import_block = multi_import_match.group(1)
        block_imports = re.findall(r'"([^"]+)"', import_block)
        imports.extend(block_imports)
    
    return imports


async def fetch_context_for_service(context: ServiceContext) -> ServiceContext:
    """
    Fetch external context (Jira, Confluence, GitLab) for service review.
    
    Args:
        context: Service context with file information
        
    Returns:
        Updated context with external data
    """
    print(f"\n📥 Fetching external data (Jira/Confluence)...")
    print(f"  🔍 MCP status: {'✅ Enabled' if is_mcp_atlassian_enabled() else '❌ Disabled'}")
    
    # Fetch task description
    task_description = await get_task_description_async(context.jira_task_number)
    context.task_description = task_description
    print(f"  ✅ Task description: {len(task_description)} chars")
    
    # Fetch related documents
    related_documents = await get_related_documents_async(context.jira_task_number)
    context.related_documents = related_documents
    print(f"  ✅ Related documents: {len(related_documents)} chars")
    
    # Fetch package context
    package_context = get_package_context(context)
    context.package_context = package_context
    print(f"  ✅ Package context gathered\n")
    
    return context


# =============================================================================
# Agent Prompts
# =============================================================================

def get_semantic_reviewer_prompt(context: ServiceContext) -> str:
    """Get the system prompt for semantic reviewer."""
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
- **high**: Bugs, errors, data corruption, security flaws, panics, race conditions, missing error handling, transaction issues, N+1 queries, memory leaks, code duplication, performance issues that you are confident will cause real problems
- **medium**: Performance issues that might cause problems but less certain, suboptimal queries, missing error wrapping
- **low**: Style issues, naming improvements, refactoring opportunities

**Performance Issue Guideline**: If you have a clear, strong concern that a performance issue will cause real problems in production, mark it as **high**. Only mark as **medium** if you're less certain about the impact.

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical. The reason should be explicit and explain the potential impact or consequence of the issue.

CONSTRAINTS:
- Be specific: exact line numbers and WHY it's wrong
- Provide actionable fixes with code examples
- Focus on changed code (Git Diff)
- Cross-check with related files in package context
- NO comments on documentation, tests, or business logic

DATABASE CONTEXT: Postgres + GORM

RESPONSE: Return ReviewResponse with "issues" array. Each issue needs: problem (specific), suggestion (actionable with code), file_name, line (exact number), severity ("high"|"medium"|"low").

Package Context (Dependencies, Structure, All Package Files):
{context.package_context if context.package_context else "Package context not available."}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


def get_business_logic_reviewer_prompt(context: ServiceContext) -> str:
    """Get the system prompt for business logic reviewer."""
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
- **high**: Bugs, errors, core functionality missing, wrong financial calculations, forbidden state transitions, required feature missing, important business rule not validated, workflow incorrect, performance issues that you are confident will cause real problems
- **medium**: Performance issues that might cause problems but less certain, edge case not handled, validation missing from spec, minor rule inconsistency
- **low**: Style issues, uncommon edge cases, optional features

**Performance Issue Guideline**: If you have a clear, strong concern that a performance issue will cause real problems in production, mark it as **high**. Only mark as **medium** if you're less certain about the impact.

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical. The reason should be explicit and explain the potential impact or consequence of the issue.

CONSTRAINTS:
- ALWAYS reference specific requirement or task description section
- Compare code vs task description and related documents
- Don't assume requirements - only report what's documented
- Cross-check with related files in package context
- Focus on changed code (Git Diff)
- NO technical/performance/error handling review

RESPONSE: Return ReviewResponse with "issues" array. Each issue needs: problem (with requirement reference), suggestion (with spec reference), file_name, line (exact number), severity ("high"|"medium"|"low").

Task Description:
{context.task_description if context.task_description else "No task description available."}

Related Documents:
{context.related_documents if context.related_documents else "No related documents available."}

Package Context (Dependencies, Structure, All Package Files):
{context.package_context if context.package_context else "Package context not available."}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


# =============================================================================
# Create Agent Instances
# =============================================================================

def create_semantic_reviewer():
    """Create semantic code reviewer agent."""
    # Note: We'll pass the prompt dynamically with context
    return create_review_agent(
        system_prompt="",  # Will be replaced with dynamic prompt
        model_name="gpt-4o",
        temperature=0.0
    )


def create_business_logic_reviewer():
    """Create business logic reviewer agent."""
    # Note: We'll pass the prompt dynamically with context
    return create_review_agent(
        system_prompt="",  # Will be replaced with dynamic prompt
        model_name="gpt-4o",
        temperature=0.0
    )


# Create singleton instances
semantic_reviewer = create_semantic_reviewer()
business_logic_reviewer = create_business_logic_reviewer()


async def run_semantic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run semantic code review.
    
    Args:
        context: Service context with file and external data
        
    Returns:
        Review response with issues
    """
    print("    " + "="*50)
    print("    🔍 LEVEL 1: Semantic & Technical Code Review")
    print("    " + "="*50)
    
    try:
        prompt = get_semantic_reviewer_prompt(context)
        result = await semantic_reviewer.run(prompt)
        
        print(f"    ✅ Found {len(result.issues)} semantic/technical issue(s)")
        for i, issue in enumerate(result.issues, 1):
            print(f"\n    Issue #{i}:")
            print(f"      📍 Line {issue.line} - [{issue.severity.upper()}]")
            print(f"      ❌ Problem: {issue.problem}")
            if issue.suggestion and issue.suggestion.strip():
                print(f"      ✅ Suggestion: {issue.suggestion}")
        
        return result
    except Exception as e:
        print(f"    ❌ Error in semantic review: {e}")
        traceback.print_exc()
        return ReviewResponse(issues=[])


async def run_business_logic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run business logic review.
    
    Args:
        context: Service context with file and external data
        
    Returns:
        Review response with issues
    """
    print("\n    " + "="*50)
    print("    🔍 LEVEL 2: Business Logic Review")
    print("    " + "="*50)
    
    try:
        prompt = get_business_logic_reviewer_prompt(context)
        result = await business_logic_reviewer.run(prompt)
        
        print(f"    ✅ Found {len(result.issues)} business logic issue(s)")
        for i, issue in enumerate(result.issues, 1):
            print(f"\n    Issue #{i}:")
            print(f"      📍 Line {issue.line} - [{issue.severity.upper()}]")
            print(f"      ❌ Problem: {issue.problem}")
            if issue.suggestion and issue.suggestion.strip():
                print(f"      ✅ Suggestion: {issue.suggestion}")
        
        return result
    except Exception as e:
        print(f"    ❌ Error in business logic review: {e}")
        traceback.print_exc()
        return ReviewResponse(issues=[])


async def run_combined_service_review(context: ServiceContext) -> ReviewResponse:
    """
    Run both semantic and business logic reviews in parallel and combine results.
    
    Args:
        context: Service context with file and external data
        
    Returns:
        Combined review response with all issues
    """
    # Fetch external context first
    context = await fetch_context_for_service(context)
    
    # Run both reviews in parallel
    semantic_result, business_result = await asyncio.gather(
        run_semantic_review(context),
        run_business_logic_review(context)
    )
    
    # Combine issues
    all_issues = semantic_result.issues + business_result.issues
    
    print("    " + "="*50)
    print(f"    📊 Total Issues Found: {len(all_issues)}")
    print("    " + "="*50 + "\n")
    
    return ReviewResponse(issues=all_issues)

