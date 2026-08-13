"""
R2.16 tests — the full DESIGN_v2.md §17 review_pipeline DAG through
engine/pipeline.py (run_review).

Covers: change_analysis populating the change context from a ChangeSource,
parallel analyzer steps producing evidence, the scout agent role, the
evidence_retrieval stage over a code-intel provider, the parallel reviewer
agent roles, merge_findings (dedup), verify_findings (adversarial verifier,
REFUTED drop), rank_findings (deterministic order), criteria_eval (Lane A)
gated by task.has_criteria, and publish_review no-op for local runs.

Hermetic: a RoleMapRouter returns a distinct StubLLM per role (scout,
runtime/contract/security reviewers, verifier, evaluator) — no real model or
network. External analyzer producers are monkeypatched to deterministic
results (or []), so no real semgrep/gitleaks/trivy/LSP binaries run.
"""

import json
import os

import pytest

from engine.llm import LLMResponse
from engine.pipeline import Pipeline
from engine.review.scout import ScoutPlan

# The DESIGN_v2.md §17 review_pipeline, exactly as YAML would deserialize it.
REVIEW_PIPELINE = [
    {"id": "change_analysis", "type": "change_analysis"},
    {
        "id": "static_evidence",
        "parallel": True,
        "steps": [
            {"id": "lsp", "type": "analyzer", "analyzer": "lsp"},
            {"id": "semgrep", "type": "analyzer", "analyzer": "semgrep"},
            {"id": "typecheck", "type": "analyzer", "analyzer": "typecheck"},
            {"id": "secrets", "type": "analyzer", "analyzer": "secrets"},
            {"id": "dependencies", "type": "analyzer", "analyzer": "dependencies"},
        ],
    },
    {"id": "scout", "type": "agent", "role": "scout"},
    {"id": "retrieval", "type": "evidence_retrieval"},
    {
        "id": "reviewers",
        "parallel": True,
        "steps": [
            {"id": "runtime", "type": "agent", "role": "runtime_reviewer"},
            {"id": "contracts", "type": "agent", "role": "contract_reviewer"},
            {"id": "security", "type": "agent", "role": "security_reviewer"},
        ],
    },
    {"id": "candidate_merge", "type": "merge_findings"},
    {"id": "verify", "type": "verify_findings", "parallel": True, "role": "verifier"},
    {"id": "rank", "type": "rank_findings"},
    {"id": "requirements", "type": "criteria_eval", "condition": "task.has_criteria"},
    {"id": "publish", "type": "publish_review"},
]


def review_config() -> dict:
    """Pipeline config carrying the §17 DAG under the review_pipeline key."""
    return {
        "pipeline": {"stages": []},
        "review_pipeline": [dict(s) for s in REVIEW_PIPELINE],
    }


def findings_json(claim, severity, line, evidence):
    return (
        '{"findings": [{"claim": "' + claim + '", "severity": "' + severity + '",'
        ' "file": "auth/session.py", "line": ' + str(line) + ","
        ' "evidence": ["' + evidence + '"], "category": "defect"}], "summary": "ok"}'
    )


SCOUT_JSON = (
    '{"changed_symbols": [{"symbol": "SessionManager.rotate_token", "risk": "high"}],'
    ' "retrieval_requests": [{"type": "callers",'
    ' "query": "SessionManager.rotate_token"}],'
    ' "review_lenses": ["state/concurrency"]}'
)

VERIFIER_JSON = (
    '{"findings": [{"finding_id": "F1", "impact": "medium",'
    ' "patch_causality": "confirmed", "verdict": "CONFIRMED",'
    ' "reproducible": true, "execution_path_confirmed": true,'
    ' "verifier_confidence": 0.94, "developer_relevance": "high",'
    ' "notes": "traced trigger to impact"}], "summary": "ok"}'
)

VERDICT_JSON = (
    '{"verdict": "COMPLETE", "items": ['
    '{"criterion": "zero-dollar promotional orders are accepted",'
    ' "status": "PASS", "detail": "verified in code"},'
    '{"criterion": "Stripe is not contacted for free orders",'
    ' "status": "PASS", "detail": "verified in code"}],'
    ' "summary": "all criteria verified"}'
)

