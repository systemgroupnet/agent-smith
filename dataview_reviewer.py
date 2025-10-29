"""
Dataview Code Reviewer

This module provides comprehensive semantic and technical review of Go dataview code,
focusing on code quality, correctness, and dataview semantic consistency.

Usage:
- Use dataview_reviewer for semantic and technical review of dataview files
- Does NOT validate business logic or business requirements
"""

from __future__ import annotations

from agents import Agent, ModelSettings, RunContextWrapper, Runner, RunConfig
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel
import os
import re
import requests
from typing import Optional


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class DataviewReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class DataviewReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, jira_task_number: Optional[str] = None,
                 package_name: Optional[str] = None, module_name: Optional[str] = None, file_name: Optional[str] = None,
                 gitlab_token: Optional[str] = None, gitlab_domain: Optional[str] = None, 
                 source_branch: Optional[str] = None, project_path: Optional[str] = None):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.jira_task_number = jira_task_number
        self.package_name = package_name
        self.module_name = module_name
        self.file_name = file_name
        self.gitlab_token = gitlab_token
        self.gitlab_domain = gitlab_domain
        self.source_branch = source_branch
        self.project_path = project_path
        
        self.task_description = get_task_description(jira_task_number)
        self.related_documents = get_related_documents(jira_task_number)
        
        # Fetch related files
        self.model_file_content = get_model_file_content(self)
        self.entity_file_content = get_entity_file_content(self)
        self.validation_file_content = get_validation_file_content(self)


def get_task_description(jira_task_number: Optional[str] = None) -> str:
    """
    Fetch Jira task description using the Jira API.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        The task description or an empty string if not available
    """
    if not jira_task_number:
        return ""
    
    try:
        jira_url = os.getenv("JIRA_URL")
        jira_email = os.getenv("JIRA_EMAIL")
        jira_api_token = os.getenv("JIRA_API_TOKEN")
        
        if not all([jira_url, jira_email, jira_api_token]):
            print("Warning: Jira credentials not configured. Skipping task description fetch.")
            return ""
        
        auth = requests.auth.HTTPBasicAuth(jira_email, jira_api_token)
        headers = {"Accept": "application/json"}

        # Fetch issue details
        issue_url = f"{jira_url}/rest/api/3/issue/{jira_task_number}"
        response = requests.get(issue_url, auth=auth, headers=headers)

        if response.status_code == 200:
            issue_data = response.json()
            description = issue_data.get("fields", {}).get("description", "")
            
            # Extract summary as well
            summary = issue_data.get("fields", {}).get("summary", "")
            
            # Combine summary and description
            if description and summary:
                return f"Task: {summary}\n\nDescription:\n{description}"
            elif summary:
                return f"Task: {summary}"
            elif description:
                return description
            else:
                return ""
        else:
            print(f"Warning: Failed to fetch Jira task {jira_task_number}. Status code: {response.status_code}")
            return ""
            
    except Exception as e:
        print(f"Error fetching Jira task description: {e}")
        return ""


def get_related_documents(jira_task_number: Optional[str] = None) -> str:
    """
    Fetch related Confluence documents from Jira task's related links.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        Related documents content as a string
    """
    if not jira_task_number:
        return ""
    
    try:
        jira_url = os.getenv("JIRA_URL")
        jira_email = os.getenv("JIRA_EMAIL")
        jira_api_token = os.getenv("JIRA_API_TOKEN")
        
        confluence_url = os.getenv("CONFLUENCE_URL")
        confluence_email = os.getenv("CONFLUENCE_EMAIL")
        confluence_api_token = os.getenv("CONFLUENCE_API_TOKEN")
        
        if not all([jira_url, jira_email, jira_api_token]):
            print("Warning: Jira credentials not configured. Skipping related documents fetch.")
            return ""
        
        if not all([confluence_url, confluence_email, confluence_api_token]):
            print("Warning: Confluence credentials not configured. Skipping related documents fetch.")
            return ""
        
        auth = requests.auth.HTTPBasicAuth(jira_email, jira_api_token)
        headers = {"Accept": "application/json"}
        
        # Fetch issue links
        issue_url = f"{jira_url}/rest/api/3/issue/{jira_task_number}"
        response = requests.get(issue_url, auth=auth, headers=headers)
        
        if response.status_code != 200:
            print(f"Warning: Failed to fetch Jira task {jira_task_number}. Status code: {response.status_code}")
            return ""
        
        issue_data = response.json()
        
        # Extract issue links
        issue_links = issue_data.get("fields", {}).get("issuelinks", [])
        
        confluence_content = []
        confluence_auth = requests.auth.HTTPBasicAuth(confluence_email, confluence_api_token)
        confluence_headers = {"Accept": "application/json"}
        
        # Process each link to find Confluence pages
        for link in issue_links:
            outward_issue = link.get("outwardIssue")
            inward_issue = link.get("inwardIssue")
            
            # Check both outward and inward links
            for issue in [outward_issue, inward_issue]:
                if issue:
                    issue_key = issue.get("key")
                    if issue_key:
                        # Check if this links to a Confluence page
                        # We need to fetch the issue to see if it's a Confluence link
                        try:
                            linked_issue_url = f"{jira_url}/rest/api/3/issue/{issue_key}"
                            linked_response = requests.get(linked_issue_url, auth=auth, headers=headers)
                            
                            if linked_response.status_code == 200:
                                linked_issue_data = linked_response.json()
                                issue_type = linked_issue_data.get("fields", {}).get("issuetype", {}).get("name", "")
                                
                                # Check if it's a Confluence page link
                                if "confluence" in issue_type.lower() or "page" in issue_type.lower():
                                    # Try to extract Confluence page ID from URL or other fields
                                    description = linked_issue_data.get("fields", {}).get("description", "")
                                    summary = linked_issue_data.get("fields", {}).get("summary", "")
                                    
                                    # Look for Confluence URLs in description
                                    confluence_page_id = extract_confluence_page_id(description, summary)
                                    
                                    if confluence_page_id:
                                        page_content = fetch_confluence_page(confluence_url, confluence_auth, confluence_headers, confluence_page_id)
                                        if page_content:
                                            confluence_content.append(f"=== {summary or 'Confluence Page'} ===\n{page_content}\n")
                        except Exception as e:
                            print(f"Error processing linked issue {issue_key}: {e}")
                            continue
        
        return "\n".join(confluence_content) if confluence_content else ""
        
    except Exception as e:
        print(f"Error fetching related documents: {e}")
        return ""


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


