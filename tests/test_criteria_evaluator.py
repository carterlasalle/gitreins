"""
Unit tests for the R2.3 refactor: CriteriaEvaluator on AgentRunner.

Covers the wiring between engine/evaluator.py and the generic bounded
runtime (engine/agents/runner.py): class structure (subclass + alias),
evaluate() delegating to AgentRunner.run(), budget/tool/schema wiring,
the verdict parser hook, criteria-progress compaction, and the retained
MANDATORY TEST VERIFICATION hard-rule + scan_security tool.
"""

import os

from engine.agents import AgentRunner, Budget
from engine.evaluator import (
    AgenticEvaluator,
    CriteriaEvaluator,
    EVALUATOR_SYSTEM_PROMPT,
    EVALUATOR_TOOLS,
    Verdict,
)
from engine.llm import LLMResponse, LLMUsage, ToolCall


# ── Stub LLM (fake-client pattern from tests/test_agents.py) ───────────────


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_seen = []
        self.tools_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        self.calls += 1
        self.messages_seen.append(list(messages))
        self.tools_seen.append(tools)
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


def tool_response(name, arguments=None, usage=None):
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name=name, arguments=arguments or {})],
        usage=usage,
    )


def content_response(text, usage=None):
    return LLMResponse(content=text, usage=usage)


VERDICT_JSON = (
    '{"verdict": "COMPLETE", "items": [{"criterion": "c1", "status": "PASS", '
    '"detail": "tests/test_auth.py:45"}], "summary": "ok"}'
)


def make_evaluator(stub, workdir, **kwargs):
    return CriteriaEvaluator(stub, workdir, **kwargs)  # type: ignore[arg-type]


# ── Class structure ────────────────────────────────────────────────────────


class TestClassStructure:
    def test_criteria_evaluator_subclasses_agent_runner(self):
        """CriteriaEvaluator re-expresses the evaluator on the AgentRunner."""
        assert issubclass(CriteriaEvaluator, AgentRunner)

    def test_agentic_evaluator_is_alias(self):
        """Backward-compat: AgenticEvaluator is the same class."""
        assert AgenticEvaluator is CriteriaEvaluator

    def test_evaluate_api_unchanged(self, tmp_workdir):
        """evaluate(task) -> Verdict, constructor keeps legacy args."""
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert isinstance(verdict, Verdict)
        assert verdict.verdict == "COMPLETE"


# ── evaluate() delegates to AgentRunner.run() ──────────────────────────────


