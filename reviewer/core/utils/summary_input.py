"""Input payload for the review-summary agent.

Unlike ``effort_insight`` — which is deliberately metadata-only, because rating
*difficulty* needs shape rather than content — a summary has to say what the MR
actually **does**, and that is only knowable from the diffs. So this builds a
text block carrying the MR's identity (title, description, Jira ticket), the
*full* changed-file list, and then as many real diff hunks as fit in a character
budget.

The budget exists because MRs in this codebase reach 250+ changed files; an
unbounded diff would blow the context window and burn a very large request on a
provider we are already being rate limited by. Files are spent largest-first (by
lines touched), on the assumption that the biggest changes carry the feature and
a one-line import tweak does not. Whatever is dropped is stated in the payload,
so the model reports on a partial view knowingly instead of confidently
summarising an MR it only half saw.
"""

from typing import Dict, List, Optional

from core.utils.effort_insight import change_type, count_diff_lines

# ~60k chars ≈ 15-20k tokens of diff — a large but sane single request, and well
# inside a 128k context alongside the file list and the model's own output.
DEFAULT_DIFF_CHAR_BUDGET = 60_000
# No single file may eat the whole budget; a 40k-line generated file would
# otherwise crowd out every other change in the MR.
MAX_CHARS_PER_FILE = 12_000
# The Jira ticket is context, not the subject — keep it from dominating.
MAX_JIRA_CHARS = 4_000


def _truncate(text: str, limit: int, note: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [{note}]"


def build_summary_input(
    mr_data: Dict,
    changes: List[dict],
    jira_body: str = "",
    char_budget: int = DEFAULT_DIFF_CHAR_BUDGET,
) -> str:
    """Render the summary agent's whole prompt input as one text block.

    Args:
        mr_data: GitLab MR object (title, description, branches).
        changes: The MR's changed-file list, each with ``diff``/``new_path``.
        jira_body: Optional Jira ticket text from the shared MR context.
        char_budget: Total characters of diff to include.

    Returns:
        A single string: MR identity, ticket, full file list, budgeted diffs.
    """
    title = (mr_data or {}).get("title") or "(no title)"
    description = ((mr_data or {}).get("description") or "").strip()
    source = (mr_data or {}).get("source_branch") or "?"
    target = (mr_data or {}).get("target_branch") or "?"

    parts: List[str] = [
        "=== Merge Request ===",
        f"Title: {title}",
        f"Branch: {source} → {target}",
    ]
    parts.append(
        "\nDescription:\n" + (_truncate(description, 4000, "description truncated")
                              if description else "(the author left no description)")
    )

    if jira_body and jira_body.strip():
        parts.append(
            "\n=== Linked Jira Ticket ===\n"
            + _truncate(jira_body.strip(), MAX_JIRA_CHARS, "ticket truncated")
        )

    # Per-file stats first, so the model sees the whole MR's shape even for the
    # files whose diffs the budget excludes.
    enriched = []
    for change in changes or []:
        path = change.get("new_path") or change.get("old_path") or "unknown"
        added, removed = count_diff_lines(change.get("diff", "") or "")
        enriched.append({
            "path": path,
            "type": change_type(change),
            "added": added,
            "removed": removed,
            "diff": change.get("diff", "") or "",
        })

    parts.append(f"\n=== All Changed Files ({len(enriched)}) ===")
    for f in enriched:
        parts.append(f"  {f['path']}  [{f['type']}] +{f['added']} -{f['removed']}")

    # Spend the diff budget largest-first.
    ranked = sorted(enriched, key=lambda f: f["added"] + f["removed"], reverse=True)
    parts.append("\n=== Diffs ===")
    spent = 0
    shown = 0
    for f in ranked:
        if not f["diff"]:
            continue
        if spent >= char_budget:
            break
        body = _truncate(f["diff"], MAX_CHARS_PER_FILE, f"diff of {f['path']} truncated")
        remaining = char_budget - spent
        if len(body) > remaining:
            body = _truncate(body, remaining, f"diff of {f['path']} truncated (budget reached)")
        parts.append(f"\n--- {f['path']} [{f['type']}] +{f['added']} -{f['removed']} ---")
        parts.append(body)
        spent += len(body)
        shown += 1

    omitted = len([f for f in ranked if f["diff"]]) - shown
    if omitted > 0:
        parts.append(
            f"\n[{omitted} more changed file(s) had their diffs omitted to fit the size "
            f"budget — their paths and line counts are in the list above. Say so if this "
            f"limits what you can conclude.]"
        )

    return "\n".join(parts)
