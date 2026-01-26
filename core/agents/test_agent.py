"""
Test Reviewer Agent - Review Go test files with Gherkin BDD steps
"""

from pydantic import BaseModel
from core.agents.base_agent import create_review_agent
from dto.general import ReviewResponse


class TestContext(BaseModel):
    """Context for test review"""
    content: str
    diff: str
    file_name: str


def get_test_reviewer_prompt(context: TestContext) -> str:
    """Get test reviewer system prompt."""
    return f"""You are an expert test reviewer for the application. Review Go test files with Gherkin BDD step implementations.

## Critical Review Areas

### 1. Test Structure
- Proper step definition format
- Complete scenario coverage
- Clear step descriptions

### 2. Test Implementation
- Correct use of godog/BDD framework
- Proper context handling in steps
- Error handling in test steps

### 3. Test Data
- Proper test data setup and cleanup
- No hardcoded values that should be parameterized
- Realistic test scenarios

### 4. Assertions
- Comprehensive assertions
- Clear error messages
- Edge case coverage

### 5. Code Quality
- No code duplication in steps
- Proper helper functions
- Clean test organization

## Severity Levels
- **high**: Missing test coverage for critical features, incorrect assertions, broken test logic
- **medium**: Suboptimal test structure, missing edge cases
- **low**: Style issues, naming improvements

**CRITICAL**: For high severity issues, explain the impact on test reliability.

File Name: {context.file_name}

File Content:
```
{context.content}
```

Git Diff:
```
{context.diff}
```"""


test_reviewer = create_review_agent(
    system_prompt="",
    model_name="gpt-4o",
    temperature=0.0
)


async def run_test_review(context: TestContext) -> ReviewResponse:
    """Run test review."""
    prompt = get_test_reviewer_prompt(context)
    return await test_reviewer.run(prompt)

