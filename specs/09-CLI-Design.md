# 09-CLI-Design.md — CLI Design

> **Document Status:** Draft | **Last Updated:** 2026-07-19 | **Author:** GitReins CLI Design Spec

---

## 1. Overview

The GitReins CLI is the human-facing entry point for the GitReins quality harness. It provides commands for one-command repository setup, task lifecycle management, static guard execution, agentic evaluation, and MCP server startup. The CLI is built on Python's `argparse` module and is designed for clarity, consistency, and discoverability.

**File:** `gitreins/cli.py` — 804 lines  
**Entry point:** `gitreins` (installed via `pyproject.toml` `[project.scripts]`)

---

## 2. CLI Entry Point

### 2.1 Module Structure

```python
# gitreins/cli.py
import argparse
import logging
import os
import sys
import yaml

from engine.version import __version__


def main():
    parser = argparse.ArgumentParser(description="GitReins — Git-Native Agent Co-Harness")
    parser.add_argument("--version", action="version", version=f"gitreins {__version__}")
    sub = parser.add_subparsers(dest="command")
    # ... subcommands ...
```

### 2.2 Global Flags

| Flag | Action | Output |
|------|--------|--------|
| `--version` | `version` | Prints `gitreins 0.6.0` and exits |

No other global flags exist. All functionality is routed through subcommands.

### 2.3 Logging

Default logging configuration:
- **Level:** `WARNING` (only warnings and errors shown by default)
- **Format:** `%(name)s: %(levelname)s: %(message)s`

This keeps CLI output clean for human consumption while still surfacing engine warnings.

---

## 3. Command Tree

```
gitreins
├── install          One-command repo activation
├── init             Smart init — detect language, size, optimal config
├── task
│   ├── create       Create a task with criteria
│   ├── start        Mark task as in-progress
│   ├── complete     Mark complete and evaluate
│   ├── list         List tasks (optionally filtered by status)
│   └── delete       Delete a task
├── guard            Run Tier 1 static guards
├── judge            Evaluate a task (Tier 1 + Tier 2)
├── commit           Commit with guard gate
├── commit-audit     Run configured Tier 2 commit-message/code review
├── mcp-server       Start MCP stdio server
├── setup-tools      Show static-analysis tool availability and install guidance
└── report           Show verdict history
```

`propagate` is currently exposed as the MCP `propagate` tool rather than a CLI parser subcommand. `gitreins propagate` is therefore not a supported local command in this release; use the MCP tool when an agent must merge guard configuration into sibling repositories.

### 3.1 install

**Purpose:** One-command GitReins activation for the current repository.

**Creates:**
1. `.gitreins/config.yaml` — default configuration (if missing)
2. `.git/hooks/pre-commit` — runs `gitreins guard` on staged changes
3. `.gitignore` entry — adds `.gitreins/tasks.yaml` if not present

**Behavior:**
- Skips existing files (does not overwrite config)
- Overwrites pre-commit hook (always ensures latest version)
- Prints summary of created vs skipped items
- Exits with code 1 if not in a git repository

**Output example:**
```
GitReins installed in /home/kara/my-project

Created:
  + .gitreins/config.yaml
  + .git/hooks/pre-commit
  + .gitignore (added .gitreins/tasks.yaml)

Next steps:
  - Run smart init:  gitreins init
  - Create a task:  gitreins task create <id> <title> [criteria...]
  - Run guards:     gitreins guard
  - Try the hook:   make a change, git add ., git commit -m 'test'
```

### 3.2 init

**Purpose:** Smart project initialization — detects language, project size, and generates optimal configuration.

**Detection:**
- **Language:** `go.mod` → Go, `pyproject.toml`/`setup.py` → Python, `package.json` → TypeScript
- **Size:** Counts packages (Go) or Python packages with `__init__.py`
- **Test command:** Inferred from language and project files

