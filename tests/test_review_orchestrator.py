"""
R2.7 tests for engine/review/orchestrator — ReviewOrchestrator.

Covers: run_from_plan (pipeline-output entry) running the three default
reviewers in parallel and merging findings, deterministic per-agent ordering,
role filtering, per-agent error capture (one failing lane never kills the
DAG), unknown-role degradation, evidence-store passthrough, scout lens flow
into reviewer prompts, injected-agent support, and the full DAG entry run()
(diff evidence → scout → optional code-intel retrieval → reviewers).

Hermetic: a RoleMapRouter returns a distinct StubLLM per role, so parallel
execution is deterministic and no real model/network is touched.
"""

import pytest

from engine.agents import Budget, BudgetExceededError
from engine.evidence import Evidence, EvidenceStore
from engine.llm import LLMResponse
from engine.review import (
    DEFAULT_REVIEWER_ROLES,
    ReviewOrchestrator,
    ReviewResult,
    ReviewerResult,
    RuntimeReviewer,
)
from engine.review.scout import ScoutPlan

# The DESIGN_v2.md §7 example plan — one callers retrieval + two lenses.
PLAN_JSON = (
    '{"changed_symbols": [{"symbol": "SessionManager.rotate_token", "risk": "high"}],'
    ' "retrieval_requests": [{"type": "callers",'
    ' "query": "SessionManager.rotate_token"}],'
    ' "review_lenses": ["state/concurrency", "authorization"]}'
)


def findings_json(claim):
    return (
        '{"findings": [{"claim": "' + claim + '", "severity": "high",'
        ' "file": "auth/session.py", "line": 182, "evidence": ["E1"],'
        ' "category": "defect"}], "summary": "ok"}'
    )


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

    def __init__(self, responses, raises=None):
        self.responses = list(responses)
        self.raises = raises
        self.calls = 0
        self.messages_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        if self.raises is not None:
            raise self.raises
        self.calls += 1
        self.messages_seen.append(list(messages))
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


class RoleMapRouter:
    """Returns a distinct client per role — deterministic parallel runs."""

    def __init__(self, clients):
        self.clients = dict(clients)
        self.roles = []

    def for_role(self, role):
        self.roles.append(role)
        return self.clients[role]


def reviewer_clients(claims=None, raises_role=None):
    """Three per-role stubs, each returning one finding (or raising)."""
    claims = claims or {
        "runtime_reviewer": "race on shared cache",
        "contract_reviewer": "changed return type breaks callers",
        "security_reviewer": "missing authz check",
    }
    clients = {}
    for role in DEFAULT_REVIEWER_ROLES:
        raise_ = RuntimeError("transport boom") if role == raises_role else None
        clients[role] = StubLLM(
            [LLMResponse(content=findings_json(claims[role]))], raises=raise_
        )
    return clients


def make_plan(lenses=None):
    return ScoutPlan(
        changed_symbols=[],
        retrieval_requests=[],
        review_lenses=lenses or ["state/concurrency", "authorization"],
    )


def make_store():
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
    return store


# ── (a) run_from_plan — the pipeline-output entry ────────────────────────


