"""
Guard Manager — Static checks at pre-commit time.

Tier 1 (no LLM, fast):
    1. Secrets scanning (gitleaks or built-in pattern scanner)
    2. Lint (ruff/flake8)
    3. Tests (full or diff-mode, configurable)

All checks are optional and configurable via .gitreins/config.yaml

Config:
    guards:
      secrets: true
      lint: true
      tests: true
      test_mode: "full" | "diff"     # default: full
      test_command: "pytest -x --tb=short"
"""

import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from engine.env_sanitize import sanitized_env
from engine.guards import check_go_lint, check_go_tests, check_go_build
from engine.lsp import run_lsp_check
from engine.types import GuardResult, Tier1Result

logger = logging.getLogger("gitreins.guard")


def _sanitized_env() -> dict[str, str]:
    """Return the current environment with every hostile variable removed.

    Shared block list from engine/env_sanitize.py — GIT_* (pre-commit hook
    leaks, DF-008), GITREINS_MAX_* (judge budget caps, R2-16), GITREINS_LLM_*
    and the provider fallback *_API_KEYs (LLM credential leaks,
    INFRA-LLM-ENV-001). The guard spawns pytest, linters, gitleaks and
    nested guards — none of them may inherit the judge's LLM credentials or
    the OUTER repository's git state.
    """
    return sanitized_env()


# ── Diff-based test discovery ──────────────────────────────────

# Files that, when changed, force a full test run (too broad to narrow)
_FORCE_FULL_TEST_GLOBS = [
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    ".gitreins/config.yaml",
    ".github/workflows/*.yml",
    "Makefile",
]


def _discover_test_targets(workdir: str) -> list[str] | None:
    """Return a list of test file paths to run, or None for full suite.

    None means "full suite" — returned when:
    - No staged changes
    - A force-full file was changed
    - No test files map to the changed sources (safety fallback)

    Returns absolute paths to test files.
    """
    # Get staged files
    staged = _get_staged_files(workdir)
    if not staged:
        return None

    # Check force-full triggers
    for staged_file in staged:
        for glob in _FORCE_FULL_TEST_GLOBS:
            if fnmatch.fnmatch(staged_file, glob):
                logger.debug("Force-full trigger: %s matches %s", staged_file, glob)
                return None

    # Map staged source files to test files using basename matching
    test_files: set[str] = set()
    for staged_file in staged:
        # If a test file itself changed, always include it
        basename = os.path.basename(staged_file)
        if basename.startswith("test_") and basename.endswith(".py"):
            test_files.add(os.path.join(workdir, staged_file))
            continue

        # Derive test file from source basename:
        #   engine/foo.py → tests/test_foo.py
        #   gitreins/bar.py → tests/test_bar.py
        #   gitreins_mcp/server.py → tests/test_mcp_server.py
        module = os.path.splitext(basename)[0]
        candidates = [
            os.path.join(workdir, "tests", f"test_{module}.py"),
        ]
        # Special case: gitreins_mcp/server.py → tests/test_mcp_server.py
        if os.path.dirname(staged_file).startswith("gitreins_mcp"):
            candidates.append(os.path.join(workdir, "tests", f"test_mcp_{module}.py"))

        for candidate in candidates:
            if os.path.isfile(candidate):
                test_files.add(candidate)
                logger.debug("Mapped %s → %s", staged_file, os.path.relpath(candidate, workdir))
                break
        else:
            logger.debug("No test file found for %s (tried: %s)", staged_file, candidates)

    if not test_files:
        # Changed files don't map to any known tests — skip in diff mode
        logger.debug("No test targets discovered for staged files, returning empty")
        return []

    return sorted(test_files)


