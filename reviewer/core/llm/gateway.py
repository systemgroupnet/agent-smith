"""``LLMGateway`` — the single path from any agent to any LLM provider.

Every LLM call the reviewer makes goes through this class: the file reviewers, the
comment reviewer, the summary and effort agents, the tool agent's salvage call, and
the startup pre-flight. Nothing else builds a chat model, retries a request, or
decides which provider to talk to.

Why it is one class
-------------------
Retry and failover used to be *policy in one module, wiring in every agent*: each
agent built its own client, kept its own chain/executor bound to it, and handed a
``_rebuild_for_fallback`` callback to a free ``with_llm_retry`` function. Three
things went wrong with that, all of them structural:

* **Every new agent had to re-implement the wiring**, and one that forgot the
  ``on_fallback`` hook silently lost failover — the failure mode is invisible
  until a provider throttles in production.
* **An agent built *after* another had already switched was born on the fallback**
  and never learned it. That is exactly how the comment reviewer — the last agent
  constructed in a run — ended up issuing its only call against a provider the
  account's guardrail rejects, and reporting ``NotFoundError: 404 No endpoints
  available matching your guardrail restrictions and data policy``.
* **A dead fallback killed the run**, because the latch was one-way. The primary's
  throttle had almost certainly cleared by then.

So: agents describe *how to build their runnable from a model* and then only ever
call :meth:`ainvoke`. Provider choice, client construction, the retry loop, both
provider transitions, and rebuilding the bound runnable all live here.

Provider state is class-level, i.e. process-wide, and reset per run
(:meth:`reset`, called from ``process_mr`` — web mode serves many reviews from one
process, so a throttle during one MR must not demote every later one). It is
shared on purpose: several file reviews are in flight at once and they hit a
concurrency limit *together*, so once any one of them has established that the
primary is throttling, the rest should join rather than each spend a full retry
budget rediscovering it.

Provider transitions
--------------------
Two, each allowed at most once per call, so a call can never ping-pong:

1. **primary → fallback**, when a rate limit outlives the retry budget (or when
   another agent has already switched). Only rate limits trigger it: a timeout or
   a gateway blip says nothing about the provider being over capacity.
2. **fallback → primary**, when the *fallback* answers with a deterministic block
   (data policy / guardrail / quota). Retrying that is futile, but the primary was
   only ever throttled, and a throttle clears. The fallback is then marked
   unusable for the rest of the run so no other agent switches onto it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableConfig

from core.config.langchain_config import (
    MODEL_NAME_MAPPING,
    build_chat_model,
    fallback_api_key,
    fallback_base_url,
    fallback_configured,
    get_default_model,
    get_fallback_model,
    primary_api_key,
    primary_base_url,
    structured_reasoning_for,
)
from core.utils.llm_retry import (
    LLMErrorInfo,
    describe_error,
    parse_llm_error,
    retry_attempt_budget,
    retry_wait_seconds,
)

PRIMARY = "primary"
FALLBACK = "fallback"


@dataclass(frozen=True)
class Provider:
    """One resolved endpoint: which model, on whose key, at which base URL."""

    name: str  # PRIMARY | FALLBACK
    model: str
    api_key: str
    base_url: str

    def describe(self) -> str:
        """For log lines. Never includes the key."""
        return f"{self.model} @ {self.base_url or 'api.openrouter.ai'}"


#: An agent's contribution: turn a chat model into the runnable it wants to call.
#: Called again whenever the provider changes, so it must be pure wiring — prompt
#: and tool assembly only, no I/O.
BuildRunnable = Callable[[BaseChatModel], Runnable]


class LLMGateway:
    """A model-bound runnable plus every policy that surrounds calling it.

    One instance per runnable (an agent's chain, its executor, its salvage chain).
    Provider state is shared across all instances.

    Usage::

        gateway = LLMGateway(
            lambda model: prompt | model.with_structured_output(Out),
            label="MyAgent structured call",
            structured=True,
        )
        result = await gateway.ainvoke({"input": text}, config=config)
    """

    # ── process-wide provider state ─────────────────────────────────────────
    # _epoch is the mechanism that makes a switch reach agents that were not
    # involved in it: it is bumped on every provider change, and an instance whose
    # runnable was built under an older epoch rebuilds before its next attempt.
    # That is what stops a late-constructed agent from being stranded on a
    # provider the run has already abandoned.
    _fallback_active: bool = False
    _fallback_blocked: bool = False  # the fallback answered with a hard/config block
    _fallback_ever_used: bool = False  # for analytics; survives a revert
    _epoch: int = 0

    def __init__(
        self,
        build: BuildRunnable,
        *,
        label: str,
        model_name: Optional[str] = None,
        temperature: float = 0.0,
        structured: bool = False,
        streaming: bool = False,
        max_tokens: Optional[int] = None,
        retries: bool = True,
    ):
        """
        Args:
            build: Builds the runnable from a chat model. Re-invoked on a provider
                   change, so keep it side-effect free.
            label: Identifies this call in retry/failover log lines.
            model_name: Requested model, or None for DEFAULT_LLM_MODEL. Ignored
                        while the fallback is active — the latch means "the primary
                        endpoint is throttling us", which is true of every model on it.
            temperature: Sampling temperature.
            structured: True for ``with_structured_output(method="function_calling")``
                        calls, which send a forced ``tool_choice``; the reasoning
                        override is then applied per the model actually being sent.
                        Agent-loop calls (``tool_choice`` auto) pass False so
                        reasoning stays on.
            streaming: Passed through to the client.
            max_tokens: Response cap, if any.
            retries: False for probes that want a single attempt and no failover
                     (the startup pre-flight, whose job is to report the endpoint's
                     state, not to work around it).
        """
        self._build = build
        self.label = label
        # Legacy short names ("gpt-4o-mini") map to endpoint ids once, here, so the
        # rest of the class only ever handles real ids.
        self.model_name = MODEL_NAME_MAPPING.get(model_name, model_name) if model_name else None
        self.temperature = temperature
        self.structured = structured
        self.streaming = streaming
        self.max_tokens = max_tokens
        self.retries = retries

        self._runnable: Optional[Runnable] = None
        self._built_epoch: int = -1
        self._built_provider: Optional[Provider] = None

    # ── provider resolution ─────────────────────────────────────────────────

    @classmethod
    def _fallback_usable(cls) -> bool:
        """Configured *and* not already proven dead this run."""
        return fallback_configured() and not cls._fallback_blocked

    @classmethod
    def active_provider(cls, requested: Optional[str] = None) -> Provider:
        """The provider currently in force."""
        if cls._fallback_active and cls._fallback_usable():
            return Provider(FALLBACK, get_fallback_model(), fallback_api_key(), fallback_base_url())
        return Provider(PRIMARY, requested or get_default_model(), primary_api_key(), primary_base_url())

    @classmethod
    def effective_model(cls, requested: Optional[str] = None) -> str:
        """The model id actually being sent right now.

        This is the per-comment model attribution in ``main.py``: read *after* a
        file's review finishes, so a file whose review only completed on the
        fallback is credited to the fallback.
        """
        return cls.active_provider(requested).model

    # ── latch control ───────────────────────────────────────────────────────

    @classmethod
    def activate_fallback(cls) -> bool:
        """Switch the run to the fallback provider. True if this call flipped it."""
        if cls._fallback_active or not cls._fallback_usable():
            return False
        cls._fallback_active = True
        cls._fallback_ever_used = True
        cls._epoch += 1
        return True

    @classmethod
    def block_fallback(cls) -> bool:
        """Mark the fallback unusable and return the run to the primary.

        Called when the fallback answers with a deterministic block: it will answer
        every request identically, so both the switch back *and* the poison flag
        matter — without the flag the next agent to be throttled would switch
        straight back onto it.
        """
        already = cls._fallback_blocked
        cls._fallback_blocked = True
        if cls._fallback_active:
            cls._fallback_active = False
            cls._epoch += 1
            return True
        return not already

    @classmethod
    def reset(cls) -> None:
        """Clear all provider state. Per *run*, not per process."""
        cls._fallback_active = False
        cls._fallback_blocked = False
        cls._fallback_ever_used = False
        cls._epoch += 1

    @classmethod
    def fallback_active(cls) -> bool:
        """True while calls are going to the fallback provider."""
        return cls._fallback_active

    @classmethod
    def fallback_ever_used(cls) -> bool:
        """True if the run ever switched, even if it has since switched back.

        ``RunMetrics.model_fallback_used`` reads this rather than
        :meth:`fallback_active`: a run that failed over and then bounced back off a
        dead fallback is still a **mixed** run, and reporting it as pure-primary
        would silently re-credit the fallback's comments to the primary model.
        """
        return cls._fallback_ever_used

    # ── the bound runnable ──────────────────────────────────────────────────

    @property
    def provider(self) -> Optional[Provider]:
        """The provider this instance's runnable is currently built against."""
        return self._built_provider

    @property
    def runnable(self) -> Runnable:
        """The runnable, rebuilt if the active provider changed since it was built.

        Rebuilding *before* an attempt (not only after our own failure) is what
        keeps a late-constructed or idle agent from spending a request against a
        provider the run has already abandoned.
        """
        if self._runnable is None or self._built_epoch != LLMGateway._epoch:
            provider = self.active_provider(self.model_name)
            model = build_chat_model(
                model=provider.model,
                api_key=provider.api_key,
                base_url=provider.base_url,
                temperature=self.temperature,
                streaming=self.streaming,
                max_tokens=self.max_tokens,
                # function_calling sends a forced tool_choice; decide the reasoning
                # override against the model actually going out, which after a
                # switch is not the one the agent asked for.
                reasoning=structured_reasoning_for(provider.model) if self.structured else None,
            )
            self._runnable = self._build(model)
            self._built_epoch = LLMGateway._epoch
            self._built_provider = provider
        return self._runnable

    # ── calling ─────────────────────────────────────────────────────────────

    async def ainvoke(self, payload: Any, *, config: Optional[RunnableConfig] = None):
        """Invoke the runnable, retrying transient failures and switching providers.

        Retries transient failures after a **random** wait (LLM_RETRY_WAIT_MIN_S ..
        LLM_RETRY_WAIT_MAX_S). Random rather than a fixed exponential ladder because
        the reviewer runs many file reviews concurrently: they collide on the same
        rate limit at the same instant, and a deterministic backoff makes them
        collide again on every retry.

        Re-raises immediately on a non-transient error, and re-raises the last
        error once the attempt budget is spent. Each provider transition resets the
        budget, so the new provider gets a full set of attempts of its own.
        """
        attempt = 0
        switched = False  # used the primary → fallback failover
        reverted = False  # used the fallback → primary revert

        while True:
            attempt += 1
            try:
                return await self.runnable.ainvoke(payload, config=config)
            except Exception as e:  # noqa: BLE001 — classify, then retry or re-raise
                info = parse_llm_error(e)
                budget = retry_attempt_budget(info) if self.retries else 1
                exhausted = attempt >= budget
                on_fallback = self._built_provider is not None and self._built_provider.name == FALLBACK

                if self.retries:
                    # 1. The fallback itself is blocked. Deterministic, so no amount
                    #    of retrying helps — but the primary was merely throttled.
                    if on_fallback and not reverted and (info.is_config_block or info.is_hard_limit):
                        if LLMGateway.block_fallback():
                            print(
                                f"  ↩️  [llm-fallback] {self.label}: the fallback provider is blocked "
                                f"({info.label}) — returning this run to the primary provider "
                                f"and not switching to the fallback again"
                            )
                        reverted = True
                        attempt = 0
                        await asyncio.sleep(retry_wait_seconds())
                        continue

                    # 2. A throttle on the primary that outlived the whole budget is
                    #    what the fallback exists for. `_fallback_active` short-circuits
                    #    the budget: with a batch of reviews in flight they are all being
                    #    throttled at once, so once any one has switched the rest should
                    #    join immediately rather than each proving the primary is busy.
                    if (
                        info.is_rate_limit
                        and not switched
                        and not reverted
                        and not on_fallback
                        and self._fallback_usable()
                        and (exhausted or LLMGateway._fallback_active)
                    ):
                        if LLMGateway.activate_fallback():
                            target = Provider(
                                FALLBACK, get_fallback_model(), fallback_api_key(), fallback_base_url()
                            )
                            print(
                                f"  🔀 [llm-fallback] {self.label}: rate limited ({info.label}) after "
                                f"every attempt on the primary provider — switching the rest of this "
                                f"run to {target.describe()}"
                            )
                        else:
                            print(
                                f"  🔀 [llm-fallback] {self.label}: joining the already-active "
                                f"fallback provider"
                            )
                        switched = True
                        attempt = 0
                        await asyncio.sleep(retry_wait_seconds())
                        continue

                if exhausted or not info.is_retryable or not self.retries:
                    raise

                delay = retry_wait_seconds()
                print(
                    f"  ⏳ [llm-retry] {self.label}: transient failure "
                    f"({info.label}, attempt {attempt}/{budget}) — retrying in {delay:.1f}s: "
                    f"{describe_error(e)[:140]}"
                )
                await asyncio.sleep(delay)

    def invoke(self, payload: Any, *, config: Optional[RunnableConfig] = None):
        """Synchronous escape hatch: one attempt, no retry, no failover.

        Only the agents' unused ``run_sync`` paths reach this. The reviewer itself
        is async end to end and goes through :meth:`ainvoke`.
        """
        return self.runnable.invoke(payload, config=config)
