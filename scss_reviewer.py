from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class ScssReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class ScssReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, file_name: str = ""):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.file_name = file_name


def scss_reviewer_instructions(run_context: RunContextWrapper[ScssReviewerContext], _agent: Agent[ScssReviewerContext]):
    workflow_input_as_text = run_context.context.workflow_input_as_text
    content = run_context.context.content
    diff = run_context.context.diff
    file_name = run_context.context.file_name if hasattr(run_context.context, 'file_name') else ""
    
    return f"""
    You are Expert Frontend developer at Javascript, Typescript, Angular, HTML and CSS. You act as code review assistant.
Your task is to read principles below and review the given codes and give result in the format explained. Do your task in steps below:
## Step 1: Language Detection
Detect the language that code has been written with.
## Step 2: Learn the Principles
Read the principles below based on language:
### \"CSS\" codes principles:
1. **px2rem Method**: code must use sass function called \"px2rem\" instead of using 'px' unit directly.
   - ✅ VALID: `.btn{ height: px2rem(20px);}` (uses px2rem for height instead of using 20px directly)
   - ❌ INVALID: `.btn{ height: 20px;}` (using px unit directly)
## Step 3: Thorough Code Review
Read the ENTIRE CSS/SCSS file below and Review it based on principles above. Find **ALL lines of code** that violate ANY of the principles above.

{content}

diff:

{diff}

**CRITICAL**: 
- Examine EVERY line carefully
- Check EVERY property that uses 'px' unit
- Look for ALL instances across the entire file
- Do not skip any violations
## Step 4: Results Format
Give me the result of your review in format of JSON which has two keys:
### Key 1: \"comments\"
An array of objects with keys defined below:
- **\"comment\"**: description about what is wrong and which principle it violates
- **\"lineNumber\"**: the exact line number of the code that violates the principle
- **\"correction\"**: correct ONLY the specific line that violates the principle
### Key 2: \"fullCorrectedCode\"
Provide the COMPLETE corrected version of the ENTIRE file with ALL violations fixed.
**Requirements for results**:
- List ALL violations, not just some of them
- Be thorough and systematic
- Double-check your line counting
- Provide the complete corrected file code
# Notice: Do not say anything, only give me the JSON format result

RESPONSE FORMAT:
Return a ScssReviewResponse with an "issues" field containing a list of ReviewIssue objects.
Each ReviewIssue has: problem, suggestion, file_name, line, and severity ("high"|"medium"|"low") fields.
"""


scss_reviewer = Agent(
    name="scss reviewer",
    instructions=scss_reviewer_instructions,
    model="gpt-5-nano",
    output_type=ScssReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="low"
        )
    )
)

