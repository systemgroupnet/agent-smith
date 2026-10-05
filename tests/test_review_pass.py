"""The review pass: one deep agent per changed file, driven by a loop.

There is no orchestrator. Coverage is arithmetic over the dispatch plan rather
than a property to verify, and the properties worth testing are the ones a loop
cannot give you for free: that a reviewer which answers without recording is
reported as a failure, that an expired budget blames nobody, and that the
filesystem really is read-only.
"""

import asyncio
import os

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from core.agents.deep_reviewer import (
    _SPECS_BY_NAME, REVIEWER_SPECS, build_dispatch_plan, build_reviewer,
    review_changed_files, route_file,
)
from core.agents.findings_sink import FindingsSink, make_record_findings_tool
from core.prompts.deep_prompts import format_checkout_addendum
from deepagents.backends import FilesystemBackend


class Scripted(BaseChatModel):
    """Reads the file, records one finding, then answers."""

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _step(self, messages):
        done = [m.name for m in messages if isinstance(m, ToolMessage) and m.name]
        if "read_file" not in done:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "id": "a", "args": {"file_path": "svc/order.go"}}])
        if "record_findings" not in done:
            return AIMessage(content="", tool_calls=[
                {"name": "record_findings", "id": "b", "args": {
                    "file_path": "svc/order.go",
                    "findings": [{"problem": "Save() error dropped", "line": 4,
                                  "severity": "high"}]}}])
        return AIMessage(content="done")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._step(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._step(messages))])


class Silent(Scripted):
    """Answers in prose without ever calling record_findings."""

    def _step(self, messages):
        return AIMessage(content="I looked at it, seems fine.")


class Attacker(Scripted):
    """Tries to write to the checkout and to run a shell."""

    def _step(self, messages):
        done = [m.name for m in messages if isinstance(m, ToolMessage) and m.name]
        if "write_file" not in done:
            return AIMessage(content="", tool_calls=[
                {"name": "write_file", "id": "1",
                 "args": {"file_path": "/pwned.txt", "content": "owned"}}])
        if "execute" not in done:
            return AIMessage(content="", tool_calls=[
                {"name": "execute", "id": "2", "args": {"command": "echo SHELL_RAN"}}])
        return AIMessage(content="done")


def _reviewer(checkout, model, sink=None, record_tool=None):
    return build_reviewer(
        _SPECS_BY_NAME["service_reviewer"],
        clone_root=str(checkout),
        source_branch="feature/x",
        mr_context="MR: add order saving",
        record_tool=record_tool or make_record_findings_tool(sink or FindingsSink()),
        backend=FilesystemBackend(root_dir=str(checkout), virtual_mode=True),
        model=model,
        file_path="svc/order.go",
        provider_policy=False,
    )


def test_there_are_ten_reviewers_and_routing_is_total():
    assert len(REVIEWER_SPECS) == 10
    assert route_file("service") == ("service_reviewer", "service_business_reviewer")
    assert route_file("unknown") == (), "an unreviewable type has no reviewer"


def test_a_service_file_draws_both_service_reviewers():
    plan = build_dispatch_plan([("svc/order.go", "service"), ("m/x.sql", "sql")])
    assert ("svc/order.go", "service_reviewer") in plan
    assert ("svc/order.go", "service_business_reviewer") in plan
    assert ("m/x.sql", "sql_reviewer") in plan


def test_reviewers_are_deep_agents_with_the_harness(checkout):
    agent = _reviewer(checkout, Scripted())
    tools = sorted(agent.nodes["tools"].bound._tools_by_name)
    assert "record_findings" in tools, "the only channel out"
    assert "write_todos" in tools, "harness planning affordance"
    for expected in ("read_file", "grep", "glob", "ls"):
        assert expected in tools, expected


def test_writes_are_refused_and_there_is_no_shell(checkout):
    """Asserting behaviour, not configuration.

    An earlier shape asserted the write tools were *absent* from the tool list,
    which kept passing after the backend deny-write silently detached.
    """
    agent = _reviewer(checkout, Attacker())
    out = asyncio.run(agent.ainvoke(
        {"messages": [{"role": "user", "content": "Review svc/order.go"}]}))
    obs = {m.name: str(m.content) for m in out["messages"] if isinstance(m, ToolMessage)}
    assert "permission denied" in obs.get("write_file", "").lower(), obs.get("write_file")
    assert "not available" in obs.get("execute", "").lower(), obs.get("execute")
    assert not (checkout / "pwned.txt").exists(), "a write reached the disk"
    assert "SHELL_RAN" not in obs.get("execute", ""), "a shell ran"