CLAIMS = {
    "runtime_reviewer": ("race on shared cache", "critical", 182, "E1"),
    "contract_reviewer": ("changed return type breaks callers", "high", 210, "E2"),
    "security_reviewer": ("missing authz check", "medium", 241, "E3"),
}


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


def review_router():
    """Per-role stubs for every §17 agent role."""
    clients = {
        "scout": StubLLM([LLMResponse(content=SCOUT_JSON)]),
        "runtime_reviewer": StubLLM(
            [LLMResponse(content=findings_json(*CLAIMS["runtime_reviewer"]))]
        ),
        "contract_reviewer": StubLLM(
            [LLMResponse(content=findings_json(*CLAIMS["contract_reviewer"]))]
        ),
        "security_reviewer": StubLLM(
            [LLMResponse(content=findings_json(*CLAIMS["security_reviewer"]))]
        ),
        "verifier": StubLLM([LLMResponse(content=VERIFIER_JSON)]),
        "evaluator": StubLLM([LLMResponse(content=VERDICT_JSON)]),
    }
    return RoleMapRouter(clients)


class FakeChangeSource:
    """Duck-typed ChangeSource (engine/github/checkout.py protocol)."""

    def __init__(self, files, diff_text, base="a" * 40, head="b" * 40):
        self.files = files
        self.diff_text = diff_text
        self.base = base
        self.head = head

    def diff(self):
        from engine.github.checkout import Diff

        return Diff(text=self.diff_text, files=self.files)

    def changed_files(self):
        return list(self.files)

    def base_sha(self):
        return self.base

    def head_sha(self):
        return self.head


class StubProvider:
    """Duck-typed CodeIntelProvider: one deterministic hit per callers query."""

    def callers(self, query, limit=3):
        return [{"file": "auth/session.py", "line": 41, "symbol": query}]

    def references(self, query, limit=3):
        return []

    def definition(self, query, limit=3):
        return []

    def implementations(self, query, limit=3):
        return []

    def callees(self, query, limit=3):
        return []

    def symbols(self, query, limit=3):
        return []


def make_task(**overrides):
    task = {
        "id": "review-task",
        "title": "Review the change",
        "criteria": [
            "zero-dollar promotional orders are accepted",
            "Stripe is not contacted for free orders",
        ],
        "change_source": FakeChangeSource(["auth/session.py"], "rotated refresh tokens\n"),
        "codeintel_provider": StubProvider(),
    }
    task.update(overrides)
    return task


@pytest.fixture(autouse=True)
def _stub_analyzer_producers(monkeypatch):
    """All §17 analyzer producers return [] — hermetic, no real binaries."""
    monkeypatch.setattr("engine.analyzers.semgrep.run_semgrep", lambda workdir: [])
    monkeypatch.setattr("engine.analyzers.gitleaks.run_gitleaks", lambda workdir: [])
    monkeypatch.setattr("engine.analyzers.trivy.run_trivy", lambda workdir: [])
    monkeypatch.setattr("engine.lsp.run_lsp_check", lambda tool, workdir, files=None: [])
    monkeypatch.setattr(
        "engine.static_analysis.run_static_check", lambda tool, workdir, timeout=120.0: []
    )