**Config generation:**
- Small projects (≤3 packages): `max_iterations=15`, `test_mode=full`
- Medium projects (≤10 packages): `max_iterations=25`, `test_mode=full`
- Large projects (≤25 packages): `max_iterations=50`, `test_mode=diff`
- Very large projects (>25 packages): `max_iterations=100`, `test_mode=diff`

**Flags:**
- `--reset` — Reset config to smart defaults (overwrites existing)

**Output example:**
```
GitReins init: /home/kara/my-project
  Language:    Python
  Packages:    8
  Test cmd:    pytest -x --tb=short
  Test mode:   full
  Eval cap:    25 iterations
  History:     enabled

Updated: guards, evaluator, history, pre-commit hook
```

### 3.3 task create

**Usage:** `gitreins task create <id> <title> [criteria...]`

**Arguments:**
- `id` — Unique task identifier (e.g., `fix-auth`, `AC-042`)
- `title` — Human-readable task description
- `criteria` — Zero or more acceptance criteria strings
- `--depends-on` — Task IDs that must complete before this one

**Output:**
```
Created task: fix-auth — Fix authentication
  Depends on: db-migration
  1. Login accepts email+password and returns JWT
  2. Invalid credentials return 401
  3. Rate limiting works after 5 failed attempts
```

### 3.4 task start

**Usage:** `gitreins task start <id>`

**Transitions task status:** `pending` → `in_progress`

**Output:**
```
Started: fix-auth → in_progress
```

### 3.5 task complete

**Usage:** `gitreins task complete <id> [--force]`

**Behavior:**
1. Validates dependencies (unless `--force`)
2. Transitions status: `in_progress` → `complete`
3. Triggers evaluation (Judge + AgenticEvaluator)
4. Prints evaluation summary
5. Persists verdict to history

**Output:**
```
Completed: fix-auth → complete

Evaluating...
Verdict: COMPLETE — All 3 criteria passed
  📋 Verdict saved: abc1234
```

### 3.6 task list

**Usage:** `gitreins task list [--status pending|in_progress|complete]`

**Output:**
```
  ○ fix-auth             Fix authentication
  ◐ db-migration         Add user table
  ● api-docs             Write API documentation
```

Status icons: `○` pending, `◐` in_progress, `●` complete

### 3.7 task delete

**Usage:** `gitreins task delete <id>`

**Output:**
```
Deleted: fix-auth
```

### 3.8 guard

**Usage:** `gitreins guard [--dead-code]`

**Behavior:**
1. Checks for updates (non-blocking, stderr notice if available)
2. Loads `.gitreins/config.yaml`
3. Runs all enabled Tier 1 guards
4. Prints pass/fail summary with test mode details

**Flags and configured guards:**
- `--dead-code` — Enable the Python AST dead-code detector for this invocation, overriding `guards.dead_code`.
- LSP diagnostics are enabled by configuration (`guards.lsp: true`, `guards.lsp_tools: [...]`) and run as part of `gitreins guard`. There is no `gitreins guard --lsp` parser flag in this release; use the config flag so LSP checks are reproducible in hooks and CI.
- Static analysis is likewise configured with `guards.static_analysis: true` and `guards.static_analysis_tools`; `gitreins setup-tools` reports available analyzers and installation guidance.

**Output (pass):**
```
Tier 1 Guards: PASS  (test mode: diff, 3 test file(s))
All guards passed — 4 checks, 0 failures
```

**Output (fail):**
```
Tier 1 Guards: FAIL  (test mode: full)
Secrets: FAIL — found potential secret in config.py:42
Lint: PASS
Tests: FAIL — 2 failed in test_auth.py

Fix the issues above and re-run: gitreins guard
```

**Exit code:** 0 on pass, 1 on fail

### 3.9 judge

**Usage:** `gitreins judge <id> [--skip-tier2]`

**Behavior:**
1. Loads task by ID
2. Runs full evaluation pipeline (Tier 1 + Tier 2)
3. Prints verdict summary
4. Persists verdict to history

**Flag:** `--skip-tier2` runs the Tier 1 guard portion only and skips the LLM evaluator. Use it for configuration, documentation, or operational changes that do not require a Tier 2 assessment.

