"""
R2.13 tests for engine/review/history — ReviewRunArchiver (DESIGN_v2.md §13
artifact layout + provenance).

Covers: the nine §13 artifacts written with their exact names, clean
overwrite on re-run, graceful degradation (archive failures are logged and
never raise), final-findings provenance (generated_by + verified_by merged
by finding id), and the ReviewOrchestrator wiring (a completed run archives
each stage that has data, and an archive failure never breaks the DAG).

Hermetic: StubLLM/RoleMapRouter (pattern from tests/test_review_orchestrator.py)
— zero LLM calls, no network, no real git.
"""

import json
import os

from engine.evidence import Evidence, EvidenceStore
from engine.llm import LLMResponse
from engine.review import DEFAULT_REVIEWER_ROLES, ReviewFinding, ReviewOrchestrator
from engine.review.history import ARTIFACT_NAMES, ReviewRunArchiver
from engine.review.scout import ScoutPlan
from engine.review.verifier import VerifierFinding

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

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
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


def reviewer_clients():
    claims = {
        "runtime_reviewer": "race on shared cache",
        "contract_reviewer": "changed return type breaks callers",
        "security_reviewer": "missing authz check",
    }
    clients = {}
    for role in DEFAULT_REVIEWER_ROLES:
        clients[role] = StubLLM([LLMResponse(content=findings_json(claims[role]))])
    return clients


def make_finding(**overrides):
    defaults = dict(
        claim="race on shared cache",
        severity="high",
        file="auth/session.py",
        line=182,
        evidence=["E1"],
        category="null-deref",
        suggestion="guard on user before deref",
    )
    defaults.update(overrides)
    return ReviewFinding(**defaults)


def make_verdict(**overrides):
    defaults = dict(
        finding_id="F1",
        impact="high",
        patch_causality="confirmed",
        reproducible=True,
        execution_path_confirmed=True,
        verifier_confidence=0.94,
        developer_relevance="high",
        verdict="CONFIRMED",
        notes="claim survived falsification",
    )
    defaults.update(overrides)
    return VerifierFinding(**defaults)


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


def make_plan():
    return ScoutPlan(
        changed_symbols=[],
        retrieval_requests=[],
        review_lenses=["state/concurrency", "authorization"],
    )


def read_json(path):
    with open(path) as f:
        return json.load(f)


# ── (a) archive_all — the nine §13 artifacts ─────────────────────────────