class TestEvaluateDelegatesToRunner:
    def test_run_loop_drives_evaluation(self, tmp_workdir):
        """Tool call → tool executed → verdict, all through the runner loop."""
        stub = StubLLM(
            [
                tool_response("get_task_item", {"id": "t1"}),
                content_response(VERDICT_JSON),
            ]
        )
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        verdict = ev.evaluate({"id": "t1", "title": "Test task", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"
        assert verdict.items[0].criterion == "c1"
        # Tool result was fed back to the model (task definition from _task_index)
        tool_msgs = [
            m.get("content", "")
            for msgs in stub.messages_seen
            for m in msgs
            if m.get("role") == "tool"
        ]
        assert tool_msgs and '"title": "Test task"' in tool_msgs[0]

    def test_task_prompt_keeps_criteria_format(self, tmp_workdir):
        """TASK → criterion list → get_task_item → verdict (Lane A prompt)."""
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        ev.evaluate(
            {
                "id": "t1",
                "title": "Implement login",
                "criteria": ["Accepts email+password", "Returns JWT"],
            }
        )
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "TASK: Implement login" in user_prompt
        assert "CRITERIA TO VERIFY (all 2 must be checked)" in user_prompt
        assert "1. Accepts email+password" in user_prompt
        assert "2. Returns JWT" in user_prompt
        assert 'get_task_item("t1")' in user_prompt

    def test_sandbox_cleared_per_run(self, tmp_workdir):
        """A fresh run starts with an empty sandbox."""
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        ev._sandbox["leftover"] = "v"
        ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert ev.sandbox == {}


# ── Tool wiring (runner registry) ──────────────────────────────────────────


class TestToolWiring:
    def test_tools_passed_to_llm(self, tmp_workdir):
        """The runner exposes the evaluator's tools to the LLM."""
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert stub.tools_seen, "chat() was never called with tools"
        names = [t["function"]["name"] for t in stub.tools_seen[0]]
        assert "read_file" in names
        assert "run_command" in names
        assert "search_pattern" in names
        assert "scan_security" in names  # ast-grep security scan retained
        assert "sandbox_write" in names
        assert "sandbox_read" in names
        # read_static_analysis is config-gated off by default
        assert "read_static_analysis" not in names

    def test_read_static_analysis_config_gated(self, tmp_workdir):
        """static_analysis_diagnostics: true re-enables the tool."""
        import yaml

        cdir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "config.yaml"), "w") as f:
            yaml.dump({"evaluator": {"static_analysis_diagnostics": True}}, f)
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        names = [t["function"]["name"] for t in stub.tools_seen[0]]
        assert "read_static_analysis" in names

    def test_sandbox_tools_never_time_critical(self, tmp_workdir):
        """sandbox_read/write survive the runner's wall-clock gating."""
        ev = make_evaluator(StubLLM([]), tmp_workdir, max_iterations=5)
        tools = {t.name: t for t in ev._build_tools({})}
        assert tools["sandbox_write"].time_critical is False
        assert tools["sandbox_read"].time_critical is False
        assert tools["scan_security"].time_critical is True

    def test_scan_security_tool_definition_retained(self):
        """scan_security is still in EVALUATOR_TOOLS (ast-grep, R2.3 AC-2)."""
        names = [t["function"]["name"] for t in EVALUATOR_TOOLS]
        assert "scan_security" in names
        desc = next(
            t["function"]["description"]
            for t in EVALUATOR_TOOLS
            if t["function"]["name"] == "scan_security"
        )
        assert "ast-grep" in desc


# ── Budget wiring ──────────────────────────────────────────────────────────


class TestBudgetWiring:
    def test_eval_cap_string_drives_budget(self, tmp_workdir):
        """eval_cap string caps the run; exhaustion returns INCOMPLETE."""
        stub = StubLLM([tool_response("read_file", {"path": "nope.py"})] * 3)
        ev = make_evaluator(stub, tmp_workdir, eval_cap="2")
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert "Increase max_iterations" in verdict.summary
        assert "2" in verdict.summary

    def test_max_iterations_param_drives_budget(self, tmp_workdir):
        """Legacy max_iterations param still caps the run."""
        stub = StubLLM([tool_response("read_file", {"path": "nope.py"})] * 3)
        ev = make_evaluator(stub, tmp_workdir, max_iterations=1)
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "split criteria" in verdict.summary

    def test_tool_call_weight_accounted(self, tmp_workdir):
        """Tool calls consume fractional iterations (tool_call_weight)."""
        budget = Budget(max_iterations=1)
        budget.start()
        assert budget.track(iterations=budget.tool_call_weight) is None
        assert budget.iteration_credit == 0.1

    def test_make_budget_mirrors_eval_cap(self, tmp_workdir):
        """_make_budget converts the resolved EvalCap into a run Budget."""
        ev = make_evaluator(StubLLM([]), tmp_workdir, eval_cap="100/30m/200k/50k")
        b = ev._make_budget({})
        assert b.max_iterations == 100.0
        assert b.max_time == 1800.0
        assert b.max_input_tokens == 200_000
        assert b.max_output_tokens == 50_000
        assert b.compaction_threshold == 0.9  # config default


# ── Verdict parser hook ────────────────────────────────────────────────────


class TestVerdictParserHook:
    def test_parser_normalizes_verdict_values(self, tmp_workdir):
        """ALMOST → INCOMPLETE, MAYBE → FAIL (legacy _parse_verdict semantics)."""
        stub = StubLLM(
            [
                content_response(
                    '{"verdict":"ALMOST","items":[{"criterion":"c1","status":"MAYBE",'
                    '"detail":"x"}],"summary":"s"}'
                )
            ]
        )
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert verdict.items[0].status == "FAIL"

    def test_parser_keyword_fallback(self, tmp_workdir):
        """Non-JSON verdict text falls back to keyword parsing, not a schema retry."""
        stub = StubLLM(
            [content_response("I have verified all criteria, everything passes and is complete.")]
        )
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"


