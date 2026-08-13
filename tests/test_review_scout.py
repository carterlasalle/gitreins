"""
R2.6 tests for engine/review/scout — ScoutAgent retrieval-plan classifier.

Covers: ScoutPlan schema shape (parse_response round-trip, nested
dataclasses), ScoutAgent.run returning a typed ScoutPlan with a stubbed LLM
(zero real calls), model_role routing through the router to the 'scout'
role, prompt construction from the changed-file list + diff context, and
env-default fallback when review.models is absent (router never raises).

Hermetic: the LLM is a deterministic StubLLM (pattern from
tests/test_agents.py) — no network, no real model.
"""

from unittest.mock import patch

import pytest

from engine.agents import Budget, BudgetExceededError, SchemaError
from engine.agents.schemas import parse_response, schema_to_prompt
from engine.llm import LLMClient, LLMResponse
from engine.review import ChangedSymbol, RetrievalRequest, ScoutAgent, ScoutPlan
from engine.router import ModelRouter

# The DESIGN_v2.md §7 example plan.
PLAN_JSON = (
    '{"changed_symbols": [{"symbol": "SessionManager.rotate_token", "risk": "high"}],'
    ' "retrieval_requests": [{"type": "callers", "query": "SessionManager.rotate_token"},'
    ' {"type": "references", "query": "refresh_token"}],'
    ' "review_lenses": ["state/concurrency", "authorization"]}'
)


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

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


def content_response(text):
    return LLMResponse(content=text)


# ── (a) Schema shape ────────────────────────────────────────────────────


class TestScoutPlanSchema:
    def test_parse_response_builds_typed_plan(self):
        plan = parse_response(PLAN_JSON, ScoutPlan)
        assert isinstance(plan, ScoutPlan)
        assert isinstance(plan.changed_symbols, list)
        sym = plan.changed_symbols[0]
        assert isinstance(sym, ChangedSymbol)
        assert sym.symbol == "SessionManager.rotate_token"
        assert sym.risk == "high"
        req = plan.retrieval_requests[0]
        assert isinstance(req, RetrievalRequest)
        assert req.type == "callers"
        assert req.query == "SessionManager.rotate_token"
        assert plan.retrieval_requests[1].type == "references"
        assert plan.retrieval_requests[1].query == "refresh_token"
        assert plan.review_lenses == ["state/concurrency", "authorization"]

    def test_schema_to_prompt_lists_all_fields(self):
        prompt = schema_to_prompt(ScoutPlan)
        assert '"changed_symbols"' in prompt
        assert '"retrieval_requests"' in prompt
        assert '"review_lenses"' in prompt
        assert "list[ChangedSymbol]" in prompt

    def test_parse_response_missing_required_field_raises(self):
        with pytest.raises(SchemaError):
            parse_response('{"changed_symbols": [], "retrieval_requests": []}', ScoutPlan)

    def test_parse_response_markdown_fence(self):
        plan = parse_response("```json\n" + PLAN_JSON + "\n```", ScoutPlan)
        assert plan.review_lenses == ["state/concurrency", "authorization"]


# ── (b) ScoutAgent.run ──────────────────────────────────────────────────


class TestScoutAgentRun:
    def test_run_returns_typed_plan(self, tmp_workdir):
        stub = StubLLM([content_response(PLAN_JSON)])
        scout = ScoutAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        plan = scout.run(["auth/session.py"], diff_context="rotated refresh tokens")
        assert isinstance(plan, ScoutPlan)
        assert plan.changed_symbols[0].symbol == "SessionManager.rotate_token"
        assert plan.retrieval_requests[1].type == "references"
        assert stub.calls == 1  # exactly one LLM turn, no tools

    def test_model_role_resolves_to_scout(self, tmp_workdir):
        stub = StubLLM([content_response(PLAN_JSON)])
        router = FakeRouter(stub)
        scout = ScoutAgent(router=router, workdir=tmp_workdir)
        scout.run(["a.py"])
        assert router.roles == ["scout"]  # ModelRouter.for_role("scout")

    def test_prompt_contains_changed_files_and_diff(self, tmp_workdir):
        stub = StubLLM([content_response(PLAN_JSON)])
        scout = ScoutAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        scout.run(["auth/session.py", "auth/tokens.py"], diff_context="token rotation")
        messages = stub.messages_seen[0]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        user_prompt = messages[1]["content"]
        assert "auth/session.py" in user_prompt
        assert "auth/tokens.py" in user_prompt
        assert "token rotation" in user_prompt
        system_prompt = messages[0]["content"]
        assert "retrieval plan" in system_prompt.lower()
        assert "changed_symbols" in system_prompt

    def test_run_without_diff_context(self, tmp_workdir):
        stub = StubLLM([content_response(PLAN_JSON)])
        scout = ScoutAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        plan = scout.run(["a.py"])
        assert isinstance(plan, ScoutPlan)
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "Diff context" not in user_prompt

    def test_run_empty_changed_files(self, tmp_workdir):
        stub = StubLLM([content_response(PLAN_JSON)])
        scout = ScoutAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        plan = scout.run([])
        assert isinstance(plan, ScoutPlan)

    def test_schema_error_retries_then_caps(self, tmp_workdir):
        """A model that never emits valid JSON is stopped at the iteration cap."""
        stub = StubLLM([content_response("this is not json")])
        scout = ScoutAgent(router=FakeRouter(stub), workdir=tmp_workdir)
        with pytest.raises(BudgetExceededError) as ei:
            scout.run(["a.py"], budget=Budget(max_iterations=3))
        assert "Cap exceeded" in str(ei.value)


# ── (c) Router integration ──────────────────────────────────────────────


class TestRouterIntegration:
    def test_config_scout_model_block_resolved(self, tmp_workdir):
        """review.models.scout block → client with the cheap scout model."""
        cfg = {
            "review": {
                "models": {
                    "scout": {
                        "provider": "openai",
                        "base_url": "https://test.local/v1",
                        "model": "qwen/qwen3.7-flash",
                    }
                }
            }
        }
        router = ModelRouter(cfg)
        assert router.for_role("scout").model == "qwen/qwen3.7-flash"
        scout = ScoutAgent(router=router, workdir=tmp_workdir)
        client = router.for_role("scout")
        with patch.object(client, "chat", return_value=LLMResponse(content=PLAN_JSON)):
            plan = scout.run(["a.py"])
        assert plan.review_lenses == ["state/concurrency", "authorization"]

    def test_missing_review_models_falls_back_no_raise(self, tmp_workdir, monkeypatch):
        """Absent review.models block → env-default client; run still works."""
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://fallback.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "fallback-key")

        router = ModelRouter({})  # no review block at all
        client = router.for_role("scout")  # never raises
        assert client.model == "fallback-model"

        scout = ScoutAgent(router=router, workdir=tmp_workdir)
        with patch.object(client, "chat", return_value=LLMResponse(content=PLAN_JSON)):
            plan = scout.run(["a.py"])
        assert isinstance(plan, ScoutPlan)
