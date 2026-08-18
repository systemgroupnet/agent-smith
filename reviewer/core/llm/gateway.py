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
   only ever throttled, and a throttle clears.

The second transition also marks the fallback unusable, so no other agent
switches onto a known-dead endpoint — but **how much** of it is unusable depends
on what answered. A quota/credit denial kills the endpoint outright. A
data-policy / guardrail block on a **structured** call does not: ``with_structured_output``
sends a *forced* ``tool_choice``, which OpenRouter routes to a narrower set of
providers than the agent loop's ``tool_choice: auto``, so a 404 there is a
statement about the call *shape*, not the endpoint. Retiring the whole fallback
for it throws away a provider that is still serving every loop call — which is
exactly what happened in production (job 3741223): the fallback took over on a
1302 throttle, completed two file reviews, and was then retired for the whole run
by a single salvage call, after which the remaining batches went back to the
throttled primary and died. So that block is scoped: structured calls return to
the primary, the agent loop keeps the fallback.

Every transition is recorded (:meth:`transitions`) and drained into the run
event's ``warnings`` by ``process_mr``. Losing the escape hatch mid-run is the
most consequential thing that can happen to a run's outcome, and it used to be
visible only as one line in a CI job log — a run whose revert was then absorbed
by a successful primary retry recorded *nothing at all*.
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


