# LangSmith Setup Guide

This guide will help you set up LangSmith for monitoring and debugging your CodeReviewAgent.

## What is LangSmith?

LangSmith is an observability platform for LangChain and LangGraph applications. It provides:

- **Tracing**: See every step of your agent's execution
- **Debugging**: Understand why agents make certain decisions
- **Monitoring**: Track performance, costs, and errors
- **Optimization**: Identify bottlenecks and improve efficiency

## Quick Start

### 1. Create a LangSmith Account

1. Visit [https://smith.langchain.com/](https://smith.langchain.com/)
2. Sign up for a free account (no credit card required)
3. You'll get generous free tier limits:
   - 5,000 traces per month
   - 30-day trace retention
   - Full feature access

### 2. Get Your API Key

1. After logging in, click on your profile (bottom left)
2. Go to **Settings** → **API Keys**
3. Click **Create API Key**
4. Give it a name (e.g., "CodeReviewAgent")
5. Copy the API key (you won't be able to see it again!)

### 3. Configure Your Environment

Add these lines to your `.env` file:

```env
# LangSmith Configuration
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=***REMOVED***
LANGCHAIN_PROJECT=codereviewagent
```

Replace `***REMOVED***` with your actual API key.

### 4. Run Your First Traced Review

```bash
python main.py --gitlab-mr "https://gitlab.example.com/project/-/merge_requests/123"
```

You should see this output at startup:

```
🔍 LangSmith tracing enabled
📊 Project: codereviewagent
```

### 5. View Your Traces

1. Go to [https://smith.langchain.com/](https://smith.langchain.com/)
2. Click on **Projects** in the left sidebar
3. Select your project (e.g., "codereviewagent")
4. You'll see a list of all traces (one per merge request review)

## Understanding Traces

### Trace Structure

Each merge request review creates a trace with the following structure:

```
📊 MR Review (Root)
├── 📄 File 1 Review
│   ├── 🔍 File Classification
│   ├── 🤖 Agent Selection
│   ├── 📝 Code Analysis
│   └── ✅ Issue Extraction
├── 📄 File 2 Review
│   └── ...
└── 🔍 Comment Filtering
    └── 🤖 Comment Reviewer Agent
```

### Key Metrics to Monitor

1. **Latency**: How long each file takes to review
2. **Token Usage**: Cost per file and per MR
3. **Error Rate**: Failed reviews or agent errors
4. **Agent Performance**: Which agents are slowest/most expensive

### Trace Details

Click on any trace to see:

- **Input/Output**: What was sent to each agent and what it returned
- **Metadata**: File path, agent type, model used
- **Timing**: Start time, end time, duration
- **Tokens**: Prompt tokens, completion tokens, total cost
- **Errors**: Stack traces and error messages

## Advanced Features

### 1. Multiple Projects

You can create different projects for different environments:

```env
# Development
LANGCHAIN_PROJECT=codereviewagent-dev

# Production
LANGCHAIN_PROJECT=codereviewagent-prod

# Testing
LANGCHAIN_PROJECT=codereviewagent-test
```

### 2. Filtering and Searching

In the LangSmith UI, you can:

- Filter by status (success, error)
- Search by file name or MR ID
- Filter by date range
- Sort by latency or cost

### 3. Sharing Traces

To share a trace with your team:

1. Click on a trace
2. Click the **Share** button (top right)
3. Copy the public link
4. Anyone with the link can view the trace (read-only)

### 4. Annotations

You can add feedback to traces:

1. Open a trace
2. Click **Add Feedback**
3. Add a score (👍/👎) or comment
4. Use this to track which reviews were helpful

## Troubleshooting

### Traces Not Appearing

1. **Check API Key**: Make sure `LANGCHAIN_API_KEY` is set correctly
2. **Check Tracing Flag**: Ensure `LANGCHAIN_TRACING_V2=true`
3. **Check Network**: LangSmith requires internet access
4. **Check Logs**: Look for LangSmith errors in console output

### Slow Performance

LangSmith adds minimal overhead (~10-50ms per trace). If you notice significant slowdowns:

1. Check your internet connection
2. Verify LangSmith service status
3. Consider disabling tracing in production if needed

### Disabling Tracing

To temporarily disable tracing:

```env
LANGCHAIN_TRACING_V2=false
```

Or comment out the line:

```env
# LANGCHAIN_TRACING_V2=true
```

## Best Practices

### 1. Use Descriptive Project Names

Instead of generic names, use descriptive ones:

```env
LANGCHAIN_PROJECT=codereviewagent-gitlab-prod
```

### 2. Monitor Regularly

Check LangSmith weekly to:

- Identify slow agents
- Track cost trends
- Find error patterns
- Optimize prompts

### 3. Use Tags

You can add tags to traces programmatically:

```python
from langsmith import Client

client = Client()
client.create_run(
    name="review",
    inputs={"file": "service.go"},
    tags=["production", "critical"]
)
```

### 4. Set Up Alerts

LangSmith can alert you when:

- Error rate exceeds threshold
- Latency is too high
- Costs spike unexpectedly

Configure alerts in Settings → Alerts.

## Cost Management

### Free Tier Limits

- 5,000 traces/month
- 30-day retention
- Full features

### Paid Plans

If you exceed free tier:

- **Developer**: $39/month (50K traces)
- **Team**: $199/month (500K traces)
- **Enterprise**: Custom pricing

### Optimizing Trace Usage

To stay within free tier:

1. **Disable in development**: Only trace production reviews
2. **Sample traces**: Only trace 10% of reviews
3. **Use filters**: Only trace specific file types

## Support

- **Documentation**: [https://docs.smith.langchain.com/](https://docs.smith.langchain.com/)
- **Discord**: [https://discord.gg/langchain](https://discord.gg/langchain)
- **GitHub Issues**: [https://github.com/langchain-ai/langsmith-sdk](https://github.com/langchain-ai/langsmith-sdk)

## Next Steps

1. ✅ Set up LangSmith account
2. ✅ Configure environment variables
3. ✅ Run your first traced review
4. 📊 Explore traces in the UI
5. 🔍 Identify optimization opportunities
6. 📈 Monitor performance over time

Happy tracing! 🚀
