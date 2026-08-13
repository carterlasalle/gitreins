"""
R2.8 tests for engine/review/verifier — the adversarial verifier + BLOCK gate.

Covers: the deterministic BLOCK policy truth-table (impact × causality ×
execution-path × confidence, with the 0.90/0.89 boundary and refuted
causality never blocking), VerifierFinding/VerifierFindings schema shape
(parse_response round-trip with nested dataclasses, required-field
enforcement), VerifierAgent.run parsing CONFIRMED and REFUTED verdicts from
a stubbed LLM, the tool belt (run_command / read_file / search_pattern plus
the 8 codeintel tools), the falsification system prompt, candidate →
user-prompt serialization (claim + verification_plan + evidence refs),
model-role routing to 'verifier', BLOCK integration on parsed output, and
budget-cap propagation.

Hermetic: the LLM is a deterministic StubLLM (pattern from
tests/test_review_reviewers.py) — no network, no real model, no tool
execution.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from engine.agents import Budget, BudgetExceededError
from engine.agents.schemas import parse_response, schema_to_prompt
from engine.llm import LLMClient, LLMResponse
from engine.review import (
    BLOCK,
    VerifierAgent,
    VerifierCandidate,
    VerifierFinding,
    VerifierFindings,
)
from engine.review.verifier import (
    BLOCK_CONFIDENCE_THRESHOLD,
    DEVELOPER_RELEVANCES,
    IMPACTS,
    PATCH_CAUSALITIES,
    VERDICTS,
    serialize_candidate,
)

# The DESIGN_v2.md §12-shaped CONFIRMED verdict (survived falsification).
CONFIRMED_JSON = (
    '{"findings": [{"finding_id": "F19", "impact": "high", '
    '"patch_causality": "confirmed", "reproducible": true, '
    '"execution_path_confirmed": true, "verifier_confidence": 0.94, '
    '"developer_relevance": "high", "verdict": "CONFIRMED", '
    '"notes": "Traced the caller guard; repro test crashes on deleted '
    'account — claim survived."}], '
    '"summary": "F19 survived falsification."}'
)

# A REFUTED verdict: the guard exists, claim is false, patch not at fault.
REFUTED_JSON = (
    '{"findings": [{"finding_id": "F19", "impact": "low", '
    '"patch_causality": "refuted", "reproducible": false, '
    '"execution_path_confirmed": false, "verifier_confidence": 0.20, '
    '"developer_relevance": "low", "verdict": "REFUTED", '
    '"notes": "get_user never returns None for deleted accounts; guard at '
    'session.py:180 covers it."}], '
    '"summary": "F19 refuted: caller guard present."}'
)


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
        self.tools_seen.append(tools or [])
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


def content_response(text):
    return LLMResponse(content=text)


def make_candidate(**overrides: Any):
    """The DESIGN_v2.md §9 candidate finding (F19)."""
    defaults = dict(
        finding_id="F19",
        file="auth/session.py",
        line=182,
        claim="Deleted users can reach this dereference with user=None.",
        trigger="refresh token belonging to deleted account",
        evidence=["E12", "E33", "E52"],
        verification_plan=[
            "inspect caller guard",
            "inspect get_user return contract",
            "run targeted regression test",
        ],
    )
    defaults.update(overrides)
    return VerifierCandidate(**defaults)


def make_verdict(**overrides: Any):
    """A maximally-blocking VerifierFinding (all §12 factors in its favor)."""
    defaults = dict(
        finding_id="F19",
        impact="high",
        patch_causality="confirmed",
        reproducible=True,
        execution_path_confirmed=True,
        verifier_confidence=0.95,
        developer_relevance="high",
        verdict="CONFIRMED",
        notes="",
    )
    defaults.update(overrides)
    return VerifierFinding(**defaults)


# ── (a) BLOCK policy truth-table ─────────────────────────────────────────


class TestBlockPolicy:
    def test_all_factors_true_blocks(self):
        assert BLOCK(make_verdict()) is True

    def test_all_factors_false_never_blocks(self):
        f = make_verdict(
            impact="info",
            patch_causality="refuted",
            execution_path_confirmed=False,
            verifier_confidence=0.0,
        )
        assert BLOCK(f) is False

    @pytest.mark.parametrize("impact", ["critical", "high"])
    def test_blocks_critical_and_high_impact(self, impact):
        assert BLOCK(make_verdict(impact=impact)) is True

    @pytest.mark.parametrize("impact", ["medium", "low", "info"])
    def test_never_blocks_lower_impact(self, impact):
        assert BLOCK(make_verdict(impact=impact)) is False

    @pytest.mark.parametrize("causality", ["unconfirmed", "refuted"])
    def test_never_blocks_unconfirmed_or_refuted_causality(self, causality):
        assert BLOCK(make_verdict(patch_causality=causality)) is False

    def test_refuted_causality_never_blocks_even_with_max_confidence(self):
        """A refuted-causality finding never blocks, whatever else is true."""
        f = make_verdict(patch_causality="refuted", verifier_confidence=0.99)
        assert BLOCK(f) is False

    def test_never_blocks_without_confirmed_execution_path(self):
        f = make_verdict(execution_path_confirmed=False, verifier_confidence=0.99)
        assert BLOCK(f) is False

    def test_boundary_confidence_0_90_blocks(self):
        assert BLOCK(make_verdict(verifier_confidence=0.90)) is True

    def test_boundary_confidence_0_89_never_blocks(self):
        assert BLOCK(make_verdict(verifier_confidence=0.89)) is False

    def test_confidence_threshold_constant_is_0_90(self):
        assert BLOCK_CONFIDENCE_THRESHOLD == 0.90

    def test_exhaustive_truth_table(self):
        """Every combination of the four factors — 5×3×2×4 = 120 rows."""
        confidences = (0.95, 0.90, 0.89, 0.0)
        seen = 0
        for impact in IMPACTS:
            for causality in PATCH_CAUSALITIES:
                for exec_path in (True, False):
                    for conf in confidences:
                        f = make_verdict(
                            impact=impact,
                            patch_causality=causality,
                            execution_path_confirmed=exec_path,
                            verifier_confidence=conf,
                        )
                        expected = (
                            impact in {"critical", "high"}
                            and causality == "confirmed"
                            and exec_path
                            and conf >= BLOCK_CONFIDENCE_THRESHOLD
                        )
                        assert BLOCK(f) is expected, (
                            f"impact={impact} causality={causality} "
                            f"exec_path={exec_path} conf={conf}"
                        )
                        seen += 1
        assert seen == len(IMPACTS) * len(PATCH_CAUSALITIES) * 2 * len(confidences)

    def test_verdict_alone_does_not_gate(self):
        """BLOCK is the §12 deterministic policy — the verdict is informational."""
        assert BLOCK(make_verdict(impact="medium", verdict="CONFIRMED")) is False
        assert BLOCK(make_verdict(verdict="UNVERIFIED")) is True


# ── Schema shape ─────────────────────────────────────────────────────────


class TestVerifierSchema:
    def test_parse_response_builds_typed_findings(self):
        result = parse_response(CONFIRMED_JSON, VerifierFindings)
        assert isinstance(result, VerifierFindings)
        assert "survived" in result.summary
        assert isinstance(result.findings, list)
        finding = result.findings[0]
        assert isinstance(finding, VerifierFinding)
        assert finding.finding_id == "F19"
        assert finding.impact == "high"
        assert finding.patch_causality == "confirmed"
        assert finding.reproducible is True
        assert finding.execution_path_confirmed is True
        assert finding.verifier_confidence == 0.94
        assert finding.developer_relevance == "high"
        assert finding.verdict == "CONFIRMED"
        assert "guard" in finding.notes

    def test_parse_response_defaults_for_optional_fields(self):
        result = parse_response(
            '{"findings": [{"finding_id": "F20", "impact": "medium", '
            '"patch_causality": "unconfirmed", "verdict": "UNVERIFIED"}], '
            '"summary": ""}',
            VerifierFindings,
        )
        f = result.findings[0]
        assert f.reproducible is False
        assert f.execution_path_confirmed is False
        assert f.verifier_confidence == 0.0
        assert f.developer_relevance == "medium"
        assert f.notes == ""

    def test_parse_response_missing_required_field_raises(self):
        with pytest.raises(Exception) as ei:
            parse_response(
                '{"findings": [{"impact": "high", "patch_causality": "confirmed", '
                '"verdict": "CONFIRMED"}], "summary": ""}',
                VerifierFindings,
            )
        assert "finding_id" in str(ei.value)

    def test_parse_response_empty_findings_ok(self):
        result = parse_response('{"findings": [], "summary": "no candidates"}', VerifierFindings)
        assert result.findings == []

    def test_parse_response_markdown_fence(self):
        result = parse_response("```json\n" + REFUTED_JSON + "\n```", VerifierFindings)
        assert result.findings[0].verdict == "REFUTED"

    def test_schema_to_prompt_lists_all_fields(self):
        prompt = schema_to_prompt(VerifierFindings)
        assert '"findings"' in prompt
        assert '"summary"' in prompt
        assert "list[VerifierFinding]" in prompt
        for name in (
            "verdict",
            "verifier_confidence",
            "patch_causality",
            "execution_path_confirmed",
        ):
            assert name in schema_to_prompt(VerifierFinding)

    def test_finding_to_dict_shape(self):
        d = make_verdict().to_dict()
        assert d["finding_id"] == "F19"
        assert d["verdict"] == "CONFIRMED"
        assert d["verifier_confidence"] == 0.95
        assert d["execution_path_confirmed"] is True
        assert d["reproducible"] is True
        assert d["patch_causality"] == "confirmed"
        assert d["developer_relevance"] == "high"

    def test_enumerated_constants(self):
        assert VERDICTS == ("CONFIRMED", "REFUTED", "UNVERIFIED")
        assert PATCH_CAUSALITIES == ("confirmed", "unconfirmed", "refuted")
        assert IMPACTS == ("critical", "high", "medium", "low", "info")
        assert DEVELOPER_RELEVANCES == ("high", "medium", "low")


# ── (b)/(c) VerifierAgent.run with a stubbed LLM ─────────────────────────


class TestVerifierRun:
    def test_run_parses_confirmed_finding(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        result = agent.run(make_candidate())
        assert isinstance(result, VerifierFindings)
        assert stub.calls == 1  # exactly one LLM turn, no tools executed
        f = result.findings[0]
        assert isinstance(f, VerifierFinding)
        assert f.verdict == "CONFIRMED"
        assert f.finding_id == "F19"
        assert f.impact == "high"
        assert f.patch_causality == "confirmed"
        assert f.execution_path_confirmed is True
        assert f.verifier_confidence == 0.94

    def test_run_parses_refuted_finding(self, tmp_workdir):
        stub = StubLLM([content_response(REFUTED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        result = agent.run(make_candidate())
        f = result.findings[0]
        assert f.verdict == "REFUTED"
        assert f.patch_causality == "refuted"
        assert f.verifier_confidence == 0.20
        assert BLOCK(f) is False  # refuted finding never blocks

    def test_run_accepts_candidate_dict(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        result = agent.run(
            {
                "finding_id": "F19",
                "file": "auth/session.py",
                "line": 182,
                "claim": "Deleted users can reach this dereference with user=None.",
                "trigger": "refresh token belonging to deleted account",
                "evidence": ["E12"],
                "verification_plan": ["inspect caller guard"],
            }
        )
        assert result.findings[0].verdict == "CONFIRMED"

    def test_model_role_resolved_to_verifier(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        router = FakeRouter(stub)
        agent = VerifierAgent(router=router, workdir=tmp_workdir)
        agent.run(make_candidate())
        assert router.roles == ["verifier"]  # ModelRouter.for_role('verifier')

    def test_subclass_of_agent_runner(self):
        assert issubclass(VerifierAgent, object)  # structural check below
        from engine.agents import AgentRunner

        assert issubclass(VerifierAgent, AgentRunner)

    def test_budget_cap_propagates(self, tmp_workdir):
        """A model that never emits valid JSON stops at the iteration cap."""
        stub = StubLLM([content_response("this is not json")])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        with pytest.raises(BudgetExceededError) as ei:
            agent.run(make_candidate(), budget=Budget(max_iterations=3))
        assert "Cap exceeded" in str(ei.value)


# ── (d) Tool belt ────────────────────────────────────────────────────────


class TestVerifierTools:
    CODEFINTEL_TOOLS = {
        "ast_search",
        "text_search",
        "get_symbol_definition",
        "find_references",
        "get_callers",
        "get_callees",
        "get_implementations",
        "get_change_impact",
    }

    def tool_names(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        agent.run(make_candidate())
        return {t["function"]["name"] for t in stub.tools_seen[0]}

    def test_execution_tools_present(self, tmp_workdir):
        names = self.tool_names(tmp_workdir)
        assert {"run_command", "read_file", "search_pattern"} <= names

    def test_codeintel_tools_present(self, tmp_workdir):
        names = self.tool_names(tmp_workdir)
        assert self.CODEFINTEL_TOOLS <= names

    def test_run_command_tool_schema(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        agent.run(make_candidate())
        by_name = {t["function"]["name"]: t["function"] for t in stub.tools_seen[0]}
        rc = by_name["run_command"]
        assert "cmd" in rc["parameters"]["properties"]
        assert "cmd" in rc["parameters"].get("required", [])
        assert "shell" in rc["description"].lower() or "command" in rc["description"].lower()

    def test_run_command_tool_strips_budget_and_llm_cred_env(self, tmp_workdir, monkeypatch):
        """The verifier run_command tool strips budget + LLM credential vars.

        Same class as the evaluator leak (2026-08-09 R2-16): judge caps
        exported into subprocess envs break EvalCap/config-priority tests.
        INFRA-LLM-ENV-001: GITREINS_LLM_* / OPENROUTER_API_KEY leak breaks
        test_llm.py env-priority tests (api_key == '' asserted, real key
        found).
        """
        from unittest.mock import patch as _patch

        from engine.review.verifier import _make_run_command_tool

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        monkeypatch.setenv("GITREINS_MAX_OUTPUT_TOKENS", "2M")
        monkeypatch.setenv("GITREINS_MAX_ITERATIONS", "400")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "sk-keep")
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
        monkeypatch.setenv("KIMI_API_KEY", "kimi-key")
        monkeypatch.setenv("GROQ_API_KEY", "groq-key")
        tool = _make_run_command_tool(tmp_workdir)
        assert tool.fn is not None
        with _patch("engine.review.verifier.subprocess.run", side_effect=fake_run):
            result = tool.fn("pytest")
        assert result["exit_code"] == 0
        assert "GITREINS_MAX_OUTPUT_TOKENS" not in captured["env"]
        assert "GITREINS_MAX_ITERATIONS" not in captured["env"]
        assert "GITREINS_LLM_API_KEY" not in captured["env"]
        assert "OPENROUTER_API_KEY" not in captured["env"]
        assert "KIMI_API_KEY" not in captured["env"]
        assert "GROQ_API_KEY" not in captured["env"]

    def test_sandbox_tools_auto_injected(self, tmp_workdir):
        names = self.tool_names(tmp_workdir)
        assert {"sandbox_write", "sandbox_read"} <= names


# ── (e) Candidate → prompt serialization ─────────────────────────────────


class TestCandidatePrompt:
    def test_user_prompt_contains_claim_plan_and_evidence(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        agent.run(make_candidate())
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "F19" in user_prompt
        assert "auth/session.py:182" in user_prompt
        assert "Deleted users can reach this dereference" in user_prompt
        assert "refresh token belonging to deleted account" in user_prompt
        for ref in ("E12", "E33", "E52"):
            assert ref in user_prompt
        for step in (
            "inspect caller guard",
            "inspect get_user return contract",
            "run targeted regression test",
        ):
            assert step in user_prompt

    def test_serialize_candidate_omits_empty_sections(self):
        text = serialize_candidate(VerifierCandidate(finding_id="F1", file="a.py", claim="c"))
        assert "F1" in text
        assert "a.py" in text
        assert "Evidence refs" not in text
        assert "Verification plan" not in text
        assert "Trigger" not in text

    def test_system_prompt_is_adversarial_falsification(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        agent.run(make_candidate())
        system_prompt = stub.messages_seen[0][0]["content"]
        assert "disprove" in system_prompt.lower()
        assert "refute" in system_prompt.lower()
        assert "survives" in system_prompt.lower()
        assert "verdict" in system_prompt
        assert "verifier_confidence" in system_prompt
        assert "CONFIRMED" in system_prompt
        assert "REFUTED" in system_prompt
        assert "UNVERIFIED" in system_prompt


# ── (f) BLOCK integration on parsed output ───────────────────────────────


class TestBlockIntegration:
    def test_confirmed_output_blocks(self, tmp_workdir):
        stub = StubLLM([content_response(CONFIRMED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        result = agent.run(make_candidate())
        assert len(result.findings) == 1
        assert BLOCK(result.findings[0]) is True
        assert any(BLOCK(f) for f in result.findings)

    def test_refuted_output_never_blocks(self, tmp_workdir):
        stub = StubLLM([content_response(REFUTED_JSON)])
        agent = VerifierAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        result = agent.run(make_candidate())
        assert all(not BLOCK(f) for f in result.findings)

    def test_parse_then_block_round_trip(self):
        """Parse → BLOCK: the pipeline's gate on real agent output."""
        result = parse_response(CONFIRMED_JSON, VerifierFindings)
        assert BLOCK(result.findings[0]) is True
        result = parse_response(REFUTED_JSON, VerifierFindings)
        assert BLOCK(result.findings[0]) is False