**Output:**
```
Verdict: COMPLETE — All 3 criteria passed
  📋 Verdict saved: def5678
```

**Exit code:** 0 if task found, 1 if task not found

### 3.10 commit

**Usage:** `gitreins commit <message>`

**Behavior:**
1. Runs Tier 1 guards
2. If guards pass: runs `git commit -m <message>`
3. If guards fail: prints failures and exits without committing

**Output (guards pass):**
```
Tier 1 PASSED — committing...
[main abc1234] Fix authentication bug
 2 files changed, 15 insertions(+)
```

**Output (guards fail):**
```
Tier 1 FAILED — cannot commit:
Secrets: FAIL — found potential secret in config.py:42
```

**Exit code:** 0 on success, 1 on guard failure or git error

### 3.11 commit-audit

**Usage:** `gitreins commit-audit [message]`

Runs the configured `commit_audit` pipeline stage against the staged diff. With no `message`, it reads `.git/COMMIT_EDITMSG`; a `gitreins.skip-tier2` trailer skips the audit.

**Review modes** (configured under `commit_audit.review_mode`):
- `message` — validate the commit message against the diff.
- `review` — one-pass CodeRabbit-style LLM review.
- `agent` — multi-turn review with read-file and search tools.

Each code-review finding includes a CVE-style 1–10 score. The pipeline computes `effective_score = issue.score × review_score_offset` and compares it with `review_score_threshold`; block-mode audits reject findings at or above the threshold.

### 3.12 mcp-server

**Usage:** `gitreins mcp-server`

**Behavior:** Starts the MCP stdio server for AI agent connections. Reads JSON-RPC 2.0 messages from stdin and writes responses to stdout. Runs until EOF or SIGTERM.

**No output** under normal operation (all communication is JSON-RPC over stdio).

### 3.13 setup-tools

**Usage:** `gitreins setup-tools`

Shows available static-analysis tools for the detected project language and prints the documented install command for missing tools. It does not install packages or modify configuration.

### 3.14 report

**Usage:** `gitreins report [-n N] [--interactive]`

**Behavior:** Shows recent evaluation verdicts from history.

**Flags:**
- `-n` — Number of recent verdicts to show (default 10)
- `--interactive` — TUI mode (requires `textual` package)

**Output:**
```
✓ fix-auth             2026-06-20  [✓✓✓]
   Fix authentication
✗ db-migration         2026-06-19  [✓✗✓]
   Add user table
```

---

## 4. Exit Codes

| Code | Meaning | Commands |
|------|---------|----------|
| 0 | Success | All commands that complete normally |
| 1 | Guard failure | `guard`, `commit` (guards failed) |
| 1 | Task not found | `task start`, `task complete`, `judge` |
| 1 | Not a git repo | `install` |
| 1 | Dependency blocked | `task complete` (without `--force`) |
| 1 | Git error | `commit` (git command failed) |

GitReins uses a simple exit code scheme: 0 for success, 1 for any failure. The specific failure reason is always printed to stdout/stderr.

---

## 5. Config Loading

### 5.1 `load_config(workdir)`

```python
def load_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml, returning {} if not found."""
```

- Returns empty dict if `.gitreins/config.yaml` does not exist
- Returns empty dict if the file cannot be parsed
- Uses `yaml.safe_load` for security

### 5.2 `get_workdir()`

```python
def get_workdir() -> str:
    """Find the git repo root."""
```

- Runs `git rev-parse --show-toplevel` with 5-second timeout
- Falls back to `os.getcwd()` if not in a git repository
- Used by all commands to determine the repository context

---

## 6. Default Config Template

The `DEFAULT_GITREINS_CONFIG` string is embedded in `cli.py` and written to `.gitreins/config.yaml` during `install` if no config exists.

