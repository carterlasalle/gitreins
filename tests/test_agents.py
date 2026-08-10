"""
Unit tests for engine/agents — generic bounded agent runtime (R2.2).

Covers: iteration cap enforcement, wall-clock cap enforcement, token
accounting + budget exceeded, tool dedup, bounded file reads, sandbox
scratch state, output-schema parsing, and AgentRunner.run returning an
output_schema-typed result with a stub LLM.
"""

import os
import time
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from engine.agents import (
    AgentRunError,
    AgentRunner,
    Budget,
    BudgetExceededError,
    SchemaError,
    Tool,
    ToolRegistry,
    make_read_file_tool,
    parse_response,
    read_file_bounded,
    sandbox_tools,
    schema_to_prompt,
)
from engine.llm import LLMClient, LLMResponse, LLMUsage, ToolCall
from engine.router import ModelRouter


# ── Stub LLM (fake-client pattern from tests/test_evaluator.py) ────────────


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls.

    A response may be a callable returning an LLMResponse (for time-based
    behavior); the last response repeats for over-calls.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        self.calls += 1
        self.messages_seen.append(list(messages))
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


class FakeRouter:
    """Minimal ModelRouter stand-in returning one fixed client for any role."""

    def __init__(self, client):
        self.client = client
        self.roles = []

    def for_role(self, role: str) -> LLMClient:
        self.roles.append(role)
        return self.client  # type: ignore[return-value]  # StubLLM is duck-typed


def tool_response(name="echo", arguments=None, usage=None):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name=name, arguments=arguments or {})],
        usage=usage,
    )


def content_response(text, usage=None):
    return LLMResponse(content=text, usage=usage)


def make_echo_tool(calls=None):
    def _echo(x=0):
        if calls is not None:
            calls.append(x)
        return {"x": x}

    return Tool(
        name="echo",
        description="Echo the argument",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        fn=_echo,
    )


# ── Output schema used across tests ────────────────────────────────────────


@dataclass
class Item:
    criterion: str
    status: str
    detail: str = ""


@dataclass
class Verdict:
    verdict: str
    items: list[Item] = field(default_factory=list)
    summary: str = ""


VERDICT_JSON = (
    '{"verdict": "COMPLETE", "items": [{"criterion": "c1", "status": "PASS", '
    '"detail": "evidence"}], "summary": "ok"}'
)


# ── (a) Iteration cap ──────────────────────────────────────────────────────


