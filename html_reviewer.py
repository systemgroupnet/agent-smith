from agents import Agent, ModelSettings, RunContextWrapper
from openai.types.shared.reasoning import Reasoning
from pydantic import BaseModel


class ReviewIssue(BaseModel):
    problem: str
    suggestion: str
    file_name: str
    line: int
    severity: str  # "high", "medium", "low"


class HtmlReviewResponse(BaseModel):
    issues: list[ReviewIssue]


class HtmlReviewerContext:
    def __init__(self, workflow_input_as_text: str, content: str, diff: str, file_name: str = ""):
        self.workflow_input_as_text = workflow_input_as_text
        self.content = content
        self.diff = diff
        self.file_name = file_name


def html_reviewer_instructions(run_context: RunContextWrapper[HtmlReviewerContext], _agent: Agent[HtmlReviewerContext]):
    workflow_input_as_text = run_context.context.workflow_input_as_text
    content = run_context.context.content
    diff = run_context.context.diff
    file_name = run_context.context.file_name if hasattr(run_context.context, 'file_name') else ""
    
    return f"""
    # 4thGen Frontend HTML Code Review Ruleset

This document contains HTML and template-related code review rules for the 4thGen Frontend project.

## 1. Translation

**Rule**: All words must be translated in Frontend templates.

**Reason**: The 4thGen system has multi-cultural features and requires full internationalization support.

**Bad Practice**:
```html
<f-button>Submit</f-button>
```

**Good Practice**:
```html
<f-button>{{{{ 'SUBMIT_BUTTON' | translate }}}}</f-button>
```

---

## 2. Unnecessary f-flex-item

**Rule**: Do not use `<f-flex-item>` tag without any attributes.

**Reason**: The `<f-flex-item>` tag provides features for flex children such as `basis` through attributes. Using it without any attributes provides no effect, and since it's a web component built with Stencil, it has multiple lifecycles that cost JS runtime and unnecessary performance overhead.

**Bad Practice**:
```html
<f-flex>
  <f-flex-item>
    <div>Content</div>
  </f-flex-item>
</f-flex>
```

**Good Practice**:
```html
<f-flex>
  <div>Content</div>
</f-flex>
```

Or with attributes:
```html
<f-flex>
  <f-flex-item basis="50%">
    <div>Content</div>
  </f-flex-item>
</f-flex>
```

---

## 3. Unnecessary f-grid with Single Column

**Rule**: Do not use `<f-grid columns="1">`.

**Reason**: Grid is a highly costly row and column layout system. A single-column grid has no difference from a simpler flex layout with column direction, but costs significantly more in terms of performance.

**Bad Practice**:
```html
<f-grid columns="1">
  <div>Item 1</div>
  <div>Item 2</div>
</f-grid>
```

**Good Practice**:
```html
<f-flex direction="column">
  <div>Item 1</div>
  <div>Item 2</div>
</f-flex>
```

---

## 4. Unnecessary f-fieldset with Single Column

**Rule**: Do not use `<f-fieldset columns="1">`.

**Reason**: Fieldset is a grid layout which is a highly costly row and column layout system. A single-column grid system has no difference from a simpler flex layout with column direction.

**Bad Practice**:
```html
<f-fieldset columns="1">
  <f-input></f-input>
  <f-input></f-input>
</f-fieldset>
```

**Good Practice**:
```html
<f-flex direction="column">
  <f-input></f-input>
  <f-input></f-input>
</f-flex>
```

---

## 5. Unnecessary f-grid-item

**Rule**: Do not use `<f-grid-item>` tag without any attributes.

**Reason**: The `<f-grid-item>` tag provides features for grid children such as `col-span` through attributes. Using it without any attributes provides no effect, and since it's a web component built with Stencil, it has multiple lifecycles that cost JS runtime and unnecessary performance overhead.

**Bad Practice**:
```html
<f-grid columns="3">
  <f-grid-item>
    <div>Content</div>
  </f-grid-item>
</f-grid>
```

**Good Practice**:
```html
<f-grid columns="3">
  <div>Content</div>
</f-grid>
```

Or with attributes:
```html
<f-grid columns="3">
  <f-grid-item col-span="2">
    <div>Content</div>
  </f-grid-item>
</f-grid>
```

---

## 6. Unnecessary ng-container

**Rule**: Do not use `<ng-container>` tag without conditions or templateOutlet.

**Reason**: Using `ng-container` without structural directives or template references has no effect and only creates unnecessary code.

**Bad Practice**:
```html
<ng-container>
  <div>Content</div>
</ng-container>
```

**Good Practice**:
```html
<div>Content</div>
```

Or with proper usage:
```html
<ng-container *ngIf="condition">
  <div>Content</div>
</ng-container>
```

---

## 7. Using *ngFor and *ngIf (Deprecated Syntax)

**Rule**: Use Angular's new control flow syntax instead of `*ngFor` and `*ngIf`.

**Reason**: Angular introduced new control flow from Angular 17, and it's recommended to use the new syntax for better performance.

**Bad Practice**:
```html
<div *ngIf="isVisible">Content</div>
<div *ngFor="let item of items">{{{{ item }}}}</div>
```

**Good Practice**:
```html
@if (isVisible) {{{{
  <div>Content</div>
}}}}

@for (item of items; track item.id) {{{{
  <div>{{{{ item }}}}</div>
}}}}
```

---

## 8. Using [ngClass] Directive without Any Conditions

**Rule**: Do not use `[ngClass]` directive for static class assignment.

**Reason**: The NgClass directive has its own logic that needs to run. Using `[ngClass]="['col-12','col-sm']"` has no difference from using the static `class` attribute except that the directive costs unnecessary extra performance for JS logic execution.

**Bad Practice**:
```html
<div [ngClass]="['col-12', 'col-sm-6']">Content</div>
```

**Good Practice**:
```html
<div class="col-12 col-sm-6">Content</div>
```

Or use it with conditions:
```html
<div [ngClass]="{{'active': isActive, 'disabled': isDisabled}}">Content</div>
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


class HtmlReviewResponse(BaseModel):
    issues: list[ReviewIssue]

```

- List ALL violations, not just some of them
- Be thorough and systematic
- Double-check your line counting
- Provide the complete corrected file code

> NOTICE: Do not say anything, only give me the JSON format result

RESPONSE FORMAT:
Return a HtmlReviewResponse with an "issues" field containing a list of ReviewIssue objects.

Each ReviewIssue has: problem, suggestion, file_name, line, and severity ("high"|"medium"|"low") fields.


--- 

The file content and the git diff will be provided below: 

file content: 

```html
{content}
```

git diff: 

```
{diff}
```
    """


html_reviewer = Agent(
    name="html reviewer",
    instructions=html_reviewer_instructions,
    model="gpt-5",
    output_type=HtmlReviewResponse,
    model_settings=ModelSettings(
        store=True,
        reasoning=Reasoning(
            effort="medium"
        )
    )
)