# ── Partial verdict on cap (sandbox survives into the runner) ──────────────


class TestPartialVerdictOnCap:
    def test_partial_verdict_from_sandbox_on_cap(self, tmp_workdir):
        """Cap hit after sandbox_write yields a per-criterion partial verdict."""
        stub = StubLLM(
            [
                tool_response(
                    "sandbox_write", {"key": "verified_0", "content": "PASS: tests/test_auth.py:45"}
                ),
                tool_response("read_file", {"path": "nope.py"}),
                tool_response("read_file", {"path": "nope.py"}),
            ]
        )
        ev = make_evaluator(stub, tmp_workdir, eval_cap="2")
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1", "c2"]})
        assert verdict.verdict == "INCOMPLETE"  # not all criteria passed
        assert len(verdict.items) == 2
        assert verdict.items[0].status == "PASS"
        assert verdict.items[0].detail == "PASS: tests/test_auth.py:45"
        assert verdict.items[1].status == "FAIL"
        assert "Not verified" in verdict.items[1].detail


# ── Compaction wiring (criteria progress survives) ─────────────────────────


class TestCompactionWiring:
    def test_compaction_uses_evaluation_progress_prompt(self, tmp_workdir):
        """Context near the input-token threshold compacts to EVALUATION PROGRESS."""
        usage = LLMUsage(prompt_tokens=950, completion_tokens=5)
        stub = StubLLM(
            [
                tool_response("read_file", {"path": "a.py"}, usage=usage),
                content_response(VERDICT_JSON),
            ]
        )
        ev = make_evaluator(
            stub, tmp_workdir, eval_cap="10//1000/1000"
        )  # 10 iter, 1k in/out → threshold 900
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"
        assert stub.calls == 2  # compacted once between the two calls
        after_compact = stub.messages_seen[1]
        assert any(
            "EVALUATION PROGRESS" in m.get("content", "")
            for m in after_compact
            if m.get("role") == "user"
        )

    def test_context_error_recovers_via_compaction(self, tmp_workdir):
        """A 4xx context error compacts and retries instead of failing."""
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
                self.messages_seen.append(list(messages))
                self.tools_seen.append(tools)
                if self.calls == 1:
                    raise ContextError()
                return content_response(VERDICT_JSON)

        stub = Flaky([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        verdict = ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"
        assert stub.calls == 2


# ── Retained hard rules (R2.3 AC-2) ────────────────────────────────────────


class TestRetainedHardRules:
    def test_mandatory_test_verification_in_system_prompt(self, tmp_workdir):
        """The MANDATORY TEST VERIFICATION hard-rule is still in the prompt."""
        # The built-in prompt keeps the hard rule (R2.3 AC-2)
        assert "MANDATORY TEST VERIFICATION (HARD RULE" in EVALUATOR_SYSTEM_PROMPT
        assert "CRITERIA TRACKING (REQUIRED)" in EVALUATOR_SYSTEM_PROMPT
        # …and it is what actually reaches the LLM
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        ev.evaluate({"id": "t1", "title": "x", "criteria": ["c1"]})
        system_prompt = stub.messages_seen[0][0]["content"]
        assert "MANDATORY TEST VERIFICATION (HARD RULE" in system_prompt
        assert "COMPLETE" in system_prompt and "INCOMPLETE" in system_prompt

    def test_runner_wiring_surfaces(self, tmp_workdir):
        """The runner machinery is wired: parser hook + fixed-role router."""
        stub = StubLLM([content_response(VERDICT_JSON)])
        ev = make_evaluator(stub, tmp_workdir, max_iterations=5)
        # The verdict parser is the runner's final-answer parser
        assert ev._parser == ev._parse_verdict
        # The legacy llm constructor arg resolves through for_role()
        assert ev._router.for_role("evaluator") is stub
