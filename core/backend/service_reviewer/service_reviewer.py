"""
Service Code Reviewer - Split Review Architecture

This module provides three ways to review Go service code:

1. semantic_reviewer - Focuses on technical code quality, Go best practices, and correctness
2. business_logic_reviewer - Focuses on business requirements fulfillment and business rules
3. service_reviewer - Performs comprehensive review covering both aspects

Usage:
- For technical-only review: Use semantic_reviewer
- For business logic-only review: Use business_logic_reviewer  
- For combined review: Use service_reviewer OR use run_combined_review() function

The run_combined_review() function can be called from your workflow to run both reviewers
and combine their results.
"""

from __future__ import annotations

from agents import Agent, ModelSettings, RunContextWrapper, Runner, RunConfig
from openai import OpenAI
from openai.types.shared.reasoning import Reasoning
import os
import re
import requests
from typing import Optional
from urllib.parse import quote
import asyncio
import json
import traceback

from dto.general import ReviewResponse

# Import Atlassian MCP client
from core.MCP.atlassian_mcp_client import (
    is_mcp_atlassian_enabled,
    get_jira_issue_via_mcp,
    get_confluence_page_via_mcp
)


class ServiceReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, jira_task_number: Optional[str] = None,
                 package_name: Optional[str] = None, module_name: Optional[str] = None, file_name: Optional[str] = None,
                 gitlab_token: Optional[str] = None, gitlab_domain: Optional[str] = None, 
                 source_branch: Optional[str] = None, project_path: Optional[str] = None,
                 file_path: Optional[str] = None):
        print(f"\n🔍 DEBUG - ServiceReviewerContext initialization:")
        print(f"  - jira_task_number: {jira_task_number}")
        print(f"  - package_name: {package_name}")
        print(f"  - module_name: {module_name}")
        print(f"  - file_name: {file_name}")
        print(f"  - file_path: {file_path}")
        print(f"  - gitlab_domain: {gitlab_domain}")
        print(f"  - source_branch: {source_branch}")
        print(f"  - project_path: {project_path}")
        print(f"  - gitlab_token: {'✓ (present)' if gitlab_token else '✗ (missing)'}\n")
        
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.jira_task_number = jira_task_number
        self.package_name = package_name
        self.module_name = module_name
        self.file_name = file_name
        self.file_path = file_path
        self.gitlab_token = gitlab_token
        self.gitlab_domain = gitlab_domain
        self.source_branch = source_branch
        self.project_path = project_path
        
        # Fetch task description and related documents (using MCP if available)
        print(f"\n📥 Fetching external data (Jira/Confluence)...")
        print(f"  🔍 MCP status: {'✅ Enabled' if is_mcp_atlassian_enabled() else '❌ Disabled'}")
        
        try:
            # Try to get existing event loop
            loop = asyncio.get_event_loop()
            print(f"  🔄 Using existing event loop")
        except RuntimeError:
            # If there's no event loop, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print(f"  🔄 Created new event loop")
        
        # Run async functions
        print(f"\n  📋 Fetching task description...")
        self.task_description = loop.run_until_complete(get_task_description_async(jira_task_number))
        print(f"  ✅ Task description: {len(self.task_description)} chars")
        
        print(f"\n  📚 Fetching related documents...")
        self.related_documents = loop.run_until_complete(get_related_documents_async(jira_task_number))
        print(f"  ✅ Related documents: {len(self.related_documents)} chars")
        
        # Fetch package context (includes all Go files)
        self.package_context = get_package_context(self)


# =============================================================================
# MCP Atlassian Integration
# =============================================================================
# Note: MCP client functions are now imported from core.MCP.atlassian_mcp_client
# The wrapper functions below use the MCP client when enabled, with fallback to direct API


# =============================================================================
# Async wrapper functions for Jira/Confluence (MCP ONLY)
# =============================================================================

