"""
HTML Reviewer Agent - Review HTML template files
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse


class HtmlContext(BaseModel):
    """Context for HTML review"""
    content: str
    diff: str
    file_name: str


def get_html_reviewer_prompt(context: HtmlContext) -> str:
    """Get HTML reviewer system prompt."""
    return f"""You are an expert HTML reviewer for the application frontend.

## Critical Review Areas

### 1. Semantic HTML
- Proper use of semantic tags
- Correct structure and nesting
- Accessibility attributes

### 2. Accessibility
- ARIA labels where needed
- Keyboard navigation support
- Screen reader compatibility

### 3. Best Practices
- No inline styles
- Proper form structure
- Valid HTML5

### 4. Angular Templates
- Correct directive usage
- Proper data binding
- Event handler patterns

## Severity Levels
- **high**: Accessibility violations, broken functionality
- **medium**: Semantic issues, missing best practices
- **low**: Style improvements, minor optimizations

File Name: {context.file_name}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


html_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o-mini",
    temperature=0.0
)


async def run_html_review(context: HtmlContext) -> ReviewResponse:
    """Run HTML review."""
    prompt = get_html_reviewer_prompt(context)
    return await html_reviewer.run(prompt)