class TestIterationCap:
    def test_iteration_cap_raises(self, tmp_workdir):
        """A model that never finishes is stopped at the iteration cap."""
        stub = StubLLM([tool_response("echo", {"x": 1})])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        budget = Budget(max_iterations=2)
        with pytest.raises(BudgetExceededError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[make_echo_tool()],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        assert "Iteration cap" in str(ei.value)
        # Lenient pre-check allows the final call: exactly 2 LLM turns
        assert stub.calls == 2

    def test_iteration_cap_wrong_format_loop(self, tmp_workdir):
        """Persistent schema-parse failures also terminate at the cap."""
        stub = StubLLM([content_response("this is not json")])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        budget = Budget(max_iterations=3)
        with pytest.raises(BudgetExceededError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        assert "Cap exceeded" in str(ei.value)
        assert "schema error" in str(ei.value).lower()
        assert stub.calls == 3


# ── (b) Wall-clock cap ─────────────────────────────────────────────────────


class TestWallClockCap:
    def test_wall_clock_cap_raises(self, tmp_workdir):
        """A slow model is stopped once the wall-clock budget is exhausted."""

        def slow_response():
            time.sleep(0.1)
            return tool_response("echo", {"x": 1})

        stub = StubLLM([slow_response])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        budget = Budget(max_time=0.05)
        with pytest.raises(BudgetExceededError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[make_echo_tool()],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        assert "Time cap" in str(ei.value)
        assert stub.calls == 1

    def test_remaining_seconds_states(self):
        b = Budget()
        assert b.remaining_seconds() == -1.0  # unlimited
        b2 = Budget(max_time=30)
        assert b2.remaining_seconds() == 30.0  # not started yet
        b2.start()
        assert 0 < b2.remaining_seconds() <= 30.0


# ── (c) Token accounting + budget exceeded ─────────────────────────────────


class TestTokenAccounting:
    def test_token_accounting_and_budget_exceeded(self, tmp_workdir):
        """Input tokens accumulate and trip the budget mid-run."""
        usage = LLMUsage(prompt_tokens=100, completion_tokens=10)
        stub = StubLLM([tool_response("echo", {"x": 1}, usage=usage)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        budget = Budget(max_input_tokens=250, max_output_tokens=1000)
        with pytest.raises(BudgetExceededError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[make_echo_tool()],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        assert "Input token budget" in str(ei.value)
        assert stub.calls == 3  # 100 + 100 + 100 → 300 >= 250
        assert budget.cumulative_input_tokens == 300
        assert budget.cumulative_output_tokens == 30

    def test_output_token_budget(self, tmp_workdir):
        usage = LLMUsage(prompt_tokens=1, completion_tokens=60)
        stub = StubLLM([tool_response("echo", {"x": 1}, usage=usage)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        budget = Budget(max_output_tokens=100)
        with pytest.raises(BudgetExceededError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[make_echo_tool()],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        assert "Output token budget" in str(ei.value)

    def test_tool_call_weight_accounting(self):
        """Tool calls cost tool_call_weight iterations (default 0.1)."""
        budget = Budget(max_iterations=1)
        budget.start()
        assert budget.track(iterations=budget.tool_call_weight) is None
        assert budget.iteration_credit == 0.1
        # One full LLM turn is still allowed (lenient pre-check)…
        assert budget.track(iterations=1.0) is None
        assert budget.iteration_credit == 1.1
        # …but the next step trips the cap
        assert budget.track(iterations=0.1) is not None


# ── (d) Tool dedup ─────────────────────────────────────────────────────────


class TestToolDedup:
    def test_registry_identical_call_is_duplicate(self):
        reg = ToolRegistry(dedup_window=8)
        assert reg.check("echo", {"x": 1}) is False
        assert reg.check("echo", {"x": 1}) is True
        assert reg.check("echo", {"x": 2}) is False  # different args
        assert reg.check("echo", {"y": 1}) is False  # different key
        reg.reset()
        assert reg.check("echo", {"x": 1}) is False  # fresh window

    def test_registry_window_evicts_old_calls(self):
        reg = ToolRegistry(dedup_window=3)
        assert reg.check("echo", {"x": 1}) is False
        assert reg.check("echo", {"x": 2}) is False
        assert reg.check("echo", {"x": 3}) is False
        assert reg.check("echo", {"x": 4}) is False  # evicts x=1
        assert reg.check("echo", {"x": 2}) is True  # x=2 still in window
        assert reg.check("echo", {"x": 1}) is False  # x=1 no longer in window

    def test_runner_adds_dedup_warning(self, tmp_workdir):
        """Repeat call executes but the model is told it already did this."""
        calls = []
        stub = StubLLM(
            [
                tool_response("echo", {"x": 1}),
                tool_response("echo", {"x": 1}),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[make_echo_tool(calls)],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert len(calls) == 2  # duplicates still execute by default
        warnings = sum(
            1
            for msgs in stub.messages_seen
            for m in msgs
            if m.get("role") == "tool" and "_dedup_warning" in m.get("content", "")
        )
        assert warnings == 1

    def test_runner_skip_duplicates(self, tmp_workdir):
        """skip_duplicates=True short-circuits the repeat call entirely."""
        calls = []
        stub = StubLLM(
            [
                tool_response("echo", {"x": 1}),
                tool_response("echo", {"x": 1}),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir, skip_duplicates=True)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[make_echo_tool(calls)],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert len(calls) == 1  # duplicate never executed
        assert isinstance(result, Verdict)


# ── (e) Bounded file reads ─────────────────────────────────────────────────


class TestBoundedFileRead:
    def test_read_line_cap(self, tmp_workdir):
        """Huge file: first-call read is capped at 400 lines with a note."""
        path = os.path.join(tmp_workdir, "big.txt")
        with open(path, "w") as f:
            f.write("\n".join(f"line{i}" for i in range(20000)))
        res = read_file_bounded("big.txt", base_dir=tmp_workdir)
        assert "error" not in res
        assert res["total_lines"] == 20000
        assert res["has_more"] is True
        assert res["shown_lines"] == 20000  # metadata: file size
        assert "showing first 400 of 20000 lines" in res["content"]

    def test_read_byte_cap(self, tmp_workdir):
        """max_bytes hard-caps the returned content in both modes."""
        path = os.path.join(tmp_workdir, "wide.txt")
        with open(path, "w") as f:
            f.write("x" * 100_000)
        res = read_file_bounded("wide.txt", base_dir=tmp_workdir, max_bytes=1000)
        assert res["capped"] is True
        assert "capped at 1000 bytes" in res["content"]
        assert len(res["content"]) < 3000

    def test_read_byte_mode_bounds(self, tmp_workdir):
        path = os.path.join(tmp_workdir, "data.bin")
        with open(path, "wb") as f:
            f.write(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        res = read_file_bounded(
            "data.bin", base_dir=tmp_workdir, mode="bytes", byte_offset=5, byte_limit=5
        )
        assert res["content"] == "FGHIJ"
        assert res["total_bytes"] == 26
        assert res["shown_bytes"] == 5
        assert res["has_more"] is True

    def test_read_offset_limit(self, tmp_workdir):
        path = os.path.join(tmp_workdir, "f.txt")
        with open(path, "w") as f:
            f.write("\n".join(f"line{i}" for i in range(10)) + "\n")
        res = read_file_bounded("f.txt", base_dir=tmp_workdir, offset=2, limit=3)
        assert res["content"] == "line1\nline2\nline3\n"
        assert res["total_lines"] == 10
        assert res["shown_lines"] == 3

    def test_read_traversal_blocked(self, tmp_workdir):
        res = read_file_bounded("../outside.txt", base_dir=tmp_workdir)
        assert "error" in res
        assert "outside" in res["error"]

    def test_read_missing_file(self, tmp_workdir):
        res = read_file_bounded("nope.txt", base_dir=tmp_workdir)
        assert "error" in res

    def test_read_file_tool_scope_enforcement(self, tmp_workdir):
        path = os.path.join(tmp_workdir, "a.py")
        with open(path, "w") as f:
            f.write("x = 1\n")
        tool = make_read_file_tool(tmp_workdir, allowed_files={"a.py"})
        assert tool.fn is not None
        assert tool.fn("a.py")["content"] == "x = 1\n"
        assert "not in scope" in tool.fn("b.py")["error"]
        # Tool renders in OpenAI function-calling format
        schema = tool.to_llm_schema()
        assert schema["function"]["name"] == "read_file"


# ── Budget unit tests ──────────────────────────────────────────────────────


class TestBudget:
    def test_from_config(self, monkeypatch):
        """from_config mirrors the .gitreins/config.yaml evaluator block."""
        for k in (
            "GITREINS_MAX_ITERATIONS",
            "GITREINS_MAX_TIME",
            "GITREINS_MAX_INPUT_TOKENS",
            "GITREINS_MAX_OUTPUT_TOKENS",
        ):
            monkeypatch.delenv(k, raising=False)
        cfg = {
            "evaluator": {
                "max_iterations": 42,
                "max_time": "5m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
                "tool_call_weight": 0.1,
                "compaction_threshold": 0.9,
                "code_context_budget": 0.7,
            }
        }
        b = Budget.from_config(cfg)
        assert b.max_iterations == 42.0
        assert b.max_time == 300.0
        assert b.max_input_tokens == 10_000_000
        assert b.max_output_tokens == 1_000_000
        assert b.tool_call_weight == 0.1
        assert b.compaction_threshold == 0.9
        assert b.code_context_budget == 0.7

    def test_from_config_empty_is_unlimited_defaults(self, monkeypatch):
        for k in (
            "GITREINS_MAX_ITERATIONS",
            "GITREINS_MAX_TIME",
            "GITREINS_MAX_INPUT_TOKENS",
            "GITREINS_MAX_OUTPUT_TOKENS",
        ):
            monkeypatch.delenv(k, raising=False)
        b = Budget.from_config({})
        # GitReinsDefaults: 100 iterations, 10M input, 128k output, no time cap
        assert b.max_iterations == 100.0
        assert b.max_time == -1.0
        assert b.max_input_tokens == 10_000_000
        assert b.max_output_tokens == 131_072
        assert b.compaction_threshold == 0.9

    def test_track_exceeded(self):
        b = Budget(max_iterations=10, max_input_tokens=1000)
        b.start()
        assert b.exceeded() is None
        assert b.track(prompt_tokens=600) is None
        assert b.track(prompt_tokens=600) is not None  # 1200 >= 1000
        assert b.iteration_credit == 2.0
        assert b.cumulative_input_tokens == 1200

    def test_iteration_precheck_leniency(self):
        b = Budget(max_iterations=2)
        b.start()
        assert b.track() is None  # 0 < 2 → credit 1.0
        assert b.track() is None  # 1.0 < 2 → credit 2.0
        assert b.track() is not None  # 2.0 >= 2 → iteration cap
        msg = b.track()
        assert msg is not None and "Iteration cap" in msg

    def test_reset_context_tracking_keeps_iterations(self):
        b = Budget(max_iterations=10)
        b.start()
        b.track(prompt_tokens=500)
        b.reset_context_tracking()
        assert b.cumulative_input_tokens == 0
        assert b.iteration_credit == 1.0  # spans the whole run


# ── Sandbox scratch state ──────────────────────────────────────────────────


class TestSandbox:
    def test_sandbox_read_write(self, tmp_workdir):
        stub = StubLLM(
            [
                tool_response("sandbox_write", {"key": "verified_0", "content": "PASS: x"}),
                tool_response("sandbox_read", {"key": "verified_0"}),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert runner.sandbox == {"verified_0": "PASS: x"}
        # The sandbox_read result was fed back to the model
        tool_contents = [
            m.get("content", "")
            for msgs in stub.messages_seen
            for m in msgs
            if m.get("role") == "tool"
        ]
        assert any('"content": "PASS: x"' in c for c in tool_contents)

    def test_sandbox_tools_are_never_time_critical(self):
        for tool in sandbox_tools({}):
            assert tool.time_critical is False

    def test_sandbox_resets_per_run(self, tmp_workdir):
        stub = StubLLM([content_response(VERDICT_JSON)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        runner._sandbox["leftover"] = "v"  # contaminate between runs
        runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert runner.sandbox == {}

    def test_custom_sandbox_tools_not_overridden(self, tmp_workdir):
        """Caller-provided sandbox_read/sandbox_write win over the defaults."""
        custom = []

        def _write(key, content):
            custom.append((key, content))
            return {"key": key, "written": 0}

        tools = [
            Tool(
                name="sandbox_write",
                description="custom",
                parameters={"type": "object", "properties": {}},
                fn=_write,
            )
        ]
        stub = StubLLM(
            [
                tool_response("sandbox_write", {"key": "k", "content": "v"}),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=tools,
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert custom == [("k", "v")]


# ── Output-schema helpers ──────────────────────────────────────────────────


class TestSchemas:
    def test_schema_to_prompt_dataclass(self):
        prompt = schema_to_prompt(Verdict)
        assert '"verdict"' in prompt
        assert '"items"' in prompt
        assert '"summary"' in prompt
        assert "list[Item]" in prompt

    def test_parse_response_plain(self):
        v = parse_response(VERDICT_JSON, Verdict)
        assert isinstance(v, Verdict)
        assert v.verdict == "COMPLETE"
        assert isinstance(v.items[0], Item)  # nested dataclass instantiated
        assert v.items[0].status == "PASS"

    def test_parse_response_markdown_fence(self):
        v = parse_response("```json\n" + VERDICT_JSON + "\n```", Verdict)
        assert isinstance(v, Verdict)

    def test_parse_response_surrounding_text(self):
        v = parse_response("Here is my answer: " + VERDICT_JSON + " hope that helps", Verdict)
        assert isinstance(v, Verdict)
        assert v.verdict == "COMPLETE"

    def test_parse_response_invalid_json(self):
        with pytest.raises(SchemaError):
            parse_response("this is not json at all", Verdict)

    def test_parse_response_missing_required_field(self):
        with pytest.raises(SchemaError):
            parse_response('{"criterion": "c"}', Item)  # status required, no default

    def test_parse_response_coercion(self):
        @dataclass
        class Nums:
            count: int
            ratio: float
            flag: bool

        n = parse_response('{"count": "42", "ratio": "1.5", "flag": "true"}', Nums)
        assert n.count == 42
        assert n.ratio == 1.5
        assert n.flag is True

    def test_parse_response_typed_dict(self):
        from typing import TypedDict

        class Summary(TypedDict):
            verdict: str
            score: int

        s = parse_response('{"verdict": "COMPLETE", "score": "7"}', Summary)
        assert s == {"verdict": "COMPLETE", "score": 7}


# ── (f) AgentRunner.run returns output_schema-typed result ─────────────────


class TestCompaction:
    def test_proactive_compaction_at_threshold(self, tmp_workdir):
        """Context near the input-token threshold triggers compaction."""
        compactions = []

        def on_compact(messages, count):
            compactions.append(count)
            return [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "resume from sandbox"},
            ]

        usage = LLMUsage(prompt_tokens=900, completion_tokens=1)
        stub = StubLLM([tool_response("echo", {"x": 1}, usage=usage)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir, on_compact=on_compact)
        budget = Budget(
            max_iterations=5, max_input_tokens=1000, compaction_threshold=0.8
        )  # threshold = 800 tokens
        with pytest.raises(BudgetExceededError):
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[make_echo_tool()],
                output_schema=Verdict,
                model_role="scout",
                budget=budget,
            )
        # Compaction fired every time context crossed 800 tokens (3 max)
        assert compactions == [0, 1, 2]
        # Token tracking reset per window: only the last window's tokens remain
        assert budget.cumulative_input_tokens == 1800  # 2 calls x 900 in final window

    def test_context_error_triggers_compaction(self, tmp_workdir):
        """A 4xx context-window error is recovered by compacting + retrying."""
        import requests

        class _FakeResponse:
            status_code = 400

        class ContextError(requests.HTTPError):
            def __init__(self):
                super().__init__("Error code: 400 - context_length_exceeded")
                self.response = _FakeResponse()  # type: ignore[assignment]

        class Flaky(StubLLM):
            def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
                self.calls += 1
                if self.calls == 1:
                    raise ContextError()
                return content_response(VERDICT_JSON)

        stub = Flaky([content_response(VERDICT_JSON)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert stub.calls == 2  # compacted once, then succeeded


class TestRunnerTypedResult:
    def test_run_returns_typed_result(self, tmp_workdir):
        stub = StubLLM(
            [content_response(VERDICT_JSON, usage=LLMUsage(prompt_tokens=7, completion_tokens=3))]
        )
        router = FakeRouter(stub)
        runner = AgentRunner(router=router, workdir=tmp_workdir)
        budget = Budget()
        result = runner.run(
            system_prompt="sys",
            user_prompt="usr",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=budget,
        )
        assert isinstance(result, Verdict)
        assert result.verdict == "COMPLETE"
        assert result.items[0].criterion == "c1"
        assert budget.cumulative_input_tokens == 7
        assert budget.cumulative_output_tokens == 3
        assert router.roles == ["scout"]  # resolved through the router

    def test_run_resolves_role_via_model_router(self, tmp_workdir):
        """run() resolves the client through ModelRouter.for_role(role)."""
        config = {
            "review": {
                "models": {
                    "scout": {
                        "provider": "openai",
                        "base_url": "https://test.local/v1",
                        "model": "stub-model",
                    }
                }
            }
        }
        router = ModelRouter(config)
        runner = AgentRunner(router=router, workdir=tmp_workdir)
        client = router.for_role("scout")  # cached instance run() will hit
        with patch.object(
            client,
            "chat",
            return_value=LLMResponse(content=VERDICT_JSON, usage=LLMUsage(prompt_tokens=1)),
        ):
            result = runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[],
                output_schema=Verdict,
                model_role="scout",
                budget=Budget(),
            )
        assert isinstance(result, Verdict)
        assert result.verdict == "COMPLETE"

    def test_run_llm_failure_raises(self, tmp_workdir):
        class Boom:
            def chat(self, *a, **k):
                raise RuntimeError("connection refused")

        runner = AgentRunner(router=FakeRouter(Boom()), workdir=tmp_workdir)
        with pytest.raises(AgentRunError):
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[],
                output_schema=Verdict,
                model_role="scout",
                budget=Budget(),
            )

    def test_run_empty_response_raises_after_retries(self, tmp_workdir):
        """Persistently empty responses raise only after the retry budget is spent.

        An empty response is not a transport error (engine/llm.py retries those),
        so the runner nudges the model up to ``max_empty_retries`` times before
        giving up — exactly 1 initial call + N corrective retries, then raise.
        """
        stub = StubLLM([LLMResponse(content=None, tool_calls=[])])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        with pytest.raises(AgentRunError) as ei:
            runner.run(
                system_prompt="s",
                user_prompt="u",
                tools=[],
                output_schema=Verdict,
                model_role="scout",
                budget=Budget(),
            )
        assert "empty response" in str(ei.value)
        assert stub.calls == runner.max_empty_retries + 1
        # Each retry appended a corrective user message to the conversation
        nudges = [
            m
            for m in stub.messages_seen[-1]
            if m.get("role") == "user" and "was empty" in m.get("content", "")
        ]
        assert len(nudges) == runner.max_empty_retries

    def test_run_empty_response_retries_then_succeeds(self, tmp_workdir):
        """An empty response is followed by a corrective nudge, then succeeds."""
        stub = StubLLM(
            [
                LLMResponse(content=None, tool_calls=[]),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert result.verdict == "COMPLETE"
        assert stub.calls == 2
        nudge = stub.messages_seen[1][-1]
        assert nudge["role"] == "user"
        assert "was empty" in nudge["content"]

    def test_run_blank_whitespace_response_retries(self, tmp_workdir):
        """Whitespace-only content counts as empty and recovers with a nudge."""
        stub = StubLLM(
            [
                LLMResponse(content="   \n\t  ", tool_calls=[]),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert result.verdict == "COMPLETE"
        assert stub.calls == 2

    def test_run_empty_retry_counter_resets_on_tool_calls(self, tmp_workdir):
        """The empty-retry budget resets whenever a non-empty response arrives.

        Three isolated empties (each separated by a tool-call turn) would
        exhaust a non-resetting counter and raise; with the per-response reset
        each empty is retry #1, so the run completes.
        """
        stub = StubLLM(
            [
                LLMResponse(content=None, tool_calls=[]),
                tool_response("echo", {"x": 1}),
                LLMResponse(content=None, tool_calls=[]),
                tool_response("echo", {"x": 2}),
                LLMResponse(content=None, tool_calls=[]),
                content_response(VERDICT_JSON),
            ]
        )
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[make_echo_tool()],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        assert result.verdict == "COMPLETE"
        assert stub.calls == 6

    def test_run_unknown_tool_reported(self, tmp_workdir):
        """An unregistered tool name is reported back to the model as an error."""
        stub = StubLLM([tool_response("ghost", {"x": 1}), content_response(VERDICT_JSON)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        tool_msgs = [
            m.get("content", "")
            for msgs in stub.messages_seen
            for m in msgs
            if m.get("role") == "tool"
        ]
        assert tool_msgs and "Unknown tool: ghost" in tool_msgs[0]

    def test_run_tool_exception_is_reported_not_raised(self, tmp_workdir):
        def _boom(x=0):
            raise RuntimeError("tool exploded")

        tool = Tool(
            name="boom",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            fn=_boom,
        )
        stub = StubLLM([tool_response("boom"), content_response(VERDICT_JSON)])
        runner = AgentRunner(router=FakeRouter(stub), workdir=tmp_workdir)
        result = runner.run(
            system_prompt="s",
            user_prompt="u",
            tools=[tool],
            output_schema=Verdict,
            model_role="scout",
            budget=Budget(),
        )
        assert isinstance(result, Verdict)
        tool_msgs = [
            m.get("content", "")
            for msgs in stub.messages_seen
            for m in msgs
            if m.get("role") == "tool"
        ]
        assert tool_msgs and "tool exploded" in tool_msgs[0]