@dataclass(frozen=True)
class ProviderTransition:
    """One provider change, kept so the run event can report it.

    ``kind`` is the stage name used in the analytics warning, ``label`` is the
    call that triggered it, ``detail`` says which provider answered with what.
    """

    kind: str  # llm_fallback_activated | llm_fallback_blocked | llm_fallback_blocked_structured
    label: str
    detail: str

    def describe(self) -> str:
        return f"{self.label}: {self.detail}" if self.detail else self.label


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
    _fallback_blocked: bool = False  # the fallback is unusable for everything
    _fallback_blocked_structured: bool = False  # ...only for forced-tool_choice calls
    _fallback_ever_used: bool = False  # for analytics; survives a revert
    _epoch: int = 0
    _transitions: tuple = ()  # ProviderTransition records, drained into the run event

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
        pin: Optional[str] = None,
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
            pin: PRIMARY or FALLBACK to bind this gateway to one endpoint regardless
                 of the latch. Only the pre-flight uses it, to ask "can the fallback
                 serve?" without committing the run to it. It exists so that probe
                 still builds its client *here* — a chat model constructed anywhere
                 else is the one thing this layering cannot tolerate.
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
        self._pin = pin

        self._runnable: Optional[Runnable] = None
        self._built_epoch: int = -1
        self._built_provider: Optional[Provider] = None

    # ── provider resolution ─────────────────────────────────────────────────

    @classmethod
    def _fallback_usable(cls, structured: bool = False) -> bool:
        """Configured *and* not already proven dead this run — for this call shape.

        ``structured`` calls send a forced ``tool_choice`` and are routed more
        narrowly, so they can be blocked while the agent loop is not.
        """
        if not fallback_configured() or cls._fallback_blocked:
            return False
        return not (structured and cls._fallback_blocked_structured)

    @classmethod
    def fallback_provider(cls) -> Provider:
        """The fallback endpoint, whether or not it is currently in force."""
        return Provider(FALLBACK, get_fallback_model(), fallback_api_key(), fallback_base_url())

    @classmethod
    def active_provider(cls, requested: Optional[str] = None, *, structured: bool = False) -> Provider:
        """The provider currently in force **for a call of this shape**.

        With the fallback blocked for structured calls only, two gateways resolve
        differently at the same instant — the reviewers' loops stay on the fallback
        while a salvage call goes to the primary. That asymmetry is the point.
        """
        if cls._fallback_active and cls._fallback_usable(structured):
            return cls.fallback_provider()
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
    def _record(cls, kind: str, label: str, detail: str) -> None:
        """Append a transition record, bounded — a run has a handful at most."""
        if len(cls._transitions) < 20:
            cls._transitions = (*cls._transitions, ProviderTransition(kind, label, detail))

    @classmethod
    def transitions(cls) -> tuple:
        """Every provider change this run made, oldest first.

        ``process_mr`` drains this into ``RunMetrics.warnings``. Without it a run
        that lost its fallback recorded nothing: the revert is *absorbed* when the
        retry on the primary then succeeds, so there is no error event either.
        """
        return cls._transitions

    @classmethod
    def activate_fallback(cls, *, label: str = "", detail: str = "") -> bool:
        """Switch the run to the fallback provider. True if this call flipped it."""
        if cls._fallback_active or not cls._fallback_usable():
            return False
        cls._fallback_active = True
        cls._fallback_ever_used = True
        cls._epoch += 1
        cls._record("llm_fallback_activated", label, detail or cls.fallback_provider().describe())
        return True

    @classmethod
    def block_fallback(cls, *, structured_only: bool = False, label: str = "", detail: str = "") -> bool:
        """Mark the fallback unusable and return the affected calls to the primary.

        Called when the fallback answers with a deterministic block: it will answer
        every request of that kind identically, so both the switch back *and* the
        poison flag matter — without the flag the next agent to be throttled would
        switch straight back onto it.

        ``structured_only`` scopes the block to forced-``tool_choice`` calls. The
        latch stays *on*, because the endpoint is still serving the agent loop; only
        structured gateways re-resolve to the primary. The epoch bump is what makes
        them do so before their next attempt.

        Returns True when this call newly established the block (so the caller logs
        it once).
        """
        if structured_only:
            if cls._fallback_blocked or cls._fallback_blocked_structured:
                return False
            cls._fallback_blocked_structured = True
            cls._epoch += 1
            cls._record("llm_fallback_blocked_structured", label, detail)
            return True

        already = cls._fallback_blocked
        cls._fallback_blocked = True
        if cls._fallback_active:
            cls._fallback_active = False
            cls._epoch += 1
            cls._record("llm_fallback_blocked", label, detail)
            return True
        if already:
            return False
        cls._record("llm_fallback_blocked", label, detail)
        return True

    @classmethod
    def reset(cls) -> None:
        """Clear all provider state. Per *run*, not per process."""
        cls._fallback_active = False
        cls._fallback_blocked = False
        cls._fallback_blocked_structured = False
        cls._fallback_ever_used = False
        cls._transitions = ()
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

    def _where(self) -> str:
        """``primary glm-5.2`` — which endpoint a log line is talking about.

        Every retry and give-up line carries this. Without it a job log full of
        ``429/1302`` says nothing about *whose* limit was hit, and diagnosing a run
        that lost its fallback meant querying the analytics database instead of
        reading the log.
        """
        provider = self._built_provider
        return f"{provider.name} {provider.model}" if provider else "unbuilt"

    @property
    def runnable(self) -> Runnable:
        """The runnable, rebuilt if the active provider changed since it was built.

        Rebuilding *before* an attempt (not only after our own failure) is what
        keeps a late-constructed or idle agent from spending a request against a
        provider the run has already abandoned.
        """
        if self._runnable is None or self._built_epoch != LLMGateway._epoch:
            provider = (
                self.fallback_provider() if self._pin == FALLBACK
                else self.active_provider(self.model_name, structured=self.structured)
            )
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
                    # 1. The fallback itself cannot serve this call — either because
                    #    the block is deterministic (no amount of retrying helps) or
                    #    because it has now failed for its whole attempt budget on
                    #    something that is *not* a throttle. Either way the primary
                    #    was merely rate limited, and a throttle clears.
                    #
                    #    The exhaustion arm is what turns "this file is lost" into
                    #    "this file runs on the throttled provider that still works".
                    #    Observed on a production project MR 1180: the primary hit its
                    #    concurrency limit (429/1302), the run switched, and the
                    #    fallback answered **HTTP 200 with a body that was not JSON**
                    #    (`JSONDecodeError: Expecting value`) — so all four files died
                    #    on an endpoint the run had no way to leave, and nothing was
                    #    posted. A rate limit on the fallback is deliberately excluded:
                    #    if it is throttled the primary is too, so bouncing back is
                    #    churn rather than recovery.
                    if on_fallback and not reverted and (
                        info.is_config_block
                        or info.is_hard_limit
                        or (exhausted and not info.is_rate_limit)
                    ):
                        # How much of the fallback this condemns depends on what it
                        # was. A quota/credit denial is about the account, so the
                        # endpoint is gone. A data-policy/guardrail 404 on a
                        # *structured* call is about the forced tool_choice being
                        # routed more narrowly than the agent loop's auto — the same
                        # endpoint keeps serving every loop call, and retiring it
                        # wholesale sends the whole run back to the throttled primary.
                        structured_only = (
                            info.is_config_block and self.structured and not info.is_hard_limit
                        )
                        detail = f"{self._where()} answered {info.label}"
                        if structured_only:
                            if LLMGateway.block_fallback(
                                structured_only=True, label=self.label, detail=detail
                            ):
                                print(
                                    f"  ↩️  [llm-fallback] {self.label}: the fallback provider cannot "
                                    f"serve a forced tool_choice ({info.label}) — structured calls "
                                    f"return to the primary provider; the agent loop keeps using it"
                                )
                        elif LLMGateway.block_fallback(label=self.label, detail=detail):
                            why = (
                                f"failed every one of {attempt} attempt(s)"
                                if not (info.is_config_block or info.is_hard_limit)
                                else "is blocked"
                            )
                            print(
                                f"  ↩️  [llm-fallback] {self.label}: the fallback provider {why} "
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
                        and self._fallback_usable(self.structured)
                        and (exhausted or LLMGateway._fallback_active)
                    ):
                        detail = f"{self._where()} answered {info.label}"
                        if LLMGateway.activate_fallback(label=self.label, detail=detail):
                            print(
                                f"  🔀 [llm-fallback] {self.label}: rate limited ({info.label}) after "
                                f"every attempt on {self._where()} — switching the rest of this "
                                f"run to {LLMGateway.fallback_provider().describe()}"
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
                    if self.retries:
                        # Name the endpoint that gave up. The caller's own error line
                        # carries the exception but not the provider, and with a
                        # fallback in play "429" alone doesn't say whose limit it was.
                        print(
                            f"  ⛔ [llm] {self.label}: giving up on {self._where()} after "
                            f"{attempt} attempt(s) ({info.label})"
                        )
                    raise

                delay = retry_wait_seconds()
                print(
                    f"  ⏳ [llm-retry] {self.label}: transient failure on {self._where()} "
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