class TestArchiveAll:
    def test_archives_all_nine_artifacts_with_exact_names(self, tmp_path):
        archiver = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="abc123")
        findings = [make_finding()]
        written = archiver.archive_all(
            change={"changed_files": ["auth/session.py"], "diff_context": "rotated tokens"},
            static_evidence=[
                Evidence(
                    id="",
                    kind="static_analysis",
                    source="semgrep",
                    file="auth/session.py",
                    line_start=17,
                    line_end=17,
                    payload={"rule": "no-hardcoded-secrets"},
                )
            ],
            scout=make_plan(),
            candidates=findings,
            verification=[make_verdict()],
            final_findings=findings,
            verifications=[make_verdict()],
            verifier_model="qwen3.7-flash",
            requirements=[
                {
                    "id": "github-842",
                    "title": "Allow promotional orders with zero-dollar total",
                    "criteria": ["zero-dollar promotional orders are accepted"],
                    "status": "in_progress",
                }
            ],
            usage={"roles": list(DEFAULT_REVIEWER_ROLES), "total_findings": 1},
            manifest={"workdir": str(tmp_path)},
        )

        assert archiver.run_dir == os.path.join(str(tmp_path), "review_runs", "abc123")
        assert set(os.listdir(archiver.run_dir)) == set(ARTIFACT_NAMES)
        for name in ARTIFACT_NAMES:
            assert os.path.isfile(os.path.join(archiver.run_dir, name)), name
            assert name in written

        # Manifest records the sha, schema, and presence of every artifact.
        manifest = read_json(os.path.join(archiver.run_dir, "manifest.json"))
        assert manifest["schema"] == "gitreins.review-run/v1"
        assert manifest["commit_sha"] == "abc123"
        assert all(manifest["artifacts"][name] for name in ARTIFACT_NAMES)

        # Static evidence and requirements land in their own artifacts.
        static = read_json(os.path.join(archiver.run_dir, "static-evidence.json"))
        assert static["count"] == 1
        assert static["evidence"][0]["kind"] == "static_analysis"
        reqs = read_json(os.path.join(archiver.run_dir, "requirements.json"))
        assert reqs["requirements"][0]["id"] == "github-842"

    def test_rerun_overwrites_cleanly(self, tmp_path):
        archiver = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="abc123")
        stages = {
            "change": {"changed_files": ["a.py"], "diff_context": "v1"},
            "static_evidence": [
                Evidence(
                    id="",
                    kind="static_analysis",
                    source="semgrep",
                    file="a.py",
                    line_start=1,
                    line_end=1,
                    payload={"rule": "r1"},
                )
            ],
            "scout": make_plan(),
            "candidates": [make_finding(claim="first")],
            "verification": [make_verdict()],
            "final_findings": [make_finding(claim="first")],
            "verifications": [make_verdict()],
            "verifier_model": "qwen3.7-flash",
            "requirements": [{"id": "t1"}],
            "usage": {"run": 1},
        }
        archiver.archive_all(**stages)
        # Re-run with the same sha and the same stage set: same nine files,
        # overwritten with the latest content — nothing stale, no duplicates.
        stages["change"] = {"changed_files": ["b.py", "c.py"], "diff_context": "v2"}
        stages["final_findings"] = [make_finding(claim="second")]
        stages["usage"] = {"run": 2}
        archiver.archive_all(**stages)

        assert len(os.listdir(archiver.run_dir)) == len(ARTIFACT_NAMES)
        change = read_json(os.path.join(archiver.run_dir, "change.json"))
        assert change["changed_files"] == ["b.py", "c.py"]
        assert change["diff_context"] == "v2"
        final = read_json(os.path.join(archiver.run_dir, "final-findings.json"))
        assert final["findings"][0]["claim"] == "second"
        usage = read_json(os.path.join(archiver.run_dir, "usage.json"))
        assert usage["run"] == 2
        manifest = read_json(os.path.join(archiver.run_dir, "manifest.json"))
        assert all(manifest["artifacts"][name] for name in ARTIFACT_NAMES)

        # A different sha gets its own run dir; the first run stays intact.
        archiver2 = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="def456")
        archiver2.archive_all(
            change={"changed_files": ["z.py"], "diff_context": ""},
            scout=make_plan(),
            final_findings=[make_finding()],
            usage={},
        )
        assert os.path.isdir(os.path.join(str(tmp_path), "review_runs", "abc123"))
        assert os.path.isdir(os.path.join(str(tmp_path), "review_runs", "def456"))
        assert read_json(os.path.join(str(tmp_path), "review_runs", "abc123", "change.json"))[
            "changed_files"
        ] == ["b.py", "c.py"]

    def test_archive_failure_never_raises(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        archiver = ReviewRunArchiver(base_dir=str(blocker / "nested"), commit_sha="abc")

        # Every entry point degrades to None / {} instead of raising.
        assert archiver.archive_manifest() is None
        assert archiver.archive_change(["a.py"]) is None
        assert archiver.archive_scout(make_plan()) is None
        assert archiver.archive_final_findings([make_finding()]) is None
        assert (
            archiver.archive_all(
                change={"changed_files": ["a.py"], "diff_context": ""},
                final_findings=[make_finding()],
                usage={"x": 1},
            )
            == {}
        )
        assert not os.path.exists(archiver.run_dir)


# ── (b) final-findings provenance (§13) ──────────────────────────────────


class TestProvenance:
    def test_final_findings_carry_generated_by_and_verified_by(self, tmp_path):
        archiver = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="abc123")
        finding = make_finding()
        finding.generated_by = {"role": "runtime_reviewer", "model": "deepseek-v4-flash"}
        written = archiver.archive_final_findings(
            [finding],
            verifications=[make_verdict(finding_id="F1")],
            verifier_model="qwen3.7-flash",
        )
        assert written is not None
        data = read_json(written)
        assert data["count"] == 1
        entry = data["findings"][0]
        assert entry["id"] == "F1"
        assert entry["generated_by"] == {
            "role": "runtime_reviewer",
            "model": "deepseek-v4-flash",
        }
        assert entry["verified_by"] == {"model": "qwen3.7-flash", "verdict": "CONFIRMED"}

    def test_final_findings_default_provenance_when_unverified(self, tmp_path):
        archiver = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="abc123")
        written = archiver.archive_final_findings([make_finding()])
        entry = read_json(written)["findings"][0]
        assert entry["generated_by"] == {}
        assert entry["verified_by"] == {}
        assert entry["id"] == "F1"  # synthetic deterministic id

    def test_verification_artifact_shape(self, tmp_path):
        archiver = ReviewRunArchiver(base_dir=str(tmp_path), commit_sha="abc123")
        written = archiver.archive_verification([make_verdict()])
        data = read_json(written)
        assert data["count"] == 1
        assert data["verifications"][0]["finding_id"] == "F1"
        assert data["verifications"][0]["verdict"] == "CONFIRMED"


