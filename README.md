# GitReins

**Git-Native Agent Co-Harness — static guards + agentic evaluator for AI-assisted code**

[![CI](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml/badge.svg)](https://github.com/totalwindupflightsystems/gitreins/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/gitreins)](https://pypi.org/project/gitreins/)

![GitReins Banner](https://raw.githubusercontent.com/totalwindupflightsystems/gitreins/main/assets/banner-dark.jpg)

GitReins lives inside your git repository as a quality harness. It provides MCP tools for task lifecycle management, an agentic evaluator that judges code completeness against task definitions, and git hooks that ensure nothing bypasses the quality gates.

> ✅ **v0.11.0** — LSP diagnostics (14 languages), static analysis (9 tools), commit audit with CVE-scored severity, optional Antares CVE-localization guard, Anthropic Messages API support, DeepSeek prompt caching telemetry, large-repo hardening (fast-track + `--skip-tier2`), MCP `propagate`, 1247 tests pass.

---

## Quick Start

```bash
pip install gitreins
cd /path/to/your-project
gitreins install        # creates .gitreins/config.yaml + pre-commit hook
gitreins init           # smart init — detects language, size, optimal config
```

New to GitReins? Read the [Onboarding Guide](docs/onboarding.md) — full
install → init → first guard run → task workflow, plus troubleshooting for
the most common first-run failures (gitleaks regex config, Python import
setup).

## How It Works

1. **Create tasks** — Define criteria via CLI or MCP tools
2. **Work with your AI agent** — Claude, Hermes, Codex, or Pi does code generation
3. **Complete tasks** — `gitreins task complete <id>` triggers automatic evaluation
4. **Tier 1: Static guards** — secrets, build, lint, tests (configurable)
5. **Tier 2: Agentic evaluator** — LLM loop reads files, runs tests, delivers per-criterion PASS/FAIL
6. **Verdicts persisted** — stored in `.gitreins/history/`, browsable via `gitreins report`
7. **Commit through harness** — pre-commit hook runs guards, blocks if checks fail

> **MCP commit rule:** the MCP `commit` tool refuses while any task is
> `in_progress` — completed work must be judged against the task's criteria
> first. Finish tasks with `task.complete` (which runs the quality judge) or
> remove them with `task.delete`, then retry the commit.

## Commands

```
gitreins install                      # Install hooks + config
gitreins init                         # Smart init (language, size, optimal config)
gitreins guard                        # Run Tier 1 static checks
gitreins security-scan [-d DIR] [--output text|json] [--force-ml]
                                       # Run the Antares CVE localization scanner
gitreins report [-n N] [--interactive]  # Browse verdict history
gitreins task create <id> <title> [criteria...] [--depends-on ...]
gitreins task start <id>
gitreins task complete <id> [--force]
gitreins task list [--status pending|in_progress|complete]
gitreins task delete <id>
gitreins judge <id>                   # Evaluate a task
gitreins commit <message>             # Commit with guard checks
gitreins mcp-server                   # Run MCP stdio server (for AI agents)
```

---

## Security Scan (optional)

GitReins ships an **opt-in** Tier 1 security guard that localizes
known CVEs against your staged Python code. It is built on the
[Antares CVE localization framework](https://huggingface.co/fdtn-ai/antares-1b)
(FDTN-AI's 1B-parameter model fine-tuned for code-level vulnerability
localization). Until the optional ML stack is installed, the guard
falls back to a keyword-based heuristic that produces
`CVE-SIMULATED` findings so the wiring can be exercised end-to-end.

### CLI

```bash
# Scan staged Python files (default; used by `gitreins guard`).
gitreins security-scan

# Recursively scan a directory instead of staged files.
gitreins security-scan --directory engine/

# Machine-readable output for piping into other tools.
gitreins security-scan --output json

# Require real ML inference — fail if huggingface_hub/transformers
# are not installed (exit code 2). Without this flag the heuristic
# fallback is used.
gitreins security-scan --force-ml
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean — no findings |
| 1 | One or more findings produced |
| 2 | `--force-ml` requested but ML dependencies are missing |

### Install requirements

The heuristic scanner has no extra dependencies. Real ML inference
requires the optional ML stack:

```bash
pip install huggingface_hub transformers
# Optional, for GPU inference:
pip install torch        # or onnxruntime
```

The model is downloaded on first use into
`~/.cache/gitreins/antares-1b/` and reused on subsequent runs.

### Configuration

Enable the guard in `.gitreins/config.yaml`:

```yaml
defaults:
  security_scan:
    enabled: true              # opt-in: default false
    model: antares-1b          # "antares-1b" | "antares-350m"
    min_confidence: 0.7        # filter by CVSS severity score
    cve_source: nvd            # "nvd" | "github" | "both"
```

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | When `true`, the security_scan guard runs alongside other Tier 1 checks |
| `model` | `antares-1b` | HuggingFace model id; `antares-350m` is a smaller variant |
| `min_confidence` | `0.7` | Drop entries whose CVSS score is below this. Severity→score: CRITICAL=1.0, HIGH=0.85, MEDIUM=0.6, LOW=0.3 |
| `cve_source` | `nvd` | `nvd` uses the NVD REST API, `github` uses the GitHub Advisory Database, `both` merges the two |

The CVE feed is cached at `~/.cache/gitreins/cve_feed/` with a
24-hour TTL. When the network is unreachable the feed serves stale
cache; when both cache and network are unavailable the feed returns
an empty list and the guard exits clean (it is **opt-in** and must
never block a commit on missing infrastructure).

---

## Test Modes: `full` vs `diff`

GitReins supports two strategies for when tests run on commit, controlled by `test_mode` in `.gitreins/config.yaml`.

### `test_mode: "full"` (default for new projects)

The entire test suite runs on every commit. Safe and thorough.

**Best for:**
- New projects with a small, fast test suite
- Projects where all tests pass reliably
- When you want maximum safety on every commit

**Tradeoff:** Slow on large projects. Pre-existing failures in untouched code block unrelated commits.

```yaml
guards:
  test_mode: "full"
```

### `test_mode: "diff"` (recommended for mature projects)

Only tests for packages you actually changed. Uses basename mapping:

| Changed file | Test run |
|---|---|
| `engine/guard_manager.py` | `tests/test_guard_manager.py` |
| `gitreins/cli.py` | `tests/test_cli.py` |
| `gitreins_mcp/server.py` | `tests/test_mcp_server.py` |

**Best for:**
- Projects with 5+ packages where full suite is slow
- Projects with pre-existing test failures in untouched code
- When you want fast feedback on the code you actually changed

**Safety nets — diff mode falls back to full suite when:**
- `pyproject.toml`, `.gitreins/config.yaml`, `Makefile`, or `setup.cfg` changed
- A test file itself changed (always included, plus its source-mapped siblings)
- Changed files don't map to any known test files (unknown file = safety)
- No staged files at all
- Test command isn't `pytest` (custom runners can't be narrowed)

**Tradeoff:** Less safety on cross-cutting changes. Config changes always trigger full suite.

```yaml
guards:
  test_mode: "diff"
```

### Which mode should I use?

| Project state | Recommended mode |
|---|---|
| Brand new, <5 packages | `full` |
| Mature, 5+ packages, tests pass | `diff` |
| Mature, pre-existing test failures | `diff` |
| Refactoring across packages | `full` (temporarily) |
| CI / PR checks | `full` (safety over speed) |

### Output examples

**Full mode:**
```
Tier 1 Guards: PASS  (test mode: full)
  ✓ secrets — clean
  ✓ lint — ok
  ✓ tests — passed
```

**Diff mode (targeted):**
```
Tier 1 Guards: PASS  (test mode: diff, 3 test file(s))
  ✓ secrets — clean
  ✓ tests — passed
```

**Diff mode (safety trigger — full suite):**
```
Tier 1 Guards: PASS  (test mode: diff, full suite — safety trigger)
  ✓ secrets — clean
  ✓ tests — passed
```

---

## Verdict History

Every `gitreins task complete` and `gitreins judge` saves a verdict to `.gitreins/history/`. Configure in `.gitreins/config.yaml`:

```yaml
history:
  enabled: true              # false = don't save verdicts
  storage: "git"             # "git" = auto-commit to gitreins branch
                             # "filesystem" = write files only, no git commits
  max_verdicts: 1000         # auto-prune old entries
```

Browse history:

```bash
gitreins report              # last 10 evaluations
gitreins report -n 20        # last 20
gitreins report --interactive  # TUI with arrow-key navigation (requires textual)
```

### Branch mechanics (git storage)

With `storage: "git"` (the default), every verdict is auto-committed to a
dedicated orphan `gitreins` branch — never to `main`. The branch is only
checked out transiently (or updated via a temporary worktree), so your
working tree is never disturbed. `.gitreins/history/` is intentionally
gitignored: the verdict files are runtime artifacts whose canonical home is
the `gitreins` branch, and a fresh clone therefore has no local
`.gitreins/history/` directory.

`gitreins report` reads verdicts in this order:

1. **Local filesystem** — `.gitreins/history/` in the working tree (used
   when present, e.g. right after a judge run in the same checkout).
2. **`gitreins` branch fallback** — when the local directory is missing or
   empty and storage is `"git"`, verdicts are read straight from the branch
   (`git ls-tree` / `git show`), so a fresh clone can still browse the full
   verdict history.

To inspect the branch directly:

```bash
git log --oneline gitreins                                            # verdict commits
git ls-tree -r --name-only gitreins -- .gitreins/history              # stored files
git show gitreins:.gitreins/history/<date>/<hash>/verdict.json        # one verdict
```

With `storage: "filesystem"`, verdicts are written locally only — no branch
is created and the fallback is skipped.

## Task Dependencies

Tasks can depend on other tasks. Evaluation is blocked until dependencies pass:

```bash
gitreins task create build "Project builds" \
  "CGO_ENABLED=0 go build ./cmd/server exits 0"

gitreins task create api-crud "CRUD endpoints" --depends-on build \
  "POST /api/users creates a user" \
  "GET /api/users lists users"

gitreins task complete api-crud
# → "Cannot complete 'api-crud' — depends on: build"

gitreins task complete build      # complete the dependency first
gitreins task complete api-crud   # now this works

# Or force-skip dependency checks:
gitreins task complete api-crud --force
```

## Configuration

Full `.gitreins/config.yaml` reference:

```yaml
# ── Global defaults ──────────────────────────────────
defaults:
  model: deepseek-v4-flash
  max_iterations: 100
  check_for_updates: true

# ── Tier 1 guards ────────────────────────────────────
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"          # "full" or "diff"
  test_command: "pytest -x --tb=short"

  # Go projects (auto-detected via go.mod):
  go:
    build: true
    lint: true
    tests: true

# ── Tier 2 evaluator caps ────────────────────────────
evaluator:
  max_iterations: 25         # LLM reasoning turns
  max_time: "5m"             # wall clock cap
  max_input_tokens: "200k"
  max_output_tokens: "50k"
  tool_call_weight: 0.1      # tool calls cost 0.1 iterations

# ── Verdict history ──────────────────────────────────
history:
  enabled: true
  storage: "git"
  max_verdicts: 1000
```

---

## Tech Stack

- **Language:** Python 3.10+
- **Dependencies:** mcp, pyyaml, requests, packaging (4 packages)
- **MCP Transport:** stdio (26 tools)
- **Config:** YAML in `.gitreins/` directory
- **Evaluator Default Model:** DeepSeek V4 Flash (~$0.01/eval)
- **Test suite:** ~1247 tests across 32 test files (parallelized with pytest-xdist)

## Architecture & Docs

| Document | What it covers |
|---|---|
| [Full Architecture](docs/architecture.md) | System design and data flow |
| [Component Map](docs/component-map.md) | Module inventory with paths and line counts |
| [Agentic Evaluator Design](docs/evaluator-loop.md) | How the evaluator loop works |
| [GitReins 2.0 Design](DESIGN_v2.md) | Evidence-driven cheap-model review ensemble (v2) |

---

## GitReins 2.0 (branch `v2`) — Review Ensemble Architecture

The `v2` branch refactors GitReins from a single-criterion evaluator into an
**evidence-driven, cheap-model review ensemble** — "GitReins + a repository
intelligence layer + cheap specialized models." The control plane (guards, task
lifecycle, MCP, verdicts) is unchanged; the review brain is new. See
[`DESIGN_v2.md`](DESIGN_v2.md) for the full spec.

### Two independent AI lanes

| | Lane A — Requirements | Lane B — Defect discovery |
|---|---|---|
| Question | Did they implement the stated criteria? | Did the patch break something? |
| Engine | `CriteriaEvaluator` (was `AgenticEvaluator`) | New review DAG (scout → evidence → reviewers → verifier) |

### New modules (R2.1–R2.6)

| Module | What it does | R2 |
|---|---|---|
| `engine/router.py` | `ModelRouter.for_role(role)` — per-role LLM client from `review.models` config | R2.1 |
| `engine/agents/` | `AgentRunner` generic bounded loop (caps, compaction, tool-dedup) + `budget.py`/`tools.py`/`schemas.py` | R2.2 |
| `engine/evaluator.py` | Re-expressed as `CriteriaEvaluator(AgentRunner)` — criteria-based Lane A | R2.3 |
| `engine/evidence/` | First-class `Evidence` type + store + provenance; analyzers become evidence producers | R2.4 |
| `engine/analyzers/` | `semgrep`/`ast-grep`/`trivy`/`gitleaks` → `Evidence` | R2.4 |
| `engine/codeintel/` | `CodeIntelProvider` Protocol + `astgrep`/`ripgrep`/`lsp`/`graph`/`serena` providers | R2.5 |
| `engine/review/scout.py` | Cheap Qwen scout → retrieval plan (symbols, requests, lenses) | R2.6 |
| `engine/review/context_builder.py` | Deterministic evidence planner executes the scout's retrieval requests | R2.6 |

### Review pipeline (in progress, R2.7+)

```
change_analysis → static_evidence (parallel analyzers) → scout → evidence_retrieval
  → reviewers (runtime/contract/security, parallel) → candidate_merge
  → verify_findings (adversarial) → rank → criteria_eval (Lane A) → publish
```

### Key behavior changes on `v2`

- **Model routing** is per-role (scout, reviewers, verifier, writer), not one
  pipeline-wide client.
- **`scan_security`** runs only against the **changed files** (git diff), not
  the whole repo — bounds the judge's cost.
- **Evidence** is a first-class type: every claim references `E<n>` ids with
  provenance, never "assertions from nowhere."
- **Code intelligence** tools return small structured answers (callers, callees,
  definitions) instead of whole files.

### v2 build progress

Completed through **R2.6** (ModelRouter, AgentRunner, CriteriaEvaluator, Evidence,
Code Intelligence, Scout + planner). R2.7+ (reviewers, verifier, ranker, writer,
ChangeSource, sandbox, intent, history, review DAG wiring) in progress on branch
`v2`. Tracked on the board at `.coding-hermes/tasks.md`.

## License

MIT
