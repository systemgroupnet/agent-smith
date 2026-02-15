# CodeReviewAgent

An AI-powered code review system for the application. This project provides specialized reviewers for different file types including Go services, SQL migrations, dataviews, test files, and error message files (pbook).

## Features

- **Multi-Type Code Reviewers**: Specialized agents for different file types:

  - **Service Reviewer**: Reviews Go service code with split architecture (semantic + business logic review)
  - **SQL Reviewer**: Reviews SQL migrations and schema changes
  - **Dataview Reviewer**: Reviews dataview implementation code
  - **Test Reviewer**: Reviews BDD/Gherkin test implementation code
  - **Pbook Reviewer**: Reviews error message files (Persian/English)
  - **Starter Agent**: Classifies files and routes them to appropriate reviewers

- **Intelligent Review**:

  - Technical code quality and Go best practices
  - Business logic and requirements validation
  - Database query optimization
  - Security vulnerability detection

- **Integration**:
  - **GitLab MCP** - Uses GitLab MCP server for all GitLab operations (merge requests, file contents, comments)
  - **Atlassian MCP** - Uses Atlassian MCP server for Jira and Confluence integration
  - All integrations use MCP (Model Context Protocol) - no direct API fallbacks
  - Note: Some internal functions use direct GitLab API for package context gathering only

## Prerequisites

- Python 3.8 or higher
- OpenAI API key
- (Optional) Jira and Confluence credentials for enhanced review context
- (Optional) GitLab token for fetching related files

## Installation

1. **Clone the repository**:

   ```bash
   git clone https://gitlab.example.com/AmirHosseinHa/codereviewagent.git
   cd codereviewagent
   ```

2. **Create a virtual environment** (recommended):

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Set up GitLab MCP Server (Required)**:

   The code review agent uses the GitLab MCP server for all GitLab operations. You must set it up:

   **Option A: Using Docker (Recommended)**

   Clone and build the GitLab MCP server:

   ```bash
   git clone https://github.com/zereight/gitlab-mcp.git mcp-gitlab
   cd mcp-gitlab
   npm install
   npm run build
   ```

   Then create a `Dockerfile` in the `mcp-gitlab` directory or update `docker-compose.yml` to build from source.

   **Option B: Run locally**

   Install and run the GitLab MCP server locally, then update `MCP_GITLAB_HOST` in your `.env` file accordingly.

   **Note**: The agent requires MCP servers to be running. There are no direct API fallbacks.

## Configuration

1. **Copy the environment template**:

   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` file** with your credentials:

   ```env
   # OpenAI API Key (Required)
   OPENAI_API_KEY=your_openai_api_key_here

   # Proxy Configuration (Optional - for OpenAI API calls)
   # Leave empty if not using a proxy
   HTTP_PROXY=http://proxy.example.com:8080
   HTTPS_PROXY=http://proxy.example.com:8080
   # Proxy authentication (optional) - leave empty if proxy doesn't require authentication
   PROXY_USERNAME=your_proxy_username
   PROXY_PASSWORD=your_proxy_password

   # Jira Configuration (Optional - for fetching task descriptions)
   JIRA_URL=http://jira.example.com
   JIRA_PERSONAL_ACCESS_TOKEN=your_jira_personal_access_token_here

   # Confluence Configuration (Optional - for fetching related documents)
   CONFLUENCE_URL=http://confluence.example.com/
   CONFLUENCE_PERSONAL_ACCESS_TOKEN=your_confluence_personal_access_token_here

   # MCP Atlassian Configuration (Required for Jira/Confluence integration)
   MCP_ATLASSIAN_ENABLED=true
   MCP_ATLASSIAN_HOST=localhost
   MCP_ATLASSIAN_PORT=3000
   MCP_ATLASSIAN_CONTAINER=mcp-atlassian
   MCP_ATLASSIAN_SSH_USER=root

   # MCP GitLab Configuration (Required - uses MCP for all GitLab operations)
   MCP_GITLAB_ENABLED=true
   MCP_GITLAB_HOST=localhost
   MCP_GITLAB_CONTAINER=mcp-gitlab
   MCP_GITLAB_SSH_USER=root

   # GitLab Configuration (Required - used by MCP servers and internal package context functions)
   GITLAB_URL=https://gitlab.example.com
   GITLAB_TOKEN=your_gitlab_personal_access_token_here
   ```

3. **Load environment variables** in your code:

   If using `python-dotenv`, add at the top of your script:

   ```python
   from dotenv import load_dotenv
   load_dotenv()
   ```

   Or export variables manually:

   ```bash
   export OPENAI_API_KEY="your_key"
   export JIRA_URL="http://jira.example.com"
   # ... etc
   ```

## Usage

This project provides reviewer agents as Python modules. Import and use them in your workflow:

### Example: Using Service Reviewer

```python
import asyncio
from agents.backend.service_reviewer.service_reviewer import ServiceReviewerContext, run_combined_review

