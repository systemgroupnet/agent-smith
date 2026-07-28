"""
Tracing Helpers (LangSmith + Langfuse)

This module builds the LangChain ``RunnableConfig`` that groups every LLM/agent
run of a single merge-request review into one LangSmith *thread* / Langfuse
*session*, and attaches the Langfuse callback handler that captures the prompts.

LangSmith forms a thread by collecting all runs that share a thread metadata key.
Three keys are recognized: ``session_id``, ``thread_id`` and ``conversation_id``
(see https://docs.langchain.com/langsmith/threads). The docs require the thread
metadata to be present on *all* runs in a trace — both the root run and every child
run — for thread filtering and token counting to be complete. We therefore thread
the config returned by these helpers down to every ``ainvoke`` call instead of
relying solely on LangChain's automatic parent→child metadata propagation.

LangSmith tracing itself is enabled purely via environment variables
(``LANGSMITH_TRACING``, ``LANGSMITH_API_KEY``, ``LANGSMITH_PROJECT``,
``LANGSMITH_ENDPOINT``), which LangChain reads automatically — for LangSmith this
module only handles thread grouping.

Langfuse needs one more thing: an explicit callback handler on the run. It is
attached here, in :func:`build_run_config`, so every top-level ``ainvoke`` in the
review is traced without touching the agents (see
:mod:`core.config.langfuse_config`). The Langfuse equivalent of a thread is a
*session*, keyed off the same per-MR thread id via the ``langfuse_session_id``
metadata key.
"""

from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from core.config.langfuse_config import get_langfuse_handler


def build_thread_id(project_path: str, mr_id: Any) -> str:
    """
    Build a deterministic, per-MR LangSmith thread id.

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
    Build a RunnableConfig that places a run inside the given LangSmith thread.

    Args:
        thread_id: The thread id all runs of this review should share.
        run_name: Optional human-readable name for the trace root (e.g. "review:foo.go").
        extra_metadata: Extra metadata to attach (file_path, mr_id, commit, ...).
        tags: Optional LangSmith tags.

    Returns:
        A RunnableConfig dict with thread metadata set. ``thread_id`` and
        ``session_id`` are both populated (both are LangSmith-recognized thread keys)
        for robustness across LangSmith UI versions, and ``langfuse_session_id``
        mirrors them for Langfuse. When Langfuse is configured, its callback
        handler is attached under ``callbacks`` so this run and every run nested
        under it is captured.
    """
    metadata: Dict[str, Any] = {
        "thread_id": thread_id,
        "session_id": thread_id,
        # Langfuse reads its trace attributes from these reserved metadata keys
        # (it strips them from the recorded metadata). Same id as the LangSmith
        # thread, so one MR is one session in both tools.
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
