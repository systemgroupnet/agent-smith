"""The review pass: one specialist agent per changed file, over a real checkout.

Each ``(file, reviewer)`` pair in the dispatch plan becomes one agent invocation
with its own message history, drawn from a queue by `FILE_BATCH_SIZE` workers so
that many are in flight at every instant. The nine reviewers and their prompts are
unchanged from the LangGraph era; what changed is where they get the code — a
checkout on disk rather than inlined text — and that the fan-out is a worker pool.

**Why every reviewer reads a checkout.** The two previous sources of code — the
GitLab REST API and a semantic code-index MCP — both answered about *a ref you have
to name*, and naming it wrong was silent: the index in particular answered from its
own base branch whenever `branch` was omitted, so a symbol the merge request added
looked undefined. A checkout has no ref to get wrong, which finally makes "is this
symbol defined anywhere?"
answerable — the question behind this reviewer's most common false positive, and
one a per-file reviewer structurally could not answer.

**There is no orchestrator.** There was one — a `deepagents` agent that delegated
each file through the `task` tool — and it did not survive its own audit. It chose
nothing: the reviewer per file comes from the static classifier, the SQL second
opinion from the routing classifier (`core/agents/route_classifier.py`), and
findings never passed through it. What it added was ~120 model calls of dispatch
chatter per run, a coverage guarantee that had to be checked and repaired
afterwards because a model can skip a file, and the loss of a per-file call site
for `FILE_REVIEW_TIMEOUT_S` to watchdog. The two things it genuinely provided,
context isolation and concurrent reviews, a worker pool provides by construction:
there is no shared context window to fill because there is no shared window, and
`asyncio` needs no model to emit parallel tool calls. Coverage stops being a
property to verify and becomes arithmetic.

`deepagents` remains a dependency, for its filesystem layer only: a real-FS
backend, a tool allowlist, permissions, and a ``read_file`` that numbers its
output (which is what keeps an issue's ``line`` a lookup rather than arithmetic,
and therefore what keeps the in-diff gate from dropping it).

**Findings are captured at the source.** Each reviewer calls ``record_findings``
(`core/agents/findings_sink.py`), a tool closed over a run-scoped sink, and the
issue is normalised through ``ReviewIssue`` there and then. Nothing summarises,
relays or re-emits it — the failure mode `restore_issue_severities` exists to
repair for the comment reviewer. The sink is also the ledger: a reviewer that
finds nothing still calls it with an empty list, so "reviewed and clean" is
distinguishable from "never ran", and a reviewer that returns without ever calling
it is reported as a failed file rather than a silent gap.

Filesystem access is **read-only in two layers**: an explicit tool allowlist,
because `FilesystemMiddleware` with ``tools`` unset enables `write_file`,
`edit_file`, `delete` and `execute`; and a backend-level deny-write covering
anything the allowlist misses. This runs on a CI runner holding a GitLab token
with untrusted merge-request code on disk.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

# deepagents supplies the filesystem layer only — a real-FS backend, a tool
# allowlist, permissions, and a read_file that numbers its output. Its agent
# harness (create_deep_agent / SubAgentMiddleware / the task tool) is not used:
# there is no orchestrator.
from deepagents import FilesystemMiddleware, FilesystemPermission, create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import (
    AgentMiddleware, ModelCallLimitMiddleware, TodoListMiddleware, hook_config,
)
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from core.config.review_profile import get_profile
from core.agents.findings_sink import FindingsSink, make_record_findings_tool
from core.agents.review_contexts import ReviewPromptContext
from core.config.langchain_config import build_chat_model
from core.llm.gateway import LLMGateway
from core.agents.tool_errors import tool_error_middleware
from core.agents.tool_trace import TRACE_ENV, tool_trace_middleware, trace_enabled
from core.llm.gateway_middleware import ProviderPolicyMiddleware
from core.prompts import (
    get_business_logic_reviewer_prompt,
    get_dataview_reviewer_prompt,
    get_html_reviewer_prompt,
    get_pbook_reviewer_prompt,
    get_scss_reviewer_prompt,
    get_semantic_reviewer_prompt,
    get_sql_reviewer_prompt,
    get_test_reviewer_prompt,
    get_typescript_reviewer_prompt,
)
from core.prompts.deep_prompts import format_checkout_addendum
from core.utils.llm_retry import describe_error, maybe_print_traceback


#: Denies every write at the backend, for the whole tree. Passed to
#: `create_deep_agent(permissions=...)`, which is the harness's own public channel
#: and reaches the filesystem middleware it builds for each agent.
#:
#: This replaced an explicit `tools=["ls","read_file","glob","grep"]` allowlist on
#: a hand-built `FilesystemMiddleware`. The allowlist was justified as stopping
#: `write_file`/`edit_file`/`delete`/`execute` reaching a reviewer, and that
#: justification does not survive testing: with these permissions a `write_file`
#: call returns "permission denied for write on /pwned.txt" and never touches the
#: disk, and `execute` returns "Execution not available — this agent's backend does
#: not support command execution", because `FilesystemBackend` does not implement
#: the sandbox protocol. There was no shell to close.
#:
#: What the allowlist did do was create a bug: a replacement middleware instance
#: carries only the arguments it is given, so when the orchestrator was removed and
#: `create_deep_agent(permissions=...)` went with it, the deny-write silently
#: detached and the only real control was gone. Letting the harness build its own
#: filesystem middleware removes that failure mode and the coupling to its private
#: `_permissions` keyword along with it. A reviewer now *sees* the write tools and
#: is refused if it calls one — a few tool descriptions in the prompt, in exchange
#: for the protection being impossible to detach by accident.
READ_ONLY_PERMISSIONS = [
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


#: Ceiling on one reviewer's model calls. `exit_behavior="end"` stops that reviewer's
#: loop and leaves whatever it already recorded through `record_findings` standing.
#:
#: It was 25, which was budgeted for "read a file, grep a few times, report" and turns
#: out to be well under what a real investigation costs: **every** tool round trip is a
#: model call, and a reviewer spends two or three before it even reaches the file
#: (looking for `.aid` instructions at the root, then the module, then a glob) plus one
#: per grep. Observed on a production run — ten files, every reviewer ending
#: `26 model call(s) — hit the 25-call ceiling; ended with: 'Model call limits
#: exceeded: run limit (25/25)'`, nothing recorded for any of them, the whole run
#: wasted. 50 is chosen to leave room for that pattern rather than to be generous; the
#: ceiling still exists to stop a genuine loop, and the salvage pass below means
#: reaching it no longer costs the file's findings.
REVIEWER_CALL_LIMIT_ENV = "REVIEWER_CALL_LIMIT"
DEFAULT_REVIEWER_CALL_LIMIT = 50

#: A reviewer that hit the ceiling is asked, once, to report what it already has. It
#: needs one call to do that and one for a tool result, so three is generous.
REPORT_PASS_CALL_LIMIT = 3

#: Share of ``FILE_REVIEW_TIMEOUT_S`` a reviewer may spend investigating before it is
#: stopped and salvaged. The call ceiling bounds *calls*, not time, and a call's cost
#: is wildly uneven: on a large production MR a `glm-5.3-flash` reviewer at
#: `xhigh` made 62 calls in which most answered in 1–3 s and fifteen spent 2–6k
#: reasoning tokens (36–121 s each) deciding the next grep — 1119 s of model time,
#: killed by the watchdog at call 62 of 75 with nothing recorded. The brake is checked
#: between calls, so the remaining quarter has to absorb one slow in-flight call
#: (~120 s observed) plus the salvage pass.
REVIEW_TIME_BRAKE_FRACTION = 0.75

#: Text of the closing message the time brake leaves, so the salvage check can tell a
#: braked reviewer from one that finished on its own.
TIME_BRAKE_MESSAGE = "Review time budget reached"

#: Floor for the salvage pass's own bound once the file's budget is spent. The pass is
#: one or two calls over a history that is already cached, but one of them can still
#: be a long reasoning call.
SALVAGE_MIN_TIMEOUT_S = 180.0


class ReviewTimeBrakeMiddleware(AgentMiddleware):
    """End a reviewer cleanly once it has investigated for ``budget_s`` seconds.

    The wall-clock twin of ``ModelCallLimitMiddleware(exit_behavior="end")``: it jumps
    to the end instead of raising, so the history survives and the reviewer goes
    through the same salvage pass as one that ran out of calls. The alternative is the
    watchdog, which cancels the invocation and loses everything it learned.

    The clock starts at the first model call, i.e. per invocation — a fresh instance
    is built for every reviewer-file pair.
    """

    def __init__(self, budget_s: float) -> None:
        super().__init__()
        self.budget_s = budget_s
        self._started: Optional[float] = None

    def _check(self) -> Optional[dict]:
        now = time.monotonic()
        if self._started is None:
            self._started = now
            return None
        elapsed = now - self._started
        if elapsed < self.budget_s:
            return None
        return {
            "jump_to": "end",
            "messages": [AIMessage(content=(
                f"{TIME_BRAKE_MESSAGE} ({elapsed:.0f}s of {self.budget_s:.0f}s); "
                f"stopping the investigation."))],
        }

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> Optional[dict]:  # noqa: ARG002
        return self._check()

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime) -> Optional[dict]:  # noqa: ARG002
        return self._check()


def reviewer_call_limit() -> int:
    """The per-reviewer model-call ceiling, overridable for a slow or chatty model."""
    raw = (os.getenv(REVIEWER_CALL_LIMIT_ENV) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REVIEWER_CALL_LIMIT
    return value if value > 0 else DEFAULT_REVIEWER_CALL_LIMIT


@dataclass(frozen=True)
class ReviewerSpec:
    """One reviewer: its name, its prompt, and what it is for."""

    name: str
    #: The ``.aid`` agent type, and the label used in analytics/logs.
    agent_type: str
    #: What this reviewer covers. No longer read by a model — kept because it is
    #: the clearest statement of each reviewer's scope in one place.
    description: str
    prompt_getter: object


#: The nine reviewers. ``service`` is two of them because it always was two — a
#: semantic reviewer and a business-logic reviewer that run against the same file
#: and whose findings merge in the sink. Declaring them separately keeps that
#: behaviour and keeps each prompt intact.
REVIEWER_SPECS: Tuple[ReviewerSpec, ...] = (
    ReviewerSpec(
        name="service_reviewer",
        agent_type="service",
        description=(
            "Reviews Go service code for technical quality: correctness, error handling, "
            "concurrency, data integrity, performance and security. Use for .go files that "
            "are not tests, dataviews, pbooks or db files."
        ),
        prompt_getter=get_semantic_reviewer_prompt,
    ),
    ReviewerSpec(
        name="service_business_reviewer",
        agent_type="service",
        description=(
            "Reviews Go service code against the merge request's stated requirements and "
            "the linked Jira ticket — does the code do what the ticket asked. Dispatch "
            "alongside service_reviewer for the same .go file."
        ),
        prompt_getter=get_business_logic_reviewer_prompt,
    ),
    ReviewerSpec(
        name="sql_reviewer",
        agent_type="sql",
        description=(
            "Reviews SQL migrations and schema changes: DDL safety, locking, indexes, "
            "constraints, data migration correctness. Use for .sql files and db.go."
        ),
        prompt_getter=get_sql_reviewer_prompt,
    ),
    ReviewerSpec(
        name="test_reviewer",
        agent_type="test",
        description=(
            "Reviews BDD/Gherkin test implementation code: whether the tests assert what "
            "they claim and cover the behaviour changed. Use for _test.go, ts.go and *_ts.go."
        ),
        prompt_getter=get_test_reviewer_prompt,
    ),
    ReviewerSpec(
        name="pbook_reviewer",
        agent_type="pbook",
        description="Reviews error-message files (Persian/English message catalogues). Use for pbook.go.",
        prompt_getter=get_pbook_reviewer_prompt,
    ),
    ReviewerSpec(
        name="dataview_reviewer",
        agent_type="dataview",
        description="Reviews dataview implementation code (query/projection layer). Use for dataview.go.",
        prompt_getter=get_dataview_reviewer_prompt,
    ),
    ReviewerSpec(
        name="typescript_reviewer",
        agent_type="typescript",
        description="Reviews TypeScript for runtime correctness, async handling and API usage. Use for .ts files.",
        prompt_getter=get_typescript_reviewer_prompt,
    ),
    ReviewerSpec(
        name="html_reviewer",
        agent_type="html",
        description="Reviews HTML templates for correctness, accessibility and injection risks. Use for .html files.",
        prompt_getter=get_html_reviewer_prompt,
    ),
    ReviewerSpec(
        name="scss_reviewer",
        agent_type="scss",
        description="Reviews SCSS/CSS for correctness and maintainability. Use for .scss and .css files.",
        prompt_getter=get_scss_reviewer_prompt,
    ),
)

_SPECS_BY_NAME: Dict[str, ReviewerSpec] = {spec.name: spec for spec in REVIEWER_SPECS}

#: Which reviewers a file type goes to is not decided here: it comes from the
#: review profile (`core/config/review_profile.py`, ``REVIEW_PROFILE``), so a
#: codebase describes its own layout in YAML.


def business_logic_enabled() -> bool:
    """Same switch the graph mode reads, so the two modes review alike."""
    return os.getenv("BUSINESS_LOGIC_REVIEW_ENABLED", "true").strip().lower() == "true"


def route_file(file_type: str) -> Tuple[str, ...]:
    """Which reviewer subagent(s) a classified file goes to; () when unreviewable."""
    names = get_profile().reviewers_for(file_type)
    if not business_logic_enabled():
        names = tuple(n for n in names if n != "service_business_reviewer")
    return names


def go_files(classified: Sequence[Tuple[str, str]]) -> List[str]:
    """Paths the SQL routing classifier should scan for embedded SQL.

    The types come from the profile's ``sql_routing.scan_file_types`` — by default
    plain Go files: `db.go` already goes to the SQL reviewer by name, and no other
    type hides SQL behind its extension.
    """
    scan = get_profile().sql_scan_types
    return [path for path, file_type in classified if file_type in scan]


def add_sql_dispatches(
    plan: List[Tuple[str, str]], sql_paths: Sequence[str]
) -> List[Tuple[str, str]]:
    """Append a ``sql_reviewer`` dispatch for each Go file that needs one.

    Purely additive: the service reviewers stay on the file. Skips a path that
    already has a SQL dispatch, so `db.go` cannot be assigned twice.
    """
    existing = {(path, name) for path, name in plan}
    for path in sql_paths:
        key = (path, "sql_reviewer")
        if key not in existing:
            plan.append(key)
            existing.add(key)
    return plan


def build_dispatch_plan(classified: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Turn ``[(path, file_type)]`` into the ``[(path, subagent)]`` assignment.

    Computed here, deterministically, from the same classifier the graph mode
    uses. A file whose type has no reviewer is simply absent from the plan.
    """
    plan: List[Tuple[str, str]] = []
    for path, file_type in classified:
        for name in route_file(file_type):
            plan.append((path, name))
    return plan


