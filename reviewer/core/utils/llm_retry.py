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
  budget, and back again if that provider turns out to be blocked.
* **Never** retry a hard limit/quota denial or a provider config block; surface
  those immediately so the run fails fast instead of hammering a wall.

This module is the **policy**: how to classify a failure, how many attempts it
deserves, how long to wait. Executing that policy — the loop, the provider
switches, rebuilding the caller's model-bound runnable — belongs to
:class:`core.llm.gateway.LLMGateway`, the one object every LLM call goes through.
The split keeps this module free of state and of any import of the gateway, so
either can be read on its own.
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from typing import Any, Optional

from core.config.langchain_config import fallback_configured

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
    # A response body that could not be parsed. "eof while parsing" above covers
    # the serde spelling; these are the stdlib/SDK ones. Observed on a live run:
    # `JSONDecodeError: Expecting value: line 5 column 1 (char 22)` from the
    # fallback endpoint — a **200** carrying something that was not JSON (a relay
    # emitting SSE keep-alive lines into a non-streaming response is the usual
    # cause). Matched by type name as well as message, since a bare
    # `json.JSONDecodeError` re-raised without a response object has no status at
    # all and would otherwise classify as unclassified.
    "jsondecodeerror",
    "expecting value",
    "unterminated string",
    "invalid json",
    # A structured call that came back with no tool call at all — the gateway raises
    # `StructuredOutputMissing` for it, and this is what gives that a retry. Measured
    # as intermittent (1 in 3 on deepseek-v4-flash at effort=xhigh), which is the
    # definition of worth retrying; matched by type name, since the message is ours.
    "structuredoutputmissing",
    # A model call with no content and no tool calls. In an agent loop that reads as
    # "finished", so the reviewer ends having read and recorded nothing — silently.
    # Measured intermittent on deepseek-v4-flash at every effort level, including
    # none, so it is the model or the relay dropping a completion, not a setting.
    "emptymodelresponse",
)

# Timeout-class failures get a lower attempt cap than other transient errors:
# every attempt costs a *full* LLM_TIMEOUT_MS window (5 min by default),
# so the usual 3 attempts would spend ~15 min on one file and starve the rest of
# the run's OVERALL_TIMEOUT_S budget. One retry is the useful part — it clears a
# stalled connection — while a genuinely too-slow generation fails in ~10 min.
_TIMEOUT_SIGNATURES = ("timeout", "timed out")
_TIMEOUT_MAX_ATTEMPTS = 2

# The opposite case: a failure so cheap that the usual budget is *too small*. An
# empty completion (see gateway.EmptyModelResponse) burns no output tokens and comes
# back in a second or two, and retrying it is the only thing that turns it into a
# review — while giving up loses the whole file. Observed on a live run:
# openai/gpt-5.6-luna produced three empties in a row for one reviewer, exhausted the
# default budget of 3, and that file failed; the same run recovered on the first or
# second retry everywhere else. So this class gets its own, larger cap, for the same
# reason timeouts get a smaller one — the cost per attempt is what should set it.
_EMPTY_RESPONSE_SIGNATURE = "emptymodelresponse"
_EMPTY_RESPONSE_MAX_ATTEMPTS = 6

# Statuses that *are* a timeout, whatever the body says. A gateway that gave up
# waiting for the model reports it as a status and nothing else: the message is
# "Error code: 524" — no "timeout", no "timed out", nothing any signature above
# matches. Cloudflare 524 ("a timeout occurred") is what the reverse proxy in
# front of the model endpoint returns when the origin stops answering, and 522
# is its connect-side twin; 504 and 408 are the standard spellings. Classifying
# them here rather than only as generic 5xx is what keeps the tighter attempt
# cap: these failures are the expensive kind, paid for in whole request windows.
# (It moves 504 from 3 attempts to 2 — correct for the same reason.)
_TIMEOUT_STATUSES = frozenset({408, 504, 522, 524})

# "Error code: 429", "status_code=429", "HTTP 503".
_STATUS_RE = re.compile(r"(?:error code|status[ _]?code|status|http)\W{0,3}([45]\d\d)\b", re.I)
# The provider's own code in a JSON body: {"error":{"code":"1302",...}}. Purely
# diagnostic — it makes a log line say "429/1302" instead of "429" — and never an
# input to classification, since the integers mean different things per provider.
# Anchored to four digits so it can't capture the HTTP status from `"code": 429`.
_PROVIDER_CODE_RE = re.compile(r"['\"]code['\"]\s*:\s*['\"]?(\d{4})['\"]?")
# The other half of that same line: a body-carried code that IS an HTTP status,
# for a response whose status lives only in the envelope (see _envelope_status).
# The two regexes partition the space by range — 4xx/5xx is a status, four digits
# is a provider code — so neither can claim the other's number. The lookahead
# stops `4001` from matching as `400`.
_ENVELOPE_STATUS_RE = re.compile(r"['\"]code['\"]\s*:\s*['\"]?([45]\d\d)(?!\d)")


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


