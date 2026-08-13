"""
R2.7 tests for engine/review/reviewers — the three Lane-B review agents.

Covers: ReviewFinding/ReviewFindings schema shape (parse_response round-trip
with nested dataclasses, required-field enforcement, to_dict), each of the
three reviewers (RuntimeReviewer / ContractReviewer / SecurityReviewer)
returning typed findings with a stubbed LLM (zero real calls), distinct
system prompts per lane, evidence-store serialization into the user prompt
(ids, kinds, locations), changed-files/diff/lenses context, empty-store
handling, budget-cap propagation, and review.models.<role> router routing.

Hermetic: the LLM is a deterministic StubLLM (pattern from
tests/test_agents.py) — no network, no real model.
"""

import pytest

from engine.agents import Budget, BudgetExceededError, SchemaError
from engine.agents.schemas import parse_response, schema_to_prompt
from engine.evidence import Evidence, EvidenceStore
from engine.llm import LLMClient, LLMResponse
from engine.review import (
    ContractReviewer,
    ReviewAgent,
    ReviewFinding,
    ReviewFindings,
    RuntimeReviewer,
    SecurityReviewer,
)
from engine.review.reviewers import SEVERITIES
from engine.router import ModelRouter

# The DESIGN_v2.md §9-style finding payload.
FINDINGS_JSON = (
    '{"findings": [{"claim": "rotate_token can dereference user=None for '
    'deleted accounts", "severity": "high", "file": "auth/session.py", '
    '"line": 182, "evidence": ["E1", "E2"], "category": "null-deref", '
    '"suggestion": "guard on user before deref"}], '
    '"summary": "One high-severity null-deref risk."}'
)

# (class, model_role, objective keyword) — the three lanes.
REVIEWER_CLASSES = [
    (RuntimeReviewer, "runtime_reviewer", "runtime defects"),
    (ContractReviewer, "contract_reviewer", "contract violations"),
    (SecurityReviewer, "security_reviewer", "security defects"),
]


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


def make_store():
    """A store with one diff and one codeintel evidence item."""
    store = EvidenceStore()
    store.append(
        Evidence(
            id="",
            kind="diff",
            source="diff",
            file="auth/session.py",
            line_start=41,
            line_end=45,
            payload={"changed": True},
        )
    )
    store.append(
        Evidence(
            id="",
            kind="call_edge",
            source="codeintel",
            file="auth/callers.py",
            line_start=10,
            line_end=None,
            payload={"query": "rotate_token"},
        )
    )
    return store


# ── (a) Schema shape ─────────────────────────────────────────────────────


class TestReviewFindingSchema:
    def test_parse_response_builds_typed_findings(self):
        result = parse_response(FINDINGS_JSON, ReviewFindings)
        assert isinstance(result, ReviewFindings)
        assert result.summary == "One high-severity null-deref risk."
        assert isinstance(result.findings, list)
        finding = result.findings[0]
        assert isinstance(finding, ReviewFinding)
        assert finding.claim.startswith("rotate_token")
        assert finding.severity == "high"
        assert finding.file == "auth/session.py"
        assert finding.line == 182
        assert finding.evidence == ["E1", "E2"]
        assert finding.category == "null-deref"
        assert "guard on user" in finding.suggestion

    def test_parse_response_optional_fields_default(self):
        result = parse_response(
            '{"findings": [{"claim": "x", "severity": "low"}], "summary": ""}',
            ReviewFindings,
        )
        finding = result.findings[0]
        assert finding.file == ""
        assert finding.line is None
        assert finding.evidence == []
        assert finding.category == ""
        assert finding.suggestion == ""

    def test_parse_response_empty_findings_ok(self):
        result = parse_response('{"findings": [], "summary": "clean"}', ReviewFindings)
        assert result.findings == []
        assert result.summary == "clean"

    def test_parse_response_missing_required_field_raises(self):
        with pytest.raises(SchemaError):
            parse_response(
                '{"findings": [{"severity": "high"}], "summary": ""}',
                ReviewFindings,
            )

    def test_parse_response_markdown_fence(self):
        result = parse_response("```json\n" + FINDINGS_JSON + "\n```", ReviewFindings)
        assert result.findings[0].line == 182

    def test_schema_to_prompt_lists_fields(self):
        prompt = schema_to_prompt(ReviewFindings)
        assert '"findings"' in prompt
        assert '"summary"' in prompt
        assert "list[ReviewFinding]" in prompt

    def test_finding_to_dict_shape(self):
        finding = ReviewFinding(claim="c", severity="high", file="a.py", line=3, evidence=["E1"])
        d = finding.to_dict()
        assert d["file"] == "a.py"
        assert d["line"] == 3
        assert d["claim"] == "c"
        assert d["severity"] == "high"
        assert d["evidence"] == ["E1"]
        assert d["category"] == ""
        assert d["suggestion"] == ""

    def test_severities_constant(self):
        assert SEVERITIES == ("critical", "high", "medium", "low", "info")


