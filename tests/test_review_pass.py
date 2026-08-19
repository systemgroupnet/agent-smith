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


def test_there_are_nine_reviewers_and_routing_is_total():
    assert len(REVIEWER_SPECS) == 9
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
        batch_size=2, per_file_timeout_s=30, provider_policy=False))
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
        batch_size=1, per_file_timeout_s=30, provider_policy=False))
    assert result.failed_files == ["svc/order.go"]
    assert result.reviewed_files == set()


def test_an_expired_budget_truncates_without_blaming_a_file(checkout):
    import time
    result = asyncio.run(review_changed_files(
        classified=[("svc/order.go", "service")], clone_root=str(checkout),
        source_branch=None, mr_context="", model=Scripted(),
        batch_size=1, deadline=time.monotonic() - 1, provider_policy=False))
    assert result.truncated
    assert result.failed_files == [], "the marker is held; a re-run picks these up"


def test_nothing_to_review_is_not_an_error(checkout):
    result = asyncio.run(review_changed_files(
        classified=[("README.md", "unknown")], clone_root=str(checkout),
        source_branch=None, mr_context="", model=Scripted(), provider_policy=False))
    assert result.all_comments == [] and result.failed_files == []
