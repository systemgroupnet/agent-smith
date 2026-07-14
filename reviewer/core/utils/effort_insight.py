"""
Review-Effort Insight Builder (pure, dependency-free)

Computes compact, diff-free metadata about an MR's changes for the review-effort
estimator: per-file change type and line counts, the language mix, the module
spread, and totals. Kept free of any LLM/langchain imports so it is cheap to unit
test and reuse. The estimator agent (``core/agents/effort_agent.py``) feeds this
to the model; the file `path`s themselves carry the tree/relations, so no separate
tree structure is built here.
"""

import os
from typing import Dict, List, Tuple

# Cap the per-file list so a huge MR can't blow up the prompt; the model still
# gets the true totals and keeps the largest-changed files (which dominate effort).
MAX_FILES_LISTED = 250


def count_diff_lines(diff: str) -> Tuple[int, int]:
    """Count added/removed lines in a unified diff (excluding the +++/--- headers)."""
    added = removed = 0
    for line in (diff or "").split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def change_type(change: dict) -> str:
    if change.get("new_file"):
        return "added"
    if change.get("deleted_file"):
        return "deleted"
    if change.get("renamed_file"):
        return "renamed"
    return "modified"


def build_effort_insight(changes: List[dict]) -> Dict:
    """Build compact, diff-free metadata about the MR for the estimator."""
    files: List[Dict] = []
    languages: Dict[str, int] = {}
    modules: Dict[str, int] = {}
    total_added = total_removed = 0

    for change in changes or []:
        path = change.get("new_path") or change.get("old_path") or ""
        added, removed = count_diff_lines(change.get("diff", ""))
        total_added += added
        total_removed += removed

        ext = os.path.splitext(path)[1] or "(none)"
        languages[ext] = languages.get(ext, 0) + 1
        top = path.split("/")[0] if "/" in path else "(root)"
        modules[top] = modules.get(top, 0) + 1

        files.append({
            "path": path,
            "change": change_type(change),
            "added": added,
            "removed": removed,
        })

    # If we must truncate, keep the biggest changes — they drive review effort.
    files_sorted = sorted(files, key=lambda f: f["added"] + f["removed"], reverse=True)
    listed = files_sorted[:MAX_FILES_LISTED]

    return {
        "total_files": len(files),
        "total_lines_added": total_added,
        "total_lines_removed": total_removed,
        "languages": dict(sorted(languages.items(), key=lambda kv: kv[1], reverse=True)),
        "top_level_modules": dict(sorted(modules.items(), key=lambda kv: kv[1], reverse=True)),
        "files_listed": len(listed),
        "files_truncated": max(0, len(files) - len(listed)),
        "files": listed,
    }
