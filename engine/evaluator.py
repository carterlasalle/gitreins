"""
Agentic Evaluator — Iterative LLM loop that judges code completeness.

The evaluator receives task criteria and iterates: read files, run tests,
search patterns — until it has enough evidence to deliver a verdict.

7 tools available to the LLM:
  1. read_file(path)         — Read any file in the working tree
  2. run_command(cmd)        — Run a shell command (tests, lint, build)
  3. search_pattern(regex)   — Grep the codebase for a pattern
  4. read_diff()             — Show staged changes
  5. get_task_item(id)       — Read a task's criteria
  6. sandbox_write(key, content) / sandbox_read(key) — Scratch space

Usage:
    evaluator = AgenticEvaluator(llm_client, workdir="/path/to/repo")
    verdict = evaluator.evaluate(task)
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from engine.llm import LLMClient, ToolCall
from engine.eval_cap import EvalCap, parse_eval_cap, eval_cap_from_config, _fmt_tokens

logger = logging.getLogger("gitreins.evaluator")

# Non-code/data paths excluded from judge code context (JUDGE-CONTEXT-001).
# Tracked board/data files (e.g. .coding-hermes/board/events.jsonl with
# multi-KB single-line JSON records) were inflating the tier2 judge prompt to
# ~140K tokens per call. Entries ending in "/" match directory prefixes
# (any path position); other entries match filename suffixes.
DATA_FILE_PATTERNS: tuple[str, ...] = (
    ".coding-hermes/",
    ".jsonl",
    ".json",
    ".parquet",
    ".db",
    ".sqlite",
    ".bak",
    ".log",
    "bin/",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".git/",
)

# Files whose average line length exceeds this are treated as data files and
# get only a short preview in FULL mode (single-line JSON records etc.).
DATA_FILE_AVG_LINE_CHARS = 500

# Number of preview lines included for data-smelling files in FULL mode.
DATA_FILE_PREVIEW_LINES = 5


def _is_data_file_path(path: str) -> bool:
    """Return True when a repo-relative path is a non-code/data file.

    Matches against DATA_FILE_PATTERNS: directory prefixes (trailing "/")
    match at any path position; other patterns match the filename suffix.
    """
    p = path.replace("\\", "/").lower()
    for pat in DATA_FILE_PATTERNS:
        if pat.endswith("/"):
            if p.startswith(pat) or f"/{pat}" in p:
                return True
        elif p.endswith(pat):
            return True
    return False


def _filter_diff_text(diff_text: str) -> str:
    """Drop per-file sections of a unified diff that belong to data files."""
    if not diff_text:
        return diff_text
    kept: list[str] = []
    current: list[str] = []
    keep_current = True
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current and keep_current:
                kept.append("\n".join(current))
            current = [line]
            # Format: "diff --git a/<path> b/<path>"
            parts = line.split(" b/", 1)
            bpath = parts[1] if len(parts) == 2 else line
            keep_current = not _is_data_file_path(bpath)
        else:
            current.append(line)
    if current and keep_current:
        kept.append("\n".join(current))
    return "\n".join(kept)

EVALUATOR_SYSTEM_PROMPT = """You are a code quality evaluator. Your job is to judge whether a completed task meets ALL of its defined criteria.

## YOUR TOOLS

You have tools to read files, run commands, and search the codebase. Use them aggressively — don't guess. Read the actual code, run the actual tests.

If available, use read_static_analysis to check for type errors and static analysis warnings, and read_lsp_diagnostics to check LSP (Language Server Protocol) diagnostics before judging criteria — type violations and LSP errors are hard evidence for FAIL.

## CRITERIA TRACKING (REQUIRED)

After you verify EACH criterion, IMMEDIATELY save your finding to the sandbox:

  sandbox_write("verified_0", "PASS: tests/test_auth.py:45 confirms token validation")
  sandbox_write("verified_2", "FAIL: missing error handling in engine/handler.py:120")

Use the criterion's index number (0-based, from the criteria list). This is NOT optional — it is how the evaluation survives context compaction. If the context window fills up, your progress is saved and you resume with a clean slate.

## COMPACTION AWARENESS

If you receive a prompt with "EVALUATION PROGRESS" at the top, you are resuming after compaction. The criteria marked ✓ are already verified — do NOT re-check them. Only the remaining criteria need your attention.

For each criterion:
1. Read the relevant files
2. Run any related tests
3. Verify the code actually does what the criterion demands
4. Record your finding with specific evidence (file paths, line numbers, test output)

## EFFICIENCY RULES

- **Token budget: {token_budget}.** You have {token_budget} input tokens and {output_budget} output tokens. The code context pre-loaded below consumes part of this — be selective about what you re-read. Prioritize criteria-driven investigation over exhaustive file reading.
- **Code context is pre-loaded.** The changed code is already included in this prompt — you do NOT need to call read_file() on files shown below unless you need a specific line range not included.
- **File scope: {file_scope}.** When scope is 'changed', you can ONLY access modified files (plus their tests and config files). read_file() and search_pattern() will reject files outside this set. This prevents context explosion on large repos.
- **Fast-track mode: {fast_track_mode}.** {fast_track_instruction}
- **Do not re-read the same file twice.** If read_file returned content, you already have it.
- **Do not re-run the same command.** Check previous results before running again.
- **Do not search for the same pattern twice.** Use sandbox to track what you've checked.

## MANDATORY TEST VERIFICATION (HARD RULE — 2026-08-08)

You MUST NOT report PASS on any criterion that mentions tests, build, lint,
type-checking, or "the code works/runs" unless you have ACTUAL command output
proving it. Reasoning from the code alone is NOT sufficient — the suite can be
red while the code looks fine (cached test results and pre-loaded context are
not evidence of a passing run).

**Step 1 — find the test command.** Read `.gitreins/config.yaml` for
`guards.test_command` (e.g. `go test -count=1 ./...`, `uv run pytest`). If it
is absent, run a sensible default for the project's language
(`go test ./...`, `pytest`, `npm test`).

**Step 2 — run it, fresh.** Execute the exact test command via `run_command`.
Use a cache-defeating flag where the toolchain supports one (`go test
-count=1 ./...`) so you do not receive a cached pass. Capture exit_code and the
tail of the output.

**Step 3 — record the evidence.** In every criterion's `detail`, if it
concerns tests/build/lint, you MUST quote the actual exit_code and the
decisive output line (e.g. `PASS`, `ok <pkg>`, `FAIL`, an error). A detail of
just "tests pass" with no command output is a FAIL.

**Exceptions:** If the project has no test suite at all (e.g. `test_command`
is `true`, or the repo is docs-only), record that explicitly: "no test suite —
verified <command or none>". You may then judge non-test criteria by code
inspection.

A criterion whose verification should have run tests but whose detail shows no
command output MUST be marked FAIL, not PASS.

## VERDICT FORMAT

When you've checked EVERY criterion, deliver your verdict. You MUST output EXACTLY this JSON format with NO surrounding text, NO markdown fences, NO commentary:

{"verdict":"COMPLETE","items":[{"criterion":"<exact criterion text>","status":"PASS","detail":"<specific file:line evidence>"},{"criterion":"<exact criterion text>","status":"FAIL","detail":"<what is missing and where>"}],"summary":"<one sentence>"}

