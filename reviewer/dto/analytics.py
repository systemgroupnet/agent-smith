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


# Bound the error/warning payload so a pathological run can't send a giant event
# (each severity list is capped independently).
_MAX_RUN_EVENTS = 100
_MAX_MESSAGE_CHARS = 2000
_MAX_TRACEBACK_CHARS = 8000
# A finding's text rides in every posted_comments entry. ~10 comments a run at a
# few hundred characters each is the norm; the cap only stops one runaway
# synthesis from carrying a whole file into the event.
_MAX_PROBLEM_CHARS = 2000


# Bump when the meaning of existing fields changes (not merely when adding new
# optional ones — the collector tolerates extra/missing fields).
ANALYTICS_SCHEMA_VERSION = "1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_reviewer_version() -> str:
    """Resolve the running reviewer's version from the PR_AGENT_IMAGE env.

    The GitLab runner sets PR_AGENT_IMAGE to this agent's full image ref
    (e.g. ``registry.example.com/codereviewagent/reviewer:v0.3.2``).
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
    # Verbosity level in force, from an MR label: "default" | "review:-v" |
    # "review:-vv" | "review:-vvv" (see core/utils/review_verbosity.py). Without it
    # a dashboard cannot interpret comment counts — a -vvv run legitimately posts
    # several times what a default run does, and pooling them makes both meaningless.
    # No collector column yet, so it rides in ``raw_json``; that is the documented
    # contract (unknown fields are preserved, never dropped).
    review_verbosity: Optional[str] = None
    # Which control flow reviewed this MR. There is only one now — a specialist
    # agent per changed file over a checkout, reported as "deep" — but the field
    # stays so pre-existing rows keep their meaning: a row saying "graph" came from
    # the LangGraph fan-out and a row saying "deep" from a run that still had an
    # orchestrator, and neither is comparable to the current shape without knowing
    # which it was. Rides in ``raw_json``.
    review_mode: Optional[str] = None
    # The *configured* (primary) LLM model, e.g. "glm-5.2". Kept as-is even when
    # the run fails over, so "what was this deployment set to" stays answerable.
    model: Optional[str] = None
    # True when a provider rate limit (HTTP 429) outlived the retry budget and the
    # run switched to FALLBACK_LLM_MODEL partway through. A run can therefore be
    # *mixed*: files reviewed before the switch ran on ``model``, files after on
    # ``model_fallback_model``. Per-comment attribution lives in the
    # ``posted_comments`` entries' own "model" key — without it, comparing comment
    # quality by model across mixed runs silently credits everything to ``model``.
    model_fallback_used: bool = False
    model_fallback_model: Optional[str] = None

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
    # Comments the dedup stage removed because the same problem was already on the
    # MR — from a previous (incremental) run of this agent or from a human — or was
    # said twice within this batch. Counted separately from ``comments_selected``,
    # which is what remained: without it a run that found the same 8 findings as
    # last time and correctly posted none is indistinguishable from a run that
    # found nothing. No collector column yet, so it rides in ``raw_json``; the
    # per-drop detail is in the ``comment_dropped_duplicate`` warnings.
    comments_deduplicated: int = 0

    # GitLab username of the MR author — the human whose code was reviewed and
    # who is most likely to score the review comments. Stored per run (not per
    # comment) since every comment in a run belongs to the same MR.
    mr_author_username: Optional[str] = None

    # One entry per comment actually posted to the MR this run, so the collector
    # can key human emoji-reaction scores (gathered via its GitLab webhook
    # endpoint) back to a specific comment and run. Entry shape (built by
    # ``posted_comment_entry``):
    # {note_id, discussion_id, comment_type: "inline"|"note", file, line,
    # severity, model, problem, file_type}. ``model`` is the model that actually
    # reviewed that file (captured when the file review finished), which is not
    # necessarily ``RunMetrics.model`` once a rate-limit failover has happened
    # mid-run. ``problem`` is the finding's text as it was formatted into the
    # comment (after the severity/problem restores, before markdown rendering),
    # clipped to ``_MAX_PROBLEM_CHARS`` — without it a human reply to the comment
    # can be read but not what it answered. ``file_type`` is the review profile's
    # classification of ``file`` (``service``, ``sql``, ...), the ``.aid/<type>.md``
    # a lesson drawn from that reply would belong in. ``note_id`` is the GitLab
    # note id — the same value an emoji webhook event reports as
    # ``object_attributes.awardable_id``, which is the join key. Bounded in
    # practice by the comment-reviewer filter (~10/run).
    posted_comments: list = Field(default_factory=list)

    # True when this run posted the MR's review-summary comment (the orientation
    # note for human reviewers). At most one per MR ever, so across a project this
    # counts MRs that got a summary, not runs.
    summary_posted: bool = False

    # The posted summary comment, kept OUT of ``posted_comments`` on purpose:
    # a summary and a finding are different artifacts, and a rating of one must
    # never be averaged with a rating of the other. Separating them here means the
    # collector can store them in their own tables, so every existing
    # "how good are the findings" query stays correct without needing a filter it
    # would be easy to forget. Shape: {note_id, discussion_id, model}.
    posted_summary: Optional[dict] = None

    duration_seconds: Optional[float] = None
    # True when the overall-timeout budget truncated the review (some files were
    # not reviewed; the marker is held so a re-run picks them up).
    timed_out: bool = False

    # "success" | "partial_failure" | "timeout" | "no_op" | "config_error" | "error".
    # "timeout" = the overall-timeout budget truncated the review (see timed_out).
    # Defaults to "error" so an unexpected crash is recorded as a failure unless
    # a clean exit path explicitly overwrites it.
    status: str = "error"

    # Structured errors captured during the run (comment-post failures, file
    # review failures, MCP init, fatal), each: {severity, stage, context,
    # message, traceback, at}. Collected so failures can be debugged and monitored
    # later instead of only living in the CI log. Populated via record_error().
    errors: list = Field(default_factory=list)

    # Structured warnings — recoverable/transient events the run survived, most
    # notably each retried GitLab comment-post attempt (stage "comment_post_retry").
    # Same shape as ``errors`` (severity="warning"). The collector stores errors
    # and warnings together in a dedicated table, keyed to the run.
    warnings: list = Field(default_factory=list)


def posted_comment_entry(
    posted_note: dict,
    *,
    file: Optional[str],
    line,
    severity: Optional[str],
    model: Optional[str],
    problem=None,
    file_type: Optional[str] = None,
) -> dict:
    """One ``RunMetrics.posted_comments`` entry for a comment GitLab accepted.

    ``posted_note`` is what ``post_gitlab_comment`` returned ({note_id,
    discussion_id, comment_type}). ``problem`` must be the finding's own text —
    the ``problem`` field the comment was formatted from — and never the rendered
    body: the body's suggestion block is the reviewed repository's source code
    verbatim, and what a reply is answering is the claim, not the patch.

    Blank text becomes ``None`` (an empty string would read as "the agent said
    nothing", which the posting loop already guarantees it did not). NUL bytes are
    removed because PostgreSQL rejects them in both TEXT and JSONB, and one in a
    model-quoted snippet would make the collector refuse the whole run event.
    """
    text = None if problem is None else str(problem).replace("\x00", "").strip()
    return {
        "note_id": posted_note.get("note_id"),
        "discussion_id": posted_note.get("discussion_id"),
        "comment_type": posted_note.get("comment_type"),
        "file": file,
        "line": line,
        "severity": severity,
        "model": model,
        "problem": text[:_MAX_PROBLEM_CHARS] if text else None,
        "file_type": file_type or None,
    }


def _record_event(
    target: list,
    severity: str,
    stage: str,
    message,
    context: Optional[str],
    exc: Optional[BaseException],
) -> None:
    """Append one structured event (error/warning) to ``target`` (bounded)."""
    if len(target) >= _MAX_RUN_EVENTS:
        return
    entry = {
        "severity": severity,
        "stage": stage,
        "context": context,
        "message": str(message)[:_MAX_MESSAGE_CHARS],
        "at": _utc_now_iso(),
    }
    if exc is not None:
        tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        entry["traceback"] = tb[-_MAX_TRACEBACK_CHARS:]
    target.append(entry)


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
    _record_event(metrics.errors, "error", stage, message, context, exc)


def record_warning(
    metrics: "RunMetrics",
    stage: str,
    message,
    *,
    context: Optional[str] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Append one structured warning to ``metrics.warnings`` (best-effort, bounded).

    Same shape/semantics as :func:`record_error` but for recoverable events the
    run survived — e.g. a GitLab comment-post attempt that failed and was then
    retried (``stage="comment_post_retry"``). A traceback is rarely useful here,
    so ``exc`` is usually omitted.
    """
    _record_event(metrics.warnings, "warning", stage, message, context, exc)
