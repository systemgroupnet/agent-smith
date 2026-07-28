"""
Tracing Helpers (Langfuse)

This module builds the LangChain ``RunnableConfig`` that groups every LLM/agent
run of a single merge-request review into one Langfuse *session*, and attaches
the Langfuse callback handler that captures the prompts.

The session is keyed off a per-MR thread id via the reserved
``langfuse_session_id`` metadata key. The config returned by these helpers is
threaded down to every top-level ``ainvoke`` in the review rather than left to
LangChain's automatic parent→child metadata propagation, so grouping is complete
even where a call doesn't inherit an ambient config.

Langfuse also needs an explicit callback handler on the run. It is attached here,
in :func:`build_run_config`, so the whole review is traced without touching the
agents (see :mod:`core.config.langfuse_config`).
"""

from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from core.config.langfuse_config import get_langfuse_handler


def build_thread_id(project_path: str, mr_id: Any) -> str:
    """
    Build a deterministic, per-MR thread id (the Langfuse session key).

    Keying on the MR (rather than a random id per run) means re-reviewing the same
    MR — including the incremental re-review flow — appends new traces to the same
    thread instead of creating a new one.

    Args:
        project_path: GitLab project path (e.g. "group/project").
        mr_id: Merge request IID/number.

    Returns:
        Thread id string, e.g. "group/project!123".
    """
    return f"{project_path}!{mr_id}"


def build_run_config(
    thread_id: str,
    run_name: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[List[str]] = None,
) -> RunnableConfig:
    """
    Build a RunnableConfig that places a run inside the given review's session.

    Args:
        thread_id: The thread id all runs of this review should share.
        run_name: Optional human-readable name for the trace root (e.g. "review:foo.go").
        extra_metadata: Extra metadata to attach (file_path, mr_id, commit, ...).
        tags: Optional trace tags.

    Returns:
        A RunnableConfig dict with the session metadata set. When Langfuse is
        configured, its callback handler is attached under ``callbacks`` so this
        run and every run nested under it is captured.
    """
    metadata: Dict[str, Any] = {
        # Langfuse reads its trace attributes from reserved metadata keys like
        # this one (and strips them from the recorded metadata), so one MR is one
        # session. ``thread_id`` is kept as ordinary, searchable metadata naming
        # the same grouping.
        "thread_id": thread_id,
        "langfuse_session_id": thread_id,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    config: RunnableConfig = {
        "metadata": metadata,
        "tags": tags or [],
    }
    if run_name:
        config["run_name"] = run_name

    # Attach the Langfuse handler only on these top-level configs. Nested runs
    # inherit it through LangChain's ambient config, and re-attaching it further
    # down would replace the inherited callback *manager* (which carries the
    # parent run id) with a bare handler list — flattening the trace into
    # unrelated roots. See merge_thread_into_config, which deliberately does not
    # touch callbacks. Absent when tracing is off, leaving behavior unchanged.
    handler = get_langfuse_handler()
    if handler is not None:
        config["callbacks"] = [handler]

    return config


def merge_thread_into_config(
    config: Optional[RunnableConfig],
    run_name: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[RunnableConfig]:
    """
    Derive a child RunnableConfig that keeps the inherited thread metadata while
    optionally overriding the run name / adding metadata.

    Used by graph nodes to give each nested agent run a useful name without losing
    the ``thread_id`` set on the parent config. Returns ``None`` unchanged when the
    incoming config is ``None`` so callers fall back to LangChain's ambient config.

    Deliberately carries **no** ``callbacks`` key: a config passed to a nested
    ``ainvoke`` overrides the ambient one key-by-key, so setting callbacks here
    would swap the inherited callback manager (which knows the parent run) for a
    plain handler list and break the parent→child links in the trace. Leaving the
    key out lets LangChain propagate the Langfuse handler from the parent run.

    Args:
        config: The parent RunnableConfig (may be None).
        run_name: Optional run name to set on the derived config.
        extra_metadata: Optional metadata to merge in.

    Returns:
        A new RunnableConfig, or None if ``config`` was None.
    """
    if config is None:
        return None

    parent_metadata = dict(config.get("metadata") or {})
    if extra_metadata:
        parent_metadata.update(extra_metadata)

    derived: RunnableConfig = {
        "metadata": parent_metadata,
        "tags": list(config.get("tags") or []),
    }
    if run_name:
        derived["run_name"] = run_name

    return derived
