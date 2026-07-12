"""Analytics event emitted once per MR-review run.

This model is populated incrementally over the course of a run (see
``main.py::process_mr``) and serialized to JSON exactly once, in a ``finally``
block, so that *every* exit path — the happy path, the "no new commits" and
"no issues" early returns, and unhandled exceptions — produces one event.

The collector service validates this payload independently (each service has
its own Docker build context and cannot import the other's code), so the
contract between the two is JSON, not a shared class. ``schema_version`` lets
the two evolve without lockstep deploys; the collector stores unknown fields
rather than rejecting them.
"""

import os
import traceback as _traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


# Bound the error payload so a pathological run can't send a giant event.
_MAX_RUN_ERRORS = 100
_MAX_MESSAGE_CHARS = 2000
_MAX_TRACEBACK_CHARS = 8000


# Bump when the meaning of existing fields changes (not merely when adding new
# optional ones — the collector tolerates extra/missing fields).
ANALYTICS_SCHEMA_VERSION = "1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_reviewer_version() -> str:
    """Resolve the running reviewer's version from the PR_AGENT_IMAGE env.

    The GitLab runner sets PR_AGENT_IMAGE to this agent's full image ref
    (e.g. ``registry.example.com/aid/codereviewagent/reviewer:v0.3.2``).
    The version is the tag after the final ':'. We guard against a registry-port
    colon with no tag (that trailing segment still contains '/') by falling back
    to the full value. Always returns a non-empty string so every event carries
    a version; "dev" when the env is unset (local runs).
    """
    raw = os.getenv("PR_AGENT_IMAGE", "").strip()
    if not raw:
        return "dev"
    tag = raw.rsplit(":", 1)[-1]
    if tag and "/" not in tag:
        return tag
    return raw


class RunMetrics(BaseModel):
    """Mutable accumulator + wire payload for a single review run.

    All fields are optional/defaulted so an event is meaningful even for a run
    that fails before it learns the MR's identity (bad URL, missing token).
    """

    schema_version: str = ANALYTICS_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Run start time; the collector treats this as the event timestamp.
    timestamp: str = Field(default_factory=_utc_now_iso)
    # Reviewer build version (image tag / commit sha), resolved at construction
    # so every event — including early-exit and error events — carries it.
    reviewer_version: str = Field(default_factory=get_reviewer_version)

    # --- MR identity (populated once the URL is parsed / MR is fetched) ---
    mr_url: Optional[str] = None
    project_path: Optional[str] = None
    mr_id: Optional[str] = None
    mr_title: Optional[str] = None
    source_branch: Optional[str] = None
    target_branch: Optional[str] = None
    head_sha: Optional[str] = None
    last_reviewed_sha: Optional[str] = None

    # "full" | "incremental" | "none" (no code to review) | None (never got there)
    review_type: Optional[str] = None
    # LLM model in provider/model form, e.g. "deepseek/deepseek-v4-pro".
    model: Optional[str] = None

    # --- file counts ---
    files_changed_total: int = 0   # full MR changed-file count
    files_reviewed: int = 0        # reviewed successfully this run
    files_failed: int = 0          # raised during review
    files_skipped: int = 0         # deleted / excluded / unfetchable

    # --- issue counts (across all reviewed files, before comment filtering) ---
    issues_total: int = 0
    issues_high: int = 0
    issues_medium: int = 0
    issues_low: int = 0

    # --- comment posting (after the comment-reviewer filter) ---
    comments_selected: int = 0
    comments_posted: int = 0
    comments_failed: int = 0

    duration_seconds: Optional[float] = None
    # True when the overall-timeout budget truncated the review (some files were
    # not reviewed; the marker is held so a re-run picks them up).
    timed_out: bool = False

    # "success" | "partial_failure" | "no_op" | "config_error" | "error".
    # Defaults to "error" so an unexpected crash is recorded as a failure unless
    # a clean exit path explicitly overwrites it.
    status: str = "error"

    # Structured errors captured during the run (comment-post failures, file
    # review failures, MCP init, fatal), each: {stage, context, message,
    # traceback, at}. Collected so failures can be debugged and monitored later
    # instead of only living in the CI log. Populated via record_error().
    errors: list = Field(default_factory=list)


def record_error(
    metrics: "RunMetrics",
    stage: str,
    message,
    *,
    context: Optional[str] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Append one structured error to ``metrics.errors`` (best-effort, bounded).

    ``stage`` is a coarse bucket ("comment_post", "file_review", "mcp_init",
    "fatal"); ``context`` locates it (e.g. a file path or ``file:line``); ``exc``,
    when given, contributes the traceback for debugging. Silently no-ops past the
    per-run cap so a failing run can't produce an unbounded event.
    """
    if len(metrics.errors) >= _MAX_RUN_ERRORS:
        return
    entry = {
        "stage": stage,
        "context": context,
        "message": str(message)[:_MAX_MESSAGE_CHARS],
        "at": _utc_now_iso(),
    }
    if exc is not None:
        tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        entry["traceback"] = tb[-_MAX_TRACEBACK_CHARS:]
    metrics.errors.append(entry)