def _as_http_status(raw: Any) -> Optional[int]:
    """Coerce ``raw`` to an HTTP status, or None if it isn't one.

    The range check is the whole point and is applied **after** coercion: it is
    what keeps a provider's own code (z.ai's ``1302``) from being read as a
    status, which is the invariant :func:`parse_llm_error` rests on.
    """
    if isinstance(raw, bool):  # bool is an int subclass; never a status
        return None
    if isinstance(raw, int):
        return raw if 400 <= raw <= 599 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if 400 <= value <= 599 else None
    return None


def _envelope_status(err: BaseException) -> Optional[int]:
    """The HTTP status carried *inside* an error body, for failures that have no
    status anywhere else.

    Some endpoints answer **HTTP 200** with an error envelope in the body rather
    than a real status — OpenRouter does this when it relays an upstream
    provider's failure. ``langchain_openai`` turns that into
    ``raise ValueError(response_dict.get("error"))``: a bare ``ValueError`` whose
    only argument is a dict. It has no ``.status_code``, no ``.response``, and its
    text says nothing a signature matches, so the throttle was invisible —
    ``ValueError({'message': 'Provider returned error', 'code': 429})`` classified
    as *unclassified*, which means one attempt, no retry, and (because the
    failover gate reads ``is_rate_limit``) **no failover either**.

    Reading the envelope's ``code`` as a status does not weaken the
    provider-agnostic rule, because of the range check in ``_as_http_status``: a
    code inside 400–599 is the OpenAI convention's mirror of the HTTP status,
    while a provider's own numbering (1302, 1304) falls outside it and stays a
    label-only diagnostic exactly as before.
    """
    for exc in _causes(err):
        bodies = [getattr(exc, "body", None), getattr(exc, "json_body", None)]
        args = getattr(exc, "args", None) or ()
        if args:
            bodies.append(args[0])
        for body in bodies:
            if not isinstance(body, dict):
                continue
            nested = body.get("error")
            for raw in (body.get("code"), nested.get("code") if isinstance(nested, dict) else None):
                status = _as_http_status(raw)
                if status is not None:
                    return status
    return None


def _status_code(err: BaseException, text: str) -> Optional[int]:
    """HTTP status for the failure, read from the exception before the text.

    The OpenAI SDK's ``APIStatusError`` carries ``.status_code`` (and a
    ``.response``), which is exact; the regex over the message is the fallback
    for errors that only stringify their status (proxies, wrapped errors), and
    the error *envelope* is the last resort for a body-only status (see
    :func:`_envelope_status`).
    """
    for exc in _causes(err):
        for candidate in (
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ):
            if isinstance(candidate, int) and 100 <= candidate <= 599:
                return candidate
            # Same range check on the string form. It was the one extraction path
            # without one (`_STATUS_RE` matches only [45]\d\d, `_envelope_status`
            # goes through `_as_http_status`), which was inert while transience
            # was a fixed tuple of statuses and stopped being inert the moment it
            # became "any 5xx": a `status_code="1302"` on some wrapper would then
            # read as a retryable server error.
            if isinstance(candidate, str) and candidate.strip().isdigit():
                value = int(candidate.strip())
                if 100 <= value <= 599:
                    return value
    match = _STATUS_RE.search(text)
    if match:
        return int(match.group(1))
    envelope = _envelope_status(err)
    if envelope is not None:
        return envelope
    # Same envelope, but the exception was re-wrapped and only its *text*
    # survived. The negative lookahead is load-bearing: without it a four-digit
    # provider code like 4001 matches on its first three digits and fabricates an
    # HTTP 400 out of something that was never a status.
    match = _ENVELOPE_STATUS_RE.search(text)
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
        """Short ``429/1302``-style tag for logs.

        The provider code is dropped when it merely echoes the HTTP status (some
        bodies carry ``{"code": 404}``), which would render an uninformative
        ``404/404``.
        """
        parts = [str(self.status_code)] if self.status_code else []
        if self.provider_code and str(self.provider_code) != str(self.status_code):
            parts.append(str(self.provider_code))
        if parts:
            return "/".join(parts)
        # A client-side timeout has no status and no provider code, so it used to
        # log as "unclassified" — which reads as "we don't know what this was"
        # next to a retry line that in fact knew exactly what it was and had
        # already cut the attempt budget for it.
        return "timeout" if self.is_timeout else "unclassified"