async def review_code():
    # Create context with file content and metadata
    context = ServiceReviewerContext(
        workflow_input_as_text="Review this service file",
        content="""
        package service

        func ProcessOrder(orderID int) error {
            // Your code here
            return nil
        }
        """,
        diff="""
        +func ProcessOrder(orderID int) error {
        +    return nil
        +}
        """,
        jira_task_number="GME-123",
        package_name="service",
        module_name="sales",
        file_name="order_service.go",
        file_path="sales/service/internal/app/sales/order/order_service.go",
        gitlab_token=os.getenv("GITLAB_TOKEN"),
        gitlab_domain="gitlab.example.com",
        source_branch="feature/new-order",
        project_path="acme/shop"
    )

    # Run combined review (semantic + business logic)
    result = await run_combined_review(context)

    # Process results
    for issue in result.issues:
        print(f"Line {issue.line} [{issue.severity}]: {issue.problem}")
        print(f"  Suggestion: {issue.suggestion}")

# Run the review
asyncio.run(review_code())
```

### Example: Using SQL Reviewer

```python
from agents.backend.sql_reviewer.sql_reviewer import SqlReviewerContext, sql_reviewer
from agents import Runner, RunConfig

async def review_sql():
    context = SqlReviewerContext(
        workflow_input_as_text="Review this SQL migration",
        content="""
        CREATE TABLE products (
            id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            name TEXT NOT NULL
        );
        """,
        diff="...",
        file_name="001_create_products.sql"
    )

    result = await Runner.run(
        sql_reviewer,
        input=[{
            "role": "user",
            "content": [{"type": "input_text", "text": "Review this SQL file"}]
        }],
        context=context,
        run_config=RunConfig()
    )

    return result.final_output
```

### Example: Using Starter Agent for File Classification

```python
from agents.start.starter_agent import starter_agent
from agents import Runner, RunConfig

