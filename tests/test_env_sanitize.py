"""Tests for the shared subprocess env sanitizer (INFRA-LLM-ENV-001).

Every subprocess GitReins spawns (guards, tier1 script steps, evaluator and
verifier run_command tools) uses :func:`engine.env_sanitize.sanitized_env` so
the block list can never drift between spawn sites. These tests pin the
shared predicate and prove the polluted-env repro is green end-to-end.
"""

import subprocess
import sys
from pathlib import Path

from engine.env_sanitize import _BLOCKED_KEYS, is_blocked_env_key, sanitized_env

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The full LLMClient fallback chain in engine/llm.py — every key it reads at
#: construction. Any one leaking into a test subprocess defeats the
#: env-priority tests in tests/test_llm.py.
FALLBACK_KEYS = (
    "NEURALWATT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)


def test_block_list_covers_full_llmclient_fallback_chain():
    """The exact-key block list mirrors engine/llm.py's fallback chain."""
    assert set(FALLBACK_KEYS) <= _BLOCKED_KEYS


def test_is_blocked_env_key_prefixes():
    """GIT_*, GITREINS_MAX_* and GITREINS_LLM_* prefixes are blocked."""
    for key in (
        "GIT_INDEX_FILE",
        "GIT_DIR",
        "GITREINS_MAX_ITERATIONS",
        "GITREINS_MAX_OUTPUT_TOKENS",
        "GITREINS_LLM_API_KEY",
        "GITREINS_LLM_BASE_URL",
        "GITREINS_LLM_MODEL",
        "GITREINS_LLM_PROVIDER",
    ):
        assert is_blocked_env_key(key), key


def test_is_blocked_env_key_exact_keys():
    """Every provider fallback key is blocked by exact match."""
    for key in FALLBACK_KEYS:
        assert is_blocked_env_key(key), key


def test_is_blocked_env_key_benign_keys_allowed():
    """Ordinary env vars must pass through untouched."""
    for key in ("PATH", "HOME", "PYTHONPATH", "CI", "GITHUB_TOKEN"):
        assert not is_blocked_env_key(key), key


def test_sanitized_env_strips_everything(monkeypatch):
    """sanitized_env() removes all blocked vars and keeps the rest."""
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/outer/.git/index")
    monkeypatch.setenv("GITREINS_MAX_ITERATIONS", "400")
    monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary-key")
    monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://llm.test/v1")
    for k in FALLBACK_KEYS:
        monkeypatch.setenv(k, "leaked-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")

    env = sanitized_env()
    for key in (
        "GIT_INDEX_FILE",
        "GITREINS_MAX_ITERATIONS",
        "GITREINS_LLM_API_KEY",
        "GITREINS_LLM_BASE_URL",
        *FALLBACK_KEYS,
    ):
        assert key not in env, key
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"


def test_sanitized_env_subprocess_env_is_clean(monkeypatch):
    """A subprocess spawned with env=sanitized_env() sees zero blocked vars.

    Direct probe of the spawn boundary: even though the parent process env is
    polluted with LLM credentials, the child must not inherit any of them.
    """
    monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    probe = (
        "import os; "
        "from engine.env_sanitize import is_blocked_env_key; "
        "leaked = [k for k in os.environ if is_blocked_env_key(k)]; "
        "print('LEAKED:', leaked); "
        "assert not leaked, leaked"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        env=sanitized_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_polluted_env_pytest_llm_goes_green(monkeypatch):
    """INFRA-LLM-ENV-001 proven repro: polluted env + sanitized subprocess.

    With GITREINS_LLM_API_KEY and OPENROUTER_API_KEY in the parent env (as a
    judge run exports them), the tier1 pytest subprocess spawned with
    env=sanitized_env() must run tests/test_llm.py green (58 passed). Before
    the fix the subprocess inherited the leaked key, LLMClient's fallback
    picked it up, and test_missing_all_keys_returns_empty failed.
    """
    monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_llm.py", "-q"],
        cwd=str(REPO_ROOT),
        env=sanitized_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