def _prompt_context(**shared) -> ReviewPromptContext:
    """Build the prompt context with **no file content in it**.

    The getters render `content`/`diff` inline because they were written for a
    world where the file arrived in the prompt. Here the subagent reads the file
    itself, so those stay empty and the deep-mode addendum tells the model where
    the code actually is. ``file_name`` is empty for the same reason: one subagent
    serves every file of its type, and the file under review is named in the
    `task` message it receives.
    """
    return ReviewPromptContext(**shared)




def resolve_model(temperature: float = 0.3):
    """Build the chat model for this run from the gateway's active provider.

    Resolved through `LLMGateway.active_provider()` rather than reading env
    directly, so a run that has already failed over starts the review pass on the
    provider it is actually using. `ProviderPolicyMiddleware` then overrides the
    model per call, so this instance is what construction-time middleware sees
    rather than what the requests go to.
    """
    provider = LLMGateway.active_provider()
    return build_chat_model(
        model=provider.model,
        api_key=provider.api_key,
        base_url=provider.base_url,
        temperature=temperature,
    )


#: The harness adds a `general-purpose` subagent to every deep agent unless a spec
#: already claims that name, and builds it with the *default* filesystem
#: middleware — all eight tools, `execute` included. For a code reviewer that is
#: both a shell and a way to hand the file to a generalist with none of the
#: specialist instructions. Claiming the name with a deliberately inert agent
#: removes both. The `task` tool still exists (SubAgentMiddleware installs it once
#: any subagent is declared) and simply has nowhere useful to go — the cost of one
#: unused tool description per reviewer.
#:
#: The alternative is `register_harness_profile` with
#: `GeneralPurposeSubagentProfile(enabled=False)`, which would suppress the
#: subagent entirely and drop the `task` tool with it. Not used: it is a beta API
#: that mutates a global registry keyed by *model id*, so it would have to be
#: re-registered for the primary model, the fallback, and anything a future
#: deployment configures — three ways to silently not apply, in exchange for
#: removing one tool description.
_INERT_GENERAL_PURPOSE = {
    "name": GENERAL_PURPOSE_SUBAGENT["name"],
    "description": (
        "Not used for code review. You are already the specialist for this file — "
        "review it yourself."
    ),
    "system_prompt": (
        "You are not part of this workflow. Reply with exactly: "
        "'Not applicable — the assigned specialist reviews this file directly.' "
        "Do not read, search, review or modify anything."
    ),
    "tools": [],
}


