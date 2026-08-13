"""
R2.16 tests — the ``gitreins review`` CLI command (gitreins/cli.py).

Covers: ChangeSource selection (working tree default, commit range, --pr with
owner/repo), the local review path end-to-end (combined Lane A + Lane B
report), the --pr path with a mocked gh CLI (PullRequestChangeSource), the
combined report formatter, and CLI registration (review --help works).

Hermetic: gitreins.cli._make_review_router is monkeypatched to return a
RoleMapRouter of StubLLMs (scout/reviewers/verifier/evaluator), all external
analyzer producers are stubbed to [], and engine.github.checkout._run_gh is
stubbed for the --pr path — no real model, network, or scanner runs.
"""

import argparse
import json
import os
import subprocess
import sys

import pytest

from engine.llm import LLMResponse

SCOUT_JSON = (
    '{"changed_symbols": [{"symbol": "SessionManager.rotate_token", "risk": "high"}],'
    ' "retrieval_requests": [{"type": "callers",'
    ' "query": "SessionManager.rotate_token"}],'
    ' "review_lenses": ["state/concurrency"]}'
)

FINDINGS_BY_ROLE = {
    "runtime_reviewer": (
        '{"findings": [{"claim": "race on shared cache", "severity": "high",'
        ' "file": "auth/session.py", "line": 42, "evidence": ["E1"],'
        ' "category": "defect"}], "summary": "ok"}'
    ),
    "contract_reviewer": (
        '{"findings": [{"claim": "changed return type breaks callers",'
        ' "severity": "medium", "file": "auth/api.py", "line": 10,'
        ' "evidence": ["E2"], "category": "defect"}], "summary": "ok"}'
    ),
    "security_reviewer": (
        '{"findings": [{"claim": "missing authz check", "severity": "high",'
        ' "file": "auth/session.py", "line": 88, "evidence": ["E3"],'
        ' "category": "defect"}], "summary": "ok"}'
    ),
}

VERIFIER_JSON = (
    '{"findings": [{"finding_id": "F1", "impact": "high",'
    ' "patch_causality": "confirmed", "verdict": "CONFIRMED",'
    ' "reproducible": true, "execution_path_confirmed": true,'
    ' "verifier_confidence": 0.95, "developer_relevance": "high",'
    ' "notes": "traced trigger to impact"}], "summary": "ok"}'
)

WRITER_JSON = (
    '{"comments": [{"file": "auth/session.py", "severity": "high",'
    ' "title": "authz missing", "body": "add a guard", "line": 88,'
    ' "finding_ids": ["F1"]}], "summary": "reviewed"}'
)

VERDICT_JSON = (
    '{"verdict": "COMPLETE", "items": ['
    '{"criterion": "zero-dollar promotional orders are accepted",'
    ' "status": "PASS", "detail": "verified in code"}],'
    ' "summary": "all criteria verified"}'
)

PR_META = {
    "baseRefOid": "cafebabe" * 5,
    "headRefOid": "deadbeef" * 5,
    "headRefName": "feature/x",
    "files": [{"path": "auth/session.py", "status": "MODIFIED"}],
}

