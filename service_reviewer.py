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
from pydantic import BaseModel
import os
import re
import requests
from typing import Optional
from urllib.parse import quote
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
 

class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class ServiceReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class SemanticReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class BusinessLogicReviewResponse(BaseModel):
    issues: list[ReviewIssue]


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
        try:
            # Try to get existing event loop
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # If there's no event loop, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Run async functions
        self.task_description = loop.run_until_complete(get_task_description_async(jira_task_number))
        self.related_documents = loop.run_until_complete(get_related_documents_async(jira_task_number))
        
        # Fetch package context (includes all Go files)
        self.package_context = get_package_context(self)


# =============================================================================
# MCP Atlassian Integration
# =============================================================================

def is_mcp_enabled() -> bool:
    """Check if MCP Atlassian integration is enabled."""
    return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"


async def get_mcp_client():
    """
    Create and connect to MCP Atlassian server.
    
    Supports two modes:
    1. Local Docker: Connects to local container via docker exec (MCP_ATLASSIAN_HOST=localhost)
    2. Remote: Connects to remote MCP server via SSH tunnel or direct connection
    
    The MCP server is expected to expose MCP over stdio.
    """
    mcp_host = os.getenv("MCP_ATLASSIAN_HOST", "localhost")
    mcp_port = os.getenv("MCP_ATLASSIAN_PORT", "3000")
    container_name = os.getenv("MCP_ATLASSIAN_CONTAINER", "mcp-atlassian")
    
    try:
        # Local Docker mode: Use docker exec
        if mcp_host in ["localhost", "127.0.0.1"]:
            server_params = StdioServerParameters(
                command="docker",
                args=["exec", "-i", container_name, "node", "/app/build/index.js"],
                env=None
            )
        else:
            # Remote mode: Use SSH to connect to remote Docker container
            # Assumes SSH access to the remote host
            # Format: ssh user@host docker exec -i container_name command
            remote_user = os.getenv("MCP_ATLASSIAN_SSH_USER", "root")
            server_params = StdioServerParameters(
                command="ssh",
                args=[
                    f"{remote_user}@{mcp_host}",
                    "docker", "exec", "-i", container_name,
                    "node", "/app/build/index.js"
                ],
                env=None
            )
        
        stdio_transport = await stdio_client(server_params)
        stdio, write = stdio_transport
        
        async with ClientSession(stdio, write) as session:
            await session.initialize()
            return session
    except Exception as e:
        print(f"  ⚠️  Warning: Failed to connect to MCP Atlassian server at {mcp_host}: {e}")
        return None