def test_a_raising_tool_does_not_end_the_file_review(checkout):
    """Only MCP tools were protected; every other tool could kill the whole file.

    `ToolErrorMiddleware` now turns the exception into an error observation, so the
    reviewer gets to try again or finish with what it has. Driven through
    `record_findings` because that is the tool whose failure matters most — and the
    message has to say the findings were *not* recorded, or the model carries on
    believing the review reached the merge request.
    """
    from langchain_core.tools import StructuredTool

    def boom(**_kwargs):
        raise RuntimeError("sink exploded")

    exploding = StructuredTool.from_function(
        func=boom, name="record_findings", description="Record findings.")

    agent = _reviewer(checkout, Scripted(), record_tool=exploding)
    out = asyncio.run(agent.ainvoke(
        {"messages": [{"role": "user", "content": "Review svc/order.go"}]}))

    errors = [m for m in out["messages"]
              if isinstance(m, ToolMessage) and m.name == "record_findings"]
    assert errors, "the tool call produced no observation at all"
    assert errors[0].status == "error", "a failure must not look like a success"
    assert "not** recorded" in str(errors[0].content), str(errors[0].content)
    assert "sink exploded" in str(errors[0].content), "the cause is named"


def test_the_tool_trace_shows_what_the_reviewer_looked_at(checkout, capsys):
    """A run's log should answer "did it actually read the file?".

    Every line carries the reviewer and its file because FILE_BATCH_SIZE of these
    run concurrently, and an unlabelled interleaving of five investigations is noise.
    """
    agent = _reviewer(checkout, Scripted())
    asyncio.run(agent.ainvoke(
        {"messages": [{"role": "user", "content": "Review svc/order.go"}]}))
    trace = [ln for ln in capsys.readouterr().out.splitlines() if "\U0001f527" in ln]

    assert trace, "no tool trace at all"
    assert any("read_file svc/order.go" in ln for ln in trace), trace
    assert any("record_findings" in ln for ln in trace), trace
    for line in trace:
        assert "[service_reviewer order.go]" in line, line


def test_the_trace_can_be_switched_off(checkout, capsys, monkeypatch):
    monkeypatch.setenv("LOG_TOOL_TRACE", "false")
    agent = _reviewer(checkout, Scripted())
    asyncio.run(agent.ainvoke(
        {"messages": [{"role": "user", "content": "Review svc/order.go"}]}))
    assert not [ln for ln in capsys.readouterr().out.splitlines() if "\U0001f527" in ln]


def test_the_error_observation_never_propagates_an_exception():
    """Returning None from the handler would end the run — nothing here is worth that."""
    from core.agents.tool_errors import MAX_ERROR_CHARS, tool_error_observation

    class Req:
        tool_call = {"name": "grep"}
        tool = None

    assert tool_error_observation(RuntimeError("x"), Req()) is not None
    assert tool_error_observation(KeyError("k"), Req()) is not None
    long = tool_error_observation(ValueError("y" * 5000), Req())
    assert len(long) < MAX_ERROR_CHARS + 200, "a huge error must not flood the context"


def test_a_silent_reviewer_says_why_nothing_was_recorded(checkout, capsys):
    """"3 file(s) failed" with nothing explaining it is the least useful line this
    pass can print. A reviewer that *raised* and one that returned normally without
    calling `record_findings` are different problems — a provider or code fault
    versus a prompt fault — and they used to look identical in the log and in the
    analytics error."""
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=Silent(),
        provider_policy=False))

    assert result.failed_files == ["svc/order.go"]
    reason = result.failure_reasons["svc/order.go"]
    assert "service_reviewer" in reason, reason
    assert "model call(s)" in reason, reason
    assert "seems fine" in reason, "the reviewer's own closing words are the diagnosis"

    out = capsys.readouterr().out
    assert "nothing recorded for svc/order.go" in out


def test_the_addendum_tells_the_reviewer_where_the_code_is():
    addendum = format_checkout_addendum(clone_root="/x", agent_type="service",
                                        source_branch="feature/x")
    assert "read_file" in addendum and "record_findings" in addendum
    assert "`feature/x`" in addendum, "the branch is interpolated as a literal"
    assert '.aid/service.md' in addendum
    assert 'glob("**/.aid/service.md")' in addendum, "centralized-layout fallback"


