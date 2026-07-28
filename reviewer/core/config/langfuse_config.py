"""
Langfuse Tracing Setup

Langfuse is the *context inspector* for this agent. For every LLM call it records
the exact prompt that was sent — system prompt, the shared MR context block, the
line-numbered file content, the diff, the ``.aid`` instructions — alongside every
MCP tool call with its observation and the model's raw answer. That is what you
need to answer "why did the reviewer say that?", which the run analytics
(counts and outcomes) deliberately do not carry.

Wiring
------
Langfuse ships a LangChain ``CallbackHandler``, so it hooks into the run configs
that :mod:`core.config.tracing` already threads into every top-level ``ainvoke``
(each file's workflow, the effort estimate, the comment reviewer). Nested runs —
graph nodes, the agent loop, individual LLM/tool calls — inherit the handler from
the ambient LangChain config, so no per-agent change is needed. Langfuse and
LangSmith are independent and can run at the same time.

Enabling
--------
Set ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` (project keys from the
Langfuse UI) and, when self-hosting, ``LANGFUSE_BASE_URL`` (e.g.
``http://localhost:3000``; unset → Langfuse Cloud). Tracing switches itself on as
soon as both keys are present; ``LANGFUSE_ENABLED`` forces it on or off.

Everything in this module is non-fatal by design: a missing package, wrong keys
or an unreachable Langfuse degrades to "no tracing" and never fails a review —
the same contract as MCP init and the analytics collector.
"""

import os
from typing import Any, Optional

# Values that read as "off" for the boolean env flags here.
_FALSY = ("", "false", "0", "no", "off")

# Resolved once per process. ``_handler_resolved`` is a separate flag because
# ``None`` (tracing disabled / init failed) is itself a cacheable answer, and we
# never want to re-run the auth check or re-print the status line per file.
_handler: Optional[Any] = None
_handler_resolved: bool = False


def _truthy(raw: Optional[str]) -> bool:
    """Whether an env value reads as enabled ('' / false / 0 / no / off => False)."""
    return (raw or "").strip().lower() not in _FALSY


def get_langfuse_base_url() -> str:
    """The endpoint traces are shipped to.

    Mirrors the SDK's own precedence (``LANGFUSE_BASE_URL`` wins over the older
    ``LANGFUSE_HOST``, then Cloud) so the status line and the proxy exclusion in
    ``proxy_config`` name the host Langfuse will actually talk to.
    """
    return (
        os.getenv("LANGFUSE_BASE_URL", "").strip()
        or os.getenv("LANGFUSE_HOST", "").strip()
        or "https://cloud.langfuse.com"
    )


def langfuse_enabled() -> bool:
    """Whether Langfuse tracing should be active for this run.

    Default is "on when configured": both keys present means someone wired a
    Langfuse up and wants the traces. ``LANGFUSE_ENABLED`` overrides in either
    direction — but forcing it on without keys can't work, so that combination
    warns and stays off rather than failing later on every single LLM call.
    """
    has_keys = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        and os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    )
    # LANGFUSE_TRACING is the pre-existing name in this repo's .env template
    # (mirroring LANGSMITH_TRACING); LANGFUSE_ENABLED matches the convention used
    # by every other switch here (COLLECTOR_ENABLED, MCP_*_ENABLED, ...) and wins.
    # The SDK's own LANGFUSE_TRACING_ENABLED=false also still works as a kill
    # switch — it disables the client underneath us.
    raw = (
        os.getenv("LANGFUSE_ENABLED", "").strip()
        or os.getenv("LANGFUSE_TRACING", "").strip()
    )
    if not raw:
        return has_keys
    if not _truthy(raw):
        return False
    if not has_keys:
        print(
            "  ⚠️  [langfuse] LANGFUSE_ENABLED is set but LANGFUSE_PUBLIC_KEY / "
            "LANGFUSE_SECRET_KEY are missing — tracing disabled"
        )
        return False
    return True