def build_reviewer(
    spec: ReviewerSpec,
    *,
    clone_root: str,
    source_branch: Optional[str],
    mr_context: str,
    record_tool: BaseTool,
    backend: FilesystemBackend,
    model,
    file_path: Optional[str] = None,
    call_limit: int = DEFAULT_REVIEWER_CALL_LIMIT,
    provider_policy: bool = True,
    time_budget_s: Optional[float] = None,
):
    """Build one reviewer as a plain agent over the checkout.

    The prompt is the specialist's own, rendered with **no file content** — the
    reviewer reads the file it is given from disk — plus the addendum that tells it
    where the code is, how `read_file` numbers lines, which `.aid` files to look
    for, and that `record_findings` is the only channel that reaches the merge
    request.

    Built with `create_deep_agent`, so each reviewer gets the full harness: a
    `write_todos` planning tool, context summarization, dangling-tool-call repair
    and prompt caching. That is worth having because a review here *is*
    multi-step — read the file, read its `.aid` instructions, grep for where a
    symbol is defined, check the callers — and the two harness middlewares that
    matter are exactly the ones that protect a long investigation: summarization
    when the greps pile up, and tool-call repair when one does not come back.

    The harness is used **per reviewer**, not as an orchestrator. Nothing delegates
    files: the loop in :func:`review_changed_files` decides what runs, and each
    reviewer is a deep agent operating on the one file it was handed.

    The filesystem comes from the harness — `create_deep_agent` builds its own
    `FilesystemMiddleware` around the backend and attaches the `permissions` given
    here, so a write is refused at the backend and `execute` is unavailable because
    `FilesystemBackend` implements no sandbox. Nothing is hand-assembled: see
    `READ_ONLY_PERMISSIONS` for why an explicit tool allowlist was removed.
    """
    middleware: list = [
        # The harness's planning affordance. A review here is genuinely multi-step —
        # read the file, read its `.aid` instructions, grep for where a symbol is
        # defined, check the callers — and `write_todos` is what keeps a reviewer on
        # that plan instead of stopping after the first thing it noticed. It is not
        # installed by `create_deep_agent` itself (the harness leaves it opt-in), so
        # it is declared here. It costs a tool description and a few calls; the
        # call ceiling below bounds that.
        TodoListMiddleware(),
        ModelCallLimitMiddleware(run_limit=call_limit, exit_behavior="end"),
        # A tool that raises must not take the file with it. Only the MCP tools were
        # protected before this (their coroutines are wrapped in `toolkit_manager`,
        # because one dropped session would otherwise crash every concurrent
        # review); an exception from `read_file`, `grep`, `glob`, `record_findings`
        # or `task` propagated out of the loop and failed the whole file, findings
        # already banked or not. See `core/agents/tool_errors.py` for why this is a
        # net over those tools rather than a replacement for that wrapper.
        tool_error_middleware(),
    ]
    if time_budget_s and time_budget_s > 0:
        # The call ceiling's wall-clock twin — see ReviewTimeBrakeMiddleware.
        middleware.append(ReviewTimeBrakeMiddleware(time_budget_s))
    # One line per tool call, so a run's log shows whether a reviewer actually read
    # the file and grepped for the caller — or answered from the diff alone. Labelled
    # per reviewer-and-file because FILE_BATCH_SIZE of these run at once.
    trace = tool_trace_middleware(
        f"{spec.name} {os.path.basename(file_path)}" if file_path else spec.name)
    if trace is not None:
        middleware.append(trace)
    if provider_policy:
        # Reviewers make every model call in the run, so this is where a throttle
        # lands. Without it there is no retry and no failover on the only calls
        # that matter.
        middleware.append(ProviderPolicyMiddleware(label=f"review {spec.name}"))

    context = _prompt_context(source_branch=source_branch, mr_context=mr_context)
    return create_deep_agent(
        model=model,
        system_prompt=spec.prompt_getter(context, get_profile().instructions_for(spec.name)) + format_checkout_addendum(
            clone_root=clone_root,
            agent_type=spec.agent_type,
            source_branch=source_branch,
        ),
        tools=[record_tool],
        backend=backend,
        # Covers anything the harness builds for itself; the middleware above
        # covers the reviewer's own filesystem tools.
        permissions=READ_ONLY_PERMISSIONS,
        middleware=middleware,
        subagents=[_INERT_GENERAL_PURPOSE],
        name=spec.name,
    )


