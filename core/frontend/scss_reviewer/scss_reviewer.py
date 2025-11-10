from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning

from dto.general import ReviewResponse


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
        # 4thGen Frontend SCSS Code Review Ruleset

Guidelines for enforcing 4thGen Frontend design system conventions and preventing styling inconsistencies.

YOU SHOULD STRICTLY FOLLOW THESE RULES AND DO NOT SKIP ANY OF THEM. IF YOU FIND ANY VIOLATION RELATED TO THESE RULES, YOU MUST MARK IT AS **HIGH** SEVERITY.
SKIP ANY OTHER RULES AND ONLY FOLLOW THESE RULES. OTHER RULES ARE NOT OF **HIGH** SEVERITY AND SHOULD BE MARKED AS **MEDIUM** OR **LOW** SEVERITY.

## 1. Using CSS Values for Spacing

**Issue:** Developers are using hardcoded CSS values for spacing instead of the design system tokens.

**Rule:** Always use the `token()` SASS function to retrieve spacing values from the Fanoos design system. This ensures consistency and protects implementations from design changes.

**Bad Practice:**
```scss
.element {{
  margin: 16px;
  padding: 8px 12px;
}}
```

**Correct Practice:**

```scss
.element {{
  margin: token(spacing-md);
  padding: token(spacing-xs) token(spacing-sm);
}}
```

---

## 2. Using CSS Values for Border Radius

**Issue:** Developers are using hardcoded CSS values for border radius instead of design system tokens.

**Rule:** Always use the `token()` SASS function to retrieve radius values from the Fanoos design system. Radius values can change across themes, and using CSS values directly causes theming issues.

**Bad Practice:**

```scss
.card {{
  border-radius: 8px;
}}
```

**Correct Practice:**

```scss
.card {{
  border-radius: token(radius-md);
}}
```

---

## 3. Using PX, VW, VH Values for Height and Width

**Issue:** Developers are using pixel, viewport width (vw), or viewport height (vh) units for sizing elements.

**Rule:** Always use `rem` units for height and width to support the 4thGen accessibility scaling system. Use the `px2rem()` SASS function for easy conversion and better readability.

**Background:** The 4thGen scaling system works by changing the `:root` font-size. Elements must use `rem` units to respond to these changes.

**Bad Practice:**

```scss
.container {{
  width: 320px;
  height: 100vh;
  max-width: 1200px;
}}
```

**Correct Practice:**

```scss
.container {{
  width: px2rem(320px);
  height: 100vh; // Note: viewport units may be acceptable in specific cases
  max-width: px2rem(1200px);
}}
```

---

## 4. Using Design System Tokens Directly as CSS Variables

**Issue:** Developers are using Fanoos CSS variables directly with hardcoded prefixes (e.g., `var(--f-T14)`).

**Rule:** Always use the `token()` function instead of accessing CSS variables directly. The prefix is configurable in the design system, and hardcoding it creates potential bugs when the Core team changes the prefix.

**Bad Practice:**

```scss
.text {{
  font-size: var(--f-T14);
  color: var(--f-color-primary);
}}  
```

**Correct Practice:**

```scss
.text {{
  font-size: token(T14);
  color: token(color-primary);
}}
```

---

## 5. Setting Numeric Values for Font Weight

**Issue:** Developers are using numeric values (e.g., 100, 400, 700) for font weights.

**Rule:** Always use design system tokens for font weights. Font weights are predetermined in the Fanoos design system and must be accessed through tokens.

**Bad Practice:**

```scss
.heading {{
  font-weight: 600;
}}

.body {{
  font-weight: 400;
}}
```

**Correct Practice:**

```scss
.heading {{
  font-weight: token(font-weight-semi-bold);
}}

.body {{
  font-weight: token(font-weight-regular);
}}
```

--- 


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

```scss
{content}
```

git diff: 

```
{diff}
```
    """

scss_reviewer = Agent(
    name="scss reviewer",
    instructions=scss_reviewer_instructions,
    model="gpt-5",
    output_type=ReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="medium"
        )
    )
)