class TestReviewDagE2E:
    def step_data(self, stage):
        """StepResult.data of a single-step (or parallel) stage."""
        return stage.steps[0].data

    def test_full_dag_runs_all_stages(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        result = p.run_review(task)

        stages = result["stages"]
        expected = {
            "change_analysis",
            "static_evidence",
            "scout",
            "retrieval",
            "reviewers",
            "candidate_merge",
            "verify",
            "rank",
            "requirements",
            "publish",
        }
        assert set(stages) == expected
        assert result["passed"] is True

    def test_change_analysis_populates_change_context(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task.pop("changed_files", None)
        task.pop("diff", None)
        p.run_review(task)

        stage = p._stage_results["change_analysis"]
        assert stage.passed is True
        assert task["changed_files"] == ["auth/session.py"]
        assert "rotated refresh tokens" in task["diff"]
        assert task["diff_context"] == task["diff"]
        assert task["base_sha"] == "a" * 40
        assert task["head_sha"] == "b" * 40
        assert self.step_data(stage)["files"] == ["auth/session.py"]

    def test_parallel_analyzer_steps_run_and_append_evidence(self, tmp_workdir, monkeypatch):
        """Each analyzer step ran; their evidence landed in the task store."""
        from engine.evidence import Evidence

        def fake_semgrep(workdir):
            return [
                Evidence(
                    id="",
                    kind="static_analysis",
                    source="semgrep",
                    file="auth/session.py",
                    line_start=5,
                    line_end=5,
                    payload={"code": "X", "message": "semgrep hit", "severity": "error"},
                )
            ]

        def fake_gitleaks(workdir):
            return [
                Evidence(
                    id="",
                    kind="static_analysis",
                    source="gitleaks",
                    file="secrets.env",
                    line_start=1,
                    line_end=1,
                    payload={"code": "", "message": "secret", "severity": "high"},
                )
            ]

        monkeypatch.setattr("engine.analyzers.semgrep.run_semgrep", fake_semgrep)
        monkeypatch.setattr("engine.analyzers.gitleaks.run_gitleaks", fake_gitleaks)
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        stage = p._stage_results["static_evidence"]
        assert len(stage.steps) == 5  # lsp/semgrep/typecheck/secrets/dependencies
        by_id = {s.id: s for s in stage.steps}
        assert by_id["semgrep"].data["count"] == 1
        assert by_id["secrets"].data["count"] == 1
        assert by_id["lsp"].data["count"] == 0
        assert by_id["typecheck"].data["count"] == 0
        assert by_id["dependencies"].data["count"] == 0

        store = task["evidence_store"]
        kinds = {e.kind for e in store.all()}
        assert "static_analysis" in kinds
        assert len(store.query(kind="static_analysis")) == 2
        # The dict-list view stays in sync: 2 analyzer + 1 retrieval (E3).
        assert len(task["evidence"]) == 3

    def test_scout_agent_role_runs_and_plan_is_stashed(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        scout = p._stage_results["scout"]
        assert scout.passed is True
        assert self.step_data(scout)["role"] == "scout"
        plan = task["scout_plan"]
        assert isinstance(plan, ScoutPlan)
        assert [r.type for r in plan.retrieval_requests] == ["callers"]
        assert task["review_lenses"] == ["state/concurrency"]

    def test_evidence_retrieval_executes_scout_plan(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        retrieval = p._stage_results["retrieval"]
        assert retrieval.passed is True
        assert self.step_data(retrieval)["retrieved"] == 1  # one callers request → one hit
        store = task["evidence_store"]
        assert len(store.query(source="codeintel")) == 1
        ev = store.query(source="codeintel")[0]
        assert ev.kind == "call_edge"
        assert ev.file == "auth/session.py"

    def test_reviewers_parallel_agents_and_findings_plumbed(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        reviewers = p._stage_results["reviewers"]
        assert len(reviewers.steps) == 3
        assert reviewers.passed is True
        assert len(task["findings"]) == 3
        claims = {f["claim"] for f in task["findings"]}
        assert claims == {c[0] for c in CLAIMS.values()}
        assert set(router.roles) == {
            "scout",
            "runtime_reviewer",
            "contract_reviewer",
            "security_reviewer",
            "verifier",
            "evaluator",
        }

    def test_merge_findings_dedupes_overlapping_candidates(self, tmp_workdir):
        """merge_findings collapses evidence-overlapping candidates."""
        config = {
            "pipeline": {"stages": []},
            "review_pipeline": [{"id": "candidate_merge", "type": "merge_findings"}],
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        task = {
            "id": "t",
            "criteria": [],
            "findings": [
                {
                    "claim": "dup A",
                    "severity": "medium",
                    "file": "a.py",
                    "line": 3,
                    "evidence": ["E1"],
                },
                {
                    "claim": "dup B",
                    "severity": "high",
                    "file": "a.py",
                    "line": 5,
                    "evidence": ["E1"],
                },
                {
                    "claim": "unique",
                    "severity": "low",
                    "file": "b.py",
                    "line": 9,
                    "evidence": ["E9"],
                },
            ],
        }
        result = p.run_review(task)
        merge = p._stage_results["candidate_merge"]
        assert self.step_data(merge)["input_count"] == 3
        assert self.step_data(merge)["output_count"] == 2
        assert len(task["findings"]) == 2
        assert result["stages"]["candidate_merge"]["passed"] is True

    def test_verify_findings_attaches_verdicts_and_keeps_confirmed(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        verify = p._stage_results["verify"]
        assert self.step_data(verify)["candidates"] == 3
        assert self.step_data(verify)["confirmed"] == 3
        assert self.step_data(verify)["refuted"] == 0
        for f in task["findings"]:
            assert f["verdict"] == "CONFIRMED"
            assert f["verifier_confidence"] == 0.94
            assert f["execution_path_confirmed"] is True
            assert f["verified_by"]["role"] == "verifier"

    def test_verify_findings_drops_refuted(self, tmp_workdir):
        """A REFUTED candidate is not reported."""
        refuted_json = (
            '{"findings": [{"finding_id": "F1", "impact": "high",'
            ' "patch_causality": "refuted", "verdict": "REFUTED",'
            ' "verifier_confidence": 0.99, "notes": "pre-existing"}], "summary": ""}'
        )
        router = RoleMapRouter(
            {
                "scout": StubLLM([LLMResponse(content=SCOUT_JSON)]),
                "runtime_reviewer": StubLLM(
                    [LLMResponse(content=findings_json("claim", "high", 5, "E1"))]
                ),
                "contract_reviewer": StubLLM(
                    [LLMResponse(content=findings_json("c2", "low", 50, "E2"))]
                ),
                "security_reviewer": StubLLM(
                    [LLMResponse(content=findings_json("c3", "low", 90, "E3"))]
                ),
                "verifier": StubLLM([LLMResponse(content=refuted_json)]),
                "evaluator": StubLLM([LLMResponse(content=VERDICT_JSON)]),
            }
        )
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        verify = p._stage_results["verify"]
        assert self.step_data(verify)["refuted"] == 3  # every candidate refuted
        assert self.step_data(verify)["confirmed"] == 0
        assert task["findings"] == []  # nothing survives falsification

    def test_rank_findings_deterministic_severity_order(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        rank = p._stage_results["rank"]
        assert self.step_data(rank)["count"] == 3
        ordered = [f["claim"] for f in task["findings"]]
        assert ordered == [
            "race on shared cache",  # critical
            "changed return type breaks callers",  # high
            "missing authz check",  # medium
        ]

    def test_criteria_eval_lane_a_verdicts(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        req = p._stage_results["requirements"]
        assert req.passed is True
        assert self.step_data(req)["verdict"] == "COMPLETE"
        assert len(task["criteria_verdicts"]) == 2
        assert all(i["status"] == "PASS" for i in task["criteria_verdicts"])

    def test_criteria_eval_gated_by_task_has_criteria(self, tmp_workdir):
        """No criteria → the requirements stage is skipped entirely."""
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        task["criteria"] = []
        result = p.run_review(task)
        assert "requirements" not in result["stages"]

    def test_publish_review_local_noop(self, tmp_workdir):
        router = review_router()
        p = Pipeline(review_config(), tmp_workdir, router=router)
        task = make_task()
        p.run_review(task)

        publish = p._stage_results["publish"]
        assert publish.passed is True
        assert self.step_data(publish)["mode"] == "local"
        assert "no PR context" in publish.steps[0].output
        assert "3 finding(s)" in publish.steps[0].output


class TestReviewStageErrors:
    def test_unknown_analyzer_name_fails_step(self, tmp_workdir):
        config = {
            "pipeline": {"stages": []},
            "review_pipeline": [{"id": "bad", "type": "analyzer", "analyzer": "bogus"}],
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        result = p.run_review({"id": "t", "criteria": []})
        step = result["stages"]["bad"]["steps"][0]
        assert step["passed"] is False
        assert "unknown analyzer name" in step["error"]

    def test_change_source_failure_is_reported_not_crashing(self, tmp_workdir):
        class BoomSource(FakeChangeSource):
            def diff(self):
                raise RuntimeError("git exploded")

        config = {
            "pipeline": {"stages": []},
            "review_pipeline": [{"id": "change_analysis", "type": "change_analysis"}],
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        result = p.run_review({"id": "t", "criteria": [], "change_source": BoomSource([], "")})
        stage = result["stages"]["change_analysis"]
        assert stage["passed"] is False
        assert "git exploded" in stage["steps"][0]["error"]

    def test_verify_findings_no_candidates_passes(self, tmp_workdir):
        config = {
            "pipeline": {"stages": []},
            "review_pipeline": [{"id": "verify", "type": "verify_findings"}],
        }
        p = Pipeline(config, tmp_workdir, router=review_router())
        result = p.run_review({"id": "t", "criteria": [], "findings": []})
        stage = result["stages"]["verify"]
        assert stage["passed"] is True
        assert "No findings to verify" in stage["steps"][0]["output"]

    def test_review_agent_type_still_dispatches(self, tmp_workdir):
        """Backward compat: the legacy ``review_agent`` type keeps working."""
        config = {
            "pipeline": {"stages": []},
            "review_pipeline": [
                {"id": "runtime", "type": "review_agent", "role": "runtime_reviewer"}
            ],
        }
        router = review_router()
        p = Pipeline(config, tmp_workdir, router=router)
        result = p.run_review({"id": "t", "criteria": [], "changed_files": ["a.py"]})
        stage = result["stages"]["runtime"]
        assert stage["passed"] is True
        assert stage["steps"][0]["data"]["count"] == 1


# ── §13 archiving: run_review persists the completed run (E2E-001) ─────────


class TestReviewDagArchiving:
    """run_review with an archiver persists review_runs/<sha>/ artifacts."""

    def test_run_review_archives_all_nine_artifacts(self, tmp_workdir):
        from engine.review.history import ARTIFACT_NAMES, ReviewRunArchiver

        p = Pipeline(review_config(), tmp_workdir, router=review_router())
        task = make_task()
        archiver = ReviewRunArchiver(base_dir=tmp_workdir, commit_sha="abc123")
        result = p.run_review(task, archiver=archiver)

        assert result["passed"] is True
        run_dir = os.path.join(tmp_workdir, "review_runs", "abc123")
        assert set(os.listdir(run_dir)) == set(ARTIFACT_NAMES)
        for name in ARTIFACT_NAMES:
            assert os.path.isfile(os.path.join(run_dir, name)), name

        # The artifact contents map the DAG's stage data (§13 / orchestrator
        # pattern): change from the task, findings from the verified/ranked
        # list, requirements from the Lane A verdicts.
        change = json.load(open(os.path.join(run_dir, "change.json")))
        assert change["changed_files"] == ["auth/session.py"]
        final = json.load(open(os.path.join(run_dir, "final-findings.json")))
        assert final["count"] == 3
        assert all(f["verified_by"] for f in final["findings"])  # verifier stamped
        verification = json.load(open(os.path.join(run_dir, "verification.json")))
        assert verification["count"] == 3
        reqs = json.load(open(os.path.join(run_dir, "requirements.json")))
        assert reqs["count"] == 2
        usage = json.load(open(os.path.join(run_dir, "usage.json")))
        assert "rank" in usage["stages"] and usage["total_findings"] == 3
        manifest = json.load(open(os.path.join(run_dir, "manifest.json")))
        assert manifest["commit_sha"] == "abc123"
        assert all(manifest["artifacts"][name] for name in ARTIFACT_NAMES)

    def test_no_archiver_skips_archiving(self, tmp_workdir):
        """Default run_review callers (GitHub-App path) are untouched."""
        p = Pipeline(review_config(), tmp_workdir, router=review_router())
        result = p.run_review(make_task())
        assert result["passed"] is True
        assert not os.path.exists(os.path.join(tmp_workdir, "review_runs"))

    def test_archive_failure_never_breaks_run_review(self, tmp_workdir):
        from engine.review.history import ReviewRunArchiver

        p = Pipeline(review_config(), tmp_workdir, router=review_router())
        task = make_task()
        blocker = os.path.join(tmp_workdir, "blocker")
        with open(blocker, "w") as f:
            f.write("not a directory")
        archiver = ReviewRunArchiver(base_dir=os.path.join(blocker, "nested"), commit_sha="abc")
        # The DAG still completes and reports passed; the archive failure is
        # logged, never raised (R2.13).
        result = p.run_review(task, archiver=archiver)
        assert result["passed"] is True
        assert not os.path.exists(archiver.run_dir)
