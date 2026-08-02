"""Parse, classify and retry transient LLM / gateway failures.

The reviewer's LLM traffic egresses through a corporate security proxy (the
``OutboundHTTPProxy`` that the token-provider reverse proxy forwards through on
its way to the model endpoint). Under a burst of concurrent automated requests
that proxy can deny with HTTP 403 ``{"error": "Access denied by security
policy"}``, and the *provider* itself can deny with HTTP 429. Both are
transient — they clear after a cooldown — unlike a daily spend / call-count cap,
which is hard until reset.

So the policy is:

* **Parse** the failure into :class:`LLMErrorInfo` (HTTP status + text) rather
  than pattern-matching a message in isolation. Classification is deliberately
  **provider-agnostic**: HTTP 429 is the rate-limit signal, and the provider's own
  numeric code is captured for logs only — see :func:`parse_llm_error`.
* **Retry** transient failures after a **random** wait (LLM_RETRY_WAIT_MIN_S ..
  LLM_RETRY_WAIT_MAX_S, default 0.5–5s). The wait is random rather than a fixed
  exponential ladder because the reviewer runs many file reviews concurrently:
  they collide on the same rate limit at the same instant, and a deterministic
  backoff makes them collide again on every retry. Independent random waits
  spread the herd out.
* **Fall back** to a second provider when a rate limit survives the whole retry
  budget — see :func:`with_llm_retry` and ``langchain_config``'s fallback latch.
* **Never** retry a hard limit/quota denial or a provider config block; surface
  those immediately so the run fails fast instead of hammering a wall.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from typing import Callable, Optional

from core.config.langchain_config import (
    activate_fallback_provider,
    describe_fallback_provider,
    fallback_configured,
    is_fallback_active,
)

# Hard denials — retrying is futile until a reset; surface immediately.
_HARD_LIMIT_SIGNATURES = (
    "daily limit",
    "limit exceeded",
    "key limit",
    "insufficient credit",
    "insufficient_quota",
    "out of credit",
    "quota",
    "payment required",
)

# Account/model configuration blocks: OpenRouter has no provider that satisfies
# the account's data policy / guardrail for the configured model. Deterministic
# (every request fails identically) and not a code bug — never retry, and surface
# a concise, actionable message instead of a stack trace.
_CONFIG_BLOCK_SIGNATURES = (
    "no endpoints available matching your guardrail",
    "no endpoints found matching your data policy",
    "guardrail restrictions and data policy",
    "no allowed providers",
    "data policy",
)

# Rate limiting / concurrency throttles — transient, and the trigger for the
# fallback provider once the retry budget is spent.
_RATE_LIMIT_SIGNATURES = (
    "too many requests",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "concurrency",
    "concurrent",
    "reduce your concurrency",
    "请求过快",  # "requests too fast" (1305)
    "并发",      # "concurrency" (1302/1303)
)

# Other transient signatures worth retrying.
_TRANSIENT_SIGNATURES = (
    "access denied by security policy",
    "security policy",
    "response validation failed",
    "eof while parsing",
    "forbidden",
    " 403",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "bad gateway",
    "service unavailable",
    " 502",
    " 503",
    " 504",
    # httpx/httpcore transport failures, matched by *type name* because these
    # arrive with an empty message (see _error_text). "timeout" above already
    # covers Read/Write/Connect/PoolTimeout.
    "connecterror",
    "readerror",
    "writeerror",
    "remoteprotocolerror",
    "protocolerror",
    "incompleteread",
)

# Timeout-class failures get a lower attempt cap than other transient errors:
# every attempt costs a *full* OPENROUTER_TIMEOUT_MS window (5 min by default),
# so the usual 3 attempts would spend ~15 min on one file and starve the rest of
# the run's OVERALL_TIMEOUT_S budget. One retry is the useful part — it clears a
# stalled connection — while a genuinely too-slow generation fails in ~10 min.
_TIMEOUT_SIGNATURES = ("timeout", "timed out")
_TIMEOUT_MAX_ATTEMPTS = 2

# "Error code: 429", "status_code=429", "HTTP 503".
_STATUS_RE = re.compile(r"(?:error code|status[ _]?code|status|http)\W{0,3}([45]\d\d)\b", re.I)
# The provider's own code in a JSON body: {"error":{"code":"1302",...}}. Purely
# diagnostic — it makes a log line say "429/1302" instead of "429" — and never an
# input to classification, since the integers mean different things per provider.
# Anchored to four digits so it can't capture the HTTP status from `"code": 429`.
_PROVIDER_CODE_RE = re.compile(r"['\"]code['\"]\s*:\s*['\"]?(\d{4})['\"]?")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _causes(err: BaseException):
    """Yield ``err`` and its ``__cause__`` chain, depth-capped.

    Only ``__cause__`` is followed (explicit ``raise ... from``, which is how
    httpx/httpcore and the OpenAI SDK chain), never ``__context__`` — that would
    drag in whatever unrelated exception happened to be in flight and could
    misread a transient failure as a hard quota block.
    """
    current: Optional[BaseException] = err
    for _ in range(6):  # chains are short, and never trust a cycle
        if current is None:
            return
        yield current
        current = current.__cause__


def _error_text(err: BaseException) -> str:
    """Lowercased text to classify an error by: every exception in its
    ``__cause__`` chain, each contributing its **type name** and its message.

    The type name is not a nicety — for transport errors it is the *only* signal.
    ``httpx`` builds a timeout as ``mapped_exc(str(httpcore_exc))``, and the
    httpcore exception wraps anyio's bare ``TimeoutError``, so what actually
    arrives is ``httpx.ReadTimeout("")``: an **empty** message. Classifying on
    ``str(err)`` alone therefore matched none of the signatures above, and a
    5-minute read timeout — the textbook transient failure this module exists for
    — was treated as non-retryable and re-raised on the first attempt. The name
    "ReadTimeout" contains "timeout"; the message contains nothing.
    """
    parts: list[str] = []
    for exc in _causes(err):
        parts.append(type(exc).__name__)
        parts.append(str(exc))
    return " ".join(parts).lower()


def _status_code(err: BaseException, text: str) -> Optional[int]:
    """HTTP status for the failure, read from the exception before the text.

    The OpenAI SDK's ``APIStatusError`` carries ``.status_code`` (and a
    ``.response``), which is exact; the regex over the message is the fallback
    for errors that only stringify their status (proxies, wrapped errors).
    """
    for exc in _causes(err):
        for candidate in (
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                return candidate
            if isinstance(candidate, str) and candidate.strip().isdigit():
                return int(candidate.strip())
    match = _STATUS_RE.search(text)
    return int(match.group(1)) if match else None


def _provider_code(err: BaseException, text: str) -> Optional[str]:
    """The provider's own error code (e.g. z.ai's ``1302``), if the body carried
    one. Diagnostic only — see :func:`parse_llm_error`; nothing branches on it."""
    for exc in _causes(err):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            raw = (error or {}).get("code") if isinstance(error, dict) else body.get("code")
            if raw is not None and str(raw).strip().isdigit():
                return str(raw).strip()
    match = _PROVIDER_CODE_RE.search(text)
    return match.group(1) if match else None


@dataclass(frozen=True)
class LLMErrorInfo:
    """What a failed LLM call actually was, parsed once and reused by every
    predicate below so the classifications can never disagree with each other."""

    text: str
    status_code: Optional[int]
    provider_code: Optional[str]
    is_rate_limit: bool
    is_hard_limit: bool
    is_config_block: bool
    is_timeout: bool
    is_retryable: bool

    @property
    def label(self) -> str:
        """Short ``429/1302``-style tag for logs."""
        parts = [str(p) for p in (self.status_code, self.provider_code) if p]
        return "/".join(parts) if parts else "unclassified"


def parse_llm_error(err: BaseException) -> LLMErrorInfo:
    """Classify a failed LLM call.

    **HTTP 429 alone decides a rate limit.** The provider's own error code is
    extracted for the log label and never for a decision, because those codes are
    provider-specific and the endpoint behind OPENROUTER_BASE_URL is whatever the
    deployment points at. z.ai's numbering (1302 concurrency, 1303 QPS, 1304 daily
    call cap, 1305 frequency) means nothing on OpenRouter or a vendor API that
    reuses the same integers for something else — reading them universally turns
    another provider's unrelated ``"code": 1304`` into a fabricated hard block.

    Ordering still matters. A rate limit is decided **first** and suppresses the
    hard-limit match, because throttle bodies routinely contain the word "limit"
    (z.ai's 1302 literally says "contact support to increase the limit"). Without
    that precedence a recoverable 429 reads as an exhausted quota: never retried,
    never failed over, and — via ``is_provider_hard_block`` — enough to abort the
    whole run through main.py's circuit breaker. A hard denial that genuinely is
    one arrives as 402/403 with credit/quota wording, which still classifies hard;
    a *429* that means "out of quota" simply costs the retry budget and then fails
    over, which is the right response to it anyway.
    """
    text = _error_text(err)
    status = _status_code(err, text)
    code = _provider_code(err, text)  # label only — never an input below

    is_rate_limit = status == 429 or any(sig in text for sig in _RATE_LIMIT_SIGNATURES)
    is_config_block = any(sig in text for sig in _CONFIG_BLOCK_SIGNATURES)
    is_hard_limit = not is_rate_limit and any(sig in text for sig in _HARD_LIMIT_SIGNATURES)
    is_timeout = any(sig in text for sig in _TIMEOUT_SIGNATURES)

    transient = (
        is_rate_limit
        or (status is not None and status in (408, 502, 503, 504))
        or any(sig in text for sig in _TRANSIENT_SIGNATURES)
    )
    is_retryable = transient and not is_hard_limit and not is_config_block

    return LLMErrorInfo(
        text=text,
        status_code=status,
        provider_code=code,
        is_rate_limit=is_rate_limit,
        is_hard_limit=is_hard_limit,
        is_config_block=is_config_block,
        is_timeout=is_timeout,
        is_retryable=is_retryable,
    )


def is_rate_limit_error(err: BaseException) -> bool:
    """True for throttles — any HTTP 429, whoever the provider is — plus the
    textual variants for endpoints that don't surface a clean status. Retryable,
    and the trigger for switching to the fallback provider."""
    return parse_llm_error(err).is_rate_limit


def is_hard_limit_error(err: BaseException) -> bool:
    """True for daily-limit / quota / payment denials — never retry these."""
    return parse_llm_error(err).is_hard_limit


def is_provider_config_error(err: BaseException) -> bool:
    """True for OpenRouter data-policy / guardrail 'no matching endpoints' blocks —
    an account/model configuration issue, deterministic and not a code bug."""
    return parse_llm_error(err).is_config_block


def is_timeout_error(err: BaseException) -> bool:
    """True for timeout-class failures, which retry on a tighter attempt cap
    (each attempt burns a full request-timeout window)."""
    return parse_llm_error(err).is_timeout


def describe_error(err: BaseException) -> str:
    """``TypeName: message`` for logging, or bare ``TypeName`` when the message is
    empty — which it is for every httpx transport error (see :func:`_error_text`).
    Without this, a read timeout logs as ``review failed after 301.06s:`` with
    nothing after the colon, and the run log says only that *something* failed."""
    msg = str(err).strip()
    name = type(err).__name__
    return f"{name}: {msg}" if msg else name


def is_provider_hard_block(err: BaseException) -> bool:
    """Account-level hard block (data-policy/guardrail OR limit/quota): every
    request will fail identically, so the run should stop, not grind through
    every file. A rate limit is explicitly *not* one of these — it clears."""
    info = parse_llm_error(err)
    return info.is_hard_limit or info.is_config_block


def is_retryable_llm_error(err: BaseException) -> bool:
    """Retryable iff the error looks transient AND is not a hard limit/quota
    denial or a config block."""
    return parse_llm_error(err).is_retryable


def maybe_print_traceback(exc: BaseException, *, indent: str = "     ") -> None:
    """Print a traceback for an *unexpected* error, but replace it with a concise,
    actionable one-liner for known provider-side conditions (data-policy/guardrail,
    limit/quota, rate limit) — those are not code bugs, so the stack is noise.
    Must be called from within the ``except`` block handling ``exc``."""
    info = parse_llm_error(exc)
    if info.is_config_block:
        print(
            f"{indent}↳ OpenRouter has no provider matching your data policy/guardrail for the "
            f"configured model. Fix at openrouter.ai/settings/privacy, or change DEFAULT_LLM_MODEL "
            f"/ OPENROUTER_PROVIDER. (not a code error)"
        )
        return
    if info.is_hard_limit:
        print(f"{indent}↳ Provider hard limit/quota reached — not a code error; retry after reset or raise the limit.")
        return
    if info.is_rate_limit:
        print(
            f"{indent}↳ Provider rate/concurrency limit ({info.label}) survived every retry"
            f"{'' if fallback_configured() else ' and no fallback provider is configured'}"
            f" — not a code error; lower FILE_BATCH_SIZE or set FALLBACK_LLM_MODEL."
        )
        return
    import traceback
    print(f"{indent}Traceback: {traceback.format_exc()}")


def retry_wait_seconds() -> float:
    """A random wait in [LLM_RETRY_WAIT_MIN_S, LLM_RETRY_WAIT_MAX_S] (0.5–5s).

    Random, not exponential: the reviewer runs several file reviews (and, for
    service files, two sub-agents each) concurrently, so they hit a concurrency
    limit simultaneously. A deterministic backoff re-synchronises them on every
    attempt; independent random draws spread the retries out.
    """
    low = max(0.0, _float_env("LLM_RETRY_WAIT_MIN_S", 0.5))
    high = _float_env("LLM_RETRY_WAIT_MAX_S", 5.0)
    if high < low:
        high = low
    return random.uniform(low, high)


def _switch_to_fallback(label: str, info: LLMErrorInfo) -> bool:
    """Flip the process-wide fallback latch. Returns False when no fallback is
    configured, in which case the caller keeps its normal retry/raise behavior.

    The latch is global and sticky on purpose: with several file reviews in
    flight, a per-call switch would make every other agent spend its own full
    retry budget against a provider already known to be throttling us.
    """
    if not fallback_configured():
        return False
    if activate_fallback_provider():
        print(
            f"  🔀 [llm-fallback] {label}: rate limited ({info.label}) after every attempt on the "
            f"primary provider — switching the rest of this run to {describe_fallback_provider()}"
        )
    else:
        print(f"  🔀 [llm-fallback] {label}: rebuilding on the already-active fallback provider")
    return True


async def with_llm_retry(
    operation: Callable[[], object],
    *,
    label: str = "LLM call",
    on_fallback: Optional[Callable[[], None]] = None,
):
    """Await ``operation()`` (a zero-arg coroutine factory), retrying transient
    failures after a random wait, and failing over to the fallback provider when
    a rate limit outlives the retry budget.

    ``operation`` must return a *fresh* awaitable each call (e.g.
    ``lambda: chain.ainvoke(...)``) so each retry issues a new request.

    ``on_fallback`` rebuilds the caller's model-bound runnable — the chain or
    agent executor was constructed against the primary provider, so it has to be
    reconstructed for the swap to take effect. The retry budget resets after a
    switch, giving the fallback a full set of attempts of its own. Call sites
    that pass nothing simply never fail over.

    Re-raises immediately on a hard limit/quota denial, a provider config block,
    or any non-transient error, and re-raises the last error after the final
    attempt. Timeout-class failures are capped at ``_TIMEOUT_MAX_ATTEMPTS``
    regardless of LLM_RETRY_ATTEMPTS, since each of their attempts costs a full
    request-timeout window rather than failing fast.

    Env knobs: LLM_RETRY_ATTEMPTS (default 3), LLM_RETRY_WAIT_MIN_S (0.5),
    LLM_RETRY_WAIT_MAX_S (5).
    """
    attempts = max(1, _int_env("LLM_RETRY_ATTEMPTS", 3))
    switched = False
    attempt = 0

    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as e:  # noqa: BLE001 — classify then re-raise
            info = parse_llm_error(e)
            budget = min(attempts, _TIMEOUT_MAX_ATTEMPTS) if info.is_timeout else attempts
            exhausted = attempt >= budget

            # A throttle that outlived the whole budget is what the fallback
            # provider exists for. Only rate limits trigger it: a timeout or a
            # gateway blip says nothing about the provider being over capacity.
            #
            # ``is_fallback_active()`` short-circuits the budget: with a batch of
            # file reviews in flight they are all being throttled at once, so once
            # any one of them has switched, the rest should join immediately
            # rather than each spending three more attempts proving the primary is
            # still busy. Their runnables are still bound to the primary client,
            # so joining means rebuilding — the latch alone changes nothing.
            if info.is_rate_limit and not switched and on_fallback is not None:
                if (exhausted or is_fallback_active()) and _switch_to_fallback(label, info):
                    on_fallback()
                    switched = True
                    attempt = 0
                    await asyncio.sleep(retry_wait_seconds())
                    continue

            if exhausted or not info.is_retryable:
                raise

            delay = retry_wait_seconds()
            print(
                f"  ⏳ [llm-retry] {label}: transient failure "
                f"({info.label}, attempt {attempt}/{budget}) — retrying in {delay:.1f}s: "
                f"{describe_error(e)[:140]}"
            )
            await asyncio.sleep(delay)