# ── (b) Reviewer runs (parametrized over the three lanes) ────────────────


class TestReviewerRun:
    @pytest.mark.parametrize("cls,model_role,_", REVIEWER_CLASSES)
    def test_run_returns_typed_findings(self, cls, model_role, _, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        result = reviewer.run(make_store(), changed_files=["auth/session.py"])
        assert isinstance(result, ReviewFindings)
        assert result.findings[0].file == "auth/session.py"
        assert result.findings[0].line == 182
        assert stub.calls == 1  # exactly one LLM turn, no tools

    @pytest.mark.parametrize("cls,model_role,_", REVIEWER_CLASSES)
    def test_model_role_resolved_per_lane(self, cls, model_role, _, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        router = FakeRouter(stub)
        reviewer = cls(router=router, workdir=tmp_workdir)
        reviewer.run(make_store())
        assert router.roles == [model_role]  # ModelRouter.for_role(model_role)

    @pytest.mark.parametrize("cls,model_role,_", REVIEWER_CLASSES)
    def test_subclass_of_agent_runner(self, cls, model_role, _):
        assert issubclass(cls, ReviewAgent)

    def test_system_prompts_are_distinct_per_lane(self, tmp_workdir):
        """Each lane has a distinct objective — no shared reviewer persona."""
        prompts = {}
        for cls, model_role, _ in REVIEWER_CLASSES:
            stub = StubLLM([content_response(FINDINGS_JSON)])
            reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
            reviewer.run(make_store())
            prompts[model_role] = stub.messages_seen[0][0]["content"]
        assert prompts["runtime_reviewer"] != prompts["contract_reviewer"]
        assert prompts["contract_reviewer"] != prompts["security_reviewer"]
        assert prompts["runtime_reviewer"] != prompts["security_reviewer"]

    @pytest.mark.parametrize("cls,model_role,keyword", REVIEWER_CLASSES)
    def test_system_prompt_contains_lane_objective(self, cls, model_role, keyword, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store())
        system_prompt = stub.messages_seen[0][0]["content"]
        assert keyword in system_prompt
        assert "severity" in system_prompt
        assert "findings" in system_prompt

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_user_prompt_contains_serialized_evidence(self, cls, _, __, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store())
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "E1" in user_prompt
        assert "E2" in user_prompt
        assert "[diff]" in user_prompt
        assert "[call_edge]" in user_prompt
        assert "auth/session.py:41-45" in user_prompt
        assert "source=codeintel" in user_prompt

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_user_prompt_contains_changed_files_and_diff(self, cls, _, __, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(
            make_store(),
            changed_files=["auth/session.py", "auth/tokens.py"],
            diff_context="rotated refresh tokens",
        )
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "auth/session.py" in user_prompt
        assert "auth/tokens.py" in user_prompt
        assert "rotated refresh tokens" in user_prompt

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_user_prompt_empty_store_handled(self, cls, _, __, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(EvidenceStore())
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "(empty" in user_prompt  # graceful no-evidence note

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_user_prompt_lenses_section(self, cls, _, __, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store(), lenses=["state/concurrency", "authorization"])
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "Review lenses" in user_prompt
        assert "state/concurrency" in user_prompt
        assert "authorization" in user_prompt

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_run_without_evidence_store(self, cls, _, __, tmp_workdir):
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        result = reviewer.run(None)
        assert isinstance(result, ReviewFindings)

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_budget_cap_propagates(self, cls, _, __, tmp_workdir):
        """A model that never emits valid JSON stops at the iteration cap."""
        stub = StubLLM([content_response("this is not json")])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        with pytest.raises(BudgetExceededError) as ei:
            reviewer.run(make_store(), budget=Budget(max_iterations=3))
        assert "Cap exceeded" in str(ei.value)


# ── (c) Router integration ───────────────────────────────────────────────


class TestRouterIntegration:
    def test_config_reviewer_model_blocks_resolved(self, tmp_workdir):
        """review.models.<role> blocks → per-role clients (DESIGN_v2.md §3)."""
        cfg = {
            "review": {
                "models": {
                    "runtime_reviewer": {
                        "provider": "openai",
                        "base_url": "https://test.local/v1",
                        "model": "deepseek/deepseek-v4-flash-0731",
                    },
                    "contract_reviewer": {
                        "provider": "openai",
                        "base_url": "https://test.local/v1",
                        "model": "deepseek/deepseek-v4-flash-0731",
                    },
                    "security_reviewer": {
                        "provider": "openai",
                        "base_url": "https://test.local/v1",
                        "model": "deepseek/deepseek-v4-flash-0731",
                    },
                }
            }
        }
        router = ModelRouter(cfg)
        assert router.for_role("runtime_reviewer").model == "deepseek/deepseek-v4-flash-0731"
        assert router.for_role("contract_reviewer").model == "deepseek/deepseek-v4-flash-0731"
        assert router.for_role("security_reviewer").model == "deepseek/deepseek-v4-flash-0731"

    def test_missing_review_models_falls_back_no_raise(self, tmp_workdir, monkeypatch):
        """Absent review.models block → env-default client; run still works."""
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://fallback.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "fallback-key")

        router = ModelRouter({})
        client = router.for_role("security_reviewer")  # never raises
        assert client.model == "fallback-model"

        reviewer = SecurityReviewer(router=router, workdir=tmp_workdir)
        from unittest.mock import patch

        with patch.object(client, "chat", return_value=LLMResponse(content=FINDINGS_JSON)):
            result = reviewer.run(make_store())
        assert result.findings[0].severity == "high"


# ── (d) R2.12: Intent-context merge (DESIGN_v2.md §10) ─────────────────────


class TestReviewerIntentContext:
    """intent_context is rendered into the user prompt BEFORE the evidence store."""

    INTENT = (
        "Task intent (developer criteria):\n"
        "- [github-842] (in_progress) Allow promotional orders with zero-dollar total\n"
        "    - zero-dollar promotional orders are accepted\n"
        "    - Stripe is not contacted for free orders"
    )

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_user_prompt_contains_intent_and_evidence(self, cls, _, __, tmp_workdir):
        """The criteria AND the evidence store both reach the prompt."""
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store(), intent_context=self.INTENT)
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "Task intent (developer criteria):" in user_prompt
        assert "github-842" in user_prompt
        assert "zero-dollar promotional orders are accepted" in user_prompt
        assert "Stripe is not contacted for free orders" in user_prompt
        # The evidence store is still serialized alongside the intent.
        assert "Evidence store:" in user_prompt
        assert "E1" in user_prompt
        assert "auth/session.py:41-45" in user_prompt

    @pytest.mark.parametrize("cls,_,__", REVIEWER_CLASSES)
    def test_intent_section_precedes_evidence_store(self, cls, _, __, tmp_workdir):
        """Intent context renders BEFORE the evidence store section."""
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = cls(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store(), intent_context=self.INTENT)
        user_prompt = stub.messages_seen[0][1]["content"]
        assert user_prompt.index("Task intent (developer criteria):") < user_prompt.index(
            "Evidence store:"
        )

    def test_structured_intent_dicts_rendered(self, tmp_workdir):
        """A list of {id, title, criteria, status} dicts is rendered inline."""
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = RuntimeReviewer(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(
            make_store(),
            intent_context=[
                {
                    "id": "login-endpoint",
                    "title": "Implement POST /login endpoint",
                    "criteria": ["Returns JWT token on success"],
                    "status": "in_progress",
                },
            ],
        )
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "Task intent (developer criteria):" in user_prompt
        assert "login-endpoint" in user_prompt
        assert "Returns JWT token on success" in user_prompt
        assert "E1" in user_prompt  # evidence still present

    def test_no_intent_context_no_section(self, tmp_workdir):
        """Default run() has no intent section — backward compatible."""
        stub = StubLLM([content_response(FINDINGS_JSON)])
        reviewer = RuntimeReviewer(router=FakeRouter(stub), workdir=tmp_workdir)
        reviewer.run(make_store())
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "Task intent" not in user_prompt
        assert "Evidence store:" in user_prompt
