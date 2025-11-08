from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel

class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class TypescriptReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class TypescriptReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, file_name: str = ""):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.file_name = file_name


def typescript_reviewer_instructions(run_context: RunContextWrapper[TypescriptReviewerContext], _agent: Agent[TypescriptReviewerContext]):
    workflow_input_as_text = run_context.context.workflow_input_as_text
    content = run_context.context.content
    diff = run_context.context.diff
    file_name = run_context.context.file_name if hasattr(run_context.context, 'file_name') else ""
    
    return f"""
    # TypeScript Code Review Ruleset

YOU SHOULD STRICTLY FOLLOW THESE RULES AND DO NOT SKIP ANY OF THEM. IF YOU FIND ANY VIOLATION RELATED TO THESE RULES, YOU MUST MARK IT AS **HIGH** SEVERITY.
SKIP ANY OTHER RULES AND ONLY FOLLOW THESE RULES. OTHER RULES ARE NOT OF **HIGH** SEVERITY AND SHOULD BE MARKED AS **MEDIUM** OR **LOW** SEVERITY.

## 1. ANY Type Usage
**Rule:** Avoid using the `any` type as it can cause many issues and is not type-safe.

**Recommendation:** In specific situations where type is unknown at compile time, use the `unknown` type instead of `any`.

**Rationale:** The `any` type bypasses TypeScript's type checking system, defeating the purpose of using TypeScript and potentially introducing runtime errors.

---

## 2. ID Type Definition
**Rule:** Always define IDs as `string` type, never as `number`.

**Explanation:** In the 4thGen product, IDs are defined as `int-64` in the Go Lang backend, which can store values up to 2<sup>63</sup>. However, JavaScript uses IEEE 754 floating point numbers which can only safely store values up to 2<sup>53</sup>.

**Problem:** Values exceeding 2<sup>53</sup> cause precision loss or infinity values. For example, JavaScript interprets `9223372036854776000 === 9223372036854776001` as `true` due to precision loss.

**Solution:** Handle large numbers as strings in the frontend to prevent precision loss.

---

## 3. Import Path Standards
**Rule:** Always import Venus packages using the defined paths in `tsconfig` files.

**Bad Practice:**
```typescript
import {{{{ Model }}}} from '../../../../../../../../../../venus/projects/ui/job/src/lib/job.model';
```

**Good Practice:**
```typescript
import {{{{ Model }}}} from '@venus/ui/job';
```

**Rationale:** Using relative paths breaks when the Core team changes file locations. Using configured paths from `tsconfig` ensures imports remain valid.

---

## 4. Variable Declaration with `var`
**Rule:** Never use the `var` keyword for variable declaration.

**Recommendation:** Use `const` or `let` instead.

**Rationale:** The `var` keyword is function-scoped (not block-scoped), which can lead to tricky bugs and unexpected behavior. It's an outdated syntax that should be avoided in modern TypeScript/JavaScript development.

---

## 5. Component Lifecycle Organization
**Rule:** Do not write complex logic directly inside lifecycle methods.

**Bad Practice:**
```typescript
connectedCallback() {{
  // 50 lines of complex logic here...
}}
```

**Good Practice:**
```typescript
connectedCallback() {{
  this.initializeComponent();
  this.loadUserData();
  this.setupEventListeners();
}}
```

**Rationale:** Lifecycle methods are entry points for reading component code. They should be self-documenting by calling well-named functions that follow the Single Responsibility Principle. This makes the code more readable and maintainable.

---

## 6. Translation Requirements
**Rule:** All user-facing strings must be translated, never hardcoded.

**Rationale:** The 4thGen system has multi-cultural features, and hardcoded strings prevent proper internationalization and localization.

**Implementation:** Use the appropriate translation service/function provided by the framework for all displayed text.
```

--

**CRITICAL**: 

- Examine EVERY line carefully
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

```py
class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class TypescriptReviewResponse(BaseModel):
    issues: list[ReviewIssue]
```

- List ALL violations, not just some of them
- Be thorough and systematic
- Double-check your line counting
- Provide the complete corrected file code

> NOTICE: Do not say anything, only give me the JSON format result

RESPONSE FORMAT:
Return a TypescriptReviewResponse with an "issues" field containing a list of ReviewIssue objects.

Each ReviewIssue has: problem, suggestion, file_name, line, and severity ("high"|"medium"|"low") fields.

--- 

The file content and the git diff will be provided below: 

file content: 

```ts
{content}
```

git diff: 

```
{diff}
```
    """ 

typescript_reviewer = Agent(
    name="typescript reviewer",
    instructions=typescript_reviewer_instructions,
    model="gpt-5",
    output_type=TypescriptReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="medium"
        )
    )
)