def parse_llm_error(err: BaseException) -> LLMErrorInfo:
    """Classify a failed LLM call.

    **HTTP 429 alone decides a rate limit.** The provider's own error code is
    extracted for the log label and never for a decision, because those codes are
    provider-specific and the endpoint behind DEFAULT_LLM_BASE_URL is whatever the
    deployment points at. z.ai's numbering (1302 concurrency, 1303 QPS, 1304 daily
    call cap, 1305 frequency) means nothing on OpenRouter or a vendor API that
    reuses the same integers for something else — reading them universally turns
    another provider's unrelated ``"code": 1304`` into a fabricated hard block.

    That rule is about *provider* codes, and the boundary is numeric: a ``code``
    inside 400–599 in an error body is the OpenAI convention's mirror of the HTTP
    status and **is** read as one (:func:`_envelope_status`), because some
    endpoints answer HTTP 200 with the real status only in the envelope; anything
    outside that range is a provider code and stays label-only. Without that, a
    relayed throttle arriving as ``ValueError({'message': 'Provider returned
    error', 'code': 429})`` classified as *unclassified* — one attempt, no retry,
    and no failover, since the failover gate reads ``is_rate_limit``.

    Ordering still matters. A rate limit is decided **first** and suppresses the
    hard-limit match, because throttle bodies routinely contain the word "limit"
    (z.ai's 1302 literally says "contact support to increase the limit"). Without
    that precedence a recoverable 429 reads as an exhausted quota: never retried,
    never failed over, and — via ``is_provider_hard_block`` — enough to abort the
    whole run through main.py's circuit breaker. A hard denial that genuinely is
    one arrives as 402/403 with credit/quota wording, which still classifies hard;
    a *429* that means "out of quota" simply costs the retry budget and then fails
    over, which is the right response to it anyway.

    **Transience is decided by range too: any 5xx is a server-side failure.** The
    check used to enumerate ``(408, 502, 503, 504)``, which is the standard list
    and not the list an endpoint behind a reverse proxy actually emits — a
    Cloudflare **524** ("a timeout occurred", the edge giving up on the origin)
    fell outside it, carried no text any signature matched, and so classified as
    *not retryable*: one attempt, then the file review died. Observed killing two
    files of a run on ``glm-5.2``, one of them after 1109s and the other after
    2280s of work, and the run posted nothing. Enumerating 52x would only defer
    the next gap; a 5xx is by definition the server's failure, never a request
    this client can fix, and the two conditions that must never be retried —
    a hard limit/quota denial and a provider config block — are decided from the
    body text below and suppress the retry whatever the status was.
    """
    text = _error_text(err)
    status = _status_code(err, text)
    code = _provider_code(err, text)  # label only — never an input below

    is_rate_limit = status == 429 or any(sig in text for sig in _RATE_LIMIT_SIGNATURES)
    is_config_block = any(sig in text for sig in _CONFIG_BLOCK_SIGNATURES)
    is_hard_limit = not is_rate_limit and any(sig in text for sig in _HARD_LIMIT_SIGNATURES)
    is_timeout = any(sig in text for sig in _TIMEOUT_SIGNATURES) or status in _TIMEOUT_STATUSES

    transient = (
        is_rate_limit
        or (status is not None and (status >= 500 or status == 408))
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
        # Don't advise setting FALLBACK_LLM_MODEL when one *is* set — it was the
        # standing advice on every throttled file of a run whose fallback had
        # already been taken away, which read as "you never configured one".
        # Whether the fallback survived is the gateway's business and is reported
        # in its own log lines and in the run event's warnings.
        remedy = (
            "lower FILE_BATCH_SIZE (a fallback provider is configured; check the "
            "[llm-fallback] lines above for whether it was still available)"
            if fallback_configured()
            else "lower FILE_BATCH_SIZE or set FALLBACK_LLM_MODEL"
        )
        print(
            f"{indent}↳ Provider rate/concurrency limit ({info.label}) survived every retry "
            f"— not a code error; {remedy}."
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


def retry_attempt_budget(info: LLMErrorInfo) -> int:
    """How many attempts a failure of this class deserves.

    LLM_RETRY_ATTEMPTS (default 3) for most transient failures, with a cap either
    way where the *cost per attempt* argues for one. Timeout-class failures are
    capped at ``_TIMEOUT_MAX_ATTEMPTS``, because each attempt costs a full
    LLM_TIMEOUT_MS window and three would spend ~15 min on one file. An empty
    completion gets a *larger* budget (``_EMPTY_RESPONSE_MAX_ATTEMPTS``) for the
    mirror-image reason: it costs no output tokens and returns in seconds, and giving
    up on it loses the file.

    The budget is per *provider* — :class:`core.llm.gateway.LLMGateway` resets the
    attempt count on a provider switch, so the new endpoint gets a full set of its
    own.
    """
    attempts = max(1, _int_env("LLM_RETRY_ATTEMPTS", 3))
    if info.is_timeout:
        return min(attempts, _TIMEOUT_MAX_ATTEMPTS)
    if _EMPTY_RESPONSE_SIGNATURE in info.text:
        # Cheap to retry and fatal to the file if we don't — see the constant above.
        return max(attempts, _EMPTY_RESPONSE_MAX_ATTEMPTS)
    return attempts