# ── (c) ReviewOrchestrator wiring ────────────────────────────────────────


class TestOrchestratorWiring:
    def test_completed_run_archives_every_stage(self, tmp_workdir):
        store = make_store()
        store.append(
            Evidence(
                id="",
                kind="static_analysis",
                source="semgrep",
                file="auth/session.py",
                line_start=17,
                line_end=17,
                payload={"rule": "no-hardcoded-secrets"},
            )
        )
        clients = {"scout": StubLLM([LLMResponse(content=PLAN_JSON)])}
        clients.update(reviewer_clients())
        orch = ReviewOrchestrator(
            router=RoleMapRouter(clients),
            workdir=tmp_workdir,
            archiver=ReviewRunArchiver(base_dir=tmp_workdir, commit_sha="abc123"),
        )
        orch.run(
            ["auth/session.py"],
            "rotated refresh tokens",
            evidence_store=store,
            intent_context=[
                {
                    "id": "t1",
                    "title": "rotate tokens",
                    "criteria": ["rotation is safe"],
                    "status": "in_progress",
                }
            ],
        )

        run_dir = os.path.join(tmp_workdir, "review_runs", "abc123")
        # Every §13 artifact except verification.json — the verifier is not
        # part of the orchestrator's DAG (wired upstream, R2.16).
        expected = [n for n in ARTIFACT_NAMES if n != "verification.json"]
        for name in expected:
            assert os.path.isfile(os.path.join(run_dir, name)), name
        assert not os.path.exists(os.path.join(run_dir, "verification.json"))

        final = read_json(os.path.join(run_dir, "final-findings.json"))
        assert final["count"] == 3
        roles = {f["generated_by"]["role"] for f in final["findings"]}
        assert roles == set(DEFAULT_REVIEWER_ROLES)

        change = read_json(os.path.join(run_dir, "change.json"))
        assert change["changed_files"] == ["auth/session.py"]
        static = read_json(os.path.join(run_dir, "static-evidence.json"))
        assert static["count"] == 1
        reqs = read_json(os.path.join(run_dir, "requirements.json"))
        assert reqs["count"] == 1 and reqs["requirements"][0]["id"] == "t1"
        usage = read_json(os.path.join(run_dir, "usage.json"))
        assert usage["total_findings"] == 3
        assert usage["all_ok"] is True
        manifest = read_json(os.path.join(run_dir, "manifest.json"))
        assert manifest["commit_sha"] == "abc123"
        assert all(manifest["artifacts"][name] for name in expected)

    def test_default_archiver_targets_workdir(self, tmp_workdir):
        """No archiver passed → the orchestrator archives under its workdir."""
        orch = ReviewOrchestrator(router=RoleMapRouter(reviewer_clients()), workdir=tmp_workdir)
        orch.run_from_plan(make_plan(), make_store())
        assert os.path.isdir(os.path.join(tmp_workdir, "review_runs"))

    def test_archive_failure_never_breaks_dag(self, tmp_workdir):
        blocker = os.path.join(tmp_workdir, "blocker")
        with open(blocker, "w") as f:
            f.write("not a directory")
        archiver = ReviewRunArchiver(base_dir=os.path.join(blocker, "nested"), commit_sha="abc")
        orch = ReviewOrchestrator(
            router=RoleMapRouter(reviewer_clients()),
            workdir=tmp_workdir,
            archiver=archiver,
        )
        result = orch.run_from_plan(make_plan(), make_store())
        assert result.all_ok
        assert len(result.findings) == 3
        # Findings still carry generated_by provenance even when archiving fails.
        assert all(f.generated_by["role"] for f in result.findings)
