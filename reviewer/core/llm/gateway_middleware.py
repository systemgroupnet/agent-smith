"""The gateway's provider policy, as agent middleware.

`LLMGateway` wraps *a runnable*: an agent supplies `build(model) -> Runnable` and
then only calls `ainvoke`, and the gateway owns provider choice, the retry loop,
both provider transitions and rebuilding the bound runnable. That shape fits the
graph mode, where one `ainvoke` is one file's whole review.

It does not fit an agent graph. A deep-agent run is dozens of model calls behind
one `ainvoke`, so wrapping the whole thing would mean a throttle on the fortieth
call replays the first thirty-nine — re-dispatching files that already reported,
and appending their findings to the sink a second time. The unit that needs
retrying is the individual model call, which is precisely the granularity
LangChain's `wrap_model_call` hook exposes.

**This middleware adds no policy of its own.** It hands each model call to a
`LLMGateway` whose `build(model)` returns a runnable that performs that one call
against the model the gateway resolved. Every decision — is this failure
transient, what is its attempt budget, how long to wait, does a 429 that outlived
the budget trigger failover, does a guardrail 404 on the fallback condemn the
endpoint wholly or only for forced-`tool_choice` calls, which transitions get
recorded for analytics — stays in the one module that already implements it. A
second implementation in middleware idiom is the one thing this layering cannot
tolerate; that is why `core/config/llm_config.py` and the `get_*_model` helpers
were deleted rather than kept alongside the gateway.

The cost is one `build_chat_model` call per model call rather than per provider
epoch. `ChatOpenAI` construction is a pydantic init with a lazily-created client,
so this is cheap next to the request it wraps; if it ever matters, the fix is a
provider-keyed cache here and nothing else changes.

Not covered, deliberately: `STRUCTURED_NO_REASONING_MODELS`. The gateway's
`structured=True` disables reasoning for models whose thinking mode rejects a
*named* forced `tool_choice`. A `create_agent` structured response binds
`tool_choice="any"` instead (see `langchain/agents/factory.py`), which is a
different request and may or may not hit the same rejection. `structured` is
exposed here so the behaviour is selectable, but which value is right for a given
model is an empirical question — probe it before relying on either.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables import RunnableLambda

from core.llm.gateway import LLMGateway


class ProviderPolicyMiddleware(AgentMiddleware):
    """Apply the run's provider policy to every model call in an agent graph.

    Args:
        label: Identifies these calls in the retry/failover log lines and in the
            provider-transition records drained into ``RunMetrics.warnings``.
        structured: Whether these calls should be treated as forced-``tool_choice``
            calls for the purposes of the reasoning override and of the
            structured-only fallback block. See the module docstring.
        temperature / max_tokens: Passed through to the model the gateway builds.
    """

    def __init__(
        self,
        *,
        label: str,
        structured: bool = False,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.label = label
        self.structured = structured
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._warned_sync = False

    def _gateway(self, request: Any, handler: Callable, *, is_async: bool) -> LLMGateway:
        """A gateway bound to exactly this model call.

        Per call rather than per instance on purpose: ``switched``/``reverted`` are
        per-``ainvoke`` state in the gateway's loop, which is the correct scope for
        one call, and a shared instance would race between the concurrent subagents
        an orchestrator dispatches in a single turn.
        """

        async def _acall(_ignored: Any = None):
            return await handler(request.override(model=_acall.model))

        def _call(_ignored: Any = None):
            return handler(request.override(model=_call.model))

        def build(model):
            # The gateway resolved the provider and built this model; the runnable
            # it gets back is "make the wrapped call with that model". A provider
            # switch mid-retry re-enters build with the new model, which is how the
            # failover reaches a call that is already in flight.
            _acall.model = model
            _call.model = model
            return RunnableLambda(_call, afunc=_acall)

        return LLMGateway(
            build,
            label=self.label,
            structured=self.structured,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

    async def awrap_model_call(self, request, handler):
        """Async path — the only one the reviewer uses."""
        return await self._gateway(request, handler, is_async=True).ainvoke(None)

    def wrap_model_call(self, request, handler):
        """Sync path: one attempt, no retry and no failover.

        The reviewer is async end to end, so nothing in it reaches here. Rather
        than silently applying no policy, say so once — a sync call that quietly
        lost failover is exactly the invisible-degradation failure the gateway was
        built to eliminate.
        """
        if not self._warned_sync:
            self._warned_sync = True
            print(
                f"  ⚠️  [llm] {self.label}: synchronous model call — retry and provider "
                f"failover are not applied on this path"
            )
        return handler(request)
