# LangSmith Architecture Integration

This document explains how LangSmith integrates with your CodeReviewAgent workflow.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     CodeReviewAgent                          │
│                                                              │
│  ┌────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │   main.py  │───▶│  LangGraph   │───▶│  Review Agents │  │
│  │            │    │  Workflow    │    │  (Service,SQL, │  │
│  │            │    │              │    │   Test, etc.)  │  │
│  └────────────┘    └──────────────┘    └────────────────┘  │
│         │                  │                     │          │
│         │                  │                     │          │
│         └──────────────────┴─────────────────────┘          │
│                            │                                │
│                            │ Automatic Tracing              │
│                            ▼                                │
└────────────────────────────┼─────────────────────────────────┘
                             │
                             │ HTTPS
                             │
                    ┌────────▼────────┐
                    │   LangSmith     │
                    │   Platform      │
                    │                 │
                    │  - Traces       │
                    │  - Metrics      │
                    │  - Analytics    │
                    └─────────────────┘
```

## How Tracing Works

### 1. Initialization (main.py)

When your application starts:

```python
# Load environment variables
load_dotenv()

# Configure LangSmith tracing
if os.getenv("LANGCHAIN_TRACING_V2") == "true":
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
    print("🔍 LangSmith tracing enabled")
```

This sets up the tracing context for all subsequent LangChain/LangGraph operations.

### 2. Automatic Instrumentation

LangSmith automatically traces:

- **LangGraph Workflows**: Every node, edge, and state transition
- **LLM Calls**: All calls to OpenAI/OpenRouter models
- **Tool Calls**: MCP tools (GitLab, Atlassian)
- **Agent Decisions**: Routing logic and agent selection

**No code changes needed!** Just set the environment variables.

### 3. Trace Structure

For each merge request review:

```
Trace: MR #123 Review
├─ File 1: service.go
│  ├─ Node: classify_file
│  │  └─ LLM Call: gpt-4o-mini (classify)
│  ├─ Node: route_to_agent
│  ├─ Node: service_agent_review
│  │  ├─ LLM Call: claude-sonnet-4.5 (semantic review)
│  │  ├─ Tool Call: gitlab_get_file (context)
│  │  └─ LLM Call: claude-sonnet-4.5 (business review)
│  └─ Node: extract_issues
│
├─ File 2: migration.sql
│  ├─ Node: classify_file
│  ├─ Node: route_to_agent
│  ├─ Node: sql_agent_review
│  │  └─ LLM Call: gpt-4o-mini (sql review)
│  └─ Node: extract_issues
│
└─ Node: comment_reviewer
   └─ LLM Call: gpt-4o-mini (filter comments)
```

### 4. Data Captured

For each trace, LangSmith captures:

#### Inputs
- File content
- Diff
- MR metadata
- Agent instructions

#### Outputs
- Review comments
- Severity levels
- Line numbers
- Recommendations

#### Metadata
- Model used
- Token counts
- Latency
- Cost estimates

#### Context
- Agent type
- File type
- MR ID
- Branch names

## Trace Lifecycle

```
┌──────────────┐
│ MR Review    │
│ Starts       │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Create Root  │
│ Trace        │
└──────┬───────┘
       │
       ▼
┌──────────────────┐
│ For Each File:   │
│ - Create Span    │
│ - Run Workflow   │
│ - Log Results    │
└──────┬───────────┘
       │
       ▼
┌──────────────┐
│ Close Trace  │
│ Send to      │
│ LangSmith    │
└──────────────┘
```

## Performance Impact

LangSmith tracing has minimal overhead:

- **Latency**: +10-50ms per trace
- **Memory**: ~1-5MB per trace
- **Network**: Async, non-blocking

The tracing happens in the background and won't slow down your reviews.

## Data Flow

```
┌─────────────────┐
│ Your Code       │
│ (LangGraph)     │
└────────┬────────┘
         │
         │ Trace Events
         ▼
┌─────────────────┐
│ LangSmith SDK   │
│ (in-memory      │
│  buffer)        │
└────────┬────────┘
         │
         │ Batched HTTPS
         │ (async)
         ▼
┌─────────────────┐
│ LangSmith API   │
│ (cloud)         │
└────────┬────────┘
         │
         │ Storage
         ▼
┌─────────────────┐
│ LangSmith DB    │
│ (persistent)    │
└─────────────────┘
```

## Security & Privacy

### What Gets Sent to LangSmith

✅ **Sent**:
- LLM inputs/outputs
- Token counts
- Timing metrics
- Model names
- Agent decisions

❌ **NOT Sent**:
- GitLab tokens
- API keys
- Credentials
- Environment variables (except trace config)

### Data Retention

- **Free Tier**: 30 days
- **Paid Tiers**: Configurable (up to 1 year)

### Access Control

- Only you can see your traces (unless you share them)
- Team members can be invited with role-based access
- Public links can be generated for specific traces

## Configuration Options

### Environment Variables

```env
# Enable/disable tracing
LANGCHAIN_TRACING_V2=true|false

