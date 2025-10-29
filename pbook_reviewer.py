from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class PbookReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class PbookReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, file_name: str = ""):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.file_name = file_name


def pbook_reviewer_instructions(run_context: RunContextWrapper[PbookReviewerContext], _agent: Agent[PbookReviewerContext]):
    workflow_input_as_text = run_context.context.workflow_input_as_text
    content = run_context.context.content
    diff = run_context.context.diff
    file_name = run_context.context.file_name if hasattr(run_context.context, 'file_name') else ""
    
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
Return a PbookReviewResponse with an "issues" field containing a list of ReviewIssue objects.
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
    name="pbook reviewer",
    instructions=pbook_reviewer_instructions,
    model="gpt-5-nano",
    output_type=PbookReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="low"
        )
    )
)

