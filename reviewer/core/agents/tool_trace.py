"""What each reviewer actually looked at, one line per tool call.

Without this a run's log jumps from "🧠 Review pass: 22 reviewer-file pair(s)" to a
list of findings, and the interesting part — *did it read the file, did it grep for
the caller, or did it answer from the diff alone?* — is invisible. That question is
not idle: a reviewer that never opened the file it was handed produces findings
worth nothing, and the only other place to see it is a Langfuse trace, which needs
tracing configured and a browser.

The graph-era `tool_agent.py` printed a summary block per run behind
``LOG_TOOL_TRACE``; that module is gone with the code index, and the deep agents it
was replaced by logged nothing at all. This restores the variable and the intent,
as middleware, per call rather than as an end-of-run tally.

**Every line is labelled with the reviewer and its file**, which is the whole
reason this is per-instance state rather than a module-level printer: reviewers run
``FILE_BATCH_SIZE`` at a time, so an unlabelled trace of five concurrent
investigations is noise. Each line ends with an outcome summary rather than the
result itself: a `read_file` on a 2,000-line file would otherwise print the file
into the job log, and the point is to see *that* it was read.

Failures are logged too, both shapes they can take. Which shape arrives depends on
whether ``ToolErrorMiddleware`` sits inside or outside this one — an error
``ToolMessage`` if it already converted the exception, a raised exception if not —
so both are handled and an exception is always re-raised. Never guess a middleware
ordering you can simply tolerate.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware

TRACE_ENV = "LOG_TOOL_TRACE"

#: Args are for orientation, not reproduction: a `grep` pattern or a `record_findings`
#: payload can be arbitrarily long, and a log line that wraps four times is harder to
#: read than one that says less.
MAX_ARG_CHARS = 80

#: The argument worth showing first for each tool the reviewers hold. Everything else
#: falls back to the first string-ish value, which is right often enough.
_HEADLINE_ARG = {
    "read_file": "file_path",
    "grep": "pattern",
    "glob": "pattern",
    "ls": "path",
    "record_findings": "file_path",
    "task": "description",
}


def trace_enabled() -> bool:
    """Whether to print the trace. Default on; any falsy spelling turns it off."""
    raw = (os.getenv(TRACE_ENV) or "").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _clip(text: str, limit: int = MAX_ARG_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _headline(name: str, args: dict) -> str:
    """A compact `tool arg` for the log line."""
    if not isinstance(args, dict) or not args:
        return name
    key = _HEADLINE_ARG.get(name)
    value = args.get(key) if key else None
    if value is None:
        value = next((v for v in args.values() if isinstance(v, str) and v), None)
    if value is None:
        # No string to show — say how big the payload was instead, which is the
        # useful thing for `record_findings(findings=[...])` and `write_todos`.
        sizes = [f"{k}[{len(v)}]" for k, v in args.items() if isinstance(v, (list, tuple))]
        return f"{name} {' '.join(sizes)}" if sizes else name
    extra = ""
    if name == "grep" and (scope := args.get("path") or args.get("glob")):
        extra = f" in {_clip(scope, 40)}"
    return f"{name} {_clip(value)}{extra}"


def _outcome(result: Any) -> str:
    """A summary of what came back — never the whole content.

    Three shapes arrive. A ``ToolMessage`` is the common one. A ``Command`` comes
    from a tool that updates agent state (``write_todos``), and stringifying it
    dumps the entire state update into the log, so its own message is dug out
    instead. And ``content`` is not always a string: ``glob`` answers with a list of
    paths, where the count is the useful part and the list is not.
    """
    status = getattr(result, "status", None)
    if not hasattr(result, "content") and hasattr(result, "update"):
        messages = (getattr(result, "update", None) or {}).get("messages") or []
        inner = getattr(messages[-1], "content", None) if messages else None
        return _clip(inner, 100) if isinstance(inner, str) and inner.strip() else "state updated"
    content = getattr(result, "content", result)
    if isinstance(content, (list, tuple)):
        return f"{len(content)} item(s)"
    if not isinstance(content, str):
        content = str(content)
    if status == "error":
        return f"error: {_clip(content, 100)}"
    if not content.strip():
        return "empty"
    lines = content.strip().count("\n") + 1
    if lines > 1:
        return f"{lines} line(s)"
    # A one-line answer *is* the summary — `grep` returning one path, `ls` a short
    # listing, `record_findings` its confirmation. A character count would hide the
    # one thing worth reading.
    return _clip(content.strip(), 100)


class ToolTraceMiddleware(AgentMiddleware):
    """Print one line per tool call, labelled with the reviewer that made it."""

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.tools = []  # registers no tools of its own

    def _log(self, name: str, args: dict, outcome: str, seconds: float) -> None:
        print(f"  🔧 [{self.label}] {_headline(name, args)} → {outcome} ({seconds:.2f}s)")

    def _describe(self, request: Any) -> tuple:
        call = getattr(request, "tool_call", None) or {}
        tool = getattr(request, "tool", None)
        name = (getattr(tool, "name", None) or call.get("name") or "tool")
        return str(name), (call.get("args") or {})

    def wrap_tool_call(self, request, handler: Callable):
        name, args = self._describe(request)
        start = time.monotonic()
        try:
            result = handler(request)
        except Exception as exc:  # noqa: BLE001 — log it, then let it travel on
            self._log(name, args, f"raised {type(exc).__name__}", time.monotonic() - start)
            raise
        self._log(name, args, _outcome(result), time.monotonic() - start)
        return result

    async def awrap_tool_call(self, request, handler: Callable):
        name, args = self._describe(request)
        start = time.monotonic()
        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001
            self._log(name, args, f"raised {type(exc).__name__}", time.monotonic() - start)
            raise
        self._log(name, args, _outcome(result), time.monotonic() - start)
        return result


def tool_trace_middleware(label: str) -> Optional[ToolTraceMiddleware]:
    """The middleware, or None when the trace is switched off."""
    return ToolTraceMiddleware(label) if trace_enabled() else None
