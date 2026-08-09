"""Dedicated tests for Go guard checks."""

import os
import subprocess
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from engine.guards import (
    GoGuardResult,
    _sanitized_env,
    check_go_build,
    check_go_lint,
    check_go_tests,
    is_go_project,
)


def completed(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_go_guard_result_defaults():
    result = GoGuardResult(name="go_build", passed=True)

    assert result.output == ""
    assert result.error == ""


def test_is_go_project_requires_go_mod_file(tmp_path):
    assert is_go_project(str(tmp_path)) is False

    (tmp_path / "go.mod").mkdir()
    assert is_go_project(str(tmp_path)) is False

    (tmp_path / "go.mod").rmdir()
    (tmp_path / "go.mod").write_text("module example.test\n")
    assert is_go_project(str(tmp_path)) is True


@pytest.mark.parametrize(
    ("checker", "name"),
    [
        (check_go_lint, "go_lint"),
        (check_go_tests, "go_tests"),
        (check_go_build, "go_build"),
    ],
)
def test_checkers_skip_when_no_go_files_are_staged(checker, name):
    with patch("engine.guards.subprocess.run", return_value=completed("README.md\n")) as run:
        result = checker("/repo")

    assert result == GoGuardResult(name=name, passed=True, output="No Go files staged")
    run.assert_called_once_with(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/repo",
        env=_sanitized_env(),
    )


def test_check_go_lint_uses_golangci_lint_when_it_passes():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\npkg/lib.go\n"), completed()],
    ) as run:
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
    assert run.call_args_list[1] == call(
        [
            "golangci-lint",
            "run",
            "--new-from-rev=HEAD~1",
            "main.go",
            "pkg/lib.go",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/repo",
        env=_sanitized_env(),
    )


@pytest.mark.parametrize("lint_result", [FileNotFoundError(), completed(returncode=1)])
def test_check_go_lint_falls_back_to_go_vet(lint_result):
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), lint_result, completed()],
    ) as run:
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
    assert run.call_args_list[-1] == call(
        ["go", "vet", "./..."],
        capture_output=True,
        text=True,
        timeout=60,
        cwd="/repo",
        env=_sanitized_env(),
    )


def test_check_go_lint_returns_truncated_vet_failure():
    output = "out" + "e" * 2100
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), FileNotFoundError(), completed("out", "e" * 2100, 1)],
    ):
        result = check_go_lint("/repo")

    assert result.passed is False
    assert result.output == output[:2000] + "\n... [truncated]"


def test_check_go_lint_reports_vet_exception_as_error():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), FileNotFoundError(), OSError("go unavailable")],
    ):
        result = check_go_lint("/repo")

    assert result == GoGuardResult(name="go_lint", passed=False, error="go unavailable")


def test_check_go_tests_returns_bounded_success_output():
    output = "a" * 600
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed(output)],
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=True, output=output[:500])


def test_check_go_tests_keeps_tail_of_failure_output():
    output = "x" * 2100 + "failure"
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed("x" * 2100, "failure", 1)],
    ):
        result = check_go_tests("/repo")

    assert result == GoGuardResult(name="go_tests", passed=False, output=output[-2000:])


def test_check_go_tests_handles_timeout_and_other_exceptions():
    timeout = subprocess.TimeoutExpired(["go", "test"], 180)
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), timeout],
    ):
        timed_out = check_go_tests("/repo")
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), OSError("go unavailable")],
    ):
        errored = check_go_tests("/repo")

    assert timed_out == GoGuardResult(
        name="go_tests",
        passed=False,
        output="Tests timed out after 180s (guards.test_timeout). Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for large projects with slow integration suites.",
    )
    assert errored == GoGuardResult(name="go_tests", passed=False, error="go unavailable")


def test_check_go_build_returns_success_and_expected_command():
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed()],
    ) as run:
        result = check_go_build("/repo")

    assert result == GoGuardResult(name="go_build", passed=True, output="go build: ok")
    assert run.call_args_list[1] == call(
        ["go", "build", "-buildvcs=false", "./..."],
        capture_output=True,
        text=True,
        timeout=120,
        cwd="/repo",
        env=_sanitized_env(),
    )


def test_sanitized_env_strips_all_git_vars():
    """GIT_* leaked by the pre-commit hook must not reach go subprocesses."""
    with patch.dict(
        os.environ,
        {
            "GIT_INDEX_FILE": "/repo/.git/index",
            "GIT_DIR": "/repo/.git",
            "GIT_WORK_TREE": "/repo",
            "PATH": "/usr/bin",
            "HOME": "/home/test",
        },
        clear=True,
    ):
        env = _sanitized_env()
    assert "GIT_INDEX_FILE" not in env
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/test"


def test_sanitized_env_strips_gitreins_max_budget_vars():
    """GITREINS_MAX_* budget controls must not leak into test subprocesses.

    Proven 2026-08-09 R2-16: a judge run exporting GITREINS_MAX_OUTPUT_TOKENS
    leaked into the tier1 pytest subprocess and broke EvalCap/config-priority
    tests (assert 2000000 == 1000000) — tier1 tests FAIL exit 2 with the env
    set, 61 passed with it stripped. Same class as the GIT_* strip.
    """
    with patch.dict(
        os.environ,
        {
            "GITREINS_MAX_ITERATIONS": "400",
            "GITREINS_MAX_OUTPUT_TOKENS": "2M",
            "GITREINS_MAX_INPUT_TOKENS": "10M",
            "GITREINS_MAX_TIME": "60m",
            "GITREINS_LLM_API_KEY": "sk-keep-me",
            "PATH": "/usr/bin",
        },
        clear=True,
    ):
        env = _sanitized_env()
    assert "GITREINS_MAX_ITERATIONS" not in env
    assert "GITREINS_MAX_OUTPUT_TOKENS" not in env
    assert "GITREINS_MAX_INPUT_TOKENS" not in env
    assert "GITREINS_MAX_TIME" not in env
    # LLM config vars are NOT budget controls — they must survive (the judge
    # subprocess needs them for its own provider calls).
    assert env["GITREINS_LLM_API_KEY"] == "sk-keep-me"
    assert env["PATH"] == "/usr/bin"


def test_go_tests_sanitizes_env_even_with_git_index_file_leak():
    """DF-008 Go variant: go test subprocess must not inherit GIT_INDEX_FILE
    (breaks worktree tests: 'fatal: .git/index: index file open failed')."""
    with patch("engine.guards.subprocess.run", return_value=completed("main.go\n")) as run:
        check_go_tests("/repo")
    _, kwargs = run.call_args_list[0]
    assert "GIT_INDEX_FILE" not in kwargs["env"]
    assert "GIT_DIR" not in kwargs["env"]


def test_check_go_build_returns_truncated_failure_or_exception():
    output = "o" * 1000 + "e" * 1100
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), completed("o" * 1000, "e" * 1100, 1)],
    ):
        failed = check_go_build("/repo")
    with patch(
        "engine.guards.subprocess.run",
        side_effect=[completed("main.go\n"), OSError("build unavailable")],
    ):
        errored = check_go_build("/repo")

    assert failed == GoGuardResult(
        name="go_build",
        passed=False,
        output=output[:2000] + "\n... [truncated]",
    )
    assert errored == GoGuardResult(name="go_build", passed=False, error="build unavailable")