def _ai_messages(state: object) -> list:
    messages = (state or {}).get("messages", []) if isinstance(state, dict) else []
    return [m for m in messages if getattr(m, "type", None) == "ai"]


def _hit_call_ceiling(state: object, call_limit: int) -> bool:
    """Whether a reviewer was stopped by a brake rather than finishing on its own.

    Two signals, because either alone can be wrong: the count reaching the limit, and
    the middleware's own closing message. `exit_behavior="end"` makes this a *clean*
    finish, indistinguishable from a real one without looking. The time brake ends
    the same way and is recognised by its message.
    """
    ai = _ai_messages(state)
    if call_limit and len(ai) >= call_limit:
        return True
    last = " ".join(str(getattr(m, "content", "") or "") for m in ai[-1:]).lower()
    return "model call limit" in last or TIME_BRAKE_MESSAGE.lower() in last


def _describe_finish(state: object, call_limit: int) -> str:
    """How a reviewer ended, in one line: model calls spent and its closing words.

    This is the answer to "why did nothing get recorded?", and it comes from the
    reviewer itself rather than from an inference. Counting assistant messages is a
    proxy for model calls, which is enough to recognise the one structural cause —
    ``ModelCallLimitMiddleware(exit_behavior="end")`` stopping a reviewer *cleanly*
    at its ceiling, which is indistinguishable from a normal finish otherwise.
    """
    ai = _ai_messages(state)
    calls = len(ai)
    last = next((str(getattr(m, "content", "") or "") for m in reversed(ai)
                 if str(getattr(m, "content", "") or "").strip()), "")
    said = " ".join(last.split())
    ended = f"{calls} model call(s)"
    if call_limit and calls >= call_limit:
        ended += f" — hit the {call_limit}-call ceiling"
    return f"{ended}; ended with: {said[:200]!r}" if said else f"{ended}; said nothing"


