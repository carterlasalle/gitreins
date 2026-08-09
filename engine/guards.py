"""Go-specific guard checks for GitReins."""

import logging
import os
import subprocess
from dataclasses import dataclass

from engine.env_sanitize import sanitized_env as _sanitized_env

# NOTE: _sanitized_env is the shared env sanitizer (engine/env_sanitize.py) —
# GIT_* (pre-commit hook leaks, DF-008), GITREINS_MAX_* (judge budget caps,
# R2-16), GITREINS_LLM_* and the provider fallback *_API_KEYs (LLM credential
# leaks, INFRA-LLM-ENV-001) are stripped from every subprocess env. Kept under
# the historical name because guards tests import it from engine.guards.

logger = logging.getLogger("gitreins.guards.go")


@dataclass
class GoGuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""


def is_go_project(workdir: str) -> bool:
    """Return True if go.mod exists in workdir."""
    return os.path.isfile(os.path.join(workdir, "go.mod"))


def check_go_lint(workdir: str) -> GoGuardResult:
    """Run go vet on staged Go files. Fall back to golangci-lint if available."""
    # Get staged Go files
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_lint", passed=True, output="No Go files staged")

    # Try golangci-lint first
    try:
        result = subprocess.run(
            ["golangci-lint", "run", "--new-from-rev=HEAD~1", *go_files],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workdir,
            env=_sanitized_env(),
        )
        if result.returncode == 0:
            return GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
        # Fall through to go vet on failure
    except FileNotFoundError:
        pass

    # Fallback: go vet (per package or per file)
    try:
        result = subprocess.run(
            ["go", "vet", "./..."],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_lint", passed=True, output="go vet: clean")
        return GoGuardResult(name="go_lint", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_lint", passed=False, error=str(e))


def check_go_tests(workdir: str, timeout: int = 180) -> GoGuardResult:
    """Run go test on staged Go files.

    timeout is configurable so large Go projects (slow integration
    suites) can raise it via guards.test_timeout in .gitreins/config.yaml.
    """
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_tests", passed=True, output="No Go files staged")

    try:
        result = subprocess.run(
            ["go", "test", "-count=1", "-short", "./..."],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[-2000:]
        if result.returncode == 0:
            return GoGuardResult(name="go_tests", passed=True, output=output[:500])
        return GoGuardResult(name="go_tests", passed=False, output=output)
    except subprocess.TimeoutExpired:
        return GoGuardResult(
            name="go_tests",
            passed=False,
            output=f"Tests timed out after {timeout}s (guards.test_timeout). "
            "Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for "
            "large projects with slow integration suites.",
        )
    except Exception as e:
        return GoGuardResult(name="go_tests", passed=False, error=str(e))


def check_go_build(workdir: str) -> GoGuardResult:
    """Run go build on staged Go files to catch compile errors."""
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=workdir,
        env=_sanitized_env(),
    )
    go_files = [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    if not go_files:
        return GoGuardResult(name="go_build", passed=True, output="No Go files staged")

    try:
        result = subprocess.run(
            ["go", "build", "-buildvcs=false", "./..."],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir,
            env=_sanitized_env(),
        )
        output = result.stdout + result.stderr
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if result.returncode == 0:
            return GoGuardResult(name="go_build", passed=True, output="go build: ok")
        return GoGuardResult(name="go_build", passed=False, output=output)
    except Exception as e:
        return GoGuardResult(name="go_build", passed=False, error=str(e))
