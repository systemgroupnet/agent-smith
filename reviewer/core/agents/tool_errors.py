"""A tool that raises must not take the file review with it.

Today exactly one family of tools is protected: ``toolkit_manager``'s
``_wrap_tools_resilient`` wraps each **MCP** tool's coroutine, because those calls
share one ``ClientSession`` across every concurrent review and a dropped session
would otherwise crash all of them. Every *other* tool a reviewer holds —
``read_file``, ``grep``, ``glob``, ``ls``, ``record_findings``, ``write_todos``,
``task`` — is unprotected. An exception in one propagates out of the agent loop
and the whole file is reported as a `file_review` error (marker held, non-zero
exit) even when the reviewer had already banked findings and could have carried on
by another route.

LangChain has the mechanism, so this module supplies the policy and nothing else:
``ToolErrorMiddleware`` calls a handler per tool exception and turns whatever the
handler returns into a ``ToolMessage(status="error")``. That shape is strictly
better than the one the MCP wrapper is forced into. The wrapper returns its error
as a *successful* observation, which means matching each tool's declared
``response_format`` by hand — a bare string where ``content_and_artifact`` is
declared raises LangChain's "a two-tuple ... is expected", and that is precisely
what made the first cut of that wrapper crash the reviews it existed to protect.
A ``ToolMessage`` carries the failure as status rather than as prose that happens
to start with "ERROR".

**This does not replace the MCP wrapper**, and the reasons are properties of the
framework rather than preferences:

1. ``ToolErrorMiddleware`` sees only exceptions raised by tool *execution* —
   argument binding and schema validation are handled upstream by ``ToolNode``,
   which converts them to an error message before the tool runs. So
   ``_repair_tool_args`` (a scalar where an array is declared, a number past its
   documented maximum — the two mistakes that once cost a whole run its code
   index) cannot move here: by the time this handler is called, the repair
   opportunity has passed.
2. No built-in bounds a tool call in wall-clock. ``MCP_TOOL_TIMEOUT_S`` exists
   because a server that accepts a request and then simply stops answering hung a
   run for 40+ minutes on one open socket, and catching exceptions does not help
   when nothing is ever raised.
3. Middleware only reaches agents built by ``create_agent``. The **summary** agent
   is a ``langchain-classic`` ``AgentExecutor`` and ``mr_context`` calls
   ``jira_get_issue`` directly with no agent at all — both hold the same tool
   objects, so the wrapper is the floor under every consumer while this is the net
   over the reviewers' own tools.

Nothing here is retried, deliberately. ``ToolRetryMiddleware`` is available and is
the wrong instrument for these tools: a dropped MCP session never recovers inside
a run (there is no reconnection), the reviewer prompt already tells the model not
to retry an unavailable tool, and a retry loop would spend the
``ModelCallLimitMiddleware`` budget that bounds the reviewer's investigation. The
model decides whether a second attempt is worth it, which is the right place for
that judgement — it can see whether it has enough to report already.
"""

from __future__ import annotations

from typing import Any, Optional

from langchain.agents.middleware import ToolErrorMiddleware

#: The tool that carries findings to the merge request (``findings_sink``). Its
#: failure needs the opposite advice from every other tool's: nothing else records
#: anything, so "carry on" would silently discard the review.
RECORD_FINDINGS_TOOL = "record_findings"

#: A tool exception's text goes into the reviewer's context, so it is clipped. A
#: pydantic validation error over a 50-finding payload can run to thousands of
#: characters, and the useful part is at the front.
MAX_ERROR_CHARS = 400


def _tool_name(request: Any) -> str:
    """The tool's name, from whichever half of the request carries it."""
    tool = getattr(request, "tool", None)
    if tool is not None and getattr(tool, "name", None):
        return str(tool.name)
    call = getattr(request, "tool_call", None) or {}
    return str(call.get("name") or "unknown")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def tool_error_observation(exc: Exception, request: Any) -> Optional[str]:
    """What the reviewer is told when one of its tools raises.

    Returns a message for **every** exception, which is the whole point: returning
    ``None`` would let it propagate and end the file review, and no tool here is
    load-bearing enough to be worth that. Cancellation is unaffected —
    ``asyncio.CancelledError`` is a ``BaseException``, so the middleware never sees
    it and the file watchdog and overall-budget timeouts still work.
    """
    name = _tool_name(request)
    detail = _clip(f"{type(exc).__name__}: {exc}")

    if name == RECORD_FINDINGS_TOOL:
        return (
            f"ERROR: `record_findings` failed and your findings were **not** recorded "
            f"({detail}). Call it again with corrected arguments — it is the only "
            f"channel that reaches the merge request, so anything you do not "
            f"successfully record is lost."
        )
    return (
        f"ERROR: the `{name}` tool failed ({detail}). Try different arguments or "
        f"another route to the same information. If it keeps failing, finish the "
        f"review with what you already have and record what you found."
    )


def tool_error_middleware() -> ToolErrorMiddleware:
    """LangChain's own tool-error middleware, carrying the policy above.

    One sync handler serves both paths: ``ToolErrorMiddleware`` falls back to
    ``on_error`` on the async path when no ``aon_error`` is given, and this handler
    only formats a string — there is nothing here to await.
    """
    return ToolErrorMiddleware(on_error=tool_error_observation)
