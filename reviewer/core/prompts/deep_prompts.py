"""The addendum every reviewer prompt carries, because the code lives on disk.

The nine reviewer prompts describe what to look for and are unchanged. This
describes *where the code is*, which changed when the reviewers got a checkout.

Reviewer prompts were written for a world where the file arrived inlined —
`format_file_content_section` rendered it with an ``NN|`` gutter, because GitLab's
raw file endpoint has no line numbers and a miscounted `line` is silently dropped
by the in-diff gate. The file is not inlined any more: there is a checkout and the
reviewer reads it, which is what lets `grep` answer about the merge request's own
branch rather than whatever ref someone remembered to name. That moves two
responsibilities into the prompt:

* **Line numbers.** deepagents' ``read_file`` numbers its output as
  ``<n>  <text>`` — a different separator from the old ``NN|`` gutter but the
  same 1-based source-branch numbering, and the same thing the gate resolves
  against. The addendum says so explicitly, because a model that assumes the
  numbers are decoration will report arithmetic instead of lookups.
* **`.aid` instructions.** These were fetched over the GitLab REST API
  (`core/utils/md_loader.py`, now deleted) because there was no checkout. Now there
  is one, and a reviewer prompt is built once per *run* — it cannot bake in
  per-module guidance for a merge request touching several modules. So the reviewer
  reads its own `.aid` files, which makes the guidance per-module again and removes
  ~290 lines of REST plumbing, a per-run cache and an API failure mode. The one
  thing the API path did that a plain two-tier read does not is detect a
  *centralized* layout — a repo whose only `.aid` lives in one module dir rather
  than at the root — so the prompt now asks for a single `glob` fallback, which the
  reviewer can answer itself.
"""

from typing import List, Optional, Sequence, Tuple


def format_checkout_addendum(
    clone_root: str,
    agent_type: str,
    source_branch: Optional[str] = None,
) -> str:
    """The block appended to every reviewer's system prompt.

    Args:
        clone_root: Absolute path of the checkout the filesystem tools are rooted at.
        agent_type: Which ``.aid`` file this reviewer should look for.
        source_branch: The MR's source branch, for the semantic-index guidance.
    """
    branch_note = (
        f'The checkout is the branch `{source_branch}` at the exact commit under review.'
        if source_branch
        else "The checkout is the merge request's source branch at the commit under review."
    )
    return f"""

# HOW YOU GET THE CODE

The file you are reviewing is **not** included in this prompt. A checkout of the
repository is on disk and you have read-only tools over it:

- `read_file(path)` — the file's contents. Output is numbered `<n>  <text>`. **Those
  numbers are the line numbers you must report.** They are 1-based and they are the
  source-branch numbering the merge request anchors comments to, so an issue's `line`
  is a lookup, never a count. Never include the number or its spacing in a `suggestion`.
- `grep(pattern, path)` — exact text/regex search across the checkout. Use it to find
  where a symbol is defined, who calls a function, whether a pattern is used elsewhere.
- `glob(pattern)` / `ls(path)` — find files by name or list a directory.
- `write_todos(...)` — your own plan, when reviewing this file takes several steps.

You will also see `write_file`, `edit_file`, `delete` and `execute` in your tools.
**They do not work here**: writes are refused at the filesystem and there is no
shell. You are reviewing code, not changing it — do not spend a turn trying.

{branch_note} Everything you read is therefore the post-merge-request state of the
code — a symbol this merge request added is present, and a signature it changed is
already changed. You are not looking at the base branch.

Your instruction names ONE file. Review that file.

## BEFORE YOU REVIEW: project instructions

Look for extra review instructions written by this project and follow them. They
override your general judgement where they conflict:

1. `read_file(".aid/{agent_type}.md")` — repository-wide instructions.
2. `read_file("<first-path-segment-of-the-file>/.aid/{agent_type}.md")` — module
   instructions, which win over the repository-wide ones.
3. If **neither** exists, this repository may keep one shared `.aid` in a single
   module instead of at the root. `glob("**/.aid/{agent_type}.md")` once; if that
   finds exactly one file, read it and follow it.

A missing file is normal and not an error — move on after one look.

## USE THE TOOLS BEFORE CLAIMING SOMETHING IS MISSING

You can now check. Before writing that a function, method, type, field or constant
is undefined, unimplemented or unused, `grep` for it. This is the single most common
false positive this reviewer produces, and it exists only because reviewers used to
see one file at a time. You do not.

## HOW TO REPORT (this is the only channel that works)

Call `record_findings` exactly once, as the last thing you do, with the file path you
were given and every issue you found. **Findings you describe in prose instead of
calling the tool are discarded and never reach the merge request.** When the file is
clean, call it with `findings=[]` — that is a normal result and it records the file as
reviewed. Do not call it more than once for the same file.
"""