CRITICAL RULES:
- Output ONLY the JSON object. Nothing before. Nothing after.
- "verdict" must be "COMPLETE" or "INCOMPLETE" — nothing else
- "status" must be "PASS" or "FAIL" — nothing else
- EVERY criterion from the task must have a corresponding item
- PASS requires concrete evidence (file path, line number, or test output)
- FAIL requires explaining exactly what's missing
- Read actual code — never assume"""

EVALUATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the working tree. Supports line-based (offset/limit) and byte-based (byte_offset/byte_limit) partial reads. Set mode='bytes' for byte-level access. First call without offset/limit returns metadata plus first 400 lines for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root."},
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start from (1-indexed). Omit to read from beginning.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return. Omit for full file.",
                    },
                    "byte_offset": {
                        "type": "integer",
                        "description": "Byte position to start from (0-indexed). Requires mode='bytes'.",
                    },
                    "byte_limit": {
                        "type": "integer",
                        "description": "Max bytes to return. Requires mode='bytes'.",
                    },
                    "mode": {
                        "type": "string",
                        "description": "'lines' (default) or 'bytes' for byte-level reads.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Returns exit code, stdout, and stderr. Use for tests, lint, build. Do NOT re-run the same command.",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string", "description": "Shell command to run."}},
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_pattern",
            "description": "Search the codebase for a regex pattern. Returns matching files and lines. Do NOT repeat the same regex search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "regex": {"type": "string", "description": "Python regex pattern."},
                    "file_glob": {
                        "type": "string",
                        "description": "Optional: glob to filter files (e.g., '*.py').",
                    },
                },
                "required": ["regex"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_static_analysis",
            "description": "Get static analysis diagnostics (type errors, warnings) for the project. Returns structured diagnostics from tools like mypy and pyright. Use this to check for type safety before judging criteria.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional: specific file or directory path. Omit to get diagnostics for all files.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_lsp_diagnostics",
            "description": "Get LSP (Language Server Protocol) diagnostics collected during Tier 1 guard run. Returns structured diagnostics from LSP tools like pylsp with file, line, severity, and message for each finding. LSP diagnostics include undefined variables, syntax errors, type mismatches, and import errors — hard evidence for FAIL on criteria.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_diff",
            "description": "Show git diff of staged and unstaged changes. Summarizes what code was changed.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_item",
            "description": "Get the full definition of a task including ALL criteria. Always call this first to know what to check.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Task ID to fetch."}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_write",
            "description": "Write to the evaluator's scratch space. Use to track which criteria you've checked, files you've read, and commands you've run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key to store under (e.g., 'checked', 'evidence').",
                    },
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_read",
            "description": "Read from the evaluator's scratch space to check what you've already verified.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key to read."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_dead_code",
            "description": "Detect dead code: unreachable code, unused functions, empty functions, and unused imports. Returns per-file findings with line numbers.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skylos_scan",
            "description": "Multi-language dead code and AI-mistake scan via Skylos. Detects unused functions, imports, classes, variables, unreachable code, and AI-hallucinated patterns across Python, TS/JS, Go, Java, PHP, Rust, Dart, C#. Returns grade and per-file findings.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_security",
            "description": "Run deterministic, syntax-aware security-pattern scanning with ast-grep against the CodeRabbit essential rules (184 rules across 14 languages: c, cpp, csharp, go, html, java, javascript, kotlin, php, python, ruby, rust, scala, swift, typescript). Catches real vulnerabilities (hardcoded secrets, insecure crypto, JWT 'none' algorithm, SQL injection, XSS, unsafe deserialization, debug-enabled, world-writable files, etc.) WITHOUT relying on the LLM to spot them. Returns findings as SARIF (file, line, message, CWE/OWASP refs). Use this before judging any security/correctness criterion — it finds bugs a code-reading model can miss. Optional 'path' arg to scan a specific file/dir; omit to scan the whole repo (may be slow on large repos).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional: file or directory to scan. Omit to scan the whole repo."},
                },
            },
        },
    },
]


@dataclass
class VerdictItem:
    criterion: str
    status: str  # "PASS" | "FAIL"
    detail: str


@dataclass
class Verdict:
    verdict: str  # "COMPLETE" | "INCOMPLETE"
    items: list[VerdictItem] = field(default_factory=list)
    summary: str = ""


class AgenticEvaluator:
    """The evaluator loop: LLM iterates with tools until it delivers a verdict.

    Caps can be set via:
      - eval_cap string: "100", "30m", "200k/50k", "100/30m/200k/50k", "-1" (unlimited)
      - eval_cap EvalCap object: pre-parsed cap
      - max_iterations int: simple iteration cap (backward compat)
      - .gitreins/config.yaml: evaluator.cap key (auto-loaded if no cap specified)

    Priority: eval_cap param > max_iterations param > config.yaml > default (100 iter)
    """

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        max_iterations: int | None = None,
        eval_cap: str | EvalCap | None = None,
        command_timeout: int = 30,
    ):
        self.llm = llm
        self.workdir = os.path.abspath(workdir)

        # Resolve eval cap — explicit param wins, then max_iterations, then config
        if isinstance(eval_cap, EvalCap):
            self.eval_cap = eval_cap
        elif isinstance(eval_cap, str):
            self.eval_cap = parse_eval_cap(eval_cap)
        elif max_iterations is not None and max_iterations > 0:
            # Explicit positive value — use it directly
            self.eval_cap = EvalCap(
                max_iterations=max_iterations, source=f"max_iterations={max_iterations}"
            )
        else:
            # max_iterations=None or max_iterations<=0 — read from config.yaml
            config = self._load_config()
            self.eval_cap = eval_cap_from_config(config)

        # max_iterations from EvalCap is authoritative — 100 by default, -1 for unlimited
        self.max_iterations = (
            self.eval_cap.max_iterations if self.eval_cap.max_iterations > 0 else 10_000
        )

        # Command timeout for tool calls — configurable for tests
        self.command_timeout = command_timeout

        self._sandbox: dict[str, str] = {}
        self._task_index: dict[str, dict] = {}
        self._files_read: set[str] = set()
        self._commands_run: set[str] = set()
        self._searches_done: set[str] = set()
        self._tier1_diagnostics: list[dict] = []
        self._allowed_files: set[str] | None = None  # None = full scope, set = restricted

        # ── Fast-track mode (GR-064a) ──
        # Resolve fast_track from config. "auto" = detect based on package count.
        self.fast_track: bool = self._resolve_fast_track()

        # ── Max file bytes (GR-064d) ──
        # Cap read_file results to prevent a single large file from eating
        # the entire evaluator context window.
        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})
        self.max_file_bytes: int = int(evaluator_cfg.get("max_file_bytes", 131_072))

    def _resolve_fast_track(self) -> bool:
        """Resolve fast_track setting: 'on'→True, 'off'→False, 'auto'→detect.

        Auto-detection: counts source packages (*.py files in non-test dirs).
        Returns True (fast_track ON) if >= 20 packages are found.
        """
        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})
        setting = str(evaluator_cfg.get("fast_track", "auto")).lower()

        if setting in ("on", "true"):
            return True
        if setting in ("off", "false"):
            return False

        # "auto" — detect based on package count
        try:
            package_count = 0
            seen_dirs: set[str] = set()
            for root, dirs, files in os.walk(self.workdir):
                # Skip hidden, test, venv, and build dirs
                dirs[:] = [
                    d
                    for d in dirs
                    if not d.startswith(".")
                    and d
                    not in (
                        "venv",
                        "__pycache__",
                        "node_modules",
                        "build",
                        "dist",
                        "target",
                        ".git",
                    )
                ]
                has_py = any(
                    f.endswith(".py") and not f.startswith("test_") and f != "__init__.py"
                    for f in files
                )
                if has_py and root != self.workdir:
                    rel = os.path.relpath(root, self.workdir)
                    # Count top-level package dirs only
                    top = rel.split(os.sep)[0]
                    if top not in seen_dirs:
                        seen_dirs.add(top)
                        package_count += 1
            logger.debug(
                "Fast-track auto-detect: %d packages → %s",
                package_count,
                "ON" if package_count >= 20 else "OFF",
            )
            return package_count >= 20
        except Exception:
            return False

    def _build_code_context(self, config: dict) -> str:
        """Build a compact code-context block to pre-load into the evaluator prompt.

        Reads test_mode from config:
        - 'diff' (default): include git diff hunks (compact, scalable)
        - 'full': include complete changed files (up to 200 lines each)

        This prevents the LLM from burning iterations/tokens on read_file()
        calls just to get basic context — it already has the code it needs.

        Respects .gitleaks.toml exclusions from the project.
        """
        import subprocess
        import os

        test_mode = config.get("guards", {}).get("test_mode") or config.get("test_mode") or "full"

        # Find changed files: staged + unstaged (data files excluded — they
        # carry no reviewable code and blow up the prompt, JUDGE-CONTEXT-001)
        changed_files: set[str] = set()
        try:
            # Staged changes
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            for line in staged.stdout.strip().splitlines():
                if line and not _is_data_file_path(line):
                    changed_files.add(line)
            # Unstaged changes
            unstaged = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            for line in unstaged.stdout.strip().splitlines():
                if line and not _is_data_file_path(line):
                    changed_files.add(line)
        except Exception:
            pass

        # Unified diff for changed files (always useful). Data-file hunks are
        # dropped — a board JSONL hunk can be hundreds of KB on its own.
        try:
            diff_result = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            diff_text = _filter_diff_text(diff_result.stdout.strip())
        except Exception:
            diff_text = ""

        if not changed_files and not diff_text:
            return ""

        lines: list[str] = []

        if test_mode == "diff":
            # Diff mode: send only the hunks
            lines.append("## CHANGED CODE (DIFF)")
            lines.append("")
            if diff_text:
                # Truncate very large diffs
                diff_lines = diff_text.splitlines()
                if len(diff_lines) > 500:
                    diff_lines = diff_lines[:500]
                    diff_lines.append(
                        f"... [truncated at 500 lines, {len(diff_text.splitlines())} total]"
                    )
                lines.append("\n".join(diff_lines))
            else:
                lines.append("(no changes detected)")
            lines.append("")

        else:
            # Full mode: include complete changed files (capped)
            lines.append("## CHANGED FILES (FULL)")
            lines.append("")
            max_files = 20
            max_lines_per_file = 200

            shown = 0
            for fname in sorted(changed_files):
                if shown >= max_files:
                    lines.append(f"... [{len(changed_files) - max_files} more files truncated]")
                    break
                if _is_data_file_path(fname):
                    continue
                fpath = os.path.join(self.workdir, fname)
                if not os.path.isfile(fpath):
                    continue
                # Skip binary and huge files
                try:
                    fsize = os.path.getsize(fpath)
                    if fsize > 500_000:
                        lines.append(f"### {fname} [binary/large — skipped]")
                        shown += 1
                        continue
                except OSError:
                    continue

                try:
                    with open(fpath, "r", errors="replace") as f:
                        content = f.read()
                except Exception:
                    continue

                file_lines = content.splitlines()
                # Data-file smell: extremely long average lines (single-line
                # JSON records, minified blobs) — preview only, never 200 lines.
                avg_line_len = len(content) / len(file_lines) if file_lines else 0
                if avg_line_len > DATA_FILE_AVG_LINE_CHARS:
                    preview = file_lines[:DATA_FILE_PREVIEW_LINES]
                    lines.append(
                        f"### {fname} [data-like — {len(file_lines)} lines, "
                        f"avg {int(avg_line_len)} chars/line — {DATA_FILE_PREVIEW_LINES}-line preview]"
                    )
                    lines.append("```")
                    lines.extend(preview)
                    lines.append("```")
                    lines.append("")
                    shown += 1
                    continue
                if len(file_lines) > max_lines_per_file:
                    file_lines = file_lines[:max_lines_per_file]
                    file_lines.append(
                        f"... [truncated at {max_lines_per_file} lines, {len(content.splitlines())} total]"
                    )

                lines.append(f"### {fname}")
                lines.append("```")
                lines.extend(file_lines)
                lines.append("```")
                lines.append("")
                shown += 1

            # Also include the diff stat for awareness
            if diff_text:
                diff_lines = diff_text.splitlines()
                if len(diff_lines) > 50:
                    diff_lines = diff_lines[:50]
                lines.append("## Diff summary (first 50 lines)")
                lines.append("```diff")
                lines.extend(diff_lines)
                lines.append("```")

        return "\n".join(lines)

    def _compute_allowed_files(self) -> set[str]:
        """Compute the set of files the evaluator is allowed to touch.

        When file_scope is 'changed', returns changed files from git diff
        plus their corresponding test files. Returns empty set on git failures.

        The LLM can only read_file / search_pattern within this set, preventing
        context explosion on large monorepos.
        """
        import subprocess

        allowed: set[str] = set()

        try:
            for args in (
                ["git", "diff", "--cached", "--name-only"],
                ["git", "diff", "--name-only"],
            ):
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.workdir,
                )
                for line in result.stdout.strip().splitlines():
                    if line:
                        allowed.add(line)
        except Exception:
            pass

        # Also include test files that map to changed source files
        test_substitutions = [
            ("src/", "tests/"),
            ("lib/", "test/"),
            ("pkg/", "test/"),
            (".py", "_test.py"),
            (".go", "_test.go"),
            (".rs", "_test.rs"),
            (".ts", ".test.ts"),
            (".tsx", ".test.tsx"),
            (".js", ".test.js"),
            (".rb", "_test.rb"),
        ]
        expanded: set[str] = set(allowed)
        for f in allowed:
            for src_suffix, test_suffix in test_substitutions:
                if src_suffix in f:
                    test_file = f.replace(src_suffix, test_suffix)
                    tpath = os.path.join(self.workdir, test_file)
                    if os.path.isfile(tpath):
                        expanded.add(test_file)
                    break

        # Always allow common config files
        always_allow = [
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "conftest.py",
            "go.mod",
            "go.sum",
            "Cargo.toml",
            "package.json",
            "Makefile",
            "Dockerfile",
            ".gitreins/config.yaml",
        ]
        for af in always_allow:
            if os.path.isfile(os.path.join(self.workdir, af)):
                expanded.add(af)

        logger.debug(
            "File scope 'changed': %d files allowed (%d changed + tests/config)",
            len(expanded),
            len(allowed),
        )
        return expanded

    def _path_in_scope(self, path: str) -> bool:
        """Check if a file path is within the current scope."""
        if self._allowed_files is None:
            return True
        clean = path.lstrip("./").rstrip("/")
        return clean in self._allowed_files

    def _load_config(self) -> dict:
        """Load .gitreins/config.yaml if present."""
        import yaml

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def _estimate_context_tokens(self, messages: list[dict], cumulative_prompt_tok: int = 0) -> int:
        """Estimate total context tokens in the message array.

        Uses cumulative LLM-reported prompt tokens as primary source.
        Falls back to character-based heuristic (chars / 3.5 ≈ tokens)
        when LLM token data is unavailable.
        """
        if cumulative_prompt_tok > 0:
            # LLM-reported tokens are most accurate
            return cumulative_prompt_tok

        # Rough heuristic: ~3.5 chars per token for English/code
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        return int(total_chars / 3.5)

    def _build_compacted_prompt(
        self,
        task: dict,
        code_context: str,
        criteria_total: int,
    ) -> str:
        """Build a compacted resume prompt from sandbox state.

        Extracts verified criteria and evidence from sandbox keys,
        then builds a fresh prompt with progress summary + remaining work.
        The LLM continues from this clean context.
        """
        criteria_list = task.get("criteria", [])
        verified: dict[str, str] = {}  # criterion → evidence
        remaining_indices: list[int] = []

        # Extract verified criteria from sandbox
        for i, criterion in enumerate(criteria_list):
            key = f"verified_{i}"
            if key in self._sandbox:
                verified[criterion] = self._sandbox[key]
            else:
                remaining_indices.append(i)

        # Build progress summary
        lines: list[str] = []
        lines.append("## EVALUATION PROGRESS (compacted — fresh context window)")
        lines.append("")

        if verified:
            lines.append(f"### Already verified ({len(verified)}/{criteria_total}):")
            for criterion, evidence in verified.items():
                lines.append(f"- ✓ {criterion}")
                if evidence and evidence != "PASS":
                    lines.append(f"  Evidence: {evidence}")
            lines.append("")

        if remaining_indices:
            lines.append(f"### Still to verify ({len(remaining_indices)}/{criteria_total}):")
            for i in remaining_indices:
                lines.append(f"  {i + 1}. {criteria_list[i]}")
            lines.append("")

        lines.append("## INSTRUCTIONS (continued evaluation)")
        lines.append("")
        lines.append("You are resuming an evaluation with a clean context window. The criteria")
        lines.append("marked ✓ above are already verified — do NOT re-check them. Focus ONLY on")
        lines.append("the remaining criteria listed above.")
        lines.append("")
        lines.append("Use sandbox_write to track each criterion as you verify it:")
        lines.append('  sandbox_write("verified_N", "PASS: evidence from file:line")')
        lines.append("")
        lines.append("When ALL remaining criteria are verified, output the JSON verdict for ALL")
        lines.append(
            f"criteria (both the {len(verified)} already verified and the {len(remaining_indices)} you just checked)."
        )
        lines.append("Output ONLY the JSON verdict — no markdown fences, no extra text.")

        prompt = "\n".join(lines)

        # Append code context for reference
        if code_context:
            prompt += f"\n\n{code_context}"

        # Append the full task criteria list for the JSON verdict
        criteria_text = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria_list))
        prompt += f"\n\n## FULL CRITERIA LIST (for verdict JSON)\n{criteria_text}"

        return prompt

    def _compact_context(
        self,
        messages: list[dict],
        task: dict,
        code_context: str,
        criteria_total: int,
        compaction_count: int,
    ) -> tuple[list[dict], int]:
        """Compact the conversation: save progress, rebuild clean context.

        Returns (new_messages, new_compaction_count).
        The sandbox holds verified criteria — the LLM resumes from a clean
        conversation with only the progress summary + remaining work.
        """
        logger.info(
            "Compacting evaluator context (compaction #%d, %d messages → clean slate)",
            compaction_count + 1,
            len(messages),
        )

        compacted_prompt = self._build_compacted_prompt(
            task,
            code_context,
            criteria_total,
        )

        # Fresh conversation: system prompt + compacted task
        new_messages: list[dict] = [
            {
                "role": "system",
                "content": EVALUATOR_SYSTEM_PROMPT.replace(
                    "{token_budget}",
                    _fmt_tokens(self.eval_cap.max_input_tokens)
                    if self.eval_cap.max_input_tokens > 0
                    else "unlimited",
                )
                .replace(
                    "{output_budget}",
                    _fmt_tokens(self.eval_cap.max_output_tokens)
                    if self.eval_cap.max_output_tokens > 0
                    else "unlimited",
                )
                .replace("{file_scope}", self._allowed_files is not None and "changed" or "full")
                .replace("{fast_track_mode}", "ON" if self.fast_track else "OFF")
                .replace(
                    "{fast_track_instruction}",
                    "ON — Verify ONLY changed lines and immediate callers. Skip deep call-graph analysis. "
                    "Trust the diff: if a criterion references code outside changed files, check only the "
                    "interface boundary (function signature, type) — do NOT trace into unchanged code."
                    if self.fast_track
                    else "OFF — Normal mode. Read surrounding code as needed to verify criteria.",
                ),
            },
            {"role": "user", "content": compacted_prompt},
        ]

        return new_messages, compaction_count + 1

    def evaluate(self, task: dict) -> Verdict:
        """Run the agentic loop against a task and return a verdict.

        Args:
            task: dict with 'id', 'title', 'criteria' (list of strings)

        Returns:
            Verdict with pass/fail for each criterion.
        """
        # Reset state
        self._sandbox.clear()
        self._files_read.clear()
        self._commands_run.clear()
        self._searches_done.clear()

        # Build the task prompt
        criteria_list = task.get("criteria", [])
        criteria_text = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria_list))
        task_prompt = f"""Evaluate this completed task:

