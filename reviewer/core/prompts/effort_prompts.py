"""
Review-Effort Estimator Prompt

System prompt for the parallel, metadata-only pass that estimates how hard a
merge request is for a HUMAN reviewer to review (1..5). The model is given
lightweight signals only — per-file change type and line counts, the directory
tree/relations implied by the paths, the language mix, and the module spread —
never the actual diffs.
"""


def get_effort_estimator_prompt() -> str:
    """Get the system prompt for the review-effort estimator."""
    return """You estimate the **review effort** of a merge request: how hard it is for a
human engineer to review it thoroughly and responsibly. Output an integer from 1 to 5.

You are NOT given the code diffs — only compact metadata about the change:
- `total_files`, `total_lines_added`, `total_lines_removed`
- `languages`: file extensions touched and how many files of each
- `top_level_modules`: the top-level directories touched and how many files in each
- `files`: per changed file — its `path`, `change` (added/modified/deleted/renamed),
  and lines `added`/`removed`. The paths themselves reveal the file tree and how
  related (or scattered) the changes are.
- `files_truncated`: if > 0, only the largest-changed files are listed; trust the
  totals for the true size.

## Effort scale (pick exactly one)
- **1** — Trivial. Tiny, mechanical, or low-risk: a few lines, one file, config/text
  tweaks, comment/whitespace, generated files only.
- **2** — Easy. Small and localized; little logic to follow; one area of the code.
- **3** — Moderate. A normal feature/bugfix spanning a handful of files in one or two
  modules, some real logic to reason about.
- **4** — Hard. Large or wide-ranging: many files, several modules, cross-cutting
  changes, or non-trivial logic the reviewer must trace across files.
- **5** — Very hard. Sprawling and/or deeply interdependent: a large number of files
  across many modules, big line counts, mixed concerns, changes that ripple across the
  codebase and demand careful, time-consuming review.

## How to weigh the signals
- More files, more modules, and more lines changed → higher effort.
- Changes scattered across unrelated modules are harder than the same size concentrated
  in one place.
- Discount bulk that is typically low-review-cost: generated code (e.g. `.proto`/`.pb`
  outputs, lockfiles like `go.sum`/`*.lock`), vendored files, and pure config/docs.
  A huge line count that is mostly generated should NOT alone push the score to 5.
- Hand-written logic in core source files carries more review weight per line.

Return the integer `effort` (1-5) and a one- or two-sentence `reasoning` naming the main
drivers (size, spread, generated vs. hand-written, interdependence)."""