class TestRunFromPlan:
    def test_runs_all_default_reviewers_and_merges_findings(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run_from_plan(make_plan(), make_store())

        assert isinstance(result, ReviewResult)
        assert set(result.per_agent.keys()) == set(DEFAULT_REVIEWER_ROLES)
        assert len(result.findings) == 3  # one per lane, merged
        claims = {f.claim for f in result.findings}
        assert claims == {
            "race on shared cache",
            "changed return type breaks callers",
            "missing authz check",
        }
        assert result.all_ok
        assert result.errors == {}
        assert set(router.roles) == set(DEFAULT_REVIEWER_ROLES)

    def test_per_agent_order_is_deterministic(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run_from_plan(make_plan(), make_store())
        assert list(result.per_agent.keys()) == list(DEFAULT_REVIEWER_ROLES)

    def test_roles_filter(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run_from_plan(
            make_plan(), make_store(), roles=["contract_reviewer"]
        )
        assert list(result.per_agent.keys()) == ["contract_reviewer"]
        assert router.roles == ["contract_reviewer"]
        assert len(result.findings) == 1

    def test_failure_in_one_lane_does_not_kill_dag(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients(raises_role="security_reviewer"))
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run_from_plan(make_plan(), make_store())

        # The failing lane reports its error…
        sec = result.per_agent["security_reviewer"]
        assert isinstance(sec, ReviewerResult)
        assert sec.ok is False
        assert "transport boom" in sec.error
        assert sec.findings == []
        # …but the other lanes' findings are still collected.
        assert result.all_ok is False
        assert "security_reviewer" in result.errors
        assert len(result.findings) == 2
        assert "race on shared cache" in {f.claim for f in result.findings}

    def test_unknown_role_reports_error(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run_from_plan(make_plan(), make_store(), roles=["bogus"])
        assert result.per_agent["bogus"].ok is False
        assert "unknown review role" in result.per_agent["bogus"].error

    def test_evidence_store_passthrough(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        store = make_store()
        result = orch.run_from_plan(make_plan(), store)
        assert result.evidence_store is store
        d = result.to_dict()
        assert d["evidence_count"] == 1
        assert d["all_ok"] is True
        assert d["errors"] == {}
        assert len(d["per_agent"]) == 3
        assert d["findings"][0]["file"] == "auth/session.py"

    def test_scout_lenses_flow_into_reviewer_prompts(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        orch.run_from_plan(make_plan(lenses=["state/concurrency", "authorization"]), make_store())
        # Every reviewer's user prompt carries the scout-selected lenses.
        for role in DEFAULT_REVIEWER_ROLES:
            user_prompt = router.clients[role].messages_seen[0][1]["content"]
            assert "state/concurrency" in user_prompt
            assert "authorization" in user_prompt

    def test_evidence_serialized_into_reviewer_prompts(self, tmp_workdir):
        router = RoleMapRouter(reviewer_clients())
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        orch.run_from_plan(make_plan(), make_store())
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "E1" in user_prompt
        assert "auth/session.py:41-45" in user_prompt

    def test_injected_agent_used_directly(self, tmp_workdir):
        """Pre-built agents are used verbatim — no class lookup for them."""
        inner_router = RoleMapRouter(
            {"runtime_reviewer": StubLLM([LLMResponse(content=findings_json("injected"))])}
        )
        injected = RuntimeReviewer(router=inner_router, workdir=tmp_workdir)
        orch = ReviewOrchestrator(
            router=RoleMapRouter({}),  # unused for injected roles
            workdir=tmp_workdir,
            agents={"runtime_reviewer": injected},
        )
        result = orch.run_from_plan(make_plan(), make_store(), roles=["runtime_reviewer"])
        assert result.per_agent["runtime_reviewer"].ok is True
        assert result.findings[0].claim == "injected"


# ── (b) run() — the full DAG entry ───────────────────────────────────────


class TestFullRun:
    def test_full_dag_scout_then_reviewers(self, tmp_workdir):
        clients = {"scout": StubLLM([LLMResponse(content=PLAN_JSON)])}
        clients.update(reviewer_clients())
        router = RoleMapRouter(clients)
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run(["auth/session.py"], "rotated refresh tokens")

        assert "scout" in router.roles  # scout classified the change first
        for role in DEFAULT_REVIEWER_ROLES:
            assert role in router.roles
        assert len(result.per_agent) == 3
        assert len(result.findings) == 3
        # run() appended one diff Evidence per changed file.
        store = result.evidence_store
        assert store is not None
        assert len(store.query(kind="diff", source="diff")) == 1

    def test_full_run_with_provider_appends_codeintel_evidence(self, tmp_workdir):
        class FakeProvider:
            def callers(self, query, file_path=None, symbol=None, limit=10):
                return [{"file": "auth/callers.py", "line": 10}]

        clients = {"scout": StubLLM([LLMResponse(content=PLAN_JSON)])}
        clients.update(reviewer_clients())
        router = RoleMapRouter(clients)
        orch = ReviewOrchestrator(
            router=router, workdir=tmp_workdir, provider=FakeProvider()
        )
        result = orch.run(["auth/session.py"], "rotated refresh tokens")

        store = result.evidence_store
        assert store is not None
        codeintel = store.query(kind="call_edge", source="codeintel")
        assert len(codeintel) == 1
        assert codeintel[0].payload["query"] == "SessionManager.rotate_token"
        # Reviewers see both the diff evidence and the codeintel evidence.
        user_prompt = router.clients["runtime_reviewer"].messages_seen[0][1]["content"]
        assert "E1" in user_prompt  # diff evidence
        assert "E2" in user_prompt  # codeintel evidence
        assert "[call_edge]" in user_prompt

    def test_full_run_empty_changed_files(self, tmp_workdir):
        clients = {"scout": StubLLM([LLMResponse(content=PLAN_JSON)])}
        clients.update(reviewer_clients())
        router = RoleMapRouter(clients)
        orch = ReviewOrchestrator(router=router, workdir=tmp_workdir)
        result = orch.run([])
        store = result.evidence_store
        assert store is not None
        assert store.all() == []  # no diff evidence appended
        assert len(result.findings) == 3

    def test_full_run_scout_budget_raises(self, tmp_workdir):
        """A scout that exhausts its budget surfaces as BudgetExceededError."""
        clients = {"scout": StubLLM([LLMResponse(content="not json")])}
        clients.update(reviewer_clients())
        router = RoleMapRouter(clients)
        orch = ReviewOrchestrator(
            router=router,
            workdir=tmp_workdir,
            scout_budget=Budget(max_iterations=2),
        )
        with pytest.raises(BudgetExceededError):
            orch.run(["a.py"])
