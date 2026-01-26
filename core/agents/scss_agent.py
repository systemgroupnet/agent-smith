"""
SCSS Reviewer Agent - Review SCSS/CSS files
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse


class ScssContext(BaseModel):
    """Context for SCSS review"""
    content: str
    diff: str
    file_name: str


def get_scss_reviewer_prompt(context: ScssContext) -> str:
    """Get SCSS reviewer system prompt."""
    return f"""You are an expert SCSS/CSS reviewer for the application frontend.

## Critical Review Areas

### 1. SCSS Best Practices
- Proper nesting (max 3 levels)
- Efficient use of variables
- Mixins and functions usage

### 2. Performance
- Efficient selectors
- No redundant rules
- Proper use of imports

### 3. Maintainability
- Clear naming conventions
- Modular structure
- No magic numbers

### 4. Responsiveness
- Mobile-first approach
- Proper media queries
- Flexible layouts

## Severity Levels
- **high**: Performance issues, broken layouts
- **medium**: Maintainability concerns, code duplication
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


scss_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o-mini",
    temperature=0.0
)


async def run_scss_review(context: ScssContext) -> ReviewResponse:
    """Run SCSS review."""
    prompt = get_scss_reviewer_prompt(context)
    return await scss_reviewer.run(prompt)

