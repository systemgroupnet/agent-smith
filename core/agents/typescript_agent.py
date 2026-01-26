"""
TypeScript Reviewer Agent - Review TypeScript files
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse


class TypescriptContext(BaseModel):
    """Context for TypeScript review"""
    content: str
    diff: str
    file_name: str


def get_typescript_reviewer_prompt(context: TypescriptContext) -> str:
    """Get TypeScript reviewer system prompt."""
    return f"""You are an expert TypeScript reviewer for the application frontend.

## Critical Review Areas

### 1. Type Safety
- Proper type annotations
- No use of 'any' without justification
- Correct interface/type definitions

### 2. Code Quality
- Proper error handling
- Clean code structure
- No code duplication

### 3. Best Practices
- Modern TypeScript patterns
- Proper async/await usage
- Correct null/undefined handling

### 4. Angular/Framework Specific
- Proper component structure
- Correct service usage
- Proper RxJS patterns

## Severity Levels
- **high**: Type errors, runtime bugs, security issues
- **medium**: Code quality issues, suboptimal patterns
- **low**: Style improvements, naming suggestions

File Name: {context.file_name}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


typescript_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o-mini",
    temperature=0.0
)


async def run_typescript_review(context: TypescriptContext) -> ReviewResponse:
    """Run TypeScript review."""
    prompt = get_typescript_reviewer_prompt(context)
    return await typescript_reviewer.run(prompt)