#: What a reviewer that ran out of calls is told. It has already read the file and
#: done its greps; what it has *not* done is report, and `record_findings` is the only
#: channel out — so the investigation is worth nothing until this happens.
REPORT_NOW_INSTRUCTION = (
    "You have used your entire tool budget for this file, so stop investigating now.\n\n"
    "Do not call any tool except `record_findings`. Call it exactly once, with the "
    "findings you have already established from what you read above. If you genuinely "
    "found nothing worth reporting, call it with an empty findings list — that is how "
    "a clean review is recorded, and saying so in prose instead records nothing at all."
)


async def _report_pass(build_agent, state: dict, path: str, spec_name: str,
                       timeout_s: Optional[float] = None,
                       why: str = "ran out of calls") -> Optional[dict]:
    """Ask a reviewer that hit the ceiling to report what it already found.

    The investigation is the expensive part and it is already paid for: the file has
    been read, the greps have run, and all of it is in the message history. Ending
    there throws it away — observed on a production run where ten files were
    investigated to the last call and *none* produced a finding, because
    ``record_findings`` is the only channel out and nobody reached it.

    So the same history is handed back with one instruction: report now, no more tools.
    A fresh agent because ``ModelCallLimitMiddleware`` counts per invocation, with a
    ceiling of three so this can never become a second investigation.

    ``timeout_s`` bounds it in wall-clock too: a reviewer is salvaged precisely when
    its model has been slow, and an unbounded pass would hold the lane indefinitely.
    """
    print(f"  🧷 [review] {spec_name} {why} on {path} — "
          f"asking it to report what it already found")
    try:
        agent = build_agent(REPORT_PASS_CALL_LIMIT)
        messages = list(state.get("messages", [])) + [
            {"role": "user", "content": REPORT_NOW_INSTRUCTION}
        ]
        call = agent.ainvoke({"messages": messages})
        if timeout_s and timeout_s > 0:
            return await asyncio.wait_for(call, timeout=timeout_s)
        return await call
    except asyncio.TimeoutError:
        print(f"  ⚠️  [review] {spec_name}: report pass timed out on {path} "
              f"after {timeout_s:.0f}s")
        return None
    except Exception as e:  # noqa: BLE001 — a failed salvage must not mask the original
        print(f"  ⚠️  [review] {spec_name}: report pass failed on {path}: {describe_error(e)}")
        return None