# Your API key
LANGCHAIN_API_KEY=lsv2_pt_...

# Project name (organizes traces)
LANGCHAIN_PROJECT=codereviewagent

# Optional: Custom endpoint (for self-hosted)
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com

# Optional: Sampling rate (trace 10% of requests)
LANGCHAIN_SAMPLING_RATE=0.1
```

### Programmatic Control

You can also control tracing in code:

```python
from langsmith import Client

# Disable tracing for specific operation
with Client().trace_off():
    # This won't be traced
    result = some_operation()

# Add custom metadata
from langchain_core.tracers import LangChainTracer

tracer = LangChainTracer(
    project_name="codereviewagent",
    tags=["production", "critical-mr"]
)
```

## Monitoring Dashboard

In LangSmith, you can create dashboards to monitor:

### Key Metrics

1. **Throughput**
   - Reviews per day
   - Files per review
   - Comments per file

2. **Performance**
   - Average review time
   - P95/P99 latency
   - Slowest agents

3. **Cost**
   - Tokens per review
   - Cost per file type
   - Daily/monthly spend

4. **Quality**
   - Error rate
   - Retry rate
   - Agent agreement

### Alerts

Set up alerts for:

- Error rate > 5%
- Latency > 30s
- Daily cost > $X
- Unusual patterns

## Debugging Workflow

When something goes wrong:

1. **Find the Trace**
   - Search by MR ID or file name
   - Filter by error status
   - Sort by date

2. **Analyze the Trace**
   - See exact inputs to each agent
   - Check LLM responses
   - Verify tool calls

3. **Identify the Issue**
   - Was the input malformed?
   - Did the LLM misunderstand?
   - Did a tool call fail?

4. **Fix and Verify**
   - Update prompt or code
   - Re-run the review
   - Compare traces

## Best Practices

### 1. Use Descriptive Project Names

```env
# Good
LANGCHAIN_PROJECT=codereviewagent-prod

# Better
LANGCHAIN_PROJECT=codereviewagent-gitlab-prod-v2
```

### 2. Add Tags for Important Reviews

```python
# Tag critical MRs
os.environ["LANGCHAIN_TAGS"] = "critical,security-review"
```

### 3. Monitor Regularly

- Check dashboard weekly
- Review error traces
- Optimize slow agents

### 4. Use Sampling in High-Volume Scenarios

```env
# Only trace 10% of reviews
LANGCHAIN_SAMPLING_RATE=0.1
```

### 5. Share Traces for Collaboration

When discussing issues with your team:
1. Open the trace
2. Click "Share"
3. Send the public link

## Integration Points

### Where Tracing Happens

1. **main.py**
   - MR processing start/end
   - Batch processing

2. **core/workflow/graph.py**
   - LangGraph workflow execution
   - Node transitions
   - State updates

3. **core/agents/*.py**
   - Individual agent reviews
   - LLM calls
   - Tool usage

4. **core/utils/file_reviewer.py**
   - File review orchestration
   - Comment filtering

### What Gets Traced

✅ **Automatically Traced**:
- All LangGraph nodes
- All LLM calls
- All tool calls
- All agent decisions

❌ **Not Traced**:
- HTTP requests (outside LangChain)
- Database queries (if any)
- File I/O operations
- GitLab API calls (direct)

## Troubleshooting

### Traces Not Appearing

1. Check environment variables
2. Verify API key is valid
3. Check internet connectivity
4. Look for errors in console

### Incomplete Traces

- May indicate a crash or exception
- Check error logs
- Verify all agents completed

### High Latency

- LangSmith adds ~10-50ms
- If higher, check network
- Consider disabling in dev

### Cost Concerns

- Free tier: 5,000 traces/month
- Monitor usage in LangSmith
- Use sampling if needed

## Summary

LangSmith integration provides:

✅ **Zero-code instrumentation** - Just set environment variables
✅ **Full visibility** - See every step of your workflow
✅ **Performance insights** - Optimize based on real data
✅ **Easy debugging** - Understand failures quickly
✅ **Cost tracking** - Monitor token usage
✅ **Team collaboration** - Share traces and insights

The integration is:
- **Non-invasive**: No code changes required
- **Optional**: Can be disabled anytime
- **Lightweight**: Minimal performance impact
- **Secure**: Credentials never sent to LangSmith

---

For more information:
- **Setup Guide**: `LANGSMITH_SETUP.md`
- **Quick Start**: `../LANGSMITH_QUICKSTART.md`
- **Changes**: `../LANGSMITH_CHANGES.md`