def test_the_prompt_carries_no_inlined_file_content(checkout):
    agent = _reviewer(checkout, Scripted())
    assert agent is not None  # construction is the assertion; content check below
    from core.agents.review_contexts import ReviewPromptContext
    from core.prompts import get_semantic_reviewer_prompt
    rendered = get_semantic_reviewer_prompt(ReviewPromptContext(), "")
    assert "(file content unavailable)" in rendered, "the reviewer reads the file itself"


def test_a_finding_reaches_the_sink_verbatim(checkout):
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=Scripted(),
        concurrency=2, per_file_timeout_s=30, provider_policy=False))
    assert result.reviewed_files == {"svc/order.go"}
    issues = dict(result.all_comments)["svc/order.go"]
    assert issues[0]["line"] == 4 and issues[0]["severity"] == "high"
    assert issues[0]["problem"] == "Save() error dropped"
    assert result.failed_files == [] and not result.truncated


def test_a_reviewer_that_never_records_is_a_failed_file(checkout):
    """Indistinguishable from never running, so it must not pass silently."""
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch=None, mr_context="", model=Silent(),
        concurrency=1, per_file_timeout_s=30, provider_policy=False))
    assert result.failed_files == ["svc/order.go"]
    assert result.reviewed_files == set()


def test_an_expired_budget_truncates_without_blaming_a_file(checkout):
    import time
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch=None, mr_context="", model=Scripted(),
        concurrency=1, deadline=time.monotonic() - 1, provider_policy=False))
    assert result.truncated
    assert result.failed_files == [], "the marker is held; a re-run picks these up"


def test_a_free_worker_takes_the_next_file_instead_of_waiting_for_a_batch(checkout):
    """The pass is a queue, not batches.

    `asyncio.gather` over a slice is a barrier, and reviews are wildly uneven —
    seconds to record a finding, minutes to grep to the call ceiling, the whole
    `FILE_REVIEW_TIMEOUT_S` to hang. Under batching the other lanes sat idle for the
    difference, which against a fixed `OVERALL_TIMEOUT_S` is files that never get
    reviewed. Asserted structurally rather than with a stopwatch: with two lanes and
    a slow first file, the third and fourth files must *start* before the first one
    finishes — under batching they could not have.
    """
    delay = {"m/slow.sql": 0.4}
    calls: dict = {}
    events: list = []

    class Paced(Scripted):
        """Records one empty finding per file, after sleeping that file's delay."""

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            asked = next(m for m in messages if getattr(m, "type", "") == "human")
            path = str(asked.content).replace("Review", "", 1).strip()
            seen = calls.get(path, 0)
            calls[path] = seen + 1
            if seen == 0:
                events.append(("start", path))
                await asyncio.sleep(delay.get(path, 0.02))
                message = AIMessage(content="", tool_calls=[
                    {"name": "record_findings", "id": "r",
                     "args": {"file_path": path, "findings": []}}])
            else:
                events.append(("finish", path))
                message = AIMessage(content="done")
            return ChatResult(generations=[ChatGeneration(message=message)])

    files = [(f"m/{n}.sql", "sql") for n in ("slow", "b", "c", "d")]
    result = asyncio.run(review_changed_files(
        classified=files, clone_root=str(checkout), source_branch=None,
        mr_context="", model=Paced(), concurrency=2, provider_policy=False))

    assert result.reviewed_files == {p for p, _ in files}
    assert result.failed_files == [] and not result.truncated
    done = events.index(("finish", "m/slow.sql"))
    for path in ("m/c.sql", "m/d.sql"):
        assert events.index(("start", path)) < done, (
            f"{path} waited for the slow file's batch to drain: {events}")


def test_an_expired_budget_blames_only_the_files_the_pool_dispatched(checkout):
    """A pool cannot infer what it started from a batch index, and guessing would
    blame the budget for a file that genuinely failed. One lane, a deadline that
    expires while the first file runs: the first is a real failure, the second was
    never dispatched and is the re-run's problem."""
    import time

    class SlowSilent(Silent):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            await asyncio.sleep(0.3)
            return ChatResult(generations=[ChatGeneration(message=self._step(messages))])

    files = [("m/a.sql", "sql"), ("m/b.sql", "sql")]
    result = asyncio.run(review_changed_files(
        classified=files, clone_root=str(checkout), source_branch=None,
        mr_context="", model=SlowSilent(), concurrency=1,
        deadline=time.monotonic() + 0.1, provider_policy=False))

    assert result.truncated
    assert result.failed_files == ["m/a.sql"], "it ran and recorded nothing"