PR_DIFF = (
    "diff --git a/auth/session.py b/auth/session.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/auth/session.py\n"
    "+++ b/auth/session.py\n"
    "@@ -40,3 +40,3 @@ def rotate_token(user):\n"
    "     if not user:\n"
    "         raise ValueError('user required')\n"
    "-    return sign(user.refresh_token)\n"
    "+    return sign(user.refresh_token, algo='none')\n"
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


def stub_router():
    """Per-role stubs for every §17 agent role (+ the publish writer)."""
    clients = {
        "scout": StubLLM([LLMResponse(content=SCOUT_JSON)]),
        "runtime_reviewer": StubLLM([LLMResponse(content=FINDINGS_BY_ROLE["runtime_reviewer"])]),
        "contract_reviewer": StubLLM([LLMResponse(content=FINDINGS_BY_ROLE["contract_reviewer"])]),
        "security_reviewer": StubLLM([LLMResponse(content=FINDINGS_BY_ROLE["security_reviewer"])]),
        "verifier": StubLLM([LLMResponse(content=VERIFIER_JSON)]),
        "evaluator": StubLLM([LLMResponse(content=VERDICT_JSON)]),
        "writer": StubLLM([LLMResponse(content=WRITER_JSON)]),
    }
    return RoleMapRouter(clients)


@pytest.fixture(autouse=True)
def _stub_llm_and_analyzers(monkeypatch):
    """Zero-LLM, zero-scanner: stub the review router + all analyzer producers."""
    from gitreins import cli as cli_module

    monkeypatch.setattr(cli_module, "_make_review_router", lambda config: stub_router())
    monkeypatch.setattr("engine.analyzers.semgrep.run_semgrep", lambda workdir: [])
    monkeypatch.setattr("engine.analyzers.gitleaks.run_gitleaks", lambda workdir: [])
    monkeypatch.setattr("engine.analyzers.trivy.run_trivy", lambda workdir: [])
    monkeypatch.setattr("engine.lsp.run_lsp_check", lambda tool, workdir, files=None: [])
    monkeypatch.setattr(
        "engine.static_analysis.run_static_check", lambda tool, workdir, timeout=120.0: []
    )


def review_args(**kw):
    """argparse.Namespace shaped like the review subparser's args."""
    defaults = {
        "pr": None,
        "owner": None,
        "repo": None,
        "base": None,
        "head": None,
        "criteria": ["zero-dollar promotional orders are accepted"],
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def make_git_repo(tmp_path, with_change=True):
    """A real git repo; optionally with one uncommitted modified file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "auth.py").write_text("def old():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    if with_change:
        (repo / "auth.py").write_text("def old():\n    return 2  # changed\n")
    return str(repo)


# ── ChangeSource selection ──────────────────────────────────────────────────


class TestChangeSourceSelection:
    def test_default_is_working_tree(self, tmp_path):
        from engine.github.checkout import WorkingTreeChangeSource
        from gitreins.cli import _make_change_source

        source = _make_change_source(review_args(), str(tmp_path))
        assert isinstance(source, WorkingTreeChangeSource)
        assert os.path.abspath(source.workdir) == os.path.abspath(str(tmp_path))

    def test_pr_builds_pull_request_source(self, tmp_path):
        from engine.github.checkout import PullRequestChangeSource
        from gitreins.cli import _make_change_source

        source = _make_change_source(review_args(pr=42, owner="acme", repo="widget"), str(tmp_path))
        assert isinstance(source, PullRequestChangeSource)
        assert source.owner == "acme"
        assert source.repo == "widget"
        assert source.pr_number == 42

    def test_pr_owner_repo_from_gh_default(self, tmp_path, monkeypatch):
        from engine.github.checkout import PullRequestChangeSource
        from gitreins.cli import _make_change_source

        monkeypatch.setattr(
            "engine.github.checkout._run_gh",
            lambda args, gh_binary="gh", timeout=60.0, input=None: (
                '{"nameWithOwner": "acme/widget"}'
            ),
        )
        source = _make_change_source(review_args(pr=7), str(tmp_path))
        assert isinstance(source, PullRequestChangeSource)
        assert (source.owner, source.repo) == ("acme", "widget")

    def test_pr_without_owner_repo_raises(self, tmp_path, monkeypatch):
        from gitreins.cli import _make_change_source

        monkeypatch.setattr(
            "engine.github.checkout._run_gh",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh missing")),
        )
        with pytest.raises(ValueError, match="cannot determine GitHub owner/repo"):
            _make_change_source(review_args(pr=7), str(tmp_path))

    def test_commit_range_builds_range_source(self, tmp_path):
        from engine.github.checkout import CommitRangeChangeSource
        from gitreins.cli import _make_change_source

        source = _make_change_source(review_args(base="main", head="feature"), str(tmp_path))
        assert isinstance(source, CommitRangeChangeSource)
        assert source.base_ref == "main"
        assert source.head_ref == "feature"


# ── The combined report formatter ──────────────────────────────────────────


class TestFormatReviewReport:
    def test_combined_lane_a_and_lane_b(self):
        from gitreins.cli import _format_review_report

        task = {
            "id": "review",
            "changed_files": ["auth/session.py"],
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "findings": [
                {
                    "claim": "missing authz check",
                    "severity": "high",
                    "file": "auth/session.py",
                    "line": 42,
                    "verdict": "CONFIRMED",
                    "verifier_confidence": 0.95,
                }
            ],
            "criteria": ["zero-dollar promotional orders are accepted"],
            "criteria_verdicts": [
                {
                    "criterion": "zero-dollar promotional orders are accepted",
                    "status": "PASS",
                    "detail": "verified in code",
                }
            ],
        }
        result = {"stages": {"rank": {"data": {"ranked": task["findings"]}}}}
        report = _format_review_report(task, result)

        assert "Lane B — defect findings (1)" in report
        assert "[high] conf=0.95 [CONFIRMED] auth/session.py:42: missing authz check" in report
        assert "Lane A — criteria evaluation (1 criterion/criteria)" in report
        assert "✓ zero-dollar promotional orders are accepted" in report
        assert "aaaaaaa" in report  # base sha prefix

    def test_no_findings_clean_and_no_criteria(self):
        from gitreins.cli import _format_review_report

        task = {"id": "review", "changed_files": ["a.py"], "criteria": [], "findings": []}
        report = _format_review_report(task, {"stages": {}})
        assert "no findings — change looks clean" in report
        assert "no criteria provided — Lane A skipped" in report


# ── cmd_review: local working-tree path ────────────────────────────────────


class TestCmdReviewLocal:
    def test_local_review_prints_combined_report(self, tmp_path, monkeypatch, capsys):
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)

        cli_module.cmd_review(review_args())

        out = capsys.readouterr().out
        assert "GitReins review" in out
        assert "auth.py" in out  # change summary
        assert "Lane B — defect findings (3)" in out
        assert "missing authz check" in out
        assert "[high] conf=0.95 [CONFIRMED] auth/session.py:42: race on shared cache" in out
        assert "Lane A — criteria evaluation (1 criterion/criteria)" in out
        assert "✓ zero-dollar promotional orders are accepted" in out

    def test_local_review_clean_tree_exits_zero(self, tmp_path, monkeypatch, capsys):
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path, with_change=False)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)

        with pytest.raises(SystemExit) as exc:
            cli_module.cmd_review(review_args())
        assert exc.value.code == 0
        assert "clean" in capsys.readouterr().out

    def test_no_criteria_skips_lane_a(self, tmp_path, monkeypatch, capsys):
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)

        cli_module.cmd_review(review_args(criteria=[]))

        out = capsys.readouterr().out
        assert "Lane A — criteria evaluation (0 criterion/criteria)" in out
        assert "no criteria provided — Lane A skipped" in out

    def test_local_review_persists_review_runs_artifacts(self, tmp_path, monkeypatch):
        """§13 (E2E-001): the local review path archives review_runs/<sha>/."""
        from engine.review.history import ARTIFACT_NAMES
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)

        cli_module.cmd_review(review_args())

        runs_dir = os.path.join(repo, "review_runs")
        shas = [d for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
        assert len(shas) == 1
        # The run dir is keyed by the working-tree change's head sha.
        assert len(shas[0]) == 40
        run_dir = os.path.join(runs_dir, shas[0])
        for name in ARTIFACT_NAMES:
            assert os.path.isfile(os.path.join(run_dir, name)), name

        # The change artifact records the file under review; the manifest
        # confirms every §13 artifact was persisted.
        with open(os.path.join(run_dir, "change.json")) as f:
            change = json.load(f)
        assert change["changed_files"] == ["auth.py"]
        with open(os.path.join(run_dir, "manifest.json")) as f:
            manifest = json.load(f)
        assert manifest["commit_sha"] == shas[0]
        assert all(manifest["artifacts"][name] for name in ARTIFACT_NAMES)


# ── cmd_review: --pr path with mocked gh ───────────────────────────────────


class TestCmdReviewPR:
    def test_pr_review_uses_pull_request_source(self, tmp_path, monkeypatch, capsys):
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)
        gh_calls = []

        def fake_run_gh(args, gh_binary="gh", timeout=60.0, input=None):
            gh_calls.append(list(args))
            if args[0] == "api":
                return "{}"  # publish_comments POST succeeds
            if args[:2] == ["pr", "view"]:
                return json.dumps(PR_META)
            if args[:2] == ["pr", "diff"]:
                return PR_DIFF
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr("engine.github.checkout._run_gh", fake_run_gh)

        cli_module.cmd_review(review_args(pr=42, owner="acme", repo="widget"))

        # PullRequestChangeSource asked gh for the PR's metadata + diff.
        assert [
            "pr",
            "view",
            "42",
            "-R",
            "acme/widget",
            "--json",
            "baseRefOid,headRefOid,headRefName,files",
        ] in gh_calls
        assert ["pr", "diff", "42", "-R", "acme/widget"] in gh_calls
        # The publish_review stage posted the batched comment via gh api.
        assert any(
            call[:4] == ["api", "-X", "POST", "/repos/acme/widget/pulls/42/comments"]
            for call in gh_calls
        )

        out = capsys.readouterr().out
        assert "GitReins review" in out
        assert "cafebabe" in out  # base sha from gh metadata
        assert "Lane B — defect findings (3)" in out
        assert "missing authz check" in out
        assert "Lane A — criteria evaluation (1 criterion/criteria)" in out

    def test_pr_uses_default_repo_when_owner_repo_omitted(self, tmp_path, monkeypatch, capsys):
        from gitreins import cli as cli_module

        repo = make_git_repo(tmp_path)
        monkeypatch.setattr(cli_module, "get_workdir", lambda: repo)

        def fake_run_gh(args, gh_binary="gh", timeout=60.0, input=None):
            if args[0] == "api":
                return "{}"
            if args[:2] == ["repo", "view"]:
                return '{"nameWithOwner": "acme/widget"}'
            if args[:2] == ["pr", "view"]:
                return json.dumps(PR_META)
            if args[:2] == ["pr", "diff"]:
                return PR_DIFF
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr("engine.github.checkout._run_gh", fake_run_gh)

        cli_module.cmd_review(review_args(pr=9))
        out = capsys.readouterr().out
        assert "cafebabe" in out  # review ran against the gh-derived repo


# ── CLI registration (subprocess, no LLM) ─────────────────────────────────


def run_cli(*args, **kwargs):
    """Run the CLI as a subprocess and return CompletedProcess."""
    cli_script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitreins", "cli.py"
    )
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = project_root + (":" + env["PYTHONPATH"] if env["PYTHONPATH"] else "")
    return subprocess.run(
        [sys.executable, cli_script] + list(args),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        **kwargs,
    )


class TestReviewCliRegistration:
    def test_review_command_in_help(self):
        result = run_cli("--help")
        assert result.returncode == 0
        assert "review" in result.stdout

    def test_review_help_lists_flags(self):
        result = run_cli("review", "--help")
        assert result.returncode == 0
        assert "--pr" in result.stdout
        assert "--owner" in result.stdout
        assert "--repo" in result.stdout
        assert "--base" in result.stdout
        assert "--head" in result.stdout
        assert "--criteria" in result.stdout

    def test_review_requires_git_repo_fails_cleanly(self, tmp_path):
        result = run_cli("review", cwd=str(tmp_path))
        # Outside a git repo the change source raises ChangeSourceError and
        # the command exits 1 with a readable error (no traceback).
        assert result.returncode == 1
        assert "Error:" in result.stderr