async def get_task_description_async(jira_task_number: Optional[str]) -> str:
    """
    Fetch Jira task description using MCP.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        The task description (translated to English) or an empty string if not available
    """
    if not jira_task_number:
        print(f"  ⚠️  No Jira task number provided, skipping task description fetch")
        return ""
    
    print(f"  📋 Fetching Jira task description via MCP: {jira_task_number}")
    
    try:
        issue_data = await get_jira_issue_via_mcp(jira_task_number)
        if not issue_data:
            print(f"  ⚠️  No data returned from MCP for {jira_task_number}")
            return ""
        
        description = issue_data.get("fields", {}).get("description", "")
        summary = issue_data.get("fields", {}).get("summary", "")
        
        print(f"     - Summary present: {bool(summary)}")
        print(f"     - Description present: {bool(description)}")
        
        # Combine summary and description
        combined_text = ""
        if description and summary:
            combined_text = f"Task: {summary}\n\nDescription:\n{description}"
        elif summary:
            combined_text = f"Task: {summary}"
        elif description:
            combined_text = description
        else:
            print(f"  ⚠️  No summary or description found in issue data")
            return ""
        
        # Translate to English
        print(f"  🌐 Translating task description to English...")
        print(f"     - Original length: {len(combined_text)} chars")
        translated_text = translate_to_english(combined_text)
        print(f"     - Translated length: {len(translated_text)} chars")
        print(f"  ✅ Task description fetched and translated via MCP")
        return translated_text
    except Exception as e:
        print(f"  ❌ MCP fetch failed: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        return ""


async def get_related_documents_async(jira_task_number: Optional[str]) -> str:
    """
    Fetch related Confluence documents using MCP.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        Related documents content as a string (translated to English)
    """
    if not jira_task_number:
        print(f"  ⚠️  No Jira task number provided, skipping related documents fetch")
        return ""
    
    print(f"  📚 Fetching related Confluence documents via MCP for: {jira_task_number}")
    
    try:
        issue_data = await get_jira_issue_via_mcp(jira_task_number)
        if not issue_data:
            print(f"  ⚠️  No issue data returned from MCP")
            return ""
        
        confluence_content = []
        
        # 1. Check remote links for Confluence URLs
        issue_links = issue_data.get("fields", {}).get("issuelinks", [])
        print(f"     - Found {len(issue_links)} issue link(s)")
        
        # 2. Process issue links to find Confluence pages
        processed_links = 0
        for link in issue_links:
            outward_issue = link.get("outwardIssue")
            inward_issue = link.get("inwardIssue")
            
            # Check both outward and inward links
            for issue in [outward_issue, inward_issue]:
                if issue:
                    issue_key = issue.get("key")
                    if issue_key:
                        processed_links += 1
                        print(f"     - Processing linked issue: {issue_key}")
                        
                        # Fetch linked issue via MCP
                        linked_issue_data = await get_jira_issue_via_mcp(issue_key)
                        
                        if linked_issue_data:
                            description = linked_issue_data.get("fields", {}).get("description", "")
                            summary = linked_issue_data.get("fields", {}).get("summary", "")
                            
                            # Look for Confluence URLs in description
                            confluence_page_id = extract_confluence_page_id(description, summary)
                            
                            if confluence_page_id:
                                print(f"        ✓ Found Confluence page ID: {confluence_page_id}")
                                page_data = await get_confluence_page_via_mcp(confluence_page_id)
                                if page_data:
                                    page_content = page_data.get("body", {}).get("storage", {}).get("value", "")
                                    
                                    # Clean HTML
                                    text = re.sub(r'<[^>]+>', '', page_content)
                                    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                                    text = re.sub(r'\s+', ' ', text).strip()
                                    
                                    print(f"        - Page content length: {len(text)} chars")
                                    print(f"  🌐 Translating: {summary or 'Confluence Page'}")
                                    translated_content = translate_to_english(text)
                                    confluence_content.append(f"=== {summary or 'Confluence Page'} ===\n{translated_content}\n")
                            else:
                                print(f"        ✗ No Confluence page ID found in linked issue")
                        else:
                            print(f"        ✗ Failed to fetch linked issue data")
        
        print(f"     - Processed {processed_links} linked issue(s)")
        result = "\n".join(confluence_content) if confluence_content else ""
        if len(confluence_content) > 0:
            print(f"  ✅ Found {len(confluence_content)} Confluence page(s) via MCP")
        else:
            print(f"  ℹ️  No Confluence documents found in linked issues")
        return result
        
    except Exception as e:
        print(f"  ❌ MCP fetch failed: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        return ""


# =============================================================================
# Helper functions
# =============================================================================

def translate_to_english(text: str) -> str:
    """
    Translate text to English using OpenAI API.
    
    Args:
        text: The text to translate
        
    Returns:
        The translated text or original text if translation fails
    """
    if not text:
        return text
    
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a translator. Translate the following text to English. If the text is already in English, return it as is. Only return the translated text without any additional comments."
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
        )
        
        translated_text = response.choices[0].message.content
        return translated_text if translated_text else text
        
    except Exception as e:
        print(f"Warning: Translation failed: {e}. Using original text.")
        return text


def extract_confluence_page_id_from_url(url: str) -> Optional[str]:
    """
    Extract Confluence page ID from a direct URL.
    
    Args:
        url: The Confluence page URL
        
    Returns:
        Confluence page ID if found, None otherwise
    """
    if not url:
        return None
    
    # Pattern to match Confluence page URLs
    # Examples: 
    # - http://confluence.example.com/pages/viewpage.action?pageId=123456789
    # - http://confluence.example.com/display/SPACE/Page+Title?pageId=123456789
    # - http://confluence.example.com/spaces/SPACE/pages/123456789/Page+Title
    # - http://confluence.example.com/wiki/spaces/SPACE/pages/123456789
    patterns = [
        r'pageId=(\d+)',
        r'/pages/(\d+)',
        r'/spaces/[\w-]+/pages/(\d+)',
        r'/wiki/spaces/[\w-]+/pages/(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None


def extract_confluence_page_id(description: str, summary: str) -> Optional[str]:
    """
    Extract Confluence page ID from text containing Confluence URLs.
    
    Args:
        description: Issue description
        summary: Issue summary
        
    Returns:
        Confluence page ID if found, None otherwise
    """
    # Combine description and summary
    text = f"{description} {summary}"
    
    # Pattern to match Confluence page URLs
    # Examples: https://confluence.atlassian.net/pages/viewpage.action?pageId=123456789
    # or /pages/viewpage.action?pageId=123456789
    patterns = [
        r'pages/viewpage\.action\?pageId=(\d+)',
        r'/spaces/\w+/pages/(\d+)',
        r'/wiki/spaces/\w+/pages/(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    
    return None


def get_gitlab_file_content(context: 'ServiceReviewerContext', file_path: str) -> Optional[str]:
    """
    Fetch file content from GitLab using direct API.
    Note: This function uses direct GitLab API for package context gathering.
    The main MR review flow uses MCP for all GitLab operations.
    
    Args:
        context: ServiceReviewerContext with GitLab credentials
        file_path: Path to the file in the repository
        
    Returns:
        File content as a string or None if failed
    """
    if not all([context.gitlab_token, context.gitlab_domain, context.project_path, context.source_branch]):
        return None
    
    try:
        gitlab_url = f"https://{context.gitlab_domain}/api/v4"
        headers = {"PRIVATE-TOKEN": context.gitlab_token}
        
        # URL-encode the project path (e.g., "acme/shop" -> "mars%2Facme-shop")
        encoded_project_path = quote(context.project_path, safe='')
        
        # Encode file path for URL
        encoded_file_path = quote(file_path, safe='')
        
        # Fetch file content
        file_url = f"{gitlab_url}/projects/{encoded_project_path}/repository/files/{encoded_file_path}/raw?ref={context.source_branch}"
        # Explicitly disable proxy for GitLab requests
        response = requests.get(file_url, headers=headers, proxies={'http': None, 'https': None})
        
        if response.status_code == 200:
            return response.text
        else:
            print(f"Warning: Failed to fetch GitLab file {file_path}. Status code: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error fetching GitLab file {file_path}: {e}")
        return None


def get_gitlab_directory_tree(context: 'ServiceReviewerContext', dir_path: str) -> list[str]:
    """
    List files in a GitLab directory using direct API.
    Note: This function uses direct GitLab API for package context gathering.
    The main MR review flow uses MCP for all GitLab operations.
    
    Args:
        context: ServiceReviewerContext with GitLab credentials
        dir_path: Path to the directory in the repository
        
    Returns:
        List of file paths in the directory
    """
    if not all([context.gitlab_token, context.gitlab_domain, context.project_path, context.source_branch]):
        print(f"  🔍 DEBUG - Missing GitLab credentials:")
        print(f"    - gitlab_token: {'✓' if context.gitlab_token else '✗'}")
        print(f"    - gitlab_domain: {context.gitlab_domain or '✗'}")
        print(f"    - project_path: {context.project_path or '✗'}")
        print(f"    - source_branch: {context.source_branch or '✗'}")
        return []
    
    try:
        gitlab_url = f"https://{context.gitlab_domain}/api/v4"
        headers = {"PRIVATE-TOKEN": context.gitlab_token}
        
        # URL-encode the project path (e.g., "acme/shop" -> "mars%2Facme-shop")
        encoded_project_path = quote(context.project_path, safe='')
        
        # Fetch directory tree
        tree_url = f"{gitlab_url}/projects/{encoded_project_path}/repository/tree"
        params = {
            "path": dir_path,
            "ref": context.source_branch,
            "recursive": False
        }
        
        print(f"  🔍 DEBUG - GitLab API Request:")
        print(f"    - URL: {tree_url}")
        print(f"    - Params: {params}")
        
        # Explicitly disable proxy for GitLab requests
        response = requests.get(tree_url, headers=headers, params=params, proxies={'http': None, 'https': None})
        
        print(f"  🔍 DEBUG - Response status: {response.status_code}")
        
        if response.status_code == 200:
            items = response.json()
            print(f"  🔍 DEBUG - Found {len(items)} items in directory")
            # Return only file paths (not directories)
            file_paths = [item['path'] for item in items if item['type'] == 'blob']
            print(f"  🔍 DEBUG - Filtered to {len(file_paths)} files (excluding directories)")
            return file_paths
        else:
            print(f"Warning: Failed to fetch GitLab directory {dir_path}. Status code: {response.status_code}")
            try:
                error_detail = response.json()
                print(f"  🔍 DEBUG - Error response: {error_detail}")
            except:
                print(f"  🔍 DEBUG - Response text: {response.text[:500]}")
            return []
            
    except Exception as e:
        print(f"Error fetching GitLab directory {dir_path}: {e}")
        return []


def extract_imports_from_go_file(content: str) -> list[str]:
    """
    Extract import statements from Go file content.
    
    Args:
        content: Go file content
        
    Returns:
        List of imported packages
    """
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
        # Extract all quoted strings
        block_imports = re.findall(r'"([^"]+)"', import_block)
        imports.extend(block_imports)
    
    return imports


def get_package_context(context: 'ServiceReviewerContext') -> str:
    """
    Gather comprehensive package context including dependencies, structure, and ALL Go files in the package.
    
    Args:
        context: ServiceReviewerContext
        
    Returns:
        Formatted package context as a string
    """
    if not context.file_path:
        print("  ⚠️  Warning: No file_path provided, skipping package context fetch.")
        return ""
    
    context_parts = []
    
    # 1. Fetch go.mod file for dependencies
    print("  📦 Fetching go.mod file...")
    # Determine the service directory prefix from file_path
    # Example: sales/service/internal/app/... -> sales/service
    file_path_parts = context.file_path.split('/')
    service_prefix = ""
    if 'service' in file_path_parts:
        service_idx = file_path_parts.index('service')
        service_prefix = '/'.join(file_path_parts[:service_idx+1])
    
    go_mod_path = f"{service_prefix}/go.mod" if service_prefix else "go.mod"
    print(f"  🔍 DEBUG - go.mod path: {go_mod_path}")
    go_mod_content = get_gitlab_file_content(context, go_mod_path)
    
    if go_mod_content:
        # Extract only the dependencies section
        dependencies = []
        in_require = False
        for line in go_mod_content.split('\n'):
            if line.strip().startswith('require'):
                in_require = True
                continue
            if in_require:
                if line.strip().startswith(')'):
                    break
                if line.strip() and not line.strip().startswith('//'):
                    dependencies.append(line.strip())
        
        if dependencies:
            context_parts.append("=== Go Module Dependencies ===\n" + "\n".join(dependencies[:20]))  # Limit to first 20
    
    # 2. Extract imports from current file
    print(f"  📝 Extracting imports from current file...")
    if context.content:
        imports = extract_imports_from_go_file(context.content)
        if imports:
            context_parts.append("\n=== Current File Imports ===\n" + "\n".join(imports))
    
    # 3. List package files
    print(f"  📂 Listing package files...")
    # Extract package directory from file_path (everything except the file name)
    # Example: sales/service/internal/app/sales-operation/invoice/invoicemodel/invoice.go
    #       -> sales/service/internal/app/sales-operation/invoice/invoicemodel
    package_dir = '/'.join(context.file_path.split('/')[:-1])
    print(f"  🔍 DEBUG - Extracted package_dir from file_path: {package_dir}")
    package_files = get_gitlab_directory_tree(context, package_dir)
    
    if package_files:
        # Filter out test files
        go_files = [f for f in package_files if f.endswith('.go') and 'test' not in f.lower()]
        context_parts.append(f"\n=== Package Structure ({len(go_files)} Go files) ===\n" + "\n".join([f.split('/')[-1] for f in go_files]))
        
        # 4. Fetch ALL Go files in the package (except test files and current file)
        print(f"  📥 Fetching all package Go files ({len(go_files)} files)...")
        for file_path in go_files:
            file_name = file_path.split('/')[-1]

            # Skip current file being reviewed
            if file_name == context.file_name:
                continue    
            
            # Skip test files
            if 'test' in file_name.lower():
                continue
            
            file_content = get_gitlab_file_content(context, file_path)
            if file_content:
                # Limit content length to avoid overwhelming the context
                if len(file_content) > 3000:
                    file_content = file_content[:3000] + "\n... (truncated)"
                context_parts.append(f"\n=== {file_name} ===\n```go\n{file_content}\n```")
    
    result = "\n".join(context_parts) if context_parts else ""
    print(f"  ✅ Package context gathered: {len(context_parts)} sections\n")
    return result


def semantic_reviewer_instructions(run_context: RunContextWrapper[ServiceReviewerContext], _agent: Agent[ServiceReviewerContext]):
    content = run_context.context.content
    diff = run_context.context.diff
    package_context = run_context.context.package_context
    
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
{package_context if package_context else "Package context not available."}

File Content:
```
{content}
```

Git Diff:
```
{diff}
```"""


def business_logic_reviewer_instructions(run_context: RunContextWrapper[ServiceReviewerContext], _agent: Agent[ServiceReviewerContext]):
    content = run_context.context.content
    diff = run_context.context.diff
    task_description = run_context.context.task_description
    related_documents = run_context.context.related_documents
    package_context = run_context.context.package_context
    
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
{task_description if task_description else "No task description available."}

Related Documents:
{related_documents if related_documents else "No related documents available."}

Package Context (Dependencies, Structure, All Package Files):
{package_context if package_context else "Package context not available."}

File Content:
```
{content}
```

Git Diff:
```
{diff}
```"""


# Semantic Code Reviewer - Focuses on technical code quality
semantic_reviewer = Agent(
    name="semantic code reviewer",
    instructions=semantic_reviewer_instructions,
    model="gpt-5-nano",
    output_type=ReviewResponse,
    model_settings=ModelSettings(
        parallel_tool_calls=True,
        store=True,
        reasoning=Reasoning(
            effort="medium"
        )
    )
)


# Business Logic Reviewer - Focuses on business requirements
business_logic_reviewer = Agent(
    name="business logic reviewer",
    instructions=business_logic_reviewer_instructions,
    model="gpt-5-nano",
    output_type=ReviewResponse,
    model_settings=ModelSettings(
        parallel_tool_calls=True,
        store=True,
        reasoning=Reasoning(
            effort="high"
        )
    )
)


# Orchestrator function that calls both reviewers and combines results
async def run_combined_review(context: ServiceReviewerContext) -> ReviewResponse:
    """
    Orchestrates both semantic and business logic reviews.
    This function should be called from the workflow level.
    """
    issues = []
    
    # Run semantic review
    print("    " + "="*50)
    print("    🔍 LEVEL 1: Semantic & Technical Code Review")
    print("    " + "="*50)
    try:
        semantic_result = await Runner.run(
            semantic_reviewer,
            input=[{
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "Review the code for semantic and technical issues."
                }]
            }],
            context=context,
            run_config=RunConfig()
        )
        if semantic_result.final_output and semantic_result.final_output.issues:
            issues.extend(semantic_result.final_output.issues)
            print(f"    ✅ Found {len(semantic_result.final_output.issues)} semantic/technical issue(s)")
            
            # Print each issue
            for i, issue in enumerate(semantic_result.final_output.issues, 1):
                print(f"\n    Issue #{i}:")
                print(f"      📍 Line {issue.line} - [{issue.severity.upper()}]")
                print(f"      ❌ Problem: {issue.problem}")
                if issue.suggestion and issue.suggestion.strip():
                    print(f"      ✅ Suggestion: {issue.suggestion}")
        else:
            print(f"    ✅ No semantic/technical issues found")
    except Exception as e:
        print(f"    ❌ Error in semantic review: {e}")
    
    # Run business logic review
    print("\n    " + "="*50)
    print("    🔍 LEVEL 2: Business Logic Review")
    print("    " + "="*50)
    try:
        business_result = await Runner.run(
            business_logic_reviewer,
            input=[{
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "Review the code for business logic issues."
                }]
            }],
            context=context,
            run_config=RunConfig()
        )
        if business_result.final_output and business_result.final_output.issues:
            issues.extend(business_result.final_output.issues)
            print(f"    ✅ Found {len(business_result.final_output.issues)} business logic issue(s)")
            
            # Print each issue
            for i, issue in enumerate(business_result.final_output.issues, 1):
                print(f"\n    Issue #{i}:")
                print(f"      📍 Line {issue.line} - [{issue.severity.upper()}]")
                print(f"      ❌ Problem: {issue.problem}")
                if issue.suggestion and issue.suggestion.strip():
                    print(f"      ✅ Suggestion: {issue.suggestion}")
        else:
            print(f"    ✅ No business logic issues found")
    except Exception as e:
        print(f"    ❌ Error in business logic review: {e}")
    
    print("    " + "="*50)
    print(f"    📊 Total Issues Found: {len(issues)}")
    print("    " + "="*50 + "\n")
    
    return ReviewResponse(issues=issues)
