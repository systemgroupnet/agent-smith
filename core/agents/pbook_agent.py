"""
Pbook Reviewer Agent - Review error message definition files (pbook)
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse


class PbookContext(BaseModel):
    """Context for pbook review"""
    content: str
    diff: str
    file_name: str


def get_pbook_reviewer_prompt(context: PbookContext) -> str:
    """Get pbook reviewer system prompt."""
    return f"""You are an expert reviewer for pbook (error message) files in the application.

## Critical Review Areas

### 1. Error Message Quality
- Clear, descriptive error messages
- Consistent formatting and tone
- Proper bilingual support (Persian/English)

### 2. Error Code Structure
- Unique error codes
- Consistent naming conventions
- Proper categorization

### 3. Translation Quality
- Accurate translations
- Cultural appropriateness
- Consistent terminology

### 4. Completeness
- All required error scenarios covered
- Proper error context included
- User-friendly messages

## Severity Levels
- **high**: Missing critical error messages, incorrect translations, duplicate error codes
- **medium**: Unclear messages, inconsistent formatting
- **low**: Minor wording improvements, style issues

File Name: {context.file_name}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


pbook_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o-mini",
    temperature=0.0
)


async def run_pbook_review(context: PbookContext) -> ReviewResponse:
    """Run pbook review."""
    prompt = get_pbook_reviewer_prompt(context)
    return await pbook_reviewer.run(prompt)

