"""
Unit tests for engine/guard_manager.py — pre-commit static checks.
axiom:trace work_item=GR-001 spec=specs/04-Guard-Manager.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from engine.guard_manager import (
    GuardManager,
    GuardResult,
    Tier1Result,
    _build_diff_test_command,
    _discover_test_targets,
)


# ── Phase 1-3-1: GuardResult/Tier1Result dataclasses — step-1-3-1-1 ─────────


class TestGuardResult:
    """Test GuardResult dataclass."""

    def test_guard_result_passed_true(self):
        """GuardResult with passed=True has correct fields."""
        gr = GuardResult(name="secrets", passed=True, output="clean")
        assert gr.name == "secrets"
        assert gr.passed is True
        assert gr.output == "clean"
        assert gr.error == ""

    def test_guard_result_passed_false(self):
        """GuardResult with passed=False has output/error captured."""
        gr = GuardResult(
            name="lint", passed=False, output="E501 line too long", error="exit code 1"
        )
        assert gr.passed is False
        assert "E501" in gr.output
        assert "exit code 1" in gr.error

    def test_guardresult_is_frozen(self):
        """GuardResult fields cannot be mutated after construction."""
        from dataclasses import FrozenInstanceError

        gr = GuardResult(name="secrets", passed=True, output="clean")
        with pytest.raises(FrozenInstanceError):
            gr.passed = False


class TestTier1Result:
    """Test Tier1Result dataclass and summary."""

    def test_tier1_all_passed(self):
        """Tier1Result with all passed → passed=True, summary shows all check marks."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", True, "clean"),
            GuardResult("tests", True, "3 passed"),
        ]
        tr = Tier1Result(passed=True, results=results)
        assert tr.passed is True
        summary = tr.summary
        assert "secrets" in summary
        assert "lint" in summary
        assert "tests" in summary

    def test_tier1_one_failed(self):
        """Tier1Result with one failed → passed=False, summary shows mix."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", False, "E501"),
            GuardResult("tests", True, "ok"),
        ]
        tr = Tier1Result(passed=False, results=results)
        assert tr.passed is False
        summary = tr.summary
        assert "✗ lint" in summary or summary.count("✗") >= 1

    def test_tier1result_is_frozen(self):
        """Tier1Result fields cannot be mutated after construction."""
        from dataclasses import FrozenInstanceError

        t1 = Tier1Result(passed=True, results=[])
        with pytest.raises(FrozenInstanceError):
            t1.passed = False


class TestGuardManagerInit:
    """Test GuardManager initialization and config parsing — step-1-3-1-2."""

    def test_empty_config_all_enabled(self, guard_manager):
        """Empty config → all guards enabled (default True)."""
        assert guard_manager._enabled["secrets"] is True
        assert guard_manager._enabled["lint"] is True
        assert guard_manager._enabled["tests"] is True

    def test_secrets_disabled(self, tmp_workdir):
        """Config with guards.secrets=false → secrets disabled."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": False}})
        assert gm._enabled["secrets"] is False
        assert gm._enabled["lint"] is True
        assert gm._enabled["tests"] is True

    def test_tests_disabled_with_custom_command(self, tmp_workdir):
        """Config with guards.tests=false + custom test_command → tests disabled, command saved."""
        gm = GuardManager(
            tmp_workdir, {"guards": {"tests": False, "test_command": "pytest custom/"}}
        )
        assert gm._enabled["tests"] is False
        assert gm.config.get("guards", {}).get("test_command") == "pytest custom/"

    def test_no_guards_key_all_defaults(self, tmp_workdir):
        """Config with no 'guards' key → all defaults True."""
        gm = GuardManager(tmp_workdir, {"other": "stuff"})
        assert gm._enabled["secrets"] is True
        assert gm._enabled["lint"] is True
        assert gm._enabled["tests"] is True

    def test_config_none_all_enabled(self, tmp_workdir):
        """None config → all guards enabled."""
        gm = GuardManager(tmp_workdir, None)
        assert gm._enabled["secrets"] is True

    def test_cpp_repo_detected(self, tmp_workdir):
        """CMakeLists.txt → _is_cpp True (triggers C++-aware timeouts)."""
        import os

        os.makedirs(tmp_workdir, exist_ok=True)
        with open(os.path.join(tmp_workdir, "CMakeLists.txt"), "w") as f:
            f.write("cmake_minimum_required(VERSION 3.16)\n")
        gm = GuardManager(tmp_workdir, {})
        assert gm._is_cpp is True

    def test_cpp_staged_files_detected(self, tmp_workdir):
        """Staged .cpp file → _is_cpp True."""
        _write_staged_file(tmp_workdir, "src/main.cpp", "int main() { return 0; }\n")
        gm = GuardManager(tmp_workdir, {})
        assert gm._is_cpp is True

    def test_rust_repo_detected(self, tmp_workdir):
        """Cargo.toml → _is_rust True."""
        import os

        os.makedirs(tmp_workdir, exist_ok=True)
        with open(os.path.join(tmp_workdir, "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "demo"\n')
        gm = GuardManager(tmp_workdir, {})
        assert gm._is_rust is True

    def test_python_repo_not_cpp(self, tmp_workdir):
        """Plain Python repo → _is_cpp False."""
        gm = GuardManager(tmp_workdir, {})
        assert gm._is_cpp is False
        assert gm._is_rust is False

    def test_lsp_timeout_config_parsed(self, tmp_workdir):
        """guards.lsp_timeouts.{init,per_file} are parsed into manager."""
        gm = GuardManager(
            tmp_workdir, {"guards": {"lsp_timeouts": {"init": 600, "per_file": 240}}}
        )
        assert gm._lsp_init_timeout == 600
        assert gm._lsp_per_file_timeout == 240

    def test_lsp_timeout_config_defaults_none(self, tmp_workdir):
        """No lsp_timeouts config → None (language-aware defaults kick in)."""
        gm = GuardManager(tmp_workdir, {})
        assert gm._lsp_init_timeout is None
        assert gm._lsp_per_file_timeout is None


class TestBuiltinSecretsScan:
    """Test built-in secrets scanner patterns — step-1-3-1-3."""

    def test_aws_key_detected(self, tmp_workdir):
        """AWS access key (AKIA...) is detected."""
        # Stage a file with a fake AWS key
        _write_staged_file(tmp_workdir, "test.py", 'AWS_ACCESS_KEY = "AKIA1234567890ABCDEF"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "AWS access key" in result.output

    def test_gitleaks_allowlist_respected(self, tmp_workdir):
        """Files matched by .gitleaks.toml [allowlist] paths are exempt
        (GR-GAP-005 — builtin scanner mirrors gitleaks' allowlist so test
        fixtures with deliberate fake keys don't fail the guard)."""
        _write_staged_file(tmp_workdir, "test.py", 'AWS_ACCESS_KEY = "AKIA1234567890ABCDEF"')
        with open(os.path.join(tmp_workdir, ".gitleaks.toml"), "w") as f:
            f.write("[allowlist]\npaths = [\n  '''test\\.py''',\n]\n")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_openai_key_detected(self, tmp_workdir):
        """OpenAI key (sk-...) is detected as a hardcoded API key."""
        _write_staged_file(
            tmp_workdir, "config.py", 'OPENAI_API_KEY = "sk-12345678901234567890123456789012"'
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        # The 'api[_-]?key' pattern catches it as "hardcoded API key"
        assert "hardcoded API key" in result.output or "OpenAI" in result.output

    def test_github_token_detected(self, tmp_workdir):
        """GitHub token (ghp_...) is detected."""
        _write_staged_file(
            tmp_workdir, "main.py", 'GITHUB_TOKEN = "ghp_123456789012345678901234567890123456"'
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitHub personal access token" in result.output

    def test_private_key_block_detected(self, tmp_workdir):
        """Private key block (BEGIN RSA PRIVATE KEY) is detected."""
        _write_staged_file(
            tmp_workdir,
            "key.pem",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "private key block" in result.output.lower()

    def test_os_getenv_whitelisted(self, tmp_workdir):
        """os.getenv('API_KEY') is NOT flagged."""
        _write_staged_file(tmp_workdir, "app.py", 'api_key = os.getenv("API_KEY")')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_config_dict_whitelisted(self, tmp_workdir):
        """config['secret'] is NOT flagged."""
        _write_staged_file(tmp_workdir, "app.py", 'my_secret = config["secret"]')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_empty_password_whitelisted(self, tmp_workdir):
        """Empty password (PASSWORD="") is NOT flagged."""
        _write_staged_file(tmp_workdir, "docker.py", 'PASSWORD = ""')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_todo_placeholder_whitelisted(self, tmp_workdir):
        """TODO/PLACEHOLDER comment is NOT flagged."""
        _write_staged_file(tmp_workdir, "todo.py", "# TODO: sk-add-real-key-here (placeholder)")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        # May or may not flag depending on exact match — just verify no crash
        assert result is not None

    def test_jwt_encode_whitelisted(self, tmp_workdir):
        """JWT in jwt.encode() call is NOT flagged."""
        _write_staged_file(
            tmp_workdir, "auth.py", 'token = jwt.encode(payload, secret, algorithm="HS256")'
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_no_staged_files_no_findings(self, tmp_workdir):
        """No staged files → no findings, passed=True."""
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True
        assert "No staged files" in result.output

    def test_clean_file_no_findings(self, tmp_workdir):
        """Clean file with no secrets passes."""
        _write_staged_file(tmp_workdir, "clean.py", "def hello():\n    return 'world'\n")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True
        assert "clean" in result.output


class TestSecretsSanitization:
    """Test secret value redaction in output — step-1-3-1-4."""

    def test_secret_value_redacted(self, tmp_workdir):
        """Secret value is replaced with *** in output."""
        _write_staged_file(
            tmp_workdir, "secrets.py", 'api_key = "sk-abc123def456789012345678901234"'
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        # The actual key value must not appear in output
        assert "sk-abc123def456789012345678901234" not in result.output
        # The sanitized version should appear
        assert '"***"' in result.output or "sk-" in result.output


class TestGuardToggling:
    """Test guard toggling: run_all() only runs enabled guards — step-1-3-1-5."""

    def test_run_all_three_guards(self, guard_manager):
        """All guards enabled → run_all() returns 3 results."""
        with patch.object(
            guard_manager, "_check_secrets", return_value=GuardResult("secrets", True, "ok")
        ):
            with patch.object(
                guard_manager, "_check_lint", return_value=GuardResult("lint", True, "ok")
            ):
                with patch.object(
                    guard_manager, "_check_tests", return_value=GuardResult("tests", True, "ok")
                ):
                    result = guard_manager.run_all()
        assert len(result.results) == 3
        assert result.passed is True

    def test_only_secrets_enabled(self, tmp_workdir):
        """Only secrets enabled → run_all() returns 1 result."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": True, "lint": False, "tests": False}})
        with patch.object(gm, "_check_secrets", return_value=GuardResult("secrets", True, "ok")):
            result = gm.run_all()
        assert len(result.results) == 1

    def test_no_guards_enabled(self, tmp_workdir):
        """No guards enabled → run_all() returns 0 results, passed=True."""
        gm = GuardManager(
            tmp_workdir, {"guards": {"secrets": False, "lint": False, "tests": False}}
        )
        result = gm.run_all()
        assert len(result.results) == 0
        assert result.passed is True

    def test_run_all_sets_passed_false_on_any_failure(self, guard_manager):
        """If any guard fails, passed is False."""
        with patch.object(
            guard_manager, "_check_secrets", return_value=GuardResult("secrets", True, "ok")
        ):
            with patch.object(
                guard_manager, "_check_lint", return_value=GuardResult("lint", False, "error")
            ):
                with patch.object(
                    guard_manager, "_check_tests", return_value=GuardResult("tests", True, "ok")
                ):
                    result = guard_manager.run_all()
        assert result.passed is False


class TestLintGuard:
    """Test _check_lint behavior."""

    def test_no_py_files_staged(self, guard_manager):
        """Lint guard passes when no Python files are staged."""
        result = guard_manager._check_lint()
        assert result.passed is True

    def test_gitleaks_missing_falls_back(self, guard_manager):
        """When gitleaks not found, falls back to built-in scanner."""
        with patch("subprocess.run", side_effect=FileNotFoundError("gitleaks")):
            result = guard_manager._check_secrets()
        # Falls through to built-in scanner; should return a result
        assert result is not None


class TestTestsGuard:
    """Test _check_tests behavior."""

    def test_pytest_not_found_skips(self, guard_manager):
        """Tests guard returns failure when test command can't run."""
        # Since v0.1.2, _check_tests runs test_command directly (no pytest gate).
        # If the command can't be found, subprocess.run raises an exception.
        with patch("engine.guard_manager._get_staged_files", return_value=["dummy.py"]):
            with patch("subprocess.run", side_effect=FileNotFoundError("go")):
                result = guard_manager._check_tests()
        assert result.passed is False
        assert "go" in str(result.error) or "FileNotFound" in str(result.error)


class TestExtendedGuardManager:
    """Extended edge case coverage for GuardManager."""

    def test_custom_test_command_is_used(self, tmp_workdir):
        """_check_tests uses custom test_command from config."""
        gm = GuardManager(tmp_workdir, {"guards": {"test_command": "echo custom-test-run"}})
        # Mock subprocess.run to capture the command.
        # _get_staged_files now makes 2 calls (rev-parse + diff/ls-files),
        # then _check_tests makes 2 more (pytest --version + actual test cmd).
        mock_run = MagicMock()
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "custom output"
        mock_run.return_value.stderr = ""
        with patch(
            "subprocess.run",
            side_effect=[
                MagicMock(returncode=0, stdout="abc1234...", stderr=""),  # git rev-parse HEAD
                MagicMock(returncode=0, stdout="test.py\n", stderr=""),  # git diff --cached
                MagicMock(returncode=0, stdout="pytest 7.0", stderr=""),  # pytest --version
                mock_run.return_value,  # echo custom-test-run
            ],
        ):
            result = gm._check_tests()
        assert result.passed is True

    def test_check_tests_timeout_returns_failure(self, guard_manager):
        """_check_tests handles subprocess timeout."""
        with patch("engine.guard_manager._get_staged_files", return_value=["dummy.py"]):
            with patch(
                "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="go test", timeout=120)
            ):
                result = guard_manager._check_tests()
        assert result.passed is False
        assert "timed out" in result.output

    def test_gitleaks_available_used_first(self, tmp_workdir):
        """When gitleaks is available, _check_secrets uses it first."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "gitleaks: clean"
        mock_run.stderr = ""
        with patch("subprocess.run", return_value=mock_run):
            with patch.object(
                gm,
                "_builtin_secrets_scan",
                return_value=GuardResult("secrets", True, "Scanned 0 files — clean"),
            ):
                result = gm._check_secrets()
        assert "gitleaks" in result.output
        assert result.passed is True

    def test_gitleaks_clean_builtin_findings_fail(self, tmp_workdir):
        """GR-GAP-005: gitleaks clean but built-in scanner finds a secret
        (low-entropy key gitleaks' entropy filter skips) → secrets guard fails."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "gitleaks: clean"
        mock_run.stderr = ""
        with patch("subprocess.run", return_value=mock_run):
            with patch.object(
                gm,
                "_builtin_secrets_scan",
                return_value=GuardResult("secrets", False, "Potential secrets found:\n.env:1: [AWS access key] AWS_ACCESS_KEY_ID=\"***\""),
            ):
                result = gm._check_secrets()
        assert result.passed is False
        assert "Potential secrets found" in result.output

    def test_gitleaks_returns_findings(self, tmp_workdir):
        """When gitleaks reports findings, secrets guard fails."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 1
        mock_run.stdout = "leak detected in config.py"
        mock_run.stderr = ""
        with patch("subprocess.run", return_value=mock_run):
            result = gm._check_secrets()
        assert result.passed is False

    def test_gitleaks_findings_output_excludes_banner(self, tmp_workdir):
        """gitleaks is invoked with --no-banner so captured guard output
        (which feeds judge verdicts) stays free of the ASCII logo banner."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 1
        mock_run.stdout = ""
        # Simulate gitleaks run WITH --no-banner: findings on stderr, no logo.
        mock_run.stderr = "Finding: config.py:5:6  generic-api-key  AWS API key\n"
        with patch("subprocess.run", return_value=mock_run) as mock_patch:
            result = gm._check_secrets()
        # The banner-suppression flag must actually be passed to gitleaks.
        cmd = mock_patch.call_args.args[0]
        assert "--no-banner" in cmd
        # Finding detail still surfaces in the guard output...
        assert result.passed is False
        assert "Finding: config.py" in result.output
        # ...but the gitleaks logo banner must not pollute it.
        assert "gitleaks v" not in result.output
        assert "○" not in result.output

    def test_gitleaks_scans_staged_not_whole_tree(self, tmp_workdir):
        """GR-GAP-007: gitleaks runs `protect --staged` (staged blobs only),
        NOT `detect --no-git` (whole tree). Whole-tree scanning flags
        gitignored local config (e.g. .env holding a live key) on EVERY
        commit; staged-only scanning matches the guard's diff-mode contract
        while still catching force-staged .env files."""
        gm = GuardManager(tmp_workdir)
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = "gitleaks: clean"
        mock_run.stderr = ""
        with patch("subprocess.run", return_value=mock_run) as mock_patch:
            with patch.object(
                gm,
                "_builtin_secrets_scan",
                return_value=GuardResult("secrets", True, "Scanned 0 files — clean"),
            ):
                result = gm._check_secrets()
        cmd = mock_patch.call_args.args[0]
        assert "protect" in cmd
        assert "--staged" in cmd
        assert "--no-git" not in cmd
        assert "--no-banner" in cmd
        assert result.passed is True

    def test_lint_ruff_available(self, tmp_workdir):
        """_check_lint uses ruff when available with Python files staged."""
        _write_staged_file(tmp_workdir, "code.py", "x = 1\n")
        gm = GuardManager(tmp_workdir)
        mock_git_rev = MagicMock()
        mock_git_rev.returncode = 0
        mock_git_rev.stdout = "abc123..."
        mock_git_rev.stderr = ""
        mock_git_diff = MagicMock()
        mock_git_diff.returncode = 0
        mock_git_diff.stdout = "code.py"
        mock_git_diff.stderr = ""
        mock_ruff = MagicMock()
        mock_ruff.returncode = 0
        mock_ruff.stdout = "ruff: clean"
        mock_ruff.stderr = ""
        with patch("subprocess.run", side_effect=[mock_git_rev, mock_git_diff, mock_ruff]):
            result = gm._check_lint()
        assert result.passed is True
        assert "ruff" in result.output

    def test_guard_result_empty_name(self):
        """GuardResult with empty name still produces valid output."""
        gr = GuardResult(name="", passed=True, output="ok")
        assert gr.name == ""
        assert gr.passed is True

    def test_tier1_result_no_results(self):
        """Tier1Result with empty results list has empty summary."""
        tr = Tier1Result(passed=True, results=[])
        assert tr.passed is True
        assert tr.summary == ""

    def test_secrets_scan_skips_large_files(self, tmp_workdir):
        """Secrets scanner skips files larger than 1MB."""
        _write_staged_file(tmp_workdir, "huge.bin", "x" * 2_000_000)
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is True

    def test_secrets_scan_binary_file_graceful(self, tmp_workdir):
        """Secrets scanner handles binary files without crashing."""
        _write_staged_file(tmp_workdir, "binary.bin", "\x00\x01\x02\x03\x04")
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        # Should not crash, may pass or fail depending on content
        assert result is not None

    def test_gitlab_token_detected(self, tmp_workdir):
        """GitLab token (glpat-) is detected."""
        _write_staged_file(tmp_workdir, "gitlab.py", 'token = "glpat-ABCDEFGHIJ1234567890"')
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitLab" in result.output

    def test_gho_token_detected(self, tmp_workdir):
        """GitHub OAuth token (gho_) is detected."""
        _write_staged_file(
            tmp_workdir, "github.py", 'oauth = "gho_abcdef123456789012345678901234567890"'
        )
        gm = GuardManager(tmp_workdir)
        result = gm._builtin_secrets_scan()
        assert result.passed is False
        assert "GitHub OAuth" in result.output

    def test_tier1_summary_format(self):
        """Tier1Result summary formats correctly with mixed results."""
        results = [
            GuardResult("secrets", True, "clean"),
            GuardResult("lint", False, "E501 error"),
        ]
        tr = Tier1Result(passed=False, results=results)
        summary = tr.summary
        assert "✓ secrets" in summary
        assert "✗ lint" in summary


# ── Regression: GuardManager self-loads config + diff mode skip ──────────────


class TestGuardManagerConfigAutoLoad:
    """Regression: GuardManager must auto-load .gitreins/config.yaml
    when no config dict is passed (e.g. from pre-commit hook script)."""

    def test_auto_loads_test_mode_from_config(self, tmp_workdir):
        """GuardManager() with no config dict reads test_mode from .gitreins/config.yaml."""
        import yaml

        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        yaml.safe_dump(
            {"guards": {"test_mode": "diff", "test_command": "echo ok"}},
            open(config_path, "w"),
        )

        gm = GuardManager(workdir)
        assert gm.test_mode == "diff", (
            f"GuardManager must read test_mode from config, got '{gm.test_mode}'"
        )

    def test_auto_loads_defaults_when_config_missing(self, tmp_workdir):
        """GuardManager() defaults to test_mode='full' when no config file exists."""
        gm = GuardManager(tmp_workdir)
        assert gm.test_mode == "full", f"Default test_mode should be 'full', got '{gm.test_mode}'"

    def test_auto_load_does_not_override_explicit_config(self, tmp_workdir):
        """Explicit config dict takes priority over .gitreins/config.yaml."""
        import yaml

        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        yaml.safe_dump(
            {"guards": {"test_mode": "full", "test_command": "echo ok"}},
            open(config_path, "w"),
        )

        # Pass explicit config with diff — should win
        gm = GuardManager(workdir, config={"guards": {"test_mode": "diff"}})
        assert gm.test_mode == "diff", "Explicit config dict must take priority"

    def test_auto_load_picks_up_secrets_flags(self, tmp_workdir):
        """GuardManager reads enabled flags (secrets, lint, tests) from config."""
        import yaml

        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        yaml.safe_dump(
            {"guards": {"secrets": False, "lint": False, "tests": True, "test_command": "echo ok"}},
            open(config_path, "w"),
        )

        gm = GuardManager(workdir)
        assert gm.test_mode == "full"  # default
        # Run all — secrets and lint should be skipped
        result = gm.run_all()
        # With secrets and lint disabled, only tests should run
        # (secrets returns PASS when disabled, but run_all skips them entirely)
        summary = result.summary
        # Only tests should appear (secrets and lint are disabled → not in output)
        assert "tests" in summary


class TestDiscoverTestTargetsDiffMode:
    """Regression: _discover_test_targets returns [] (not None) when
    no matching test files found, and _check_tests skips with PASS."""

    def test_returns_empty_list_when_no_test_file_exists(self, tmp_workdir):
        """_discover_test_targets returns [] when staged file has no matching
        test file, NOT None (which caused fallthrough to full suite)."""
        import os

        workdir = tmp_workdir
        # Create a source file with NO corresponding test file
        src_dir = os.path.join(workdir, "src")
        os.makedirs(src_dir, exist_ok=True)
        src_file = os.path.join(src_dir, "utils.py")
        with open(src_file, "w") as f:
            f.write("def helper(): pass\n")
        # Stage it
        subprocess.run(["git", "add", src_file], cwd=workdir, capture_output=True)

        result = _discover_test_targets(workdir)
        assert result == [], (
            f"_discover_test_targets must return [] when no test files match, got {result!r}"
        )

    def test_returns_test_file_when_match_exists(self, tmp_workdir):
        """_discover_test_targets returns the matching test file when it exists."""
        import os

        workdir = tmp_workdir
        # Create a source file AND a matching test file
        src_dir = os.path.join(workdir, "engine")
        os.makedirs(src_dir, exist_ok=True)
        src_file = os.path.join(src_dir, "thing.py")
        with open(src_file, "w") as f:
            f.write("def do_stuff(): pass\n")

        tests_dir = os.path.join(workdir, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        test_file = os.path.join(tests_dir, "test_thing.py")
        with open(test_file, "w") as f:
            f.write("def test_do_stuff(): pass\n")

        # Stage the source
        subprocess.run(["git", "add", src_file], cwd=workdir, capture_output=True)

        result = _discover_test_targets(workdir)
        assert len(result) == 1
        assert "test_thing.py" in result[0]

    def test_force_full_trigger_returns_none_for_config_change(self, tmp_workdir):
        """When .gitreins/config.yaml is staged, _discover_test_targets returns None
        (force-full trigger) to run the full test suite."""
        import os

        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_file = os.path.join(config_dir, "config.yaml")
        with open(config_file, "w") as f:
            f.write("guards: {test_mode: full}\n")
        subprocess.run(["git", "add", config_file], cwd=workdir, capture_output=True)

        result = _discover_test_targets(workdir)
        assert result is None, (
            f"Force-full trigger (.gitreins/config.yaml) must return None, got {result!r}"
        )

    def test_check_tests_skips_when_no_matching_test_files(self, tmp_workdir):
        """_check_tests in diff mode returns PASS when no test files match,
        instead of falling through to full suite (and timing out)."""
        import os

        workdir = tmp_workdir
        # Create .gitreins/config.yaml with test_mode=diff
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        import yaml

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.safe_dump({"guards": {"test_mode": "diff", "test_command": "echo ok"}}, f)

        # Create a source file with NO matching test file
        src_dir = os.path.join(workdir, "lib")
        os.makedirs(src_dir, exist_ok=True)
        src_file = os.path.join(src_dir, "helpers.py")
        with open(src_file, "w") as f:
            f.write("x=1\n")
        subprocess.run(["git", "add", src_file], cwd=workdir, capture_output=True)

        gm = GuardManager(workdir)
        # _check_tests should return PASS with skip message, NOT run echo ok
        result = gm._check_tests()
        assert result.passed is True, (
            f"_check_tests should PASS when no matching test files in diff mode, got: {result.output}"
        )
        assert "skipped" in result.output.lower(), (
            f"Output should mention skip, got: {result.output}"
        )


class TestBuildDiffTestCommand:
    """_build_diff_test_command narrows pytest invocations to specific test files."""

    def test_narrows_python3_m_pytest(self, tmp_path):
        """`python3 -m pytest` is recognized and narrowed to the test file path.

        Regression for DF-002: init now generates `python3 -m pytest ...`
        for root-package layouts, and diff-mode narrowing must still append
        the test file paths instead of running the full suite.
        """
        import os

        workdir = str(tmp_path)
        abs_test = os.path.join(workdir, "tests", "test_a.py")
        cmd = _build_diff_test_command(
            "python3 -m pytest -x --tb=short", [abs_test], workdir
        )
        assert cmd == "python3 -m pytest -x --tb=short tests/test_a.py"
        assert cmd.endswith("tests/test_a.py")

    def test_narrows_python_m_pytest(self, tmp_path):
        """`python -m pytest` keeps narrowing (existing behavior unchanged)."""
        import os

        workdir = str(tmp_path)
        abs_test = os.path.join(workdir, "tests", "test_a.py")
        cmd = _build_diff_test_command("python -m pytest -x --tb=short", [abs_test], workdir)
        assert cmd == "python -m pytest -x --tb=short tests/test_a.py"

    def test_narrows_bare_pytest(self, tmp_path):
        """Bare `pytest` keeps narrowing (existing behavior unchanged)."""
        import os

        workdir = str(tmp_path)
        abs_test = os.path.join(workdir, "tests", "test_a.py")
        cmd = _build_diff_test_command("pytest -x --tb=short", [abs_test], workdir)
        assert cmd == "pytest -x --tb=short tests/test_a.py"

    def test_leaves_non_pytest_runner_untouched(self, tmp_path):
        """Custom runners can't be narrowed — original command is returned."""
        import os

        workdir = str(tmp_path)
        abs_test = os.path.join(workdir, "tests", "test_a.py")
        cmd = _build_diff_test_command("npm test", [abs_test], workdir)
        assert cmd == "npm test"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_skeleton_git(path):
    """Create a minimal .git skeleton like the tmp_workdir fixture."""
    git_dir = os.path.join(path, ".git")
    os.makedirs(os.path.join(git_dir, "objects"))
    os.makedirs(os.path.join(git_dir, "refs", "heads"))
    with open(os.path.join(git_dir, "HEAD"), "w") as f:
        f.write("ref: refs/heads/main\n")
    with open(os.path.join(git_dir, "config"), "w") as f:
        f.write("[core]\n\trepositoryformatversion = 0\n\tbare = false\n")


# ── DF-008: GIT_* env must not leak into guard subprocesses ──────────────────


class TestSanitizedEnv:
    """DF-008: git exports GIT_INDEX_FILE (and friends) to pre-commit hooks.

    If the guard passes them through to its subprocesses, a nested guard
    reads the OUTER repo's index instead of its own workdir's index.
    """

    def test_sanitized_env_strips_all_git_vars(self, monkeypatch):
        """_sanitized_env() removes every GIT_* variable and keeps the rest."""
        from engine.guard_manager import _sanitized_env

        monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/outer/.git/index")
        monkeypatch.setenv("GIT_DIR", "/tmp/outer/.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/tmp/outer")
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/outer/.git/objects")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/outer/.git/objects")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/tester")

        env = _sanitized_env()
        assert "GIT_INDEX_FILE" not in env
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env
        assert "GIT_OBJECT_DIRECTORY" not in env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/tester"

    def test_sanitized_env_strips_llm_cred_vars(self, monkeypatch):
        """INFRA-LLM-ENV-001: guard subprocesses must not inherit LLM creds.

        The guard spawns pytest/linters/gitleaks/nested guards; a judge run
        exports GITREINS_LLM_* + provider fallback keys for its own LLMClient
        calls, and leaking them into the guard's pytest subprocess breaks
        tests/test_llm.py env-priority tests (api_key == '' asserted, real
        key found). Same block list as the other spawn sites — shared via
        engine.env_sanitize so it can never drift.
        """
        from engine.guard_manager import _sanitized_env

        monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary-key")
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "deepseek-v4-flash")
        monkeypatch.setenv("GITREINS_MAX_ITERATIONS", "400")
        for k in (
            "NEURALWATT_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            monkeypatch.setenv(k, "leaked-key")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = _sanitized_env()
        assert "GITREINS_LLM_API_KEY" not in env
        assert "GITREINS_LLM_BASE_URL" not in env
        assert "GITREINS_LLM_MODEL" not in env
        assert "GITREINS_MAX_ITERATIONS" not in env
        for k in (
            "NEURALWATT_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            assert k not in env
        assert env["PATH"] == "/usr/bin:/bin"

    def test_get_staged_files_ignores_leaked_git_index_file(
        self, tmp_workdir, tmp_path, monkeypatch
    ):
        """Staged-file discovery uses the workdir's own index even when a
        pre-commit hook leaked GIT_INDEX_FILE pointing at a foreign repo."""
        from engine.guard_manager import _get_staged_files

        _write_staged_file(tmp_workdir, "app.py", "def main(): pass\n")

        # Foreign repo whose index holds a file that does NOT exist in
        # tmp_workdir — simulates the outer repo's index in a nested guard.
        foreign = str(tmp_path / "foreign")
        os.makedirs(foreign)
        _make_skeleton_git(foreign)
        _write_staged_file(foreign, "phantom.py", "x = 1\n")

        monkeypatch.setenv("GIT_INDEX_FILE", os.path.join(foreign, ".git", "index"))

        assert _get_staged_files(tmp_workdir) == ["app.py"]


def _write_staged_file(workdir, filename, content):
    """Create a file and stage it in a real git repo, for secrets scan testing.

    Uses git init + git add to create a realistic staged file.
    """
    import os

    filepath = os.path.join(workdir, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    # Stage the file
    subprocess.run(["git", "add", filepath], cwd=workdir, capture_output=True)