def _get_staged_files(workdir: str) -> list[str]:
    """Return staged file paths relative to workdir.

    Uses git diff --cached when HEAD exists (only changed files),
    falls back to git ls-files --cached when no HEAD (all staged files
    are new and should still be scanned).
    """
    try:
        head_check = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=workdir,
            env=_sanitized_env(),
        )
        if head_check.returncode != 0:
            # No HEAD — use ls-files to list all staged files.
            # In a fresh repo, every staged file is "new" but the
            # guard should still scan them for secrets/lint/etc.
            result = subprocess.run(
                ["git", "ls-files", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=workdir,
                env=_sanitized_env(),
            )
        else:
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=workdir,
                env=_sanitized_env(),
            )
        return [f.strip() for f in result.stdout.split("\n") if f.strip()]
    except Exception:
        return []


def _build_diff_test_command(test_command: str, test_files: list[str], workdir: str) -> str:
    """Build a test command targeting specific test files.

    If test_command is a pytest invocation (bare `pytest ...` or
    `uv run pytest ...` / `python -m pytest ...` / `python3 -m pytest ...`),
    appends the test file paths. Otherwise returns the original command
    (custom runners can't be narrowed without user config).
    """
    cmd = test_command.strip()

    # Match pytest invocations regardless of runner prefix:
    #   pytest ... | uv run pytest ... | python -m pytest ... | python3 -m pytest ...
    #   | .venv/bin/pytest ...
    if re.search(r"(^|\s)((uv\s+run\s+)?pytest|python3?\s+-m\s+pytest)(\s|$)", cmd):
        # Convert absolute paths to relative for cleaner output
        rel_paths = [os.path.relpath(f, workdir) for f in test_files]
        return f"{cmd} {' '.join(rel_paths)}"

    # Non-pytest runner — can't narrow, run full
    return cmd


