"""Retry transient LLM / gateway failures with backoff.

The reviewer's LLM traffic egresses through a corporate security proxy (the
``OutboundHTTPProxy`` that the token-provider reverse proxy forwards through on
its way to openrouter.ai). Under a burst of concurrent automated requests that
proxy can deny with HTTP 403 ``{"error": "Access denied by security policy"}``.
That block is **transient** — it clears after a short cooldown — unlike a
daily spend / key-quota cap, which is hard until the daily reset.

So the policy here is: retry transient failures (security-policy 403s, malformed
or truncated upstream bodies, 429s, momentary gateway/network errors) with
exponential backoff + jitter, but **never** retry a hard limit/quota denial —
surface those immediately so the run fails fast instead of hammering a wall.

Errors are matched on their string form so we don't couple to the OpenRouter
SDK's exception classes (the security-policy 403 currently surfaces as a
``ResponseValidationError`` because its body isn't the shape the SDK expects).
"""

import asyncio
import os
import random

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

# Transient signatures worth retrying.
_TRANSIENT_SIGNATURES = (
    "access denied by security policy",
    "security policy",
    "response validation failed",
    "eof while parsing",
    "forbidden",
    " 403",
    "429",
    "too many requests",
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
)


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


def is_hard_limit_error(err: BaseException) -> bool:
    """True for daily-limit / quota / payment denials — never retry these."""
    msg = str(err).lower()
    return any(sig in msg for sig in _HARD_LIMIT_SIGNATURES)


def is_provider_config_error(err: BaseException) -> bool:
    """True for OpenRouter data-policy / guardrail 'no matching endpoints' blocks —
    an account/model configuration issue, deterministic and not a code bug."""
    msg = str(err).lower()
    return any(sig in msg for sig in _CONFIG_BLOCK_SIGNATURES)


def is_provider_hard_block(err: BaseException) -> bool:
    """Account-level hard block (data-policy/guardrail OR limit/quota): every
    request will fail identically, so the run should stop, not grind through
    every file."""
    return is_hard_limit_error(err) or is_provider_config_error(err)


def is_retryable_llm_error(err: BaseException) -> bool:
    """Retryable iff the error looks transient AND is not a hard limit/quota
    denial or a config block. Hard blocks win, so a quota/guardrail error is
    never treated as transient."""
    if is_hard_limit_error(err) or is_provider_config_error(err):
        return False
    msg = str(err).lower()
    return any(sig in msg for sig in _TRANSIENT_SIGNATURES)


def maybe_print_traceback(exc: BaseException, *, indent: str = "     ") -> None:
    """Print a traceback for an *unexpected* error, but replace it with a concise,
    actionable one-liner for known account-level blocks (data-policy/guardrail,
    limit/quota) — those are configuration issues, not code bugs, so the stack is
    noise. Must be called from within the ``except`` block handling ``exc``."""
    if is_provider_config_error(exc):
        print(
            f"{indent}↳ OpenRouter has no provider matching your data policy/guardrail for the "
            f"configured model. Fix at openrouter.ai/settings/privacy, or change DEFAULT_LLM_MODEL "
            f"/ OPENROUTER_PROVIDER. (not a code error)"
        )
        return
    if is_hard_limit_error(exc):
        print(f"{indent}↳ Provider hard limit/quota reached — not a code error; retry after reset or raise the limit.")
        return
    import traceback
    print(f"{indent}Traceback: {traceback.format_exc()}")


async def with_llm_retry(operation, *, label: str = "LLM call"):
    """Await ``operation()`` (a zero-arg coroutine factory), retrying transient
    failures with exponential backoff + jitter.

    Re-raises immediately on a hard limit/quota denial or any non-transient
    error, and re-raises the last error after the final attempt. ``operation``
    must return a *fresh* awaitable each call (e.g. ``lambda: x.ainvoke(...)``)
    so each retry issues a new request.

    Env knobs: LLM_RETRY_ATTEMPTS (default 3), LLM_RETRY_BASE_DELAY_S (default 3).
    """
    attempts = max(1, _int_env("LLM_RETRY_ATTEMPTS", 3))
    base_delay = max(0.0, _float_env("LLM_RETRY_BASE_DELAY_S", 3.0))

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as e:  # noqa: BLE001 — classify then re-raise
            if attempt >= attempts or not is_retryable_llm_error(e):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay)
            print(
                f"  ⏳ [llm-retry] {label}: transient failure "
                f"(attempt {attempt}/{attempts}) — retrying in {delay:.1f}s: {str(e)[:140]}"
            )
            await asyncio.sleep(delay)
