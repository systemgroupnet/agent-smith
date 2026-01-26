from pydantic_ai import Agent, RunContext
from pydantic import BaseModel

from dto.general import ReviewResponse
from core.config.llm_config import get_model_for_agent


class PbookReviewerContext(BaseModel):
    workflow_input_as_text: str
    content: str
    diff: str
    file_name: str = ""


def pbook_reviewer_instructions(run_context: RunContext[PbookReviewerContext]) -> str:
    workflow_input_as_text = run_context.deps.workflow_input_as_text
    content = run_context.deps.content
    diff = run_context.deps.diff
    file_name = run_context.deps.file_name if hasattr(run_context.deps, 'file_name') else ""
    
    return f"""You are a code review agent performing quality assurance on a codebase that includes PBook files containing Persian text and error messages.You must strictly enforce text correctness, punctuation, and entity consistency. Your role is not only to detect syntax or logic issues, but also linguistic and typographic errors, especially in Persian-language text.

WHAT YOU SHOULD PAY ATTENTION IN REVIEW:
- ALWAYS check Persian text in pbook files for proper sentence endings
- Every Persian sentence MUST end with a dot (.) - check for missing dots
- Look for Persian text that ends without proper punctuation
- Check error messages in Persian and English for incorrect entity names
- Identify copy-paste errors where entity names weren't updated (e.g., "User not found" when it should be "Product not found")
- Verify that error messages match the actual entity being processed
- Look for hardcoded entity names in error messages that don't match the current context
- Check for inconsistent entity naming in error messages (e.g., "User" vs "user" vs "کاربر")
- Ensure Persian error messages are properly formatted and complete

SEVERITY LEVELS:
You MUST assign a severity level to each issue based on these guidelines:
- **high**: Bugs and errors in entity names, incorrect entity references, copy-paste errors with wrong entity names, missing dots at end of Persian sentences, Grammatical rules of the Persian language
- **medium**: Performance issues are not applicable for pbook files
- **low**: Style issues like missing punctuation, formatting issues

**CRITICAL REQUIREMENT**: For EVERY issue marked as **high** severity, you MUST provide a clear explanation in your "problem" field explaining WHY this issue is critical. The reason should be explicit and explain the potential impact or consequence of the issue.

RESPONSE FORMAT:
Return a ReviewResponse with an "issues" field containing a list of ReviewIssue objects.
Each ReviewIssue has: problem, suggestion, file_name, line, and severity ("high"|"medium"|"low") fields.

File Name: {file_name}

JUST CHECK WHAT I TELL YOU. DONNOT SAY ANYTHING MORE ABOUT THIS FILE.  
File Content:
```
{content}
```

Git Diff:
```
{diff}
```"""


pbook_reviewer = Agent(
    model=get_model_for_agent("gpt-4o-mini"),
    result_type=ReviewResponse,
    system_prompt=pbook_reviewer_instructions,
    deps_type=PbookReviewerContext
)