def test_nothing_to_review_is_not_an_error(checkout):
    result = asyncio.run(review_changed_files(
        classified=[("README.md", "unknown")], clone_root=str(checkout),
        source_branch=None, mr_context="", model=Scripted(), provider_policy=False))
    assert result.all_comments == [] and result.failed_files == []


class Thrasher(Scripted):
    """Investigates until its call budget is gone, then records when told to.

    The production shape: every tool round trip is a model call, so a reviewer that
    keeps grepping reaches the ceiling with nothing recorded — `record_findings` is
    the only channel out and it never got there.
    """

    def _step(self, messages):
        told_to_report = any(
            "used your entire tool budget" in str(getattr(m, "content", "") or "")
            for m in messages
        )
        if told_to_report:
            return AIMessage(content="", tool_calls=[
                {"name": "record_findings", "id": "r", "args": {
                    "file_path": "svc/order.go",
                    "findings": [{"problem": "Save() error dropped", "line": 4,
                                  "severity": "high"}]}}])
        n = sum(1 for m in messages if isinstance(m, ToolMessage))
        return AIMessage(content="", tool_calls=[
            {"name": "grep", "id": f"g{n}", "args": {"pattern": f"probe{n}", "path": "svc"}}])


def test_running_out_of_calls_no_longer_discards_the_investigation(checkout, monkeypatch, capsys):
    """Observed on a production run: ten files investigated to the last call,
    every reviewer ending 'Model call limits exceeded: run limit (25/25)', and not one
    finding recorded. The reading and grepping is the expensive part and it is already
    paid for, so the ceiling now buys one bounded pass to get it reported."""
    monkeypatch.setenv("REVIEWER_CALL_LIMIT", "4")

    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=Thrasher(),
        provider_policy=False))

    assert result.failed_files == [], "the file should have been salvaged, not failed"
    assert "svc/order.go" in result.reviewed_files
    assert sum(len(issues) for _p, issues in result.all_comments) >= 1
    assert "ran out of calls" in capsys.readouterr().out


def test_the_call_ceiling_is_configurable():
    from core.agents.deep_reviewer import DEFAULT_REVIEWER_CALL_LIMIT, reviewer_call_limit
    import os

    assert DEFAULT_REVIEWER_CALL_LIMIT == 50, "25 was under what one investigation costs"
    os.environ["REVIEWER_CALL_LIMIT"] = "0"
    try:
        assert reviewer_call_limit() == DEFAULT_REVIEWER_CALL_LIMIT, "0 is not a ceiling"
    finally:
        del os.environ["REVIEWER_CALL_LIMIT"]


class SlowThrasher(Thrasher):
    """Every call is slow, the way a reasoning model at high effort is.

    Observed on a large production MR: 62 calls, fifteen of them 36–121 s of
    reasoning, 1119 s of model time — the watchdog fired well before the call ceiling
    and the reviewer's whole investigation was discarded.
    """

    delay_s: float = 0.15

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.delay_s)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def test_a_slow_reviewer_is_braked_and_salvaged_before_the_watchdog(checkout, capsys):
    """The call ceiling bounds calls, not time. The time brake ends the reviewer cleanly
    at a share of FILE_REVIEW_TIMEOUT_S, so it goes through the same salvage pass as one
    that ran out of calls instead of being cancelled with nothing recorded."""
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=SlowThrasher(),
        per_file_timeout_s=2.0, provider_policy=False))

    out = capsys.readouterr().out
    assert result.failed_files == [], out
    assert sum(len(issues) for _p, issues in result.all_comments) >= 1
    assert "ran out of time" in out
    assert "timed out" not in out, "the brake should fire before the watchdog"


class Hanger(Thrasher):
    """Greps a few times, then makes one call that never comes back.

    The brake is checked between calls, so it cannot interrupt this; the watchdog
    does, and what was read before it fired must still be reported."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        told = any("used your entire tool budget" in str(getattr(m, "content", "") or "")
                   for m in messages)
        if not told and sum(1 for m in messages if isinstance(m, ToolMessage)) >= 2:
            await asyncio.sleep(60)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def test_a_watchdog_timeout_still_salvages_what_was_read(checkout, capsys):
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch="feature/x", mr_context="", model=Hanger(),
        per_file_timeout_s=1.0, provider_policy=False))

    out = capsys.readouterr().out
    assert "timed out" in out
    assert result.failed_files == [], out
    assert sum(len(issues) for _p, issues in result.all_comments) >= 1
