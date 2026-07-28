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
Three variables, no separate switch: ``LANGFUSE_BASE_URL`` is the endpoint *and*
the on/off knob (``http://localhost:3000`` self-hosted,
``https://cloud.langfuse.com`` for Cloud; unset → no tracing), and
``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` are the project keys from the
Langfuse UI. All three must be set for tracing to run.

Everything in this module is non-fatal by design: a missing package, wrong keys
or an unreachable Langfuse degrades to "no tracing" and never fails a review —
the same contract as MCP init and the analytics collector.
"""

import os
from typing import Any, Optional

# Resolved once per process. ``_handler_resolved`` is a separate flag because
# ``None`` (tracing disabled / init failed) is itself a cacheable answer, and we
# never want to re-run the auth check or re-print the status line per file.
_handler: Optional[Any] = None
_handler_resolved: bool = False


def get_langfuse_base_url() -> str:
    """The endpoint traces are shipped to, or "" when tracing isn't configured.

    ``LANGFUSE_BASE_URL`` is the SDK's own first-choice variable (langfuse 4.x
    reads it before the older ``LANGFUSE_HOST``, then falls back to Cloud), so
    pointing the reviewer at a Langfuse and pointing the SDK at one are the same
    act — which is why this can double as the on/off switch. No Cloud default
    here on purpose: unset must mean "off", not "ship traces somewhere".
    """
    return os.getenv("LANGFUSE_BASE_URL", "").strip()


def langfuse_enabled() -> bool:
    """Whether Langfuse tracing should be active for this run.

    Setting the base URI *is* enabling tracing — there's no separate flag to
    forget or to contradict it. The keys are then simply required: a URI without
    them can't authenticate, so it warns rather than failing later on every
    single LLM call.
    """
    if not get_langfuse_base_url():
        return False
    if os.getenv("LANGFUSE_PUBLIC_KEY", "").strip() and os.getenv(
        "LANGFUSE_SECRET_KEY", ""
    ).strip():
        return True
    print(
        "  ⚠️  [langfuse] LANGFUSE_BASE_URL is set but LANGFUSE_PUBLIC_KEY / "
        "LANGFUSE_SECRET_KEY are missing — tracing disabled"
    )
    return False


def is_langfuse_cloud(base_url: str) -> bool:
    """Whether a base URI points at Langfuse Cloud rather than a self-hosted one.

    Matters only for proxy handling: Cloud is an external host that must keep
    using ``HTTP_PROXY``, while a self-hosted Langfuse must bypass it. Before the
    base URI became the on/off switch, "was it configured at all?" answered this
    — Cloud was the unset default. Now Cloud users set it explicitly like
    everyone else, so the host itself has to be inspected.
    """
    from urllib.parse import urlparse

    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname == "langfuse.com" or hostname.endswith(".langfuse.com")


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

    Only applies to a self-hosted host. Langfuse Cloud is external and must keep
    using the proxy, so it is left alone.
    """
    if not base_url or is_langfuse_cloud(base_url):
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
    nothing. One bounded call up front turns that into a startup warning. It only
    runs when tracing is configured, and costs a single blocking round trip per
    process — cheap enough not to be worth a flag of its own.
    """
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