def _ensure_no_proxy(base_url: str) -> None:
    """Keep the trace export from being routed into an outbound proxy.

    Spans leave over plain HTTP (OTLP), so a self-hosted Langfuse is subject to
    ``HTTP_PROXY``/``ALL_PROXY`` like any other request — and a corporate proxy
    has no route to an internal host. The failure is quiet and easy to misread:
    ``auth_check`` can still pass over a different client while every span batch
    dies with "Failed to export span batch", leaving an empty project.

    ``proxy_config.setup_proxy`` covers this too, but it only runs in web mode
    (``app.py``); doing it here as well means the CLI — the CI path — is covered
    no matter how the process was started. Appends, so it never drops entries
    another component added.

    Only applies to an explicitly configured host, i.e. self-hosted. Langfuse
    Cloud is external and must keep using the proxy, so it is left alone.
    """
    if not (
        os.getenv("LANGFUSE_BASE_URL", "").strip()
        or os.getenv("LANGFUSE_HOST", "").strip()
    ):
        return
    if not any(
        os.getenv(var)
        for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                    "ALL_PROXY", "all_proxy")
    ):
        return  # No proxy to bypass.

    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if not parsed.hostname:
        return
    # Both forms: some clients match the bare hostname, others host:port.
    wanted = [parsed.hostname]
    if parsed.port:
        wanted.append(f"{parsed.hostname}:{parsed.port}")

    existing = [
        entry.strip()
        for entry in (os.getenv("NO_PROXY") or os.getenv("no_proxy") or "").split(",")
        if entry.strip()
    ]
    added = [host for host in wanted if host not in existing]
    if not added:
        return

    value = ",".join(existing + added)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value  # Some clients only read the lowercase form.
    print(f"  ✓ [langfuse] Added {', '.join(added)} to NO_PROXY for trace export")


def _reviewer_release() -> str:
    """Reviewer build tag, so a trace says which image produced it.

    Reuses the analytics version resolver (the ``PR_AGENT_IMAGE`` tag, ``dev``
    locally) rather than inventing a second notion of "version". Imported lazily
    to keep this config module free of a dto import at module load.
    """
    try:
        from dto.analytics import get_reviewer_version

        return get_reviewer_version()
    except Exception:
        return "dev"


def _auth_ok(client: Any, base_url: str) -> bool:
    """Probe credentials + reachability once, before the review starts.

    Without this a misconfigured Langfuse fails *silently*: spans are buffered
    and dropped on export, and the run looks fine until you open the UI and find
    nothing. One bounded call up front turns that into a startup warning. Set
    ``LANGFUSE_AUTH_CHECK=false`` to skip it (it costs a round trip, and
    ``auth_check`` is blocking).
    """
    if not _truthy(os.getenv("LANGFUSE_AUTH_CHECK", "true")):
        return True
    try:
        if client.auth_check():
            return True
        reason = "credentials rejected"
    except Exception as e:
        reason = str(e) or type(e).__name__
    print(
        f"  ⚠️  [langfuse] Cannot reach {base_url} with the configured keys "
        f"({reason}) — tracing disabled for this run"
    )
    return False


def get_langfuse_handler() -> Optional[Any]:
    """Return the shared Langfuse LangChain callback handler, or ``None``.

    ``None`` means "not tracing" for any reason — disabled, package missing, bad
    keys, unreachable host — and callers must treat it as a no-op rather than an
    error. Safe to call from anywhere: the first call resolves and prints the
    status, every later call returns the cached result.
    """
    global _handler, _handler_resolved

    if _handler_resolved:
        return _handler
    _handler_resolved = True

    if not langfuse_enabled():
        return None

    try:
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
    except ImportError as e:
        print(
            f"  ⚠️  [langfuse] langfuse is not installed ({e}) — tracing disabled. "
            f"Install it with: pip install 'langfuse>=4.0,<5.0'"
        )
        return None

    base_url = get_langfuse_base_url()
    try:
        # Must happen before the client is built — the exporter reads the proxy
        # environment when it is constructed.
        _ensure_no_proxy(base_url)

        # Stamp the build onto every trace. setdefault so an explicitly configured
        # LANGFUSE_RELEASE still wins.
        os.environ.setdefault("LANGFUSE_RELEASE", _reviewer_release())

        # The handler is stateless w.r.t. auth: it resolves the singleton client
        # that get_client() builds from the LANGFUSE_* env vars.
        client = get_client()
        if not _auth_ok(client, base_url):
            return None

        _handler = CallbackHandler()
        print(f"🔭 Langfuse tracing enabled → {base_url}")
    except Exception as e:
        print(f"  ⚠️  [langfuse] Could not start tracing ({e}) — continuing without it")
        _handler = None

    return _handler


def flush_langfuse() -> None:
    """Ship buffered spans before the process exits.

    Langfuse batches spans on a background thread. A one-shot CI reviewer
    container finishes and exits well inside the flush interval, so without an
    explicit flush the last (often the only) run's traces are lost. Called from
    the CLI's cleanup path and after each web-mode review; non-fatal and a no-op
    when tracing never started.
    """
    if _handler is None:
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception as e:
        print(f"  ⚠️  [langfuse] Flush failed ({e}) — some traces may be missing")
