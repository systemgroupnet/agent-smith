# CodeReviewAgent

An AI-powered code review system for the application. This project provides specialized reviewers for different file types including Go services, SQL migrations, dataviews, test files, and error message files (pbook).

> **Repository layout (monorepo):** this repo hosts two services — **`reviewer/`** (the code-review agent described below) and **`collector/`** (a second service, currently a scaffold). The commands in this README run from inside **`reviewer/`** (run `cd reviewer` first). The repo-root `.gitlab-ci.yml` builds and pushes one image per service (`reviewer` and `collector`) on git tags.

## Features

- **Multi-Type Code Reviewers**: Specialized agents for different file types:
  - **Service Reviewer**: Reviews Go service code with split architecture (semantic + business logic review)
  - **SQL Reviewer**: Reviews SQL migrations and schema changes
  - **Dataview Reviewer**: Reviews dataview implementation code
  - **Test Reviewer**: Reviews BDD/Gherkin test implementation code
  - **Pbook Reviewer**: Reviews error message files (Persian/English)
  - **Static File Classifier**: Classifies files based on name patterns and routes them to appropriate reviewers

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

### Agent-Specific Instructions (`.aid` Directory)

The code review system supports agent-specific instructions and conventions through Markdown files stored in your GitLab repository. These files allow you to customize the behavior of each review agent per module.

#### Directory Structure

```
{root_dir}/.aid/              # Root-level instructions (apply to all modules)
├── service.md                # Instructions for service code reviewer
├── sql.md                    # Instructions for SQL reviewer
├── dataview.md               # Instructions for dataview reviewer
├── test.md                   # Instructions for test reviewer
├── typescript.md             # Instructions for TypeScript reviewer
├── html.md                   # Instructions for HTML reviewer
├── scss.md                   # Instructions for SCSS reviewer
└── pbook.md                  # Instructions for pbook reviewer

{root_dir}/{module_name}/.aid/  # Module-specific instructions (override root)
├── service.md
├── sql.md
└── ...
```

#### How It Works

1. **Root-level instructions**: Place `.aid/{agent_type}.md` at the root of your project to define project-wide instructions
2. **Module-specific instructions**: Place `.aid/{agent_type}.md` within a module directory to add module-specific instructions
3. **Merging**: Both root and module instructions are loaded and merged (module instructions are appended after root instructions)

#### Example Markdown File

Create `acme-shop/.aid/service.md`:

```markdown
## Project Conventions

1. Always use dependency injection for database connections
2. Never use hardcoded configuration values
3. All service methods must return errors using custom error types

## Security Guidelines

- Validate all user inputs before processing
- Use parameterized queries for all database operations
- Log all authentication attempts

## Module-Specific Notes

This module handles financial transactions - extra care with decimal precision.
```

These instructions will be injected into the system prompt under the "Additional Instructions" section.

#### Benefits

- **Flexible formatting**: Use Markdown formatting, code blocks, lists, and links
- **Project-specific rules**: Customize review criteria per project and module
- **Version controlled**: Instructions are stored in your GitLab repository
- **No code changes needed**: Update instructions without modifying the review system

### Environment Configuration

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

### LangSmith Configuration (Optional - for Observability)

LangSmith provides powerful tracing and monitoring capabilities for your LangGraph workflows. It helps you debug, optimize, and understand how your code review agents are performing.

