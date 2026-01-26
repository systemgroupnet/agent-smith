# Migration Guide: OpenAI Agents → Pydantic AI with OpenRouter

This guide explains the refactoring of the code review agent from OpenAI's `agents` library to **Pydantic AI** with **OpenRouter** support.

## Table of Contents

1. [Overview](#overview)
2. [Key Changes](#key-changes)
3. [Setup Instructions](#setup-instructions)
4. [Configuration](#configuration)
5. [Code Changes Summary](#code-changes-summary)
6. [Testing](#testing)
7. [Troubleshooting](#troubleshooting)

## Overview

### Why Pydantic AI?

- **Provider Flexibility**: Use any LLM provider through OpenRouter (OpenAI, Anthropic, Google, Meta, etc.)
- **Cost Optimization**: Easily switch between models based on cost/performance needs
- **Modern Architecture**: Built on Pydantic v2 with type safety
- **Better Testing**: Easier to mock and test than OpenAI's agents

### Why OpenRouter?

- **200+ Models**: Access to models from multiple providers with a single API key
- **Automatic Fallbacks**: If one model is unavailable, OpenRouter can fall back to alternatives
- **Cost Tracking**: Built-in cost tracking and analytics
- **Rate Limiting**: Handles rate limits intelligently across providers

## Key Changes

### 1. Dependencies

**Before:**
```python
openai-agents==0.4.2
openai>=1.0.0
```

**After:**
```python
pydantic-ai>=0.0.14
openai>=1.0.0  # Still needed for translation function
httpx>=0.27.0  # Required by pydantic-ai
```

### 2. Agent Definition

**Before (OpenAI Agents):**
```python
from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning

sql_reviewer = Agent(
    name="SQL reviewer",
    instructions=sql_reviewer_instructions,
    model="gpt-5-nano",
    output_type=ReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(effort="medium")
    )
)
```

**After (Pydantic AI):**
```python
from pydantic_ai import Agent, RunContext
from core.config.llm_config import get_model_for_agent

sql_reviewer = Agent(
    model=get_model_for_agent("gpt-4o"),
    result_type=ReviewResponse,
    system_prompt=sql_reviewer_instructions,
    deps_type=SqlReviewerContext
)
```

### 3. Context Classes

**Before (Plain Class):**
```python
class SqlReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
```

**After (Pydantic BaseModel):**
```python
from pydantic import BaseModel

class SqlReviewerContext(BaseModel):
    workflow_input_as_text: str
    content: str
    diff: str
    file_name: str = ""
```

### 4. Instruction Functions

**Before:**
```python
def sql_reviewer_instructions(run_context: RunContextWrapper[SqlReviewerContext], _agent: Agent):
    content = run_context.context.content
    diff = run_context.context.diff
    return f"Review this SQL..."
```

**After:**
```python
def sql_reviewer_instructions(run_context: RunContext[SqlReviewerContext]) -> str:
    content = run_context.deps.content
    diff = run_context.deps.diff
    return f"Review this SQL..."
```

### 5. Agent Execution

**Before:**
```python
from agents import Runner, RunConfig

result = await Runner.run(
    sql_reviewer,
    input=[{
        "role": "user",
        "content": [{"type": "input_text", "text": "Review this SQL"}]
    }],
    context=context,
    run_config=RunConfig()
)

if result.final_output:
    issues = result.final_output.issues
```

**After:**
```python
result = await sql_reviewer.run(
    "Review this SQL file",
    deps=context
)

if result.data:
    issues = result.data.issues
```

## Setup Instructions

### Step 1: Install Dependencies

```bash
cd /home/amirhossein/projects/codereviewagent
pip install -r requirements.txt
```

### Step 2: Get OpenRouter API Key

1. Go to [https://openrouter.ai/keys](https://openrouter.ai/keys)
2. Sign up or log in
3. Create a new API key
4. Copy the key

### Step 3: Configure Environment

```bash
# Copy the example file
cp .env.example .env

# Edit .env and add your keys
nano .env
```

**Minimum required configuration:**

```env
# OpenRouter (required)
OPENROUTER_API_KEY=sk-or-v1-...

# OpenAI (still needed for translation)
OPENAI_API_KEY=sk-...

# GitLab (required for MR reviews)
GITLAB_TOKEN=glpat-...
GITLAB_URL=https://your-gitlab.com
GITLAB_PROJECT_PATH=group/project
```

## Configuration

### Model Selection

The refactored code uses `llm_config.py` to manage model selection:

```python
from core.config.llm_config import get_model_for_agent

# Get model with automatic mapping
model = get_model_for_agent("gpt-4o-mini")

# Or use environment variables
model = get_llm_model()  # Uses DEFAULT_LLM_MODEL from .env
```

### Model Mapping

Legacy model names are automatically mapped:

| Old Model (OpenAI Agents) | New Model (OpenRouter) |
|---------------------------|------------------------|
| `gpt-5-nano` (doesn't exist) | `openai/gpt-4o-mini` |
| `gpt-4o-mini` | `openai/gpt-4o-mini` |
| `gpt-4o` | `openai/gpt-4o` |

### Switching Models

You can easily switch to different providers:

```env
# Use Anthropic Claude instead of OpenAI
DEFAULT_LLM_MODEL=anthropic/claude-3-5-sonnet-20241022

# Use Google Gemini
DEFAULT_LLM_MODEL=google/gemini-pro

# Use open source Llama
DEFAULT_LLM_MODEL=meta-llama/llama-3.1-70b-instruct
```

## Code Changes Summary

### Files Modified

1. **`requirements.txt`**: Updated dependencies
2. **`core/config/llm_config.py`**: NEW - OpenRouter configuration
3. **`core/start/starter_agent.py`**: Refactored to Pydantic AI
4. **`core/backend/service_reviewer/service_reviewer.py`**: Refactored to Pydantic AI
5. **`core/backend/sql_reviewer/sql_reviewer.py`**: Refactored to Pydantic AI
6. **`core/backend/test_reviewer/test_reviewer.py`**: Refactored to Pydantic AI
7. **`core/backend/pbook_reviewer/pbook_reviewer.py`**: Refactored to Pydantic AI
8. **`core/backend/dataview_reviewer/dataview_reviewer.py`**: Refactored to Pydantic AI
9. **`core/frontend/html_reviewer/html_reviewer.py`**: Refactored to Pydantic AI
10. **`core/frontend/scss_reviewer/scss_reviewer.py`**: Refactored to Pydantic AI
11. **`core/frontend/ts_reviewer/typescript_reviewer.py`**: Refactored to Pydantic AI
12. **`main.py`**: Updated agent invocations
13. **`agent.py`**: Updated workflow orchestration

### Files Created

1. **`core/config/llm_config.py`**: OpenRouter configuration and model management
2. **`.env.example`**: Environment variable template
3. **`MIGRATION.md`**: This file

## Testing

### Basic Test

Test the starter agent classification:

```bash
python -c "
import asyncio
from core.start.starter_agent import starter_agent

async def test():
    result = await starter_agent.run('''
file_path: test.sql
content: CREATE TABLE users (id BIGINT PRIMARY KEY);
diff: +CREATE TABLE users
merge_request_title: Add users table GME-123
target_branch: main
source_branch: feature/users
''')
    print(result.data)

asyncio.run(test())
"
```

### Full MR Review Test

```bash
python main.py --gitlab-mr "https://your-gitlab.com/group/project/-/merge_requests/123"
```

## Troubleshooting

### Error: "OPENROUTER_API_KEY not found"

**Solution:** Add `OPENROUTER_API_KEY` to your `.env` file.

```env
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

### Error: "Module 'pydantic_ai' not found"

**Solution:** Install dependencies:

```bash
pip install -r requirements.txt
```

### Error: "No attribute 'deps'"

**Solution:** Make sure you're accessing context through `run_context.deps` not `run_context.context`:

```python
# Correct
content = run_context.deps.content

# Incorrect (old way)
content = run_context.context.content
```

### Error: "No attribute 'final_output'"

**Solution:** Use `result.data` instead of `result.final_output`:

```python
# Correct
if result.data:
    issues = result.data.issues

# Incorrect (old way)
if result.final_output:
    issues = result.final_output.issues
```

### Model Not Working / Rate Limits

**Solution 1:** Try a different model:

```env
# Switch to Anthropic
DEFAULT_LLM_MODEL=anthropic/claude-3-5-sonnet-20241022
```

**Solution 2:** Check OpenRouter dashboard for credits and rate limits:
- Visit [https://openrouter.ai/activity](https://openrouter.ai/activity)

### Translation Still Using OpenAI

This is expected! The translation function in `service_reviewer.py` still uses OpenAI directly:

```python
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
```

You need both API keys:
- `OPENROUTER_API_KEY` for code reviews
- `OPENAI_API_KEY` for translations

## Benefits of the Refactoring

### 1. Cost Optimization

```python
# Use cheaper models for simple tasks
starter_agent = Agent(model=get_model_for_agent("gpt-4o-mini"))  # $0.15/1M tokens

# Use powerful models only when needed
business_logic_reviewer = Agent(model=get_model_for_agent("gpt-4o"))  # $5/1M tokens
```

### 2. Provider Diversity

```env
# Easy to switch providers for different reviewers
FAST_LLM_MODEL=openai/gpt-4o-mini
SMART_LLM_MODEL=anthropic/claude-3-5-sonnet-20241022
```

### 3. Better Testing

Pydantic AI agents are easier to mock and test:

```python
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

# Use test model for unit tests
test_model = TestModel()
agent = Agent(model=test_model, result_type=ReviewResponse)
```

### 4. Type Safety

All context classes are now Pydantic models with validation:

```python
class SqlReviewerContext(BaseModel):
    content: str  # Required, validated
    diff: str     # Required, validated
    file_name: str = ""  # Optional with default
```

## Next Steps

1. ✅ Set up `.env` with your API keys
2. ✅ Test with a single file review
3. ✅ Test with a full MR review
4. ⚠️ Monitor costs on OpenRouter dashboard
5. ⚠️ Adjust model selection based on performance/cost
6. ⚠️ Consider caching for repeated reviews

## Support

If you encounter issues:

1. Check this migration guide
2. Review Pydantic AI docs: [https://ai.pydantic.dev](https://ai.pydantic.dev)
3. Check OpenRouter docs: [https://openrouter.ai/docs](https://openrouter.ai/docs)
4. Review the code changes in git history

## Conclusion

The migration to Pydantic AI with OpenRouter provides:

- ✅ **Flexibility**: Use any LLM provider
- ✅ **Cost Control**: Switch models easily
- ✅ **Better Code**: Type-safe, testable, modern
- ✅ **OpenRouter Benefits**: Fallbacks, analytics, rate limiting

You're now ready to use the refactored code review agent! 🚀

