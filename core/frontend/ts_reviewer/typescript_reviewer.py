from pydantic_ai import Agent, RunContext
from pydantic import BaseModel

from dto.general import ReviewResponse
from core.config.llm_config import get_model_for_agent


class TypescriptReviewerContext(BaseModel):
    workflow_input_as_text: str
    content: str
    diff: str
    file_name: str = ""


def typescript_reviewer_instructions(run_context: RunContext[TypescriptReviewerContext]) -> str:
    workflow_input_as_text = run_context.deps.workflow_input_as_text
    content = run_context.deps.content
    diff = run_context.deps.diff
    file_name = run_context.deps.file_name if hasattr(run_context.deps, 'file_name') else ""
    
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


class ReviewResponse(BaseModel):
    issues: list[ReviewIssue]
```

- List ALL violations, not just some of them
- Be thorough and systematic
- Double-check your line counting
- Provide the complete corrected file code

> NOTICE: Do not say anything, only give me the JSON format result

RESPONSE FORMAT:
Return a ReviewResponse with an "issues" field containing a list of ReviewIssue objects.

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
    model=get_model_for_agent("gpt-4o"),
    result_type=ReviewResponse,
    system_prompt=typescript_reviewer_instructions,
    deps_type=TypescriptReviewerContext
)