async def classify_file():
    result = await Runner.run(
        starter_agent,
        input=[{
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": f"""
                file_path: sales/service/internal/app/sales/invoice/invoice.go
                content: [file content here]
                diff: [git diff here]
                merge_request_title: Add invoice feature GME-456
                """
            }]
        }],
        context=None,
        run_config=RunConfig()
    )

    classification = result.final_output
    print(f"File type: {classification.file_type}")
    print(f"Module: {classification.module_name}")
    print(f"Package: {classification.package_name}")
```

### Available Reviewers

1. **`service_reviewer`** - For Go service files

   - `semantic_reviewer` - Technical code quality only
   - `business_logic_reviewer` - Business requirements only
   - `run_combined_review()` - Combined review function

2. **`sql_reviewer`** - For SQL migration files

3. **`dataview_reviewer`** - For dataview implementation files

4. **`test_reviewer`** - For BDD/Gherkin test files

5. **`pbook_reviewer`** - For error message files (pbook)

6. **`starter_agent`** - For file classification and routing

## Project Structure

```
codereviewagent/
├── main.py                 # Main entry point for GitLab MR reviews
├── service_reviewer.py    # Go service code reviewer
├── sql_reviewer.py         # SQL migration reviewer
├── dataview_reviewer.py    # Dataview code reviewer
├── test_reviewer.py        # BDD/test code reviewer
├── pbook_reviewer.py       # Error message file reviewer
├── starter_agent.py        # File classification agent
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (not in git)
├── .env.example            # Environment variable template
└── README.md               # This file
```

## Environment Variables

| Variable                           | Required | Description                                            |
| ---------------------------------- | -------- | ------------------------------------------------------ |
| `OPENAI_API_KEY`                   | Yes      | OpenAI API key for AI-powered reviews                  |
| `HTTP_PROXY`                       | No       | HTTP proxy URL (e.g., http://proxy.example.com:8080)   |
| `HTTPS_PROXY`                      | No       | HTTPS proxy URL (e.g., http://proxy.example.com:8080)  |
| `PROXY_USERNAME`                   | No       | Proxy authentication username (if required)            |
| `PROXY_PASSWORD`                   | No       | Proxy authentication password (if required)            |
| `GITLAB_TOKEN`                     | Yes\*    | GitLab personal access token (required for MR reviews) |
| `JIRA_URL`                         | No       | Jira instance URL                                      |
| `JIRA_PERSONAL_ACCESS_TOKEN`       | No       | Jira personal access token                             |
| `CONFLUENCE_URL`                   | No       | Confluence instance URL                                |
| `CONFLUENCE_PERSONAL_ACCESS_TOKEN` | No       | Confluence personal access token                       |
| `MCP_ATLASSIAN_ENABLED`            | No       | Enable MCP Atlassian integration (true/false)          |
| `MCP_ATLASSIAN_HOST`               | No       | MCP server host (default: localhost)                   |
| `MCP_ATLASSIAN_PORT`               | No       | MCP server port (default: 3000)                        |
| `MCP_ATLASSIAN_CONTAINER`          | No       | Docker container name (default: mcp-atlassian)         |
| `MCP_GITLAB_ENABLED`               | No       | Enable MCP GitLab integration (default: true)          |
| `MCP_GITLAB_HOST`                  | No       | GitLab MCP server host (default: localhost)            |
| `MCP_GITLAB_CONTAINER`             | No       | GitLab MCP container name (default: mcp-gitlab)        |
| `GITLAB_URL`                       | Yes\*    | GitLab instance URL                                    |
| `GITLAB_TOKEN`                     | Yes\*    | GitLab personal access token (required for MR reviews) |
| `REVIEW_EXCLUDE_PATTERNS`          | No       | Comma-separated glob patterns to exclude files from review (e.g., `*.md,docs/*,test/fixtures/*`) |

\* Required when using `main.py --gitlab-mr` command

## Integration with Workflows

This project is designed to be integrated into CI/CD pipelines or code review workflows. Example workflow:

1. Trigger on merge request creation/update
2. Use `starter_agent` to classify changed files
3. Route files to appropriate reviewers based on classification
4. Collect and format review results
5. Post comments back to merge request

## Security Notes

- **Never commit `.env` file** - It contains sensitive credentials
- All personal access tokens and API keys are stored in environment variables
- The `.env` file is already in `.gitignore`
- Use `.env.example` as a template for team members
- Make sure to set `GITLAB_TOKEN` in `.env` for merge request reviews

## Proxy Configuration

If you're behind a corporate proxy or need to route OpenAI API calls through a proxy server, configure the proxy settings in your `.env` file:

### Without Authentication

```env
HTTP_PROXY=http://proxy.example.com:8080
HTTPS_PROXY=http://proxy.example.com:8080
```

### With Authentication

```env
HTTP_PROXY=http://proxy.example.com:8080
HTTPS_PROXY=http://proxy.example.com:8080
PROXY_USERNAME=your_username
PROXY_PASSWORD=your_password
```

The proxy configuration is automatically applied at startup for all OpenAI API calls. The credentials are URL-encoded and embedded in the proxy URL securely.

**Note**: Leave the proxy variables empty or unset if you don't need a proxy.

## Troubleshooting

### Environment Variables Not Loading

If environment variables aren't being read:

- Ensure `.env` file exists in the project root
- Install `python-dotenv`: `pip install python-dotenv`
- Add `load_dotenv()` at the top of your script before importing reviewers

### API Connection Issues

- Verify API keys are correct in `.env`
- Check network connectivity
- Ensure URLs are accessible (Jira, Confluence, etc.)
- If using a proxy, verify proxy settings and credentials are correct

### Proxy Issues

If OpenAI API calls fail with proxy:

- Verify `HTTP_PROXY` and `HTTPS_PROXY` are correctly formatted
- Check proxy credentials if authentication is required
- Ensure proxy server allows outbound connections to api.openai.com
- Test proxy connectivity: `curl -x http://proxy:8080 https://api.openai.com`

### Missing Dependencies

Install all required packages:

```bash
pip install -r requirements.txt
```

## Contributing

1. Follow the existing code structure
2. Ensure all sensitive data uses environment variables
3. Test reviewers with appropriate file types
4. Update documentation for new features

## License

[Add your license here]

## Support

For issues or questions, please open an issue in the GitLab repository.

## Flow

```
                                                                                                                       
                                                       Gitlab MR                                                       
                                                                                                                       
                                                           │                                                           
                                                           │                           ┌───────────┐                   
                                                     ┌─────▼─────┐       Get MR Data   │           │                   
┌───────────────────────────────────────────────────▷│   Main    │────────────────────▷│  GitLab   │                   
│                                                    │           │◁────────────────────│  Server   │                   
│                                                    └─────┬─────┘                     │           │                   
│                                                          │                           └───────────┘                   
│                                                          │                                                           
│   ┌──────────────────────────────────────────────────────┼──────────────────────────────────────────────────────────┐
│   │               Reviewer                         ┌─────▼──────┐                                                   │
│   │               Langchain                        │ Classifier │                                                   │
│   │                                                │   Agent    │                                                   │
│   │                                                └─────┬──────┘                                                   │
│   │     ┌──────────────┬─────────────┬─────────────┬─────┴───────┬─────────────┬──────────────┬────────────┐        │
│   │     │              │             │             │             │             │              │            │        │
│   │┌────▽───────┐┌─────▽──────┐┌─────▽──────┐┌─────▽──────┐┌─────▽──────┐┌─────▽───────┐┌─────▽──────┐┌────▽───────┐│
│   ││ SCSS Agent ││ HTML Agent ││ SQL Agent  ││ Test Agent ││PBook Agent ││Service Agent││  TS Agent  ││  DV Agent  ││
│   │└────┬───────┘└─────┬──────┘└─────┬──────┘└─────┬──────┘└─┬──────────┘└──────┬──────┘└───────────┬┘└───┬────────┘│
│   │     │              │             │             │         │         ┌─▻──────▽──────◅──┐         │     │         │
│   │     │              │             │             │         │         │        │         │         │     │         │
│   │     │              │             │             │         │  ┌──────▽───────┐│  ┌──────▽────────┐│     │         │
│   │     │              │             │             │         │  │Semantic Agent││  │Business Agent ││     │         │
│   │     │              │             │             │         │  └──────────────┘│  └───────────────┘│     │         │
│   │     ▽──────────────▽─────────────▽─────────────▽──────┬──▽──────────────────▽───────────────────▽─────▽         │
│   │                                                       │                                                         │
│   │                                           ┌───────────▽───────────┐                                             │
│   │                                           │ Comment Review Agent  │                                             │
│   │                                           └───────────┬───────────┘                                             │
│   └───────────────────────────────────────────────────────┼─────────────────────────────────────────────────────────┘
└───────────────────────────────────────────────────────────┘                                                          
```