TASK: {task.get("title", task.get("id", "unnamed"))}
ID: {task.get("id", "unknown")}

CRITERIA TO VERIFY (all {len(criteria_list)} must be checked):
{criteria_text}

Start by calling get_task_item("{task.get("id", "unknown")}") to see the full task definition.
Then read the relevant code, run tests, and search for patterns.
Use sandbox_write to track which criteria you have verified.
Output ONLY the JSON verdict when done — no markdown fences, no extra text."""

        self._task_index[task.get("id", "unknown")] = task

        # Store and inject LSP diagnostics from Tier 1 if present
        tier1_diags = task.get("tier1_diagnostics", [])
        self._tier1_diagnostics = tier1_diags
        if tier1_diags:
            diag_lines = ["## TIER 1 LSP DIAGNOSTICS"]
            for d in tier1_diags:
                file = d.get("file", "")
                line = d.get("line", 0)
                severity = d.get("severity", "warning")
                message = d.get("message", "")
                tool = d.get("tool", "")
                diag_lines.append(f"  {file}:{line}:{severity}:{message} [{tool}]")
            task_prompt += "\n\n" + "\n".join(diag_lines)

        # Build tools list based on config
        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})

        # Compute file scope — which files the evaluator is allowed to touch
        file_scope = evaluator_cfg.get("file_scope", "changed")
        self._allowed_files = (
            self._compute_allowed_files() if file_scope == "changed" else None
        )  # None = full scope

        # Inject code context (changed files or diffs) into the initial prompt
        # so the LLM has relevant code upfront — reduces read_file() calls
        # and prevents context-bloat from accumulating tool results.
        code_context = self._build_code_context(config)
        if code_context:
            # Cap code context to configured budget (default 70% of input budget)
            ctx_budget = evaluator_cfg.get("code_context_budget", 0.70)
            max_ctx_tokens = (
                int(self.eval_cap.max_input_tokens * ctx_budget)
                if self.eval_cap.max_input_tokens > 0
                else None
            )
            if max_ctx_tokens:
                ctx_est = len(code_context) // 3  # rough char→token: ~3 chars/token
                if ctx_est > max_ctx_tokens:
                    ctx_lines = code_context.splitlines()
                    keep_lines = int(len(ctx_lines) * (max_ctx_tokens / ctx_est))
                    code_context = "\n".join(ctx_lines[:keep_lines])
                    code_context += f"\n\n... [code context truncated: {ctx_est} → {max_ctx_tokens} estimated tokens]"
            task_prompt += f"\n\n{code_context}"

        messages: list[dict] = [
            {
                "role": "system",
                "content": EVALUATOR_SYSTEM_PROMPT.replace(
                    "{token_budget}",
                    _fmt_tokens(self.eval_cap.max_input_tokens)
                    if self.eval_cap.max_input_tokens > 0
                    else "unlimited",
                )
                .replace(
                    "{output_budget}",
                    _fmt_tokens(self.eval_cap.max_output_tokens)
                    if self.eval_cap.max_output_tokens > 0
                    else "unlimited",
                )
                .replace("{file_scope}", self._allowed_files is not None and "changed" or "full")
                .replace("{fast_track_mode}", "ON" if self.fast_track else "OFF")
                .replace(
                    "{fast_track_instruction}",
                    "ON — Verify ONLY changed lines and immediate callers. Skip deep call-graph analysis. "
                    "Trust the diff: if a criterion references code outside changed files, check only the "
                    "interface boundary (function signature, type) — do NOT trace into unchanged code."
                    if self.fast_track
                    else "OFF — Normal mode. Read surrounding code as needed to verify criteria.",
                ),
            },
            {"role": "user", "content": task_prompt},
        ]

        # Per-request token cap — read from config (default 16384)
        max_tokens_per_call = int(evaluator_cfg.get("max_tokens_per_call", 16384))

        tools = list(EVALUATOR_TOOLS)  # copy
        if not evaluator_cfg.get("static_analysis_diagnostics", False):
            tools = [t for t in tools if t["function"]["name"] != "read_static_analysis"]  # type: ignore[index]

        # Start tracking
        self.eval_cap.start()

        # Compaction state
        MAX_COMPACTIONS = 3
        compaction_count = 0
        cumulative_prompt_tok = 0
        criteria_total = len(criteria_list)

        # Use while loop so we can reset iteration counter after compaction
        iteration = 0
        iter_limit = self.eval_cap.max_iterations_int

        while iteration < iter_limit:
            # Check caps (handles time/token limits even with unlimited iterations)
            cap_error = self.eval_cap.check()
            if cap_error:
                logger.warning("Eval cap exceeded: %s", cap_error)
                partial = self._extract_partial_verdict(criteria_list)
                if partial is not None:
                    return partial
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Cap exceeded: {cap_error}",
                )

            # Proactive compaction: compact when context exceeds configured threshold
            # (default 90% of input budget — 10% remaining)
            if cumulative_prompt_tok > 0 and compaction_count < MAX_COMPACTIONS:
                threshold_ratio = evaluator_cfg.get("compaction_threshold", 0.90)
                threshold = int(self.eval_cap.max_input_tokens * threshold_ratio)
                if cumulative_prompt_tok > threshold:
                    logger.warning(
                        "Context near limit (%d/%d tokens) — compacting (compaction #%d)",
                        cumulative_prompt_tok,
                        self.eval_cap.max_input_tokens,
                        compaction_count + 1,
                    )
                    messages, compaction_count = self._compact_context(
                        messages,
                        task,
                        code_context,
                        criteria_total,
                        compaction_count,
                    )
                    iteration = 0  # Reset — clean conversation
                    cumulative_prompt_tok = 0
                    self.eval_cap.reset_context_tracking()  # Fresh context = fresh token budget
                    continue

            try:
                response = self.llm.chat(
                    messages,
                    tools=tools,
                    max_tokens=max_tokens_per_call,
                )
            except Exception as e:
                # Detect HTTP 400-499 errors (context window exceeded, etc.)
                is_context_error = False
                try:
                    import requests

                    if isinstance(e, requests.HTTPError):
                        if hasattr(e, "response") and e.response is not None:
                            status = e.response.status_code
                            if 400 <= status < 500 and status != 429:
                                is_context_error = True
                except Exception:
                    pass

                # Also check error message for common context-window keywords
                err_msg = str(e).lower()
                if not is_context_error:
                    is_context_error = any(
                        kw in err_msg
                        for kw in (
                            "context",
                            "token",
                            "maximum",
                            "exceeded",
                            "window",
                            "truncat",
                            "length",
                        )
                    )

                if is_context_error and compaction_count < MAX_COMPACTIONS:
                    logger.warning(
                        "Context error on iteration %d (compacting #%d): %s",
                        iteration,
                        compaction_count + 1,
                        str(e)[:200],
                    )
                    messages, compaction_count = self._compact_context(
                        messages,
                        task,
                        code_context,
                        criteria_total,
                        compaction_count,
                    )
                    iteration = 0  # Reset — fresh context
                    cumulative_prompt_tok = 0
                    self.eval_cap.reset_context_tracking()  # Fresh context = fresh token budget
                    continue

                logger.error("LLM call failed on iteration %d: %s", iteration, e)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Evaluator error: LLM call failed: {e}",
                )

            # Track LLM call (costs 1.0 iterations + token usage)
            prompt_tok = response.usage.prompt_tokens if response.usage else 0
            completion_tok = response.usage.completion_tokens if response.usage else 0
            cache_read = response.usage.cache_read_tokens if response.usage else 0
            cache_write = response.usage.cache_write_tokens if response.usage else 0

            # Track cumulative prompt tokens for compaction threshold
            cumulative_prompt_tok = max(cumulative_prompt_tok, prompt_tok)

            cap_error = self.eval_cap.record_llm_call(
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            if cap_error:
                logger.warning("Eval cap exceeded: %s", cap_error)
                partial = self._extract_partial_verdict(criteria_list)
                if partial is not None:
                    return partial
                return Verdict(
                    verdict="INCOMPLETE",
                    summary=f"Cap exceeded: {cap_error}",
                )

            # No tool calls → LLM is delivering a verdict
            if not response.tool_calls:
                if response.content:
                    return self._parse_verdict(response.content)
                return Verdict(
                    verdict="INCOMPLETE",
                    summary="Evaluator returned empty response.",
                )

            # Add assistant message with tool calls
            assistant_msg: dict = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute each tool call — with dedup hints
            for tc in response.tool_calls:
                result, was_dup = self._execute_tool_with_dedup(tc)

                # Track tool call (costs tool_call_weight, default 0.1 iterations)
                cap_error = self.eval_cap.record_tool_call()
                if cap_error:
                    logger.warning("Eval cap exceeded during tool call: %s", cap_error)
                    partial = self._extract_partial_verdict(criteria_list)
                    if partial is not None:
                        return partial
                    return Verdict(
                        verdict="INCOMPLETE",
                        summary=f"Cap exceeded: {cap_error}",
                    )

                # Add dedup warning to result if this was a repeat
                if was_dup:
                    if isinstance(result, dict):
                        result["_dedup_warning"] = (
                            f"You already used {tc.name} with these arguments. "
                            "See previous result above. Move on to unchecked criteria."
                        )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

            logger.debug(
                "Evaluator iteration %d: %d tool calls, %d messages",
                iteration + 1,
                len(response.tool_calls),
                len(messages),
            )

            iteration += 1

        # Hit iteration cap — return clear error
        msg = (
            f"Cap exceeded: {self.eval_cap.summary()}. "
            "Increase caps (eval_cap param or evaluator.cap in .gitreins/config.yaml) "
            "or split criteria into focused single-criterion tasks."
        )
        logger.warning(msg)
        return Verdict(verdict="INCOMPLETE", summary=msg)

    def _extract_partial_verdict(self, criteria_list: list[str]) -> Verdict | None:
        """Extract a best-effort verdict from sandbox when caps are exceeded.

        Returns a Verdict with verdict="COMPLETE" if ANY criterion was
        verified (partial findings are still a signal), or None if NO
        criteria were verified (caller should fall through to INCOMPLETE).
        """
        items: list[VerdictItem] = []
        has_any = False
        for i, criterion in enumerate(criteria_list):
            key = f"verified_{i}"
            if key in self._sandbox:
                has_any = True
                evidence = self._sandbox[key]
                status = "PASS" if evidence.startswith("PASS") else "FAIL"
                items.append(
                    VerdictItem(
                        criterion=criterion,
                        status=status,
                        detail=evidence,
                    )
                )
            else:
                items.append(
                    VerdictItem(
                        criterion=criterion,
                        status="FAIL",
                        detail="Not verified — evaluation terminated before this criterion was checked",
                    )
                )
        if not has_any:
            return None
        return Verdict(
            verdict="COMPLETE",
            items=items,
            summary="Partial verdict — evaluation hit resource cap before all criteria verified",
        )

    def _execute_tool_with_dedup(self, tc: ToolCall) -> tuple[dict, bool]:
        """Execute a tool and return (result, was_duplicate).

        Time-budget check: before running an expensive tool
        (read_file, run_command, search_pattern), verify the time cap
        is not critical. If <10s remain, skip the tool and return a
        TIME_CRITICAL error so the LLM can deliver a verdict immediately.
        If over budget, return TIME_EXCEEDED.
        """
        # ── Time-budget pre-check (BEFORE dedup tracking) ─────────
        # When time is critical we want to intercept quickly — don't
        # pollute dedup state with skipped calls.
        # remaining_seconds() returns -1.0 when no time budget is
        # configured (unlimited) — we must NOT block in that case.
        if tc.name in ("read_file", "run_command", "search_pattern"):
            try:
                remaining = self.eval_cap.remaining_seconds()
            except Exception:
                remaining = -1.0

            if remaining < 0 and remaining != -1.0:
                # Real negative remaining = over a configured budget
                return (
                    {
                        "error": (
                            "TIME_EXCEEDED: Time budget exhausted. "
                            "Deliver your verdict immediately based on what is in the sandbox."
                        )
                    },
                    False,
                )
            if 0 < remaining < 10:
                return (
                    {
                        "error": (
                            f"TIME_CRITICAL: Only {int(remaining)} seconds remaining. "
                            "Deliver your verdict NOW with what you have. "
                            "Use sandbox_write to save any verified criteria first."
                        )
                    },
                    False,
                )

        was_dup = False

        if tc.name == "read_file":
            path = tc.arguments.get("path", "")
            if path in self._files_read:
                was_dup = True
            else:
                self._files_read.add(path)
        elif tc.name == "run_command":
            cmd = tc.arguments.get("cmd", "")
            if cmd in self._commands_run:
                was_dup = True
            else:
                self._commands_run.add(cmd)
        elif tc.name == "search_pattern":
            regex = tc.arguments.get("regex", "")
            if regex in self._searches_done:
                was_dup = True
            else:
                self._searches_done.add(regex)

        result = self._execute_tool(tc)
        return result, was_dup

    def _execute_tool(self, tc: ToolCall) -> dict:
        """Execute a tool call and return the result."""
        try:
            if tc.name == "read_file":
                return self._tool_read_file(**tc.arguments)
            elif tc.name == "run_command":
                return self._tool_run_command(**tc.arguments)
            elif tc.name == "search_pattern":
                return self._tool_search_pattern(**tc.arguments)
            elif tc.name == "read_diff":
                return self._tool_read_diff()
            elif tc.name == "get_task_item":
                return self._tool_get_task_item(**tc.arguments)
            elif tc.name == "sandbox_write":
                return self._tool_sandbox_write(**tc.arguments)
            elif tc.name == "sandbox_read":
                return self._tool_sandbox_read(**tc.arguments)
            elif tc.name == "detect_dead_code":
                return self._tool_detect_dead_code()
            elif tc.name == "skylos_scan":
                return self._tool_skylos_scan()
            elif tc.name == "scan_security":
                return self._tool_scan_security(**tc.arguments)
            elif tc.name == "read_static_analysis":
                return self._tool_read_static_analysis(**tc.arguments)
            elif tc.name == "read_lsp_diagnostics":
                return self._tool_read_lsp_diagnostics()
            else:
                return {"error": f"Unknown tool: {tc.name}"}
        except Exception as e:
            logger.exception("Tool %s failed", tc.name)
            return {"error": str(e)}

    # ── Tool implementations ──────────────────────────────────────

    def _tool_read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 0,
        byte_offset: int = 0,
        byte_limit: int = 0,
        mode: str = "lines",
    ) -> dict:
        """Read a file from the working tree with line or byte range support.

        Args:
            path: File path relative to workdir.
            offset: Start line (1-indexed). 0 = from beginning. Used when mode='lines'.
            limit: Max lines to return. 0 = no limit. Used when mode='lines'.
            byte_offset: Byte position to start from (0-indexed). Used when mode='bytes'.
            byte_limit: Max bytes to return. 0 = no limit. Used when mode='bytes'.
            mode: 'lines' (default) or 'bytes'.
        """
        full_path = os.path.join(self.workdir, path)
        real = os.path.realpath(full_path)
        if not real.startswith(os.path.realpath(self.workdir)):
            return {"error": f"Path outside working tree: {path}"}
        # Enforce file scope BEFORE existence check — reject out-of-scope files
        # even if they exist, so the LLM knows why it can't access them
        if not self._path_in_scope(path):
            return {
                "error": f"File not in scope: {path}. Evaluator is scoped to changed files only. Set evaluator.file_scope: full in .gitreins/config.yaml to allow all files."
            }
        if not os.path.exists(real):
            return {"error": f"File not found: {path}"}
        if os.path.isdir(real):
            return {"error": f"Path is a directory: {path}"}
        try:
            total_bytes = os.path.getsize(real)

            if mode == "bytes":
                # Byte-level read
                with open(real, "rb") as f:
                    if byte_offset > 0:
                        if byte_offset >= total_bytes:
                            return {
                                "error": f"Byte offset {byte_offset} exceeds file size ({total_bytes} bytes)",
                                "path": path,
                                "total_bytes": total_bytes,
                            }
                        f.seek(byte_offset)
                    if byte_limit > 0:
                        raw = f.read(byte_limit)
                    else:
                        raw = f.read()
                # Decode as UTF-8 with replacement chars for binary content
                content = raw.decode("utf-8", errors="replace")
                shown_bytes = len(raw)
                has_more = (byte_offset + shown_bytes) < total_bytes if byte_limit > 0 else False

                # ── Byte cap (GR-064d) ──
                max_bytes = getattr(self, "max_file_bytes", 131072)
                content_bytes = content.encode("utf-8")
                if len(content_bytes) > max_bytes:
                    capped = content_bytes[:max_bytes].decode("utf-8", errors="replace")
                    content = capped + (
                        f"\n\n... [capped at {max_bytes} bytes, {total_bytes} total. "
                        "Use offset/limit or byte_offset/byte_limit to read specific ranges.]"
                    )

                return {
                    "path": path,
                    "content": content,
                    "total_bytes": total_bytes,
                    "shown_bytes": shown_bytes,
                    "byte_offset_start": byte_offset,
                    "has_more": has_more,
                    "mode": "bytes",
                }

            # Line-based read (default)
            with open(real, "r", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            total_chars = sum(len(line) for line in lines)

            # Apply offset/limit
            if offset > 0:
                start_idx = offset - 1
                if start_idx >= total_lines:
                    return {
                        "error": f"Offset {offset} exceeds file length ({total_lines} lines)",
                        "path": path,
                        "total_lines": total_lines,
                    }
                lines = lines[start_idx:]
            if limit > 0:
                lines = lines[:limit]

            content = "".join(lines)

            # Only truncate if no range was requested AND file is very large
            if not offset and not limit and total_chars > 12000:
                content = "".join(lines[:400])  # First 400 lines
                content += f"\n\n... [showing first 400 of {total_lines} lines, {total_chars} chars. Use offset/limit to read specific ranges.]"

            # ── Byte cap (GR-064d) ──
            # Prevent individual read_file from consuming entire context window
            max_bytes = getattr(self, "max_file_bytes", 131072)
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > max_bytes:
                capped = content_bytes[:max_bytes].decode("utf-8", errors="replace")
                content = capped + (
                    f"\n\n... [capped at {max_bytes} bytes, {total_bytes} total. "
                    "Use offset/limit or byte_offset/byte_limit to read specific ranges.]"
                )

            return {
                "path": path,
                "content": content,
                "total_lines": total_lines,
                "total_chars": total_chars,
                "total_bytes": total_bytes,
                "shown_lines": len(lines),
                "has_more": (offset > 0 and (offset - 1 + limit < total_lines))
                or (not offset and not limit and total_chars > 12000)
                or (offset > 0 and not limit),
                "mode": "lines",
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_run_command(self, cmd: str = None, command: str = None) -> dict:
        """Run a shell command."""
        cmd = cmd or command
        if not cmd:
            return {"error": "No command provided"}
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                cwd=self.workdir,
            )
            output = result.stdout + result.stderr
            if len(output) > 4000:
                output = output[:4000] + f"\n... [truncated, exit_code={result.returncode}]"
            return {
                "cmd": cmd,
                "exit_code": result.returncode,
                "output": output,
            }
        except subprocess.TimeoutExpired:
            return {"cmd": cmd, "error": f"Command timed out after {self.command_timeout}s"}
        except Exception as e:
            return {"cmd": cmd, "error": str(e)}

    def _tool_search_pattern(self, regex: str, file_glob: str = "*") -> dict:
        """Search the codebase for a regex pattern using ripgrep (rg) with fallback."""
        # Validate regex first — catch invalid patterns before shelling out
        try:
            re.compile(regex)
        except re.error as e:
            return {"error": f"Invalid regex: {e}", "matches": [], "count": 0}

        # When file scope is restricted, use Python (rg can't filter by file list)
        if self._allowed_files is not None:
            return self._tool_search_pattern_python(regex, file_glob)

        # ── Ripgrep primary path ──
        if rg_path := shutil.which("rg"):
            return self._tool_search_pattern_rg(regex, file_glob, rg_path)

        # ── GNU grep fallback ──
        if grep_path := shutil.which("grep"):
            return self._tool_search_pattern_grep(regex, file_glob, grep_path)

        # ── Pure Python last resort ──
        return self._tool_search_pattern_python(regex, file_glob)

    def _tool_search_pattern_rg(self, regex: str, file_glob: str, rg_path: str) -> dict:
        """Search using ripgrep (fast, respects .gitignore)."""
        cmd = [
            rg_path,
            "--line-number",
            "--no-heading",
            "--smart-case",
            "-e",
            regex,
            "--glob",
            "!__pycache__/**",
            "--glob",
            "!.git/**",
            "--glob",
            "!venv/**",
            "--glob",
            "!.venv/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!.pytest_cache/**",
        ]
        if file_glob != "*":
            cmd.extend(["--glob", file_glob])
        # Restrict to allowed files scope
        if self._allowed_files is not None:
            # rg doesn't support file lists natively — filter post-hoc
            pass
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.workdir,
            )
            if result.returncode == 2 and "No files were searched" in result.stderr:
                return {"matches": [], "count": 0}
            if result.returncode > 1:
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, result.stdout, result.stderr
                )
            # rg returns 0=matches, 1=no matches, >1=error
            lines = result.stdout.strip().split("\n") if result.stdout else []
            return {"matches": lines[:200], "count": min(len(lines), 200)}
        except subprocess.TimeoutExpired:
            if grep_path := shutil.which("grep"):
                return self._tool_search_pattern_grep(regex, file_glob, grep_path)
            return {"error": "rg timed out after 60s"}
        except Exception:
            if grep_path := shutil.which("grep"):
                return self._tool_search_pattern_grep(regex, file_glob, grep_path)
            return {"error": "rg failed"}

    def _tool_search_pattern_grep(self, regex: str, file_glob: str, grep_path: str) -> dict:
        """Search using GNU grep (slower than rg, faster than Python)."""
        cmd = [grep_path, "-rnI", "--exclude-dir=__pycache__", "--include=*", "-e", regex, "."]
        if file_glob != "*":
            cmd = [
                grep_path,
                "-rnI",
                "--exclude-dir=__pycache__",
                f"--include={file_glob}",
                "-e",
                regex,
                ".",
            ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.workdir,
            )
            lines = result.stdout.strip().split("\n") if result.stdout else []
            return {"matches": lines[:200], "count": min(len(lines), 200)}
        except subprocess.TimeoutExpired:
            return self._tool_search_pattern_python(regex, file_glob)
        except Exception:
            return self._tool_search_pattern_python(regex, file_glob)

    def _tool_search_pattern_python(self, regex: str, file_glob: str = "*") -> dict:
        """Pure Python search — last resort fallback (slow, no .gitignore)."""
        matches: list[str] = []
        try:
            pattern = re.compile(regex)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        skip_dirs = {
            ".git",
            "venv",
            ".venv",
            "node_modules",
            "__pycache__",
            ".gitreins-sandbox",
            ".pytest_cache",
        }
        files_searched = 0
        for root, dirs, files in os.walk(self.workdir):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for fname in files:
                if file_glob != "*":
                    import fnmatch

                    if not fnmatch.fnmatch(fname, file_glob):
                        continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, self.workdir)
                # Enforce file scope: only search within allowed files
                if self._allowed_files is not None and not self._path_in_scope(rel):
                    continue
                files_searched += 1
                try:
                    if os.path.getsize(fpath) > 500_000:
                        continue  # Skip large files
                    with open(fpath, "r", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                matches.append(f"{rel}:{i}: {line.rstrip()}")
                except Exception:
                    pass

            if len(matches) > 200:
                matches = matches[:200]
                matches.append(f"... [truncated at 200 matches, searched {files_searched} files]")
                break

        return {"regex": regex, "matches": matches, "count": len(matches)}

    def _tool_read_diff(self) -> dict:
        """Show git diff."""
        try:
            staged = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            unstaged = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            return {
                "staged": staged.stdout.strip() or "(no staged changes)",
                "unstaged": unstaged.stdout.strip() or "(no unstaged changes)",
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_read_static_analysis(self, path: str | None = None) -> dict:
        """Return static analysis diagnostics for the project or a specific path."""
        from engine.static_analysis import run_static_check

        config = self._load_config()
        evaluator_cfg = config.get("evaluator", {})
        if not evaluator_cfg.get("static_analysis_diagnostics", False):
            return {"error": "static_analysis_diagnostics is not enabled in .gitreins/config.yaml"}

        guards_cfg = config.get("guards", {})
        static_tools = guards_cfg.get("static_analysis_tools", {})

        # Determine which language tools to run
        lang_tools: list[str] = []
        for _lang, tools in static_tools.items():
            lang_tools.extend(tools)

        if not lang_tools:
            return {"diagnostics": [], "note": "No static analysis tools configured"}

        # Determine workdir: if path is provided, use its parent; otherwise project root
        import os

        target_dir = self.workdir
        if path:
            abs_path = os.path.join(self.workdir, path)
            if os.path.isfile(abs_path):
                target_dir = os.path.dirname(abs_path)
            elif os.path.isdir(abs_path):
                target_dir = abs_path

        all_diagnostics: list[dict] = []
        for tool in lang_tools:
            try:
                diags = run_static_check(tool, target_dir)
                all_diagnostics.extend(diags)
            except Exception as exc:
                all_diagnostics.append(
                    {
                        "tool": tool,
                        "error": str(exc),
                        "file": "",
                        "line": 0,
                        "severity": "error",
                        "message": f"Tool failed: {exc}",
                        "code": "",
                    }
                )

        return {
            "diagnostics": all_diagnostics,
            "count": len(all_diagnostics),
            "tools_used": lang_tools,
        }

    def _tool_read_lsp_diagnostics(self) -> dict:
        """Return LSP diagnostics collected during the Tier 1 guard run.

        These diagnostics were gathered by LSP tools (e.g. pylsp) during
        the guard phase and passed to the evaluator. No new LSP check is
        triggered — this returns the cached results from Tier 1.
        """
        return {
            "diagnostics": self._tier1_diagnostics,
            "count": len(self._tier1_diagnostics),
        }

    def _tool_get_task_item(self, id: str) -> dict:
        """Get task definition."""
        task = self._task_index.get(id)
        if task:
            return dict(task)
        return {"error": f"Task not found: {id}"}

    def _tool_sandbox_write(self, key: str, content: str) -> dict:
        """Write to sandbox."""
        self._sandbox[key] = content
        return {"key": key, "written": len(content)}

    def _tool_sandbox_read(self, key: str) -> dict:
        """Read from sandbox."""
        if key in self._sandbox:
            content = self._sandbox[key]
            if len(content) > 4000:
                content = content[:4000] + "... [truncated]"
            return {"key": key, "content": content}
        return {"error": f"Key not found: {key}"}

    def _parse_verdict(self, content: str) -> Verdict:
        """Parse JSON verdict from LLM response — robust against markdown fences and extra text."""
        logger.debug("Parsing verdict from: %.200s", content)

        # Strategy 1: Strip markdown fences
        cleaned = content.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            # Remove closing fence
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)

        # Strategy 2: Find JSON object boundaries
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            json_str = cleaned[start : end + 1]
            try:
                data = json.loads(json_str)

                # Validate required fields
                if "verdict" not in data or "items" not in data:
                    raise ValueError("Missing required fields 'verdict' or 'items'")

                verdict_val = str(data["verdict"]).upper()
                if verdict_val not in ("COMPLETE", "INCOMPLETE"):
                    verdict_val = "INCOMPLETE"

                items = []
                for item in data.get("items", []):
                    status = str(item.get("status", "FAIL")).upper()
                    if status not in ("PASS", "FAIL"):
                        status = "FAIL"
                    items.append(
                        VerdictItem(
                            criterion=item.get("criterion", "unknown"),
                            status=status,
                            detail=item.get("detail", ""),
                        )
                    )
                return Verdict(
                    verdict=verdict_val,
                    items=items,
                    summary=data.get("summary", ""),
                )
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("JSON parse failed: %s", e)

        # Strategy 3: Keyword-based fallback
        content_lower = content.lower()
        if '"complete"' in content_lower or 'verdict":"complete"' in content_lower.replace(" ", ""):
            verdict = "COMPLETE"
        elif "all criteria" in content_lower and "pass" in content_lower:
            verdict = "COMPLETE"
        else:
            verdict = "INCOMPLETE"

        logger.warning("Falling back to keyword parse: verdict=%s", verdict)
        return Verdict(
            verdict=verdict,
            summary=f"(auto-parsed from non-JSON response) {content[:300]}",
        )

    def _tool_detect_dead_code(self) -> dict:
        """Run dead code detection and return structured results."""
        try:
            from engine.dead_code import DeadCodeDetector

            detector = DeadCodeDetector(self.workdir)
            report = detector.scan()
            unused = detector.find_unused_functions()
            report.findings.extend(unused)

            by_category: dict[str, list[dict]] = {}
            for f in report.findings:
                by_category.setdefault(f.category, []).append(
                    {
                        "file": f.file,
                        "line": f.line,
                        "message": f.message,
                    }
                )

            return {
                "total_findings": len(report.findings),
                "passed": report.passed,
                "by_category": {
                    cat: {"count": len(items), "items": items[:20]}
                    for cat, items in by_category.items()
                },
            }
        except ImportError:
            return {"error": "Dead code detector unavailable"}
        except Exception as e:
            return {"error": str(e)}

    def _tool_skylos_scan(self) -> dict:
        """Run Skylos multi-language dead code and AI-mistake scan."""
        try:
            import json as _json
            import subprocess as _sp

            result = _sp.run(
                ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workdir,
            )
            if result.returncode != 0:
                return {
                    "error": f"skylos exited {result.returncode}",
                    "stderr": result.stderr[:500],
                }

            data = _json.loads(result.stdout)

            findings = {
                "unused_functions": [
                    {"file": f["file"], "line": f["line"], "name": f["name"]}
                    for f in data.get("unused_functions", [])
                ],
                "unused_imports": [
                    {"file": f["file"], "line": f["line"], "name": f["name"]}
                    for f in data.get("unused_imports", [])
                ],
                "dead_symbols": [
                    {"file": info["file"], "line": info["line"], "name": name}
                    for name, info in data.get("definitions", {}).items()
                    if info.get("dead")
                ],
            }

            grade = data.get("grade", {}).get("overall", {})
            return {
                "grade": f"{grade.get('letter', '?')} ({grade.get('score', '?')})",
                "total_findings": sum(len(v) for v in findings.values()),
                "findings": findings,
            }
        except FileNotFoundError:
            return {"error": "skylos not installed — pip install skylos"}
        except Exception as e:
            return {"error": str(e)}

    def _tool_scan_security(self, path: str | None = None) -> dict:
        """Run ast-grep security-pattern scan against the CodeRabbit essential
        rules (deterministic, syntax-aware — finds real vulns without relying on
        the LLM). Scans each rule individually (--rule) because bulk directory
        loading aborts on any rule that uses CodeRabbit's inline-utils schema,
        which stock ast-grep rejects. Rules that fail to parse are skipped; the
        rest are aggregated. Returns findings as SARIF-derived entries."""
        try:
            import json as _json
            import glob as _glob
            import shutil as _shutil
            import subprocess as _sp

            ast_grep = _shutil.which("ast-grep")
            if not ast_grep:
                return {"error": "ast-grep not installed — cargo install ast-grep"}

            rules_dir = os.path.expanduser("~/.gitreins-rules/rules")
            if not os.path.isdir(rules_dir):
                return {
                    "error": "gitreins security rules not installed "
                    "(~/.gitreins-rules/rules — clone coderabbitai/ast-grep-essentials)"
                }

            rule_files = sorted(_glob.glob(os.path.join(rules_dir, "*", "*", "*.yml")))
            if not rule_files:
                rule_files = sorted(_glob.glob(os.path.join(rules_dir, "*", "*.yml")))

            scan_target = path if path else "."
            all_findings = []
            skipped = 0
            for rf in rule_files:
                result = _sp.run(
                    [ast_grep, "scan", "--rule", rf, "--format", "sarif", scan_target],
                    capture_output=True, text=True, timeout=30,
                    cwd=self.workdir,
                )
                if result.returncode != 0:
                    skipped += 1  # rule failed to parse — skip, don't abort
                    continue
                if not result.stdout.strip():
                    continue
                try:
                    data = _json.loads(result.stdout)
                except _json.JSONDecodeError:
                    continue
                for run in data.get("runs", []):
                    for res in run.get("results", []):
                        msg = (res.get("message", {}) or {}).get("text", "")
                        loc = (res.get("locations") or [{}])[0].get("physicalLocation", {})
                        art = (loc.get("artifactLocation", {}) or {}).get("uri", "")
                        reg = loc.get("region", {}) or {}
                        all_findings.append({
                            "file": os.path.basename(art),
                            "path": art,
                            "line": reg.get("startLine"),
                            "message": msg[:300],
                            "rule": os.path.basename(rf),
                        })

            return {
                "total_findings": len(all_findings),
                "rules_loaded": len(rule_files),
                "rules_skipped": skipped,
                "findings": all_findings,
                "note": "Deterministic AST security scan (CodeRabbit essentials, per-rule) — treat findings as strong FAIL evidence for security/correctness criteria.",
            }
        except FileNotFoundError:
            return {"error": "ast-grep not installed — cargo install ast-grep"}
        except Exception as e:
            return {"error": str(e)}