async def call_mcp_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Call a tool on the MCP Atlassian server.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments as a dictionary
        
    Returns:
        Tool result as a dictionary or None if failed
    """
    try:
        session = await get_mcp_client()
        if not session:
            return None
        
        async with session:
            result = await session.call_tool(tool_name, arguments)
            
            # Parse the result
            if result and len(result.content) > 0:
                content = result.content[0]
                if hasattr(content, 'text'):
                    return json.loads(content.text)
            
            return None
            
    except Exception as e:
        print(f"  ❌ Error calling MCP tool {tool_name}: {e}")
        return None


async def get_jira_issue_via_mcp(issue_key: str) -> Optional[dict]:
    if not issue_key:
        return None
    
    print(f"  🔌 Fetching Jira issue via MCP: {issue_key}")
    
    result = await call_mcp_tool("jira_get_issue", {"issue_key": issue_key})
    
    if result:
        print(f"  ✅ Jira issue fetched via MCP")
    
    return result


async def get_confluence_page_via_mcp(page_id: str) -> Optional[dict]:
    """
    Fetch Confluence page using MCP Atlassian server.
    
    Args:
        page_id: Confluence page ID
        
    Returns:
        Confluence page data as dictionary or None if failed
    """
    if not page_id:
        return None
    
    print(f"  🔌 Fetching Confluence page via MCP: {page_id}")
    
    result = await call_mcp_tool("confluence_get_page", {"page_id": page_id})
    
    if result:
        print(f"  ✅ Confluence page fetched via MCP")
    
    return result


# =============================================================================
# Async wrapper functions for Jira/Confluence (MCP or direct API)
# =============================================================================

async def get_task_description_async(jira_task_number: Optional[str]) -> str:
    """
    Fetch Jira task description using MCP if enabled, otherwise fall back to direct API.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        The task description (translated to English) or an empty string if not available
    """
    if not jira_task_number:
        return ""
    
    print(f"  📋 Fetching Jira task description: {jira_task_number}")
    
    # Try MCP first if enabled
    if is_mcp_enabled():
        try:
            issue_data = await get_jira_issue_via_mcp(jira_task_number)
            if issue_data:
                description = issue_data.get("fields", {}).get("description", "")
                summary = issue_data.get("fields", {}).get("summary", "")
                
                # Combine summary and description
                combined_text = ""
                if description and summary:
                    combined_text = f"Task: {summary}\n\nDescription:\n{description}"
                elif summary:
                    combined_text = f"Task: {summary}"
                elif description:
                    combined_text = description
                else:
                    return ""
                
                # Translate to English
                print(f"  🌐 Translating task description to English...")
                translated_text = translate_to_english(combined_text)
                print(f"  ✅ Task description fetched and translated (via MCP)")
                return translated_text
        except Exception as e:
            print(f"  ⚠️  Warning: MCP fetch failed, falling back to direct API: {e}")
    
    # Fall back to direct API call
    return get_task_description(jira_task_number)


async def get_related_documents_async(jira_task_number: Optional[str]) -> str:
    """
    Fetch related Confluence documents using MCP if enabled, otherwise fall back to direct API.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        Related documents content as a string (translated to English)
    """
    if not jira_task_number:
        return ""
    
    print(f"  📚 Fetching related Confluence documents...")
    
    # Try MCP first if enabled
    if is_mcp_enabled():
        try:
            issue_data = await get_jira_issue_via_mcp(jira_task_number)
            if not issue_data:
                print(f"  ⚠️  Warning: MCP fetch failed, falling back to direct API")
                return get_related_documents(jira_task_number)
            
            confluence_content = []
            
            # 1. Check remote links for Confluence URLs
            issue_links = issue_data.get("fields", {}).get("issuelinks", [])
            
            # Get remote links via MCP (if the MCP server supports it)
            # For now, we'll parse the issue data we already have
            
            # 2. Process issue links to find Confluence pages
            for link in issue_links:
                outward_issue = link.get("outwardIssue")
                inward_issue = link.get("inwardIssue")
                
                # Check both outward and inward links
                for issue in [outward_issue, inward_issue]:
                    if issue:
                        issue_key = issue.get("key")
                        if issue_key:
                            # Fetch linked issue via MCP
                            linked_issue_data = await get_jira_issue_via_mcp(issue_key)
                            
                            if linked_issue_data:
                                description = linked_issue_data.get("fields", {}).get("description", "")
                                summary = linked_issue_data.get("fields", {}).get("summary", "")
                                
                                # Look for Confluence URLs in description
                                confluence_page_id = extract_confluence_page_id(description, summary)
                                
                                if confluence_page_id:
                                    page_data = await get_confluence_page_via_mcp(confluence_page_id)
                                    if page_data:
                                        page_content = page_data.get("body", {}).get("storage", {}).get("value", "")
                                        
                                        # Clean HTML
                                        text = re.sub(r'<[^>]+>', '', page_content)
                                        text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                                        text = re.sub(r'\s+', ' ', text).strip()
                                        
                                        print(f"  🌐 Translating: {summary or 'Confluence Page'}")
                                        translated_content = translate_to_english(text)
                                        confluence_content.append(f"=== {summary or 'Confluence Page'} ===\n{translated_content}\n")
            
            result = "\n".join(confluence_content) if confluence_content else ""
            if len(confluence_content) > 0:
                print(f"  ✅ Found {len(confluence_content)} Confluence page(s) (via MCP)")
            else:
                print(f"  ℹ️  No Confluence documents found")
            return result
            
        except Exception as e:
            print(f"  ⚠️  Warning: MCP fetch failed, falling back to direct API: {e}")
    
    # Fall back to direct API call
    return get_related_documents(jira_task_number)


# =============================================================================
# Direct API functions (fallback when MCP is not available)
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


def get_task_description(jira_task_number) -> str:
    """
    Fetch Jira task description using the Jira API and translate it to English.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        The task description (translated to English) or an empty string if not available
    """
    if not jira_task_number:
        return ""
    
    print(f"  📋 Fetching Jira task description: {jira_task_number}")
    
    try:
        jira_url = os.getenv("JIRA_URL")
        jira_personal_access_token = os.getenv("JIRA_PERSONAL_ACCESS_TOKEN")
        
        if not all([jira_url, jira_personal_access_token]):
            print("  ⚠️  Warning: Jira credentials not configured. Skipping task description fetch.")
            return ""
        
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {jira_personal_access_token}"
        }
        
        # Fetch issue details (using API v2 which is more widely supported)
        issue_url = f"{jira_url}/rest/api/2/issue/{jira_task_number}"
        response = requests.get(issue_url, headers=headers)
        
        if response.status_code == 200:
            issue_data = response.json()
            description = issue_data.get("fields", {}).get("description", "")
            
            # Extract summary as well
            summary = issue_data.get("fields", {}).get("summary", "")
            
            # Combine summary and description
            combined_text = ""
            if description and summary:
                combined_text = f"Task: {summary}\n\nDescription:\n{description}"
            elif summary:
                combined_text = f"Task: {summary}"
            elif description:
                combined_text = description
            else:
                return ""
            
            # Translate to English
            print(f"  🌐 Translating task description to English...")
            translated_text = translate_to_english(combined_text)
            print(f"  ✅ Task description fetched and translated")
            return translated_text
        else:
            print(f"  ⚠️  Warning: Failed to fetch Jira task {jira_task_number}. Status code: {response.status_code}")
            return ""
            
    except Exception as e:
        print(f"  ❌ Error fetching Jira task description: {e}")
        return ""


def get_related_documents(jira_task_number: Optional[str] = None) -> str:
    """
    Fetch related Confluence documents from Jira task's related links.
    Automatically translates content to English.
    
    Args:
        jira_task_number: The Jira task number (e.g., "PROJ-123")
        
    Returns:
        Related documents content as a string (translated to English)
    """
    if not jira_task_number:
        return ""
    
    print(f"  📚 Fetching related Confluence documents...")
    
    try:
        jira_url = os.getenv("JIRA_URL")
        jira_personal_access_token = os.getenv("JIRA_PERSONAL_ACCESS_TOKEN")
        
        confluence_url = os.getenv("CONFLUENCE_URL")
        confluence_personal_access_token = os.getenv("CONFLUENCE_PERSONAL_ACCESS_TOKEN")
        
        if not all([jira_url, jira_personal_access_token]):
            print("  ⚠️  Warning: Jira credentials not configured. Skipping related documents fetch.")
            return ""
        
        if not all([confluence_url, confluence_personal_access_token]):
            print("  ⚠️  Warning: Confluence credentials not configured. Skipping related documents fetch.")
            return ""
        
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {jira_personal_access_token}"
        }
        
        # Fetch issue details (using API v2 which is more widely supported)
        issue_url = f"{jira_url}/rest/api/2/issue/{jira_task_number}"
        response = requests.get(issue_url, headers=headers)
        
        if response.status_code != 200:
            print(f"  ⚠️  Warning: Failed to fetch Jira task {jira_task_number}. Status code: {response.status_code}")
            return ""
        
        issue_data = response.json()
        
        # Extract issue links
        issue_links = issue_data.get("fields", {}).get("issuelinks", [])

        confluence_content = []
        confluence_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {confluence_personal_access_token}"
        }
        
        # 1. Check remote links (web links) for direct Confluence URLs
        try:
            remotelink_url = f"{jira_url}/rest/api/2/issue/{jira_task_number}/remotelink"
            remotelink_response = requests.get(remotelink_url, headers=headers)
            
            if remotelink_response.status_code == 200:
                remote_links = remotelink_response.json()
                if len(remote_links) > 0:
                    print(f"  🔗 Found {len(remote_links)} remote link(s)")
                
                for remote_link in remote_links:
                    link_object = remote_link.get("object", {})
                    link_url = link_object.get("url", "")
                    link_title = link_object.get("title", "")
                    
                    # Check if this is a Confluence URL
                    if "confluence" in link_url.lower() or confluence_url.replace("http://", "").replace("https://", "").strip("/") in link_url:
                        # Extract page ID from URL
                        confluence_page_id = extract_confluence_page_id_from_url(link_url)
                        
                        if confluence_page_id:
                            page_content = fetch_confluence_page(confluence_url, confluence_headers, confluence_page_id)
                            if page_content:
                                print(f"  🌐 Translating: {link_title or 'Confluence Page'}")
                                translated_content = translate_to_english(page_content)
                                confluence_content.append(f"=== {link_title or 'Confluence Page'} ===\n{translated_content}\n")
            else:
                print(f"  ⚠️  Warning: Failed to fetch remote links. Status code: {remotelink_response.status_code}")
        except Exception as e:
            print(f"  ❌ Error fetching remote links: {e}")
        
        # 2. Process issue links to find Confluence pages
        for link in issue_links:
            outward_issue = link.get("outwardIssue")
            inward_issue = link.get("inwardIssue")
            
            # Check both outward and inward links
            for issue in [outward_issue, inward_issue]:
                if issue:
                    issue_key = issue.get("key")
                    if issue_key:
                        # Check if this links to a Confluence page
                        try:
                            linked_issue_url = f"{jira_url}/rest/api/2/issue/{issue_key}"
                            linked_response = requests.get(linked_issue_url, headers=headers)
                            
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
                                        page_content = fetch_confluence_page(confluence_url, confluence_headers, confluence_page_id)
                                        if page_content:
                                            print(f"  🌐 Translating: {summary or 'Confluence Page'}")
                                            translated_content = translate_to_english(page_content)
                                            confluence_content.append(f"=== {summary or 'Confluence Page'} ===\n{translated_content}\n")
                        except Exception as e:
                            print(f"  ❌ Error processing linked issue {issue_key}: {e}")
                            continue
        
        result = "\n".join(confluence_content) if confluence_content else ""
        if len(confluence_content) > 0:
            print(f"  ✅ Found {len(confluence_content)} Confluence page(s)")
        else:
            print(f"  ℹ️  No Confluence documents found")
        return result
         
    except Exception as e:
        print(f"  ❌ Error fetching related documents: {e}")
        return ""

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


def get_gitlab_file_content(context: 'ServiceReviewerContext', file_path: str) -> Optional[str]:
    """
    Fetch file content from GitLab.
    
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
        response = requests.get(file_url, headers=headers)
        
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
    List files in a GitLab directory.
    
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
        
        response = requests.get(tree_url, headers=headers, params=params)
        
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

RESPONSE: Return SemanticReviewResponse with "issues" array. Each issue needs: problem (specific), suggestion (actionable with code), file_name, line (exact number), severity ("high"|"medium"|"low").

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

RESPONSE: Return BusinessLogicReviewResponse with "issues" array. Each issue needs: problem (with requirement reference), suggestion (with spec reference), file_name, line (exact number), severity ("high"|"medium"|"low").

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
    output_type=SemanticReviewResponse,
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
    output_type=BusinessLogicReviewResponse,
    model_settings=ModelSettings(
        parallel_tool_calls=True,
        store=True,
        reasoning=Reasoning(
            effort="high"
        )
    )
)


# Orchestrator function that calls both reviewers and combines results
async def run_combined_review(context: ServiceReviewerContext) -> ServiceReviewResponse:
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
    
    return ServiceReviewResponse(issues=issues)