def fetch_confluence_page(confluence_url: str, auth, headers: dict, page_id: str) -> Optional[str]:
    """
    Fetch content from a Confluence page.
    
    Args:
        confluence_url: Base URL of Confluence instance
        auth: HTTPBasicAuth credentials
        headers: Request headers
        page_id: Confluence page ID
        
    Returns:
        Page content as a string or None if failed
    """
    try:
        # Fetch page content in storage format
        page_url = f"{confluence_url}/rest/api/content/{page_id}?expand=body.storage"
        response = requests.get(page_url, auth=auth, headers=headers)
        
        if response.status_code == 200:
            page_data = response.json()
            # Extract text from storage format
            body_storage = page_data.get("body", {}).get("storage", {}).get("value", "")
            
            # Basic HTML tag removal (you might want to use BeautifulSoup for better parsing)
            # Remove HTML tags
            text = re.sub(r'<[^>]+>', '', body_storage)
            # Decode HTML entities
            text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            # Clean up whitespace
            text = re.sub(r'\s+', ' ', text).strip()
            
            return text

        else:
            print(f"Warning: Failed to fetch Confluence page {page_id}. Status code: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error fetching Confluence page {page_id}: {e}")
        return None


def get_gitlab_file_content(context: 'DataviewReviewerContext', file_path: str) -> Optional[str]:
    """
    Fetch file content from GitLab.
    
    Args:
        context: DataviewReviewerContext with GitLab credentials
        file_path: Path to the file in the repository
        
    Returns:
        File content as a string or None if failed
    """
    if not all([context.gitlab_token, context.gitlab_domain, context.project_path, context.source_branch]):
        return None
    
    try:
        gitlab_url = f"https://{context.gitlab_domain}/api/v4"
        headers = {"PRIVATE-TOKEN": context.gitlab_token}
        
        # Encode file path for URL
        encoded_path = file_path.replace('/', '%2F')
        
        # Fetch file content
        file_url = f"{gitlab_url}/projects/{context.project_path}/repository/files/{encoded_path}/raw?ref={context.source_branch}"
        response = requests.get(file_url, headers=headers)
        
        if response.status_code == 200:
            return response.text
        else:
            print(f"Warning: Failed to fetch GitLab file {file_path}. Status code: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"Error fetching GitLab file {file_path}: {e}")
        return None


def get_model_file_content(context: 'DataviewReviewerContext') -> str:
    """
    Fetch model.go file content for the package.
    
    Args:
        context: DataviewReviewerContext
        
    Returns:
        Model file content as a string
    """
    if not context.package_name:
        return ""
    
    # Construct model.go file path
    # Example: internal/app/sales-operation/package_name/model.go
    model_file_path = f"service/internal/app/{context.module_name}/{context.package_name}/model.go"
    
    content = get_gitlab_file_content(context, model_file_path)
    return content if content else ""


def get_entity_file_content(context: 'DataviewReviewerContext') -> str:
    """
    Fetch entity file content (based on entity name derived from context).
    
    Args:
        context: DataviewReviewerContext
        
    Returns:
        Entity file content as a string
    """
    if not context.package_name:
        return ""
    
    # Extract entity name from package name or file name
    # For example, if package_name is "returninvoice", entity might be "returninvoice" or "return_invoice"
    entity_name = context.package_name
    
    # Construct entity file path
    # Example: internal/app/sales-operation/package_name/returninvoice.go
    entity_file_path = f"service/internal/app/{context.module_name}/{context.package_name}/{entity_name}.go"
    
    content = get_gitlab_file_content(context, entity_file_path)
    return content if content else ""


def get_validation_file_content(context: 'DataviewReviewerContext') -> str:
    """
    Fetch validation.go file content for the package.
    
    Args:
        context: DataviewReviewerContext
        
    Returns:
        Validation file content as a string
    """
    if not context.package_name:
        return ""
    
    validation_file_path = f"service/internal/app/{context.module_name}/{context.package_name}/validation.go"
    
    content = get_gitlab_file_content(context, validation_file_path)
    return content if content else ""


def dataview_reviewer_instructions(run_context: RunContextWrapper[DataviewReviewerContext], _agent: Agent[DataviewReviewerContext]):
    content = run_context.context.content
    diff = run_context.context.diff
    model_file_content = run_context.context.model_file_content
    entity_file_content = run_context.context.entity_file_content
    validation_file_content = run_context.context.validation_file_content
    
    return f"""You are a senior Go engineer performing a comprehensive semantic and technical code review for dataview code.

Your review must cover:
1. TECHNICAL ASPECTS:
   - Correctness & Logic – Check for logical errors, edge cases, missing error handling, or misuse of Go idioms
   - Code Quality & Readability – Assess naming conventions, modularity, and function clarity
   - Performance & Efficiency – Identify any unnecessary database calls, loops, or allocations
   - Concurrency & Transactions – Review any use of context.Context, goroutines, and database transactions for safety and consistency
   - Error Handling – Check if errors are handled properly, wrapped, and returned with enough context
   - Database (GORM) Usage – Identify possible issues with transaction scoping, query building, and data consistency
   - Go Best Practices – Adherence to Go idioms, conventions, and standard library usage

2. DATAVIEW SEMANTIC CHECKS (MOST IMPORTANT):
   - Query outputs MUST match search field definitions exactly
   - Field names consistency between query SELECT and search field definitions
   - Data types consistency between query outputs and expected field types
   - Proper joins between related tables with correct foreign keys
   - Filtering logic is syntactically correct and uses proper GORM methods
   - Sorting implementation uses correct field names
   - Pagination is properly implemented (LIMIT/OFFSET or cursor-based)
   - Data relationships are correctly represented with proper associations
   - SQL injection prevention through parameterized queries
   - Field mappings between database columns and struct fields

SEVERITY LEVELS:
You MUST assign a severity level to each issue based on these guidelines:
- **high**: Bugs, errors, data corruption, security flaws, wrong data shown, SQL injection vulnerability, risk of production issue, major maintainability problem, semantic mismatch in dataview, error handling issues, exception problems, error propagation, inconsistency between query outputs and search field of dataview, performance issues that you are confident will cause real problems (N+1 queries, missing pagination in large datasets, inefficient queries on large data)
- **medium**: Performance issues that might cause problems but less certain, readability improvements, convention issues, code duplication
- **low**: Style issues, code style formatting, naming conventions, minor refactoring, edge cases, minor improvements

**Performance Issue Guideline**: If you have a clear, strong concern that a performance issue will cause real problems in production, mark it as **high**. Only mark as **medium** if you're less certain about the impact.

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical. The reason should be explicit and explain the potential impact or consequence of the issue.

CRITICAL NOTES: 
- We use Postgres for the database
- We use GORM for the ORM
- We use Go for the backend
- DO NOT comment about docs and writing comment or documentations
- DO NOT comment about nil checks, especially for function arguments in dataview
- Developer should run his code to run correctly, do not need to remind it
- PAY SPECIAL ATTENTION to consistency between query outputs and search field definitions
- Focus on SEMANTIC correctness, not business logic requirements


RESPONSE FORMAT:
Return a DataviewReviewResponse with an "issues" field containing a list of ReviewIssue objects.
Each ReviewIssue has: problem, suggestion, file_name, line, and severity ("high"|"medium"|"low") fields.

IMPORTANT: Only report significant issues that impact correctness, performance, maintainability, or semantic consistency. Be specific and avoid generic suggestions. DO NOT validate business logic or business requirements.

File Content:
```
{content}
```

Git Diff:
```
{diff}
```"""


dataview_reviewer = Agent(
    name="dataview reviewer",
    instructions=dataview_reviewer_instructions,
    model="gpt-5-nano",
    output_type=DataviewReviewResponse,
    model_settings=ModelSettings(
        parallel_tool_calls=True,
        store=True,
        reasoning=Reasoning(
            effort="medium"
        )
    )
)
