"""
Dataview Reviewer Agent - Review dataview registration files
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from core.agents.service_agents import ServiceContext, fetch_context_for_service
from dto.general import ReviewResponse


# Dataview uses ServiceContext since it needs external data like service files
DataviewContext = ServiceContext


def get_dataview_reviewer_prompt(context: DataviewContext) -> str:
    """Get dataview reviewer system prompt."""
    return f"""You are an expert reviewer for dataview files in the application.

## Critical Review Areas

### 1. Dataview Definition
- Proper dataview registration
- Correct field mappings
- Valid data sources

### 2. Query Optimization
- Efficient data retrieval
- Proper joins and filters
- No N+1 query issues

### 3. Data Transformation
- Correct data formatting
- Proper type conversions
- Consistent output structure

### 4. Business Logic Alignment
- Matches business requirements
- Proper access control
- Correct data aggregation

## Severity Levels
- **high**: Incorrect data mapping, security issues, broken queries
- **medium**: Suboptimal queries, missing validations
- **low**: Style issues, naming improvements

**CRITICAL**: For high severity issues, explain the impact.

Task Description:
{context.task_description if context.task_description else "No task description available."}

Related Documents:
{context.related_documents if context.related_documents else "No related documents available."}

File Name: {context.file_name}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


dataview_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o",
    temperature=0.0
)


async def run_dataview_review(context: DataviewContext) -> ReviewResponse:
    """Run dataview review."""
    # Fetch context like service reviewer
    context = await fetch_context_for_service(context)
    prompt = get_dataview_reviewer_prompt(context)
    return await dataview_reviewer.run(prompt)