1. **Sign up for LangSmith**:
   - Go to [https://smith.langchain.com/](https://smith.langchain.com/)
   - Create a free account
   - Navigate to Settings → API Keys
   - Create a new API key

2. **Add LangSmith configuration to your `.env` file**:

   ```env
   # LangSmith Configuration
   LANGSMITH_TRACING=true
   LANGSMITH_API_KEY=your_langsmith_api_key_here
   LANGSMITH_PROJECT=codereviewagent
   LANGSMITH_ENDPOINT=https://api.smith.langchain.com
   ```

3. **What you'll see in LangSmith**:
   - **One thread per MR review**: every run of a review (one trace per file plus the
     comment reviewer) is grouped into a single thread keyed by the MR
     (`{project_path}!{mr_id}`). Re-reviewing the same MR appends to the same thread.
     Find them under the project's **Threads** tab.
   - **Full trace of each review**: See every step of the LangGraph workflow
   - **Token usage**: Monitor costs per file review
   - **Latency metrics**: Identify slow agents or bottlenecks
   - **Agent decisions**: Debug why certain issues were flagged or filtered
   - **Error tracking**: Catch and diagnose failures in production

4. **Test your configuration**:

   ```bash
   python scripts/test_langsmith.py
   ```

   This will verify your LangSmith setup and optionally create a test trace.

5. **Viewing traces**:
   - Once configured, run a code review
   - Visit [https://smith.langchain.com/](https://smith.langchain.com/)
   - Navigate to your project (e.g., "codereviewagent")
   - Click on any trace to see the full execution flow

**Benefits**:
- Debug complex multi-agent workflows
- Optimize performance by identifying slow components
- Track costs and token usage over time
- Share traces with your team for collaboration

**For detailed setup instructions**, see [docs/LANGSMITH_SETUP.md](docs/LANGSMITH_SETUP.md)

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

### Example: Using Static File Classifier

```python
from core.utils.file_classifier import static_classifier

async def classify_file():
    input_text = """
file_path: sales/service/internal/app/sales/invoice/invoice.go
content: [file content here]
diff: [git diff here]
merge_request_title: Add invoice feature GME-456
target_branch: main
source_branch: feature/invoice
"""

    classification = await static_classifier.run(
        input_text,
        file_path="sales/service/internal/app/sales/invoice/invoice.go"
    )

    print(f"File type: {classification.file_type}")
    print(f"Module: {classification.module_name}")
    print(f"Package: {classification.package_name}")
```

**Static Classification Rules:**

- `*.sql` or `db.go` → SQL agent
- `dataview.go` → Dataview agent
- `*_test.go` or `ts.go` or `*_ts.go` → Test agent
- `pbook.go` → Pbook agent
- `*.ts` → TypeScript agent
- `*.html` → HTML agent
- `*.scss` or `*.css` → SCSS agent
- `service.go`, `validation.go`, or other `.go` files → Service agent

### Available Reviewers

1. **`service_reviewer`** - For Go service files
   - `semantic_reviewer` - Technical code quality only
   - `business_logic_reviewer` - Business requirements only
   - `run_combined_review()` - Combined review function

2. **`sql_reviewer`** - For SQL migration files

3. **`dataview_reviewer`** - For dataview implementation files

4. **`test_reviewer`** - For BDD/Gherkin test files

5. **`pbook_reviewer`** - For error message files (pbook)

6. **Static File Classifier** - For file classification and routing (no LLM required)

## Project Structure

```
codereviewagent/
├── main.py                          # Main entry point for GitLab MR reviews
├── core/
│   ├── agents/                      # Specialized review agents
│   │   ├── service_agents.py       # Go service code reviewer
│   │   ├── sql_agent.py            # SQL migration reviewer
│   │   ├── dataview_agent.py       # Dataview code reviewer
│   │   ├── test_agent.py           # BDD/test code reviewer
│   │   ├── pbook_agent.py          # Error message file reviewer
│   │   ├── typescript_agent.py     # TypeScript reviewer
│   │   ├── html_agent.py           # HTML reviewer
│   │   └── scss_agent.py           # SCSS/CSS reviewer
│   ├── utils/
│   │   └── file_classifier.py      # Static file classification (no LLM)
│   └── workflow/
│       ├── graph.py                 # LangGraph workflow orchestration
│       └── state.py                 # Workflow state management
├── requirements.txt                 # Python dependencies
├── .env                             # Environment variables (not in git)
├── .env.example                     # Environment variable template
└── README.md                        # This file
```

## Environment Variables

| Variable                           | Required | Description                                                                                      |
| ---------------------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| `OPENAI_API_KEY`                   | Yes      | OpenAI API key for AI-powered reviews                                                            |
| `HTTP_PROXY`                       | No       | HTTP proxy URL (e.g., http://proxy.example.com:8080)                                             |
| `HTTPS_PROXY`                      | No       | HTTPS proxy URL (e.g., http://proxy.example.com:8080)                                            |
| `PROXY_USERNAME`                   | No       | Proxy authentication username (if required)                                                      |
| `PROXY_PASSWORD`                   | No       | Proxy authentication password (if required)                                                      |
| `GITLAB_TOKEN`                     | Yes\*    | GitLab personal access token (required for MR reviews)                                           |
| `JIRA_URL`                         | No       | Jira instance URL                                                                                |
| `JIRA_PERSONAL_ACCESS_TOKEN`       | No       | Jira personal access token                                                                       |
| `CONFLUENCE_URL`                   | No       | Confluence instance URL                                                                          |
| `CONFLUENCE_PERSONAL_ACCESS_TOKEN` | No       | Confluence personal access token                                                                 |
| `MCP_ATLASSIAN_ENABLED`            | No       | Enable MCP Atlassian integration (true/false)                                                    |
| `MCP_ATLASSIAN_HOST`               | No       | MCP server host (default: localhost)                                                             |
| `MCP_ATLASSIAN_PORT`               | No       | MCP server port (default: 3000)                                                                  |
| `MCP_ATLASSIAN_CONTAINER`          | No       | Docker container name (default: mcp-atlassian)                                                   |
| `MCP_GITLAB_ENABLED`               | No       | Enable MCP GitLab integration (default: true)                                                    |
| `MCP_GITLAB_HOST`                  | No       | GitLab MCP server host (default: localhost)                                                      |
| `MCP_GITLAB_CONTAINER`             | No       | GitLab MCP container name (default: mcp-gitlab)                                                  |
| `GITLAB_URL`                       | Yes\*    | GitLab instance URL                                                                              |
| `GITLAB_TOKEN`                     | Yes\*    | GitLab personal access token (required for MR reviews)                                           |
| `REVIEW_EXCLUDE_PATTERNS`          | No       | Comma-separated glob patterns to exclude files from review (e.g., `*.md,docs/*,test/fixtures/*`) |

\* Required when using `main.py --gitlab-mr` command

## Integration with Workflows

This project is designed to be integrated into CI/CD pipelines or code review workflows. Example workflow:

1. Trigger on merge request creation/update
2. Use static file classifier to classify changed files based on file name patterns
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