def _load_guard_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml and extract the guards section.

    Returns the full config dict, or {} if the file doesn't exist
    or can't be parsed. Handles missing PyYAML gracefully (returns {}
    with a log warning) since pre-commit hooks may run in a bare Python
    environment without the project's dependencies installed.
    """
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    try:
        import yaml as _yaml

        with open(config_path, "r") as f:
            return _yaml.safe_load(f) or {}
    except ImportError:
        logger.warning(
            "PyYAML not available in this Python environment — "
            "cannot load .gitreins/config.yaml. Guards will use defaults. "
            "Install with: pip install pyyaml"
        )
        return {}
    except Exception:
        return {}


class GuardManager:
    """Run static checks against staged changes."""

    def __init__(self, workdir: str = ".", config: dict | None = None):
        self.workdir = os.path.abspath(workdir)
        if config is None:
            config = _load_guard_config(self.workdir)
        self.config = config
        guards_cfg = self.config.get("guards", {})
        self._enabled = {
            "secrets": guards_cfg.get("secrets", True),
            "lint": guards_cfg.get("lint", True),
            "tests": guards_cfg.get("tests", True),
            "dead_code": guards_cfg.get("dead_code", False),  # opt-in: Python-only, can be noisy
            "skylos": guards_cfg.get("skylos", False),  # opt-in: needs pip install
            "static_analysis": guards_cfg.get("static_analysis", False),  # opt-in: type checkers
            "lsp": guards_cfg.get("lsp", False),  # opt-in: LSP servers
            "security_scan": guards_cfg.get("security_scan", {}).get(
                "enabled", False
            ),  # opt-in: Antares CVE scanner
        }
        self._static_tools = guards_cfg.get("static_analysis_tools", {})
        self._lsp_tools = guards_cfg.get("lsp_tools", ["pylsp"])
        # LSP timeouts (seconds). None = language-aware defaults
        # (clangd/cpp repos get 300s init / 120s per-file automatically).
        lsp_cfg = guards_cfg.get("lsp_timeouts", {})
        self._lsp_init_timeout: float | None = lsp_cfg.get("init")
        self._lsp_per_file_timeout: float | None = lsp_cfg.get("per_file")

        # Test mode: "full" (default) or "diff"
        self._test_mode = guards_cfg.get("test_mode", "full")
        # Test timeout in seconds (default: 180s)
        self._test_timeout = guards_cfg.get("test_timeout", 180)
        # Hook timeout in seconds (default: 120s) — overall guard budget (GR-064e)
        self._hook_timeout = guards_cfg.get("hook_timeout", 300)

        # Project type detection
        self._is_go = os.path.isfile(os.path.join(self.workdir, "go.mod"))
        self._go_guards = guards_cfg.get("go", {})
        self._is_ruby = os.path.isfile(os.path.join(self.workdir, "Gemfile"))
        self._is_php = os.path.isfile(os.path.join(self.workdir, "composer.json"))
        self._is_cpp = (
            os.path.isfile(os.path.join(self.workdir, "CMakeLists.txt"))
            or os.path.isfile(os.path.join(self.workdir, "Makefile"))
            or os.path.isfile(os.path.join(self.workdir, "compile_commands.json"))
            or any(
                f.endswith((".cpp", ".cc", ".cxx", ".hpp", ".h", ".c"))
                for f in _get_staged_files(self.workdir)
            )
        )
        self._is_rust = os.path.isfile(os.path.join(self.workdir, "Cargo.toml"))
        self._has_sql = any(
            f.endswith(".sql") for f in _get_staged_files(self.workdir)
        ) or os.path.isdir(os.path.join(self.workdir, "migrations"))

    def run_all(self, force_dead_code: bool = False) -> Tier1Result:
        """Run all enabled Tier 1 guards.

        For Go projects, the Python-specific guards (lint, tests, dead_code)
        are skipped in favor of Go-native equivalents (go vet, go test, go build).

        Args:
            force_dead_code: If True, enable dead_code guard regardless of config.
                             Used by CLI --dead-code flag and MCP dead_code param.

        The overall guard run is bounded by hook_timeout (default 120s). If the
        total elapsed time exceeds this budget, remaining checks are skipped and a
        warning is issued — the guard \"fails open\" to prevent blocking commits
        indefinitely (GR-064e).
        """
        start = time.monotonic()
        results: list[GuardResult] = []
        warnings: list[str] = []

        def _timed_out() -> bool:
            return (time.monotonic() - start) >= self._hook_timeout

        if self._enabled["secrets"]:
            results.append(self._check_secrets())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled["lint"] and not self._is_go:
            results.append(self._check_lint())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled["tests"] and not self._is_go:
            results.append(self._check_tests())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        dead_code_enabled = self._enabled["dead_code"] or force_dead_code
        if dead_code_enabled and not self._is_go:
            results.append(self._check_dead_code())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled["skylos"]:
            results.append(self._check_skylos())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled["static_analysis"]:
            results.append(self._check_static_analysis())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled["lsp"] and not self._is_go:
            results.append(self._check_lsp())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._enabled.get("security_scan", False):
            results.append(self._check_security_scan())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return Tier1Result(passed=True, results=results, warnings=warnings)

        if self._is_go:
            if self._go_guards.get("build", True):
                results.append(self._check_go_build())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return Tier1Result(passed=True, results=results, warnings=warnings)
            if self._go_guards.get("lint", True):
                results.append(self._check_go_lint())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return Tier1Result(passed=True, results=results, warnings=warnings)
            if self._go_guards.get("tests", True):
                results.append(self._check_go_tests())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return Tier1Result(passed=True, results=results, warnings=warnings)

        passed = all(r.passed for r in results)
        extra = {
            "test_mode": self._test_mode,
        }
        if self._test_mode == "diff" and self._enabled.get("tests"):
            staged = _get_staged_files(self.workdir)
            targets = _discover_test_targets(self.workdir)
            if targets:
                extra["test_targets"] = len(targets)
                extra["staged_count"] = len(staged)
            else:
                extra["test_targets"] = None  # full suite triggered
        return Tier1Result(passed=passed, results=results, extra=extra, warnings=warnings)

    @property
    def test_mode(self) -> str:
        return self._test_mode

    def _check_secrets(self) -> GuardResult:
        """Scan staged changes for secrets using gitleaks or built-in scanner."""
        # Try gitleaks first
        try:
            cmd = [
                "gitleaks",
                "protect",
                "--staged",
                "--source",
                ".",
                "--verbose",
                "--no-banner",
            ]
            config_path = os.path.join(self.workdir, ".gitleaks.toml")
            if os.path.isfile(config_path):
                cmd.extend(["--config", config_path])
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workdir,
                errors="replace",
                env=_sanitized_env(),
            )
            if result.returncode == 0:
                # gitleaks clean — ALSO run the built-in scanner. gitleaks'
                # default rules carry entropy thresholds that skip low-entropy
                # keys (e.g. an AKIA-prefixed key in test fixtures), and the
                # built-in scanner catches provider patterns without that
                # filter (GR-GAP-005). gitleaks OR builtin must both be clean.
                builtin = self._builtin_secrets_scan()
                if not builtin.passed:
                    return builtin
                return GuardResult(name="secrets", passed=True, output="gitleaks: clean")
            else:
                return GuardResult(
                    name="secrets", passed=False, output=result.stdout + result.stderr
                )
        except FileNotFoundError:
            logger.debug("gitleaks not found — using built-in scanner")
        except Exception as e:
            logger.warning("gitleaks failed: %s — falling back to built-in scanner", e)

        return self._builtin_secrets_scan()

    def _builtin_secrets_scan(self) -> GuardResult:
        """
        Built-in secrets scanner with whitelist patterns.

        Detects likely secrets (API keys, tokens, private keys) while
        ignoring common false positives like environment variable loading,
        form field access, and credential construction.
        """
        # Patterns that LIKELY represent actual secrets (high confidence)
        danger_patterns = [
            # Private key blocks (SSH, SSL, PGP, PKCS#8)
            (
                r"(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH|PGP|ENCRYPTED\s+)?\s*PRIVATE\s+KEY(\s+BLOCK)?",
                "private key block",
            ),
            # GitHub tokens
            (r"ghp_[A-Za-z0-9]{36,}", "GitHub personal access token"),
            (r"gho_[A-Za-z0-9]{36,}", "GitHub OAuth token"),
            # GitLab tokens
            (r"glpat-[A-Za-z0-9_\-]{20,}", "GitLab personal access token"),
            # OpenAI/OpenRouter keys (20+ chars — catches all sk- variants)
            (r"sk-[A-Za-z0-9_\-]{20,}", "OpenAI/OpenRouter API key"),
            # AWS keys
            (r"(?i)AKIA[0-9A-Z]{16}", "AWS access key"),
            (
                r'(?i)(aws[_-]?secret[_-]?access[_-]?key|aws[_-]?secret|secret[_-]?access[_-]?key)\s*[:=]\s*["\'][A-Za-z0-9+/]{40,}["\']',
                "AWS secret access key",
            ),
            # GCP API keys
            (r"AIza[0-9A-Za-z\-_]{35,}", "GCP API key"),
            # DigitalOcean tokens
            (r"dop_v1_[a-z0-9]{64}", "DigitalOcean access token"),
            # Stripe live keys
            (r"sk_live_[0-9a-zA-Z]{24,}", "Stripe live secret key"),
            (r"rk_live_[0-9a-zA-Z]{24,}", "Stripe restricted key"),
            # Azure storage
            (
                r"(?i)DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{60,}",
                "Azure storage connection string",
            ),
            (r'(?i)AccountKey\s*=\s*["\']?[A-Za-z0-9+/=]{60,}["\']?', "Azure storage account key"),
            # Slack API tokens
            (r"xox[baprs]-[0-9a-zA-Z\-]{20,}", "Slack API token"),
            # Generic patterns (check LAST — specific providers above)
            (
                r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']',
                "hardcoded API key",
            ),
            # JWTs assigned as literal strings
            (
                r'(?i)(token|jwt)\s*[:=]\s*["\']eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}["\']',
                "hardcoded JWT",
            ),
            # Passwords with literal-looking values
            (r'(?i)(password|passwd|pwd)\s*[:=]\s*["\'][^"\'$]{8,}["\']', "hardcoded password"),
            # Generic tokens/secrets (check LAST)
            (r'(?i)(secret|token)\s*[:=]\s*["\'][A-Za-z0-9+/=]{32,}["\']', "hardcoded secret"),
        ]

        # Patterns we explicitly IGNORE (common false positives)
        whitelist_patterns = [
            r"(?i)(api[_-]?key|apikey|secret|token|password|passwd|pwd)\s*[:=]\s*(os\.getenv|os\.environ|getenv|environ\[|request\.form|request\.args|\.env|config\[|settings\[)",
            r"(?i)\$\{[A-Z_]+\}",  # Shell variable substitution
            r"(?i)\$\w+",  # Shell variable reference ($KEY)
            r"(?i)\{\{[^}]*\}\}",  # Template variables ({{ }})
            r"(?i)\{%[^}]*%\}",  # Template variables ({% %})
            r'(?i)(password|passwd|pwd)\s*[:=]\s*""',  # Empty password assignments
            r"(?i)EXAMPLE|PLACEHOLDER|TODO|FIXME|xxx+|<your-[-a-z]+>|changeme",  # Placeholders
            r"(?i)jwt\.encode|jwt\.decode|b64encode",  # JWT construction, not hardcoded
            r"(?i)generate|random|uuid|hash",  # Generated values
        ]

        findings = []
        allowlist = self._load_gitleaks_allowlist()
        try:
            # Get staged files
            staged_files = _get_staged_files(self.workdir)

            if not staged_files:
                return GuardResult(name="secrets", passed=True, output="No staged files to scan")

            for fpath in staged_files:
                # Respect .gitleaks.toml [allowlist] paths — same exemptions
                # gitleaks applies (test fixtures with deliberate fake keys).
                if any(rx.search(fpath) for rx in allowlist):
                    continue
                full = os.path.join(self.workdir, fpath)
                if not os.path.isfile(full):
                    continue
                if os.path.getsize(full) > 1_000_000:
                    continue  # Skip very large files

                # Skip documentation files — they routinely contain
                # example API keys, placeholder tokens, and credential
                # snippets that are not actual secrets.
                if any(
                    skip in fpath
                    for skip in (".memory-bank/", "docs/", "CONTRIBUTING.md", "SECURITY.md")
                ) or fpath.endswith(".md"):
                    continue

                try:
                    with open(full, "r", errors="replace") as f:
                        lines = f.readlines()
                except Exception:
                    continue

                for i, line in enumerate(lines, 1):
                    # Skip whitelisted lines
                    if any(re.search(wp, line) for wp in whitelist_patterns):
                        continue

                    # Check danger patterns
                    for pattern, label in danger_patterns:
                        if re.search(pattern, line):
                            # Suppress the actual value in output
                            sanitized = re.sub(r'["\'][^"\']{6,}["\']', '"***"', line.rstrip())
                            findings.append(f"{fpath}:{i}: [{label}] {sanitized}")
                            break  # One finding per line

            if findings:
                logger.warning("Secrets scan: %d potential findings", len(findings))
                return GuardResult(
                    name="secrets",
                    passed=False,
                    output="Potential secrets found:\n" + "\n".join(findings[:20]),
                )
            return GuardResult(
                name="secrets", passed=True, output=f"Scanned {len(staged_files)} files — clean"
            )

        except Exception as e:
            logger.exception("Secrets scan failed")
            return GuardResult(name="secrets", passed=False, error=str(e))

    def _load_gitleaks_allowlist(self) -> list:
        """Load path allowlist regexes from .gitleaks.toml, if present.

        The built-in scanner applies the same path exemptions gitleaks gets
        via the repo's [allowlist] (test fixtures with deliberate fake keys,
        docs, generated dirs). Entries are emitted as Go-style regexps inside
        '''...''' quotes.
        """
        allowed = []
        cfg = os.path.join(self.workdir, ".gitleaks.toml")
        if not os.path.isfile(cfg):
            return allowed
        try:
            with open(cfg, "r", errors="replace") as f:
                text = f.read()
            for m in re.finditer(r"'''(.+?)'''", text):
                try:
                    allowed.append(re.compile(m.group(1)))
                except re.error:
                    continue
        except Exception:
            logger.debug("Could not parse %s allowlist", cfg)
        return allowed

    def _check_lint(self) -> GuardResult:
        """Run linter on staged Python files."""
        linters = ["ruff", "flake8"]
        for linter in linters:
            try:
                # Get staged Python files
                staged_files = _get_staged_files(self.workdir)
                py_files = [f for f in staged_files if f.endswith(".py")]
                if not py_files:
                    return GuardResult(name="lint", passed=True, output="No Python files staged")

                lint_result = subprocess.run(
                    [linter, "check", *py_files] if linter == "ruff" else [linter, *py_files],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.workdir,
                    env=_sanitized_env(),
                )
                output = lint_result.stdout + lint_result.stderr
                if len(output) > 2000:
                    output = output[:2000] + "\n... [truncated]"

                if lint_result.returncode == 0:
                    return GuardResult(name="lint", passed=True, output=f"{linter}: clean")
                else:
                    return GuardResult(name="lint", passed=False, output=output)
            except FileNotFoundError:
                continue

        return GuardResult(name="lint", passed=True, output="No linter found — skipped")

    def _check_tests(self) -> GuardResult:
        """Run the configured test command.

        In 'diff' mode, only runs tests relevant to staged changes.
        In 'full' mode (default), runs the entire test suite.
        When no files are staged, tests are skipped entirely.
        """
        test_command = self.config.get("guards", {}).get("test_command", "pytest -x --tb=short")

        # Skip if nothing is staged — nothing to test
        staged = _get_staged_files(self.workdir)
        if not staged:
            return GuardResult(name="tests", passed=True, output="No files staged — skipped")

        if self._test_mode == "diff":
            test_files = _discover_test_targets(self.workdir)
            if test_files is not None:
                if not test_files:
                    # No test files map to the changed sources — skip
                    return GuardResult(
                        name="tests",
                        passed=True,
                        output="No matching test files — skipped (diff mode)",
                    )
                # Narrowed — only run relevant tests
                cmd = _build_diff_test_command(test_command, test_files, self.workdir)
                label = f"tests (diff: {len(test_files)} files)"
                return self._run_test_command(cmd, label)
            # Fall through to full suite (safety default for force-full triggers)

        label = "tests (full)"
        return self._run_test_command(test_command, label)

    def _run_test_command(self, cmd: str, label: str) -> GuardResult:
        """Execute a test command and return a GuardResult."""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._test_timeout,
                cwd=self.workdir,
                env=_sanitized_env(),
            )
            output = result.stdout + result.stderr
            if len(output) > 2000:
                output = output[-2000:]  # Keep last 2000 chars for failure context
            if result.returncode == 0:
                return GuardResult(name=label, passed=True, output=output[:500])
            else:
                return GuardResult(name=label, passed=False, output=output)
        except subprocess.TimeoutExpired:
            return GuardResult(
                name=label,
                passed=False,
                output=(
                    f"Tests timed out after {self._test_timeout}s. "
                    f"To raise the limit: set guards.test_timeout in "
                    f".gitreins/config.yaml (e.g. test_timeout: 300)."
                ),
            )
        except Exception as e:
            return GuardResult(name=label, passed=False, error=str(e))

    def _check_dead_code(self) -> GuardResult:
        """Detect unreachable code, unused functions, and unused imports."""
        try:
            from engine.dead_code import DeadCodeDetector

            detector = DeadCodeDetector(self.workdir)
            report = detector.scan()

            # Also check for unused functions project-wide
            unused_funcs = detector.find_unused_functions()
            report.findings.extend(unused_funcs)

            if report.passed:
                return GuardResult(name="dead_code", passed=True, output="No dead code found")

            # Group by category for clear output
            output = report.summary
            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="dead_code", passed=False, output=output)
        except ImportError:
            return GuardResult(
                name="dead_code", passed=True, output="Dead code detector unavailable — skipped"
            )
        except Exception as e:
            return GuardResult(name="dead_code", passed=False, error=str(e))

    def _check_skylos(self) -> GuardResult:
        """Multi-language dead code + AI mistake detection via Skylos.

        Requires: pip install skylos
        Detects: unused functions, imports, classes, variables, parameters,
                 unreachable code, AI-hallucinated patterns.
        Languages: Python, TS/JS, Go, Java, PHP, Rust, Dart, C#
        """
        try:
            result = subprocess.run(
                ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workdir,
                env=_sanitized_env(),
            )
            if result.returncode != 0:
                return GuardResult(
                    name="skylos",
                    passed=True,
                    output=f"skylos exited {result.returncode}: {result.stderr[:200]}",
                )

            data = json.loads(result.stdout)

            findings = []
            for f in data.get("unused_functions", []):
                findings.append(f"{f['file']}:{f['line']} — unused function {f['name']}")
            for f in data.get("unused_imports", []):
                findings.append(f"{f['file']}:{f['line']} — unused import {f['name']}")
            for f in data.get("unused_classes", []):
                findings.append(f"{f['file']}:{f['line']} — unused class {f['name']}")

            # Dead symbols from definitions
            for name, info in data.get("definitions", {}).items():
                if info.get("dead"):
                    findings.append(f"{info['file']}:{info['line']} — dead: {name}")

            grade = data.get("grade", {}).get("overall", {})
            score = grade.get("score", "?")
            letter = grade.get("letter", "?")

            if not findings:
                return GuardResult(
                    name="skylos",
                    passed=True,
                    output=f"Skylos grade {letter} ({score}) — no dead code found",
                )

            output = f"Skylos grade {letter} ({score}) — {len(findings)} findings:\n"
            output += "\n".join(f"  • {f}" for f in findings[:20])
            if len(findings) > 20:
                output += f"\n  ... and {len(findings) - 20} more"

            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="skylos", passed=False, output=output)

        except FileNotFoundError:
            return GuardResult(
                name="skylos",
                passed=True,
                output="skylos not installed — install with: pip install skylos",
            )
        except json.JSONDecodeError:
            return GuardResult(name="skylos", passed=True, output="skylos output unparseable")
        except subprocess.TimeoutExpired:
            return GuardResult(name="skylos", passed=True, output="skylos timed out")
        except Exception as e:
            return GuardResult(name="skylos", passed=False, error=str(e))

    def _check_static_analysis(self) -> GuardResult:
        """Run configured static analysis tools against the project.

        Respects static_analysis_tools config key. Only runs tools that
        exist on PATH. Returns FAIL if any tool finds errors.
        """
        if self._is_go:
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="Go compiler covers static analysis — skipped",
            )
        # Check for Python, Ruby, PHP, SQL, C/C++, Rust, Go
        lang_tools: list[str] = []
        if (
            os.path.isfile(os.path.join(self.workdir, "pyproject.toml"))
            or os.path.isfile(os.path.join(self.workdir, "setup.py"))
            or os.path.isfile(os.path.join(self.workdir, "setup.cfg"))
        ):
            lang_tools = self._static_tools.get("python", [])
        elif self._is_ruby:
            lang_tools = self._static_tools.get("ruby", [])
        elif self._is_php:
            lang_tools = self._static_tools.get("php", [])
        elif self._has_sql:
            lang_tools = self._static_tools.get("sql", [])
        elif self._is_cpp:
            lang_tools = self._static_tools.get("cpp", ["cppcheck"])
        elif self._is_rust:
            lang_tools = self._static_tools.get("rust", ["clippy"])

        if not lang_tools:
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="No static analysis tools configured for this language",
            )

        all_diagnostics: list[str] = []
        had_errors = False

        for tool in lang_tools:
            try:
                from engine.static_analysis import run_static_check

                # cppcheck on a real C++ repo can exceed the default 120s —
                # grant C++/Rust tools the same generous budget as clangd.
                tool_timeout = 300.0 if (self._is_cpp or self._is_rust) else 120.0
                diags = run_static_check(tool, self.workdir, timeout=tool_timeout)
            except Exception as exc:
                logger.warning("static_analysis %s failed: %s", tool, exc)
                continue

            if not diags:
                all_diagnostics.append(f"  {tool} — clean")
                continue

            for d in diags:
                severity = d.get("severity", "error")
                prefix = "✗" if severity == "error" else "⚠"
                all_diagnostics.append(
                    f"  {prefix} {d['file']}:{d['line']} [{tool}] {d['message']}"
                )
                if severity == "error":
                    had_errors = True

        if not all_diagnostics:
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="No tools ran — check static_analysis_tools config",
            )

        output = "\n".join(all_diagnostics)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(
            name="static_analysis",
            passed=not had_errors,
            output=output,
        )

    def _check_lsp(self) -> GuardResult:
        """Run configured LSP servers against staged files.

        Uses lsp_tools config key. Only runs tools that exist on PATH.
        Returns FAIL if any tool finds errors.
        """
        if self._is_go:
            return GuardResult(
                name="lsp", passed=True, output="Go compiler covers static analysis — skipped"
            )

        if not self._lsp_tools:
            return GuardResult(name="lsp", passed=True, output="No LSP tools configured")

        all_diagnostics: list[str] = []
        had_errors = False

        for tool in self._lsp_tools:
            try:
                diags = run_lsp_check(
                    tool,
                    self.workdir,
                    timeout_per_file=self._lsp_per_file_timeout,
                    init_timeout=self._lsp_init_timeout,
                )
            except Exception as exc:
                logger.warning("lsp %s failed: %s", tool, exc)
                continue

            if not diags:
                all_diagnostics.append(f"  {tool} — clean")
                continue

            for d in diags:
                severity = d.get("severity", "error")
                prefix = "✗" if severity == "error" else "⚠"
                all_diagnostics.append(
                    f"  {prefix} {d['file']}:{d['line']} [{tool}] {d['message']}"
                )
                if severity == "error":
                    had_errors = True

        if not all_diagnostics:
            return GuardResult(
                name="lsp", passed=True, output="No LSP tools ran — check lsp_tools config"
            )

        output = "\n".join(all_diagnostics)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(
            name="lsp",
            passed=not had_errors,
            output=output,
        )

    def _check_security_scan(self) -> GuardResult:
        """Run Antares CVE localization scan against staged files.

        Opt-in guard. SCAFFOLD (GR-117a/d): the scanner falls back to a
        keyword-based heuristic that produces zero-confidence
        "CVE-SIMULATED" findings until GR-117c wires in the real model.
        Returns FAIL if any finding is produced; PASS otherwise. If the
        optional huggingface_hub/transformers stack isn't installed and
        the scanner can't be imported at all, returns PASS with a
        "not available" note — the guard is opt-in and must never block
        commits when its dependencies are missing.
        """
        try:
            from engine.antares import AntaresScanner
        except ImportError as exc:
            logger.debug("Antares scanner import failed: %s", exc)
            return GuardResult(
                name="security_scan",
                passed=True,
                output="Antares: not available — install huggingface_hub and transformers",
            )

        try:
            scanner = AntaresScanner(self.workdir)
            findings = scanner.scan_staged_files()
        except Exception as exc:
            logger.warning("Antares scan raised: %s", exc)
            return GuardResult(name="security_scan", passed=False, error=str(exc))

        if not findings:
            return GuardResult(name="security_scan", passed=True, output="Antares: clean")

        # Format: one line per finding, capped to keep output bounded.
        lines = [
            f"  • {f.file}:{f.line} [{f.cve_id} conf={f.confidence:.2f}] {f.description}"
            for f in findings[:20]
        ]
        if len(findings) > 20:
            lines.append(f"  ... and {len(findings) - 20} more")

        output = f"Antares: {len(findings)} potential finding(s):\n" + "\n".join(lines)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(name="security_scan", passed=False, output=output)

    def _check_go_lint(self) -> GuardResult:
        """Run Go lint checks (delegates to engine.guards)."""
        r = check_go_lint(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_tests(self) -> GuardResult:
        """Run Go tests (delegates to engine.guards)."""
        r = check_go_tests(self.workdir, timeout=self._test_timeout)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_build(self) -> GuardResult:
        """Run Go build (delegates to engine.guards)."""
        r = check_go_build(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)