@dataclass
class ReviewPassResult:
    """Outcome of the review pass."""

    #: ``[(file_path, issues)]`` — what the comment pipeline consumes.
    all_comments: List[Tuple[str, list]] = field(default_factory=list)
    #: Files at least one reviewer reported on, findings or not.
    reviewed_files: set = field(default_factory=set)
    #: Files no reviewer could review at all. These are run errors: the marker is
    #: held and the run exits non-zero, so a re-run retries them.
    failed_files: List[str] = field(default_factory=list)
    #: Why each failed file failed, keyed by path. A failure has two very different
    #: causes — a reviewer that *raised*, and a reviewer that returned perfectly
    #: normally without ever calling ``record_findings`` — and only the first used to
    #: print anything, so a run could report "3 file(s) failed" with nothing anywhere
    #: saying why. This is what makes the second case legible, in the log and in the
    #: analytics error it becomes.
    failure_reasons: Dict[str, str] = field(default_factory=dict)
    #: True when the run's budget ran out before every file was reviewed.
    truncated: bool = False
    sink: Optional[FindingsSink] = None
    summary: str = ""


async def review_changed_files(
    *,
    classified: Sequence[Tuple[str, str]],
    clone_root: str,
    source_branch: Optional[str],
    mr_context: str = "",
    sql_paths: Sequence[str] = (),
    model=None,
    config: Optional[RunnableConfig] = None,
    concurrency: int = 5,
    per_file_timeout_s: Optional[float] = None,
    deadline: Optional[float] = None,
    provider_policy: bool = True,
) -> ReviewPassResult:
    """Review every classified file with its specialist reviewer.

    A loop, not an orchestrator. Each ``(file, reviewer)`` pair in the dispatch
    plan becomes one agent invocation with its own message history, so context
    isolation is total — a 50-file merge request never accumulates into a shared
    window because there is no shared window.

    An LLM orchestrator was here and did not survive its own audit. It chose
    nothing: the reviewer per file comes from the static classifier, the SQL second
    opinion from the routing classifier, and findings never passed through it
    anyway. What it added was ~120 model calls of dispatch chatter, a coverage
    guarantee that had to be checked and repaired afterwards, and the loss of a
    per-file call site to watchdog. Everything it genuinely provided — context
    isolation, concurrent reviews — a worker pool provides by construction, and
    the pool cannot decide to skip a file.

    Args:
        classified: ``[(file_path, file_type)]`` to review.
        clone_root: The checkout the filesystem tools are rooted at.
        source_branch: The MR's source branch, for the prompts' branch discipline.
        mr_context: The shared MR context block.
        sql_paths: Go files the routing classifier sent to the SQL reviewer too.
        model: Chat model; ``None`` resolves one from the gateway's provider.
        config: Tracing run config, so every reviewer lands in the MR's session.
        concurrency: Reviewer-file pairs in flight at once (``FILE_BATCH_SIZE``).
            A ceiling held continuously, not a batch size: a worker that finishes
            takes the next pair immediately.
        per_file_timeout_s: Watchdog per reviewer invocation
            (``FILE_REVIEW_TIMEOUT_S``). Every file is its own await, so unlike a
            `task` dispatch this can actually be bounded.
        deadline: ``time.monotonic()`` value past which no further review is
            dispatched. Checked per pair, so the pool stops taking work the moment
            the budget is gone rather than at the next batch boundary.
        provider_policy: Whether to apply the run's retry/failover policy. Leave
            it on in production. Turning it off is the only way to run against a
            model supplied here, because :class:`ProviderPolicyMiddleware` resolves
            the provider per call and overrides ``request.model`` with it — right
            for a real run (the run's policy decides, not a captured model) and
            exactly what makes a fake-model test impossible otherwise.

    Returns:
        A :class:`ReviewPassResult`. Never raises: a reviewer that fails costs its
        file, not the run.
    """
    plan = add_sql_dispatches(build_dispatch_plan(classified), sql_paths)
    sink = FindingsSink()
    if not plan:
        return ReviewPassResult(sink=sink, summary="nothing to review")

    jobs: List[Tuple[str, ReviewerSpec]] = []
    for path, name in plan:
        spec = _SPECS_BY_NAME.get(name)
        if spec is not None:
            jobs.append((path, spec))

    backend = FilesystemBackend(root_dir=clone_root, virtual_mode=True)
    resolved = model if model is not None else resolve_model()
    record_tool = make_record_findings_tool(sink)
    # `FILE_BATCH_SIZE` names a concurrency ceiling, not a batch — the variable is
    # kept because it is deployed, and lowering it is still the documented answer to
    # a provider's concurrency limit.
    lanes = max(1, min(concurrency, len(jobs)))

    limit = reviewer_call_limit()
    print(
        f"\n🧠 Review pass: {len(jobs)} reviewer-file pair(s) over {len({p for p, _ in jobs})} "
        f"file(s), {lanes} at a time, {limit} model call(s) each"
    )
    # Say when the trace is off, because its *absence* is the confusing case: a run
    # with no 🔧 lines looks like a run whose reviewers never read anything, and the
    # variable predates this middleware — an old `LOG_TOOL_TRACE=false` set to quieten
    # the graph era's end-of-run tally silently disables it here too.
    if not trace_enabled():
        print(f"  🔇 [review] per-tool-call trace is off ({TRACE_ENV}="
              f"{os.getenv(TRACE_ENV)!r}); unset it to see what each reviewer reads")

    failed: List[str] = []
    truncated = False
    reasons: Dict[Tuple[str, str], str] = {}

    async def _one(path: str, spec: ReviewerSpec) -> Optional[str]:
        """Run one reviewer on one file. Returns the path on failure."""

        bounded = bool(per_file_timeout_s and per_file_timeout_s > 0)

        def build_agent(limit: int, time_budget_s: Optional[float] = None):
            return build_reviewer(
                spec,
                clone_root=clone_root,
                source_branch=source_branch,
                mr_context=mr_context,
                record_tool=record_tool,
                backend=backend,
                model=resolved,
                file_path=path,
                call_limit=limit,
                provider_policy=provider_policy,
                time_budget_s=time_budget_s,
            )

        agent = build_agent(
            limit,
            per_file_timeout_s * REVIEW_TIME_BRAKE_FRACTION if bounded else None,
        )
        # Streamed rather than invoked so the latest state survives a watchdog
        # cancellation: `ainvoke` is this same stream's last value, but a cancelled
        # `ainvoke` returns nothing, and the history is what the salvage pass needs.
        latest: dict = {}

        async def _run() -> dict:
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": f"Review {path}"}]},
                config=config,
                stream_mode="values",
            ):
                latest["state"] = chunk
            return latest.get("state") or {}

        began = time.monotonic()

        def salvage_budget() -> Optional[float]:
            if not bounded:
                return None
            left = per_file_timeout_s - (time.monotonic() - began)
            return max(left, SALVAGE_MIN_TIMEOUT_S)

        try:
            if bounded:
                state = await asyncio.wait_for(_run(), timeout=per_file_timeout_s)
            else:
                state = await _run()
        except asyncio.TimeoutError:
            print(f"  ⏳ {spec.name} timed out on {path}")
            state = latest.get("state") or {}
            if path not in sink.reviewed_files and _ai_messages(state):
                # The backstop for a call the time brake could not interrupt (it is
                # checked between calls). What the reviewer read before the watchdog
                # fired is still worth reporting.
                salvaged = await _report_pass(
                    build_agent, state, path, spec.name,
                    timeout_s=SALVAGE_MIN_TIMEOUT_S, why="timed out")
                if path in sink.reviewed_files:
                    reasons[(path, spec.name)] = _describe_finish(salvaged or state, limit)
                    return None
            reasons[(path, spec.name)] = f"timed out after {per_file_timeout_s:.0f}s"
            return path
        except Exception as e:
            reasons[(path, spec.name)] = describe_error(e)
            print(f"  ❌ {spec.name} failed on {path}: {describe_error(e)}")
            return path
        # It returned. That is not the same as having reported: `record_findings` is
        # the only channel out, and a reviewer can finish without using it — by
        # answering in prose, or by being stopped by the call ceiling or time brake.
        if path not in sink.reviewed_files and _hit_call_ceiling(state, limit):
            # Everything it learned is in the history and cost real calls; ending here
            # would discard all of it. One bounded pass to get it recorded.
            braked = TIME_BRAKE_MESSAGE.lower() in " ".join(
                str(getattr(m, "content", "") or "") for m in _ai_messages(state)[-1:]).lower()
            salvaged = await _report_pass(
                build_agent, state, path, spec.name, timeout_s=salvage_budget(),
                why="ran out of time" if braked else "ran out of calls")
            if salvaged is not None:
                state = salvaged
        reasons[(path, spec.name)] = _describe_finish(state, limit)
        return None

    # A queue, not batches. `asyncio.gather` over a slice is a barrier, and these
    # slices are wildly uneven: a reviewer that finds its file and reports costs
    # seconds, one that greps its way to the call ceiling costs minutes, and one
    # that hangs costs the whole `FILE_REVIEW_TIMEOUT_S` watchdog. Under batching
    # the other four lanes sat idle for the difference — on a long merge request
    # most of the wall clock, and against a fixed `OVERALL_TIMEOUT_S` idle lanes
    # are files that never get reviewed. Workers take the next pair the instant
    # they are free, so `lanes` reviews are in flight until the queue is empty.
    queue: "asyncio.Queue[Tuple[int, str, ReviewerSpec]]" = asyncio.Queue()
    for position, (path, spec) in enumerate(jobs, start=1):
        queue.put_nowait((position, path, spec))

    # Which pairs were actually dispatched. A batch loop could derive this from the
    # batch index; a pool cannot, and the truncation arithmetic below needs it to be
    # exact — inferring it would blame the budget for a file that genuinely failed.
    started: Set[str] = set()

    async def _worker() -> None:
        nonlocal truncated
        while True:
            try:
                position, path, spec = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            # Checked per dispatch rather than per batch, which is the finer grain
            # the queue makes available: nothing new starts past the deadline, and
            # whatever is already running keeps its own watchdog. The queue is
            # drained rather than abandoned so every worker exits by the same door.
            if deadline is not None and time.monotonic() >= deadline:
                if not truncated:
                    truncated = True
                    print(
                        f"\n⏳ Review budget reached with {queue.qsize() + 1} review(s) "
                        f"still queued; skipping them and proceeding to posting"
                    )
                continue
            started.add(path)
            print(f"  ▶️  [{position}/{len(jobs)}] {spec.name} on {path}")
            began = time.monotonic()
            try:
                outcome = await _one(path, spec)
            except Exception as e:
                # `_one` handles its own failures; this is the net under it, and it
                # must not take the other lanes down with it. CancelledError is a
                # BaseException and deliberately passes through, so the run's own
                # watchdogs still stop the pool.
                print(f"  ❌ review raised: {describe_error(e)}")
                continue
            if outcome is not None:
                failed.append(outcome)
            print(
                f"  {'✔️ ' if outcome is None else '⚠️ '} [{position}/{len(jobs)}] "
                f"{spec.name} on {path} — {time.monotonic() - began:.0f}s, "
                f"{queue.qsize()} queued"
            )

    await asyncio.gather(*(_worker() for _ in range(lanes)))

    # A path failed only if *nothing* reported on it: one of two service reviewers
    # erroring still leaves the file reviewed. Also catches a reviewer that
    # returned successfully without ever calling record_findings, which is
    # indistinguishable from not being run.
    attempted = {path for path, _spec in jobs}
    if truncated:
        # Files we never got to are not failures — the marker is held and a re-run
        # picks them up, same contract as the overall timeout has always had. The
        # pool records what it dispatched, so this is the exact set rather than the
        # approximation a batch index could give.
        attempted = set(started)
    unreported = sorted(attempted - sink.reviewed_files)

    # Say why, per file. "3 file(s) failed" with nothing explaining it is the least
    # useful thing this pass can print: the two causes need opposite responses — a
    # reviewer that raised is a provider or code problem, a reviewer that returned
    # without recording is a *prompt* problem, and they used to look identical.
    failure_reasons: Dict[str, str] = {}
    for path in unreported:
        per_reviewer = [f"{name}: {why}" for (p, name), why in reasons.items() if p == path]
        failure_reasons[path] = "; ".join(per_reviewer) or "no reviewer ran"
        print(f"  ⚠️  nothing recorded for {path}")
        for line in per_reviewer:
            print(f"       {line}")
        if not per_reviewer:
            print("       no reviewer ran on it")

    print(f"  ✅ Review pass: {sink.describe()}")
    return ReviewPassResult(
        all_comments=sink.as_all_comments(),
        reviewed_files=sink.reviewed_files,
        failed_files=unreported,
        failure_reasons=failure_reasons,
        truncated=truncated,
        sink=sink,
        summary=sink.describe(),
    )