```yaml
# GitReins Configuration

# ── Global defaults ─────
defaults:
  max_iterations: 100
  max_input_tokens: "10M"
  max_output_tokens: "1M"
  tool_call_weight: 0.1
  check_for_updates: true
  update_check_ttl: "24h"

# ── Guards ─────────────────────────────────
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"
  test_command: "pytest -x --tb=short"

# ── Evaluator caps ─────────────────────────
evaluator:
  max_iterations: 100

# ── Verdict history ────────────────────────
history:
  enabled: true
  storage: "git"
  max_verdicts: 1000
```

This template is conservative: all guards enabled, full test mode, moderate evaluator caps. Users can customize via `init` or manual editing.

---

## 7. Pre-Commit Hook Template

The `PRE_COMMIT_HOOK` string is embedded in `cli.py` and written to `.git/hooks/pre-commit` during `install`.

```bash
#!/usr/bin/env bash
# GitReins pre-commit hook — runs Tier 1 guards on staged changes.

REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ ! -f "$REPO_ROOT/.gitreins/config.yaml" ]; then
    exit 0
fi

cd "$REPO_ROOT"
gitreins guard
exit $?
```

**Behavior:**
- Skips cleanly if `.gitreins/config.yaml` is missing (repo not initialized with GitReins)
- Runs `gitreins guard` in the repo root
- Propagates exit code — non-zero blocks the commit
- The hook is made executable (`chmod 755`) during installation

---

## 8. Human Experience

### 8.1 Help Output

All commands support `--help`:

```bash
gitreins --help
gitreins task create --help
gitreins guard --help
```

The help output follows argparse conventions: command description, positional arguments, optional flags, and defaults.

### 8.2 Error Messages

Error messages are concise and actionable:

| Situation | Message |
|-----------|---------|
| Not a git repo | `Error: /path is not a git repository. Run 'git init' first.` |
| Task not found | `Task not found: fix-auth` |
| Dependency blocked | `Cannot complete 'fix-auth' — depends on incomplete tasks: db-migration` |
| Guard failure | `Tier 1 FAILED — cannot commit:` + per-guard details |
| Missing API key | `No LLM API key configured` (judge/complete with LLM eval) |

### 8.3 Success Summaries

Success output includes next steps and context:

- `install` — Lists created files and next steps
- `task create` — Shows criteria list
- `task complete` — Shows verdict and persisted commit hash
- `guard` — Shows test mode and target count (in diff mode)
- `commit` — Shows git commit output

### 8.4 Color Output

Limited color usage for human readability:
- **Yellow** (`\033[33m`) — Update availability notices (stderr)
- **No other colors** — Keeps output clean and terminal-agnostic

---

## 9. Verification Checklist

| # | Check | Verification |
|---|-------|------------|
| 1 | `gitreins --version` prints version | `gitreins --version` → `gitreins 0.6.0` |
| 2 | `install` creates config, hook, gitignore | `ls .gitreins/config.yaml .git/hooks/pre-commit` |
| 3 | `task create` persists to tasks.yaml | `cat .gitreins/tasks.yaml` contains new task |
| 4 | `task complete` triggers evaluation | Output shows "Evaluating..." and verdict |
| 5 | `guard` exit code reflects pass/fail | `gitreins guard; echo $?` → 0 or 1 |
| 6 | `commit` blocks on guard failure | `gitreins commit "msg"` with failing guards → exit 1, no commit |
| 7 | `mcp-server` starts without error | `gitreins mcp-server` runs, responds to JSON-RPC |
| 8 | `init` detects language correctly | `gitreins init` shows correct language in output |
| 9 | Pre-commit hook runs on `git commit` | `git commit -m "test"` triggers guard check |
| 10 | `load_config` returns {} for missing config | Non-GitReins repo → empty dict, no crash |

---

## 10. Document Status

| Field | Value |
|-------|-------|
| **Version** | v0.6.0 |
| **Status** | Draft |
| **Last updated** | 2026-07-19 |
| **Author** | totalwindupflightsystems <totalwindupflightsystems@gmail.com> |
| **Co-author** | wojons <wojonstech@gmail.com> |
