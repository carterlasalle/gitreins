# 04-Guard-System.md — Guard System Design

> **Document Status:** Draft | **Last Updated:** 2026-07-19 | **Author:** GitReins Guard System Spec

---

## 1. Mission

Document the Tier 1 static guard system that runs on every commit. Guards are fast, deterministic, and require no LLM. They catch secrets, lint errors, test failures, dead code, and Go build issues before code reaches the remote. The GuardManager orchestrates all checks; each guard is independently configurable and can be skipped via `.gitreins/config.yaml`.

---

## 2. Scope

### In scope

- `GuardManager` class (`engine/guard_manager.py`, ~552 lines) — initialization, configuration, `run_all()` orchestration
- Secrets scanner — gitleaks integration, built-in regex fallback, danger/whitelist patterns, output sanitization
- Diff-mode test selection (`_discover_test_targets()`) — smart mapping of staged source files to test files
- Go project detection — `go.mod` presence, Python guard gating, Go-native guards (`go_build`, `go_lint`, `go_tests`)
- Lint — ruff primary, flake8 fallback, Python-only
- Tests — configurable `test_command`, pytest default, full vs diff mode
- Dead code detection — Python AST-based, opt-in (`engine/dead_code.py`, ~281 lines)
- Skylos — multi-language dead code + AI mistake detection, opt-in
- Test mode configuration — `guards.test_mode: "full" | "diff"`

### Out of scope

- Tier 2 agentic evaluator (covered in `03-Evaluator-Design.md`)
- Pipeline engine (covered in `06-Pipeline.md`)
- LLM client and cap system (covered in `03-Evaluator-Design.md`)

---

## 3. Inputs & Outputs

### Inputs

| Input | Source | Description |
|-------|--------|-------------|
| `workdir` | Constructor | Absolute path to repository root |
| `config` | Constructor | Dict from `.gitreins/config.yaml` overlay |
| Staged files | `git diff --cached` | Files to scan for secrets, lint, test mapping |
| `go.mod` | Filesystem | Triggers Go project detection |

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| `Tier1Result` | dataclass | `{passed: bool, results: [GuardResult], extra: dict}` |
| `GuardResult` | dataclass | `{name: str, passed: bool, output: str, error: str}` |

---

## 4. Operating Contract

- **Guards run in parallel where possible** — secrets, lint, and tests are independent and can execute concurrently
- **Go projects skip Python-specific guards** — when `go.mod` exists, `lint`, `tests`, and `dead_code` are gated with `and not self._is_go`
- **Secrets guard BLOCKS on failure** — no commit may proceed with potential secrets
- **Diff-mode falls back to full suite on safety triggers** — if no test files map to staged changes, the full suite runs
- **All guards are optional** — each can be disabled in `.gitreins/config.yaml`
- **Opt-in guards default to OFF** — `dead_code` and `skylos` must be explicitly enabled

---

## 5. Assumptions & Dependencies

| # | Assumption | Risk if Violated |
|---|------------|------------------|
| 1 | `git` binary available in PATH | Diff-mode and secrets scan fail |
| 2 | `gitleaks` installed (optional) | Falls back to built-in regex scanner |
| 3 | `ruff` or `flake8` installed (optional) | Lint guard skips if neither found |
| 4 | `pytest` installed (optional) | Tests guard fails if command not found |
| 5 | `go` toolchain installed (optional) | Go guards skip if `go` not in PATH |
| 6 | Repository has `tests/` directory with `test_*.py` files | Diff-mode mapping works; otherwise falls back to full suite |
| 7 | Staged files are the only files that need checking | Unstaged changes are not scanned |

---

## 6. Architecture

### 6.1 GuardManager Class

```python
class GuardManager:
    def __init__(self, workdir: str = ".", config: dict | None = None)
    def run_all(self) -> Tier1Result
```

**Constructor behavior:**
1. Resolves `workdir` to absolute path
2. Extracts `guards:` section from config (defaults to all enabled)
3. Sets `test_mode` — `"full"` (default) or `"diff"`
4. Detects Go project — checks for `go.mod` in `workdir`
5. Loads Go guard config from `guards.go:` subsection (`build`, `lint`, `tests` booleans)

**`run_all()` execution flow:**

```mermaid
flowchart TD
    A[GuardManager.run_all] --> B{secrets enabled?}
    B -->|yes| C[_check_secrets]
    B -->|no| D{lint enabled?}
    C --> D
    D -->|yes AND not is_go| E[_check_lint]
    D -->|no| F{tests enabled?}
    E --> F
    F -->|yes AND not is_go| G[_check_tests]
    F -->|no| H{dead_code enabled?}
    G --> H
    H -->|yes AND not is_go| I[_check_dead_code]
    H -->|no| J{skylos enabled?}
    I --> J
    J -->|yes| K[_check_skylos]
    J -->|no| L{is_go?}
    K --> L
    L -->|yes| M[Go guards:<br/>go_build, go_lint, go_tests]
    L -->|no| N[Compile Tier1Result]
    M --> N
    N --> O{all passed?}
    O -->|yes| P[Tier1Result passed=True]
    O -->|no| Q[Tier1Result passed=False]
```

**Guard enablement matrix:**

| Guard | Config Key | Default | Go Project Behavior |
|-------|------------|---------|---------------------|
| Secrets | `guards.secrets` | `true` | Runs on all projects |
| Lint | `guards.lint` | `true` | Skipped if `is_go` |
| Tests | `guards.tests` | `true` | Skipped if `is_go` |
| Dead Code | `guards.dead_code` | `false` | Skipped if `is_go` |
| Skylos | `guards.skylos` | `false` | Runs on all projects |
| Go Build | `guards.go.build` | `true` | Only if `is_go` |
| Go Lint | `guards.go.lint` | `true` | Only if `is_go` |
| Go Tests | `guards.go.tests` | `true` | Only if `is_go` |

### 6.2 Guard Run Flow (Parallel Checks)

```mermaid
sequenceDiagram
    autonumber
    actor Human as Human Developer
    participant Git as git
    participant Hook as .git/hooks/pre-commit
    participant GM as GuardManager
    participant Secrets as Secrets Scanner
    participant Lint as Lint (ruff/flake8)
    participant Tests as Tests (pytest)
    participant Go as Go Guards (go vet/build)

    Human->>Git: git commit -m "..."
    Git->>Hook: invoke pre-commit hook
    Hook->>GM: gitreins guard run
    
    par Parallel Checks
        GM->>Secrets: _check_secrets()
        Secrets-->>GM: GuardResult(passed, output)
        
        GM->>Lint: _check_lint()
        Lint-->>GM: GuardResult(passed, output)
        
        GM->>Tests: _check_tests(mode)
        Tests-->>GM: GuardResult(passed, output)
        
        alt Go project detected (go.mod exists)
            GM->>Go: check_go_lint()
            Go-->>GM: GoGuardResult
            GM->>Go: check_go_tests()
            Go-->>GM: GoGuardResult
            GM->>Go: check_go_build()
            Go-->>GM: GoGuardResult
        end
    end
    
    GM-->>Hook: Tier1Result(passed, results[], summary)
    
    alt All passed
        Hook-->>Git: exit 0
        Git-->>Human: Commit allowed
    else Any failed
        Hook-->>Git: exit 1
        Git-->>Human: Commit blocked<br/>(show failure summary)
    end
```

---

## 7. Core Design Decisions

### Decision 1: Go Project Gating

When `go.mod` exists in the repo root, Python-specific guards (`lint`, `tests`, `dead_code`) are skipped entirely. Go-native guards (`go_build`, `go_lint`, `go_tests`) are added instead. This prevents false positives from Python tools running on Go repositories.

**Rationale:** A Go project may contain Python scripts (e.g., build tools), but the primary language is Go. Running `ruff` on a Go repo is meaningless. The `go.mod` check is a simple, reliable heuristic.

**Trade-off:** Mixed-language projects (Go + Python in the same repo) lose Python guard coverage. Users can work around this by disabling Go detection or running guards manually.

### Decision 2: Diff-Mode Test Selection

When `test_mode: "diff"`, GitReins maps staged source files to test files using basename matching. This avoids running the full test suite on every commit, dramatically speeding up the commit cycle for large projects.

**Rationale:** In a 384-test project, running the full suite on every commit is wasteful. Most commits touch only a few files. Smart test selection reduces feedback time from minutes to seconds.

**Trade-off:** The mapping is heuristic-based. Edge cases (cross-package dependencies, integration tests) may be missed. Force-full triggers and the safety fallback mitigate this.

### Decision 3: Secrets Output Sanitization

When the built-in secrets scanner finds a potential secret, it sanitizes the output by replacing the actual value with `"***"`. This prevents the secret from appearing in guard output, logs, or CI artifacts.

**Rationale:** A secrets scanner that leaks the secret in its own output defeats its purpose. Sanitization ensures the secret is caught without exposing it further.

**Trade-off:** Users cannot see the exact value that triggered the scan, which may make debugging false positives harder. The file:line reference is preserved for locating the issue.

### Decision 4: Opt-In for Dead Code and Skylos

Dead code detection and Skylos scanning are disabled by default. They must be explicitly enabled in `.gitreins/config.yaml`.

**Rationale:** Dead code detection can be noisy (false positives on framework hooks, test fixtures, intentionally unused functions). Skylos requires a separate `pip install`. Making them opt-in respects user preference and keeps the default guard run fast.

**Trade-off:** Users who would benefit from these guards may not know they exist. Documentation and the install command's generated config include commented examples.

---

## 8. Detailed Design

### 8.1 GuardManager Class

**File:** `engine/guard_manager.py` (~552 lines)

**Constructor:**

```python
GuardManager(workdir: str = ".", config: dict | None = None)
```

**State fields:**

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `workdir` | `str` | Constructor arg | Absolute path to repo root |
| `config` | `dict` | Constructor arg | Full config dict (not just guards section) |
| `_enabled` | `dict[str, bool]` | `config.guards` | Per-guard enable flags |
| `_test_mode` | `str` | `config.guards.test_mode` | `"full"` or `"diff"` |
| `_is_go` | `bool` | `os.path.isfile(go.mod)` | True if Go project detected |
| `_go_guards` | `dict` | `config.guards.go` | Go guard enable flags |

### 8.2 Secrets Scanner

**Two-tier approach:** gitleaks first, built-in regex fallback.

**Tier 1: gitleaks**

```bash
gitleaks detect --source <workdir> --no-git --verbose
```

- Timeout: 30 seconds
- If `gitleaks` not found → fallback to built-in scanner
- If `gitleaks` fails (non-zero exit) → returns failure output

**Tier 2: Built-in regex scanner**

Runs over staged files only (not the entire repo). Skips files > 1MB.

**Danger patterns (high-confidence secret detection):**

| Pattern | Description | Example Match |
|---------|-------------|---------------|
| `api[_-]?key\s*[:=]\s*["\'][A-Za-z0-9_\-]{20,}["\']` | Hardcoded API key | `api_key = "abc123..."` |
| `-----BEGIN (RSA\|DSA\|EC\|OPENSSH\|PGP) PRIVATE KEY` | Private key block | PEM-encoded key |
| `ghp_[A-Za-z0-9]{36,}` | GitHub PAT | `ghp_xxxxxxxx...` |
| `gho_[A-Za-z0-9]{36,}` | GitHub OAuth token | `gho_xxxxxxxx...` |
| `glpat-[A-Za-z0-9_\-]{20,}` | GitLab PAT | `glpat-xxxxxxxx...` |
| `sk-[A-Za-z0-9_\-]{32,}` | OpenAI/OpenRouter key | `sk-or-v1-...` |
| `AKIA[0-9A-Z]{16}` | AWS access key | `AKIAIOSFODNN7EXAMPLE` |
| `token\s*[:=]\s*["\']eyJ[A-Za-z0-9_\-]{20,}\..*\..*` | Hardcoded JWT | `token = "eyJhbG..."` |
| `password\s*[:=]\s*["\'][^"\'$]{8,}["\']` | Hardcoded password | `password = "secret123"` |
| `secret\s*[:=]\s*["\'][A-Za-z0-9+/=]{32,}["\']` | Hardcoded secret | `secret = "base64..."` |

**Whitelist patterns (false positive suppression):**

| Pattern | Rationale |
|---------|-----------|
| `os.getenv`, `os.environ`, `getenv`, `environ[` | Environment variable loading |
| `request.form`, `request.args` | Form field access |
| `config[`, `settings[` | Config dictionary access |
| `${VAR}` | Shell variable substitution |
| `{{...}}` | Template variables |
| `PASSWORD = ""` | Empty password placeholder |
| `EXAMPLE`, `PLACEHOLDER`, `TODO`, `FIXME`, `xxx+` | Intentional placeholders |
| `jwt.encode`, `jwt.decode`, `b64encode` | JWT construction, not hardcoded |
| `generate`, `random`, `uuid`, `hash` | Generated values |

**File skip rules:**

| Skip Rule | Rationale |
|-----------|-----------|
| `.md` files | Documentation contains example keys |
| `.memory-bank/` | Memory bank files contain examples |
| `docs/` | Documentation directory |
| `CONTRIBUTING.md` | Contribution guidelines with examples |
| `SECURITY.md` | Security policy with examples |
| Files > 1MB | Performance protection |

**Output sanitization:**

```python
sanitized = re.sub(r'["\'][^"\']{6,}["\']', '"***"', line)
```

The actual secret value is replaced with `"***"` in the guard output. The file path, line number, and pattern label are preserved.

### 8.3 Diff-Mode Test Selection

**Algorithm:** `_discover_test_targets(workdir) → list[str] | None`

```mermaid
flowchart TD
    A[_discover_test_targets] --> B[Get staged files via git diff --cached]
    B --> C{Any staged files?}
    C -->|no| D[Return None → full suite]
    C -->|yes| E[Check force-full triggers]
    E --> F{Any force-full file changed?}
    F -->|yes| D
    F -->|no| G[Map each source file to test file]
    G --> H{Any test files found?}
    H -->|no| D
    H -->|yes| I[Return sorted test file paths]
```

**Force-full triggers** (when changed, run the entire test suite):

| Glob | Rationale |
|------|-----------|
| `pyproject.toml` | Dependency or config changes affect everything |
| `setup.cfg` | Package configuration changes |
| `setup.py` | Build configuration changes |
| `conftest.py` | Pytest fixtures shared across tests |
| `.gitreins/config.yaml` | Guard config changes may affect test behavior |
| `.github/workflows/*.yml` | CI workflow changes |
| `Makefile` | Build command changes |

**Source → test file mapping:**

| Source Pattern | Test File | Example |
|----------------|-----------|---------|
| `engine/foo.py` | `tests/test_foo.py` | `engine/guard_manager.py` → `tests/test_guard_manager.py` |
| `gitreins/bar.py` | `tests/test_bar.py` | `gitreins/cli.py` → `tests/test_cli.py` |
| `gitreins_mcp/server.py` | `tests/test_mcp_server.py` | Special case for MCP package |
| `test_*.py` (itself) | Itself | Test files are always included |

**Safety fallback:** If no test files map to the staged sources, `None` is returned and the full suite runs. This prevents silently skipping tests when the mapping fails.

**Test command building:**

If the test command starts with `pytest`, the discovered test file paths are appended:

```bash
pytest -x --tb=short tests/test_foo.py tests/test_bar.py
```

For non-pytest runners, the original command is used unchanged (custom runners cannot be safely narrowed).

### 8.4 Go Project Detection

**Detection:** `os.path.isfile(os.path.join(workdir, "go.mod"))`

**Guard gating:**

```python
if self._enabled["lint"] and not self._is_go:
    results.append(self._check_lint())

if self._enabled["tests"] and not self._is_go:
    results.append(self._check_tests())

if self._enabled["dead_code"] and not self._is_go:
    results.append(self._check_dead_code())

if self._is_go:
    if self._go_guards.get("build", True):
        results.append(self._check_go_build())
    if self._go_guards.get("lint", True):
        results.append(self._check_go_lint())
    if self._go_guards.get("tests", True):
        results.append(self._check_go_tests())
```

**Go guard implementations** (`engine/guards.py`, ~113 lines):

| Guard | Implementation | Fallback |
|-------|---------------|----------|
| `go_lint` | `golangci-lint run --new-from-rev=HEAD~1` | `go vet ./...` |
| `go_tests` | `go test -count=1 -short ./...` | None |
| `go_build` | `go build ./...` | None |

All Go guards operate on staged Go files only (via `git diff --cached --name-only --diff-filter=ACM`). If no Go files are staged, the guard returns `passed=True` with a "No Go files staged" message.

### 8.5 Lint Guard

**Primary:** `ruff check <staged_py_files>`
**Fallback:** `flake8 <staged_py_files>`

If neither linter is installed, returns `passed=True` with "No linter found — skipped".

Only runs on staged Python files. If no Python files are staged, returns `passed=True` with "No Python files staged".

Output is capped at 2,000 characters with `[truncated]` notice.

### 8.6 Tests Guard

**Configuration:**

```yaml
guards:
  tests: true
  test_mode: "full"      # "full" or "diff"
  test_command: "pytest -x --tb=short"
```

**Full mode:** Runs `test_command` directly.

**Diff mode:**
1. Calls `_discover_test_targets(workdir)`
2. If targets found → builds narrowed command with test file paths
3. If targets is `None` → falls through to full suite

**Execution:**

```python
subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
```

Output capped at 2,000 characters (keeps last 2,000 for failure context). Timeout: 120 seconds.

### 8.7 Dead Code Guard

**File:** `engine/dead_code.py` (~281 lines)

**Opt-in:** `guards.dead_code: true` (default: `false`)

**Detection categories:**

| Category | Detection Method | Example |
|----------|------------------|---------|
| `unreachable` | AST: statements after `return`/`raise`/`break`/`continue` | Code after `return` in a function |
| `unused_function` | Cross-file call graph analysis | Function defined but never called |
| `unused_import` | Import vs. name usage comparison | `import os` but `os` never referenced |
| `empty_function` | AST: body is `pass` or `...` only | `def foo(): pass` |

**Whitelist:**

- Dunder methods (`__init__`, `__repr__`, `__eq__`, etc.)
- Test framework hooks (`setUp`, `tearDown`, `setUpClass`, `tearDownClass`)
- Decorated functions (`@property`, `@staticmethod`, `@classmethod`, `@pytest.fixture`)
- Common entry points (`main`, `run`, `handle`, `process`, `execute`, `dispatch`)
- Private functions (starting with `_`) — excluded from unused function detection by design
- Test functions (starting with `test_`) — called by pytest, not user code

**Scan scope:** All Python files in the project (excluding `.git`, `__pycache__`, `.venv`, `venv`, `node_modules`, `.tox`, `.eggs`, `build`, `dist`, `.pytest_cache`, `.gitreins`).

### 8.8 Skylos Guard

**Opt-in:** `guards.skylos: true` (default: `false`)

**Requires:** `pip install skylos`

**Command:**

```bash
skylos <workdir> --format json --no-grep-verify
```

**Detection categories:**

| Category | Description |
|----------|-------------|
| `unused_functions` | Functions defined but never called |
| `unused_imports` | Modules imported but never referenced |
| `unused_classes` | Classes defined but never instantiated |
| `unused_variables` | Variables assigned but never read |
| `unused_parameters` | Function parameters never used |
| `dead_code` | Unreachable code blocks |
| `ai_mistakes` | Patterns common in AI-generated code |

**Languages:** Python, TypeScript/JavaScript, Go, Java, PHP, Rust, Dart, C#

Timeout: 120 seconds. If `skylos` is not installed, returns `passed=True` with installation instructions.

### 8.9 Test Mode Configuration

**Config key:** `guards.test_mode`

| Value | Behavior | Use Case |
|-------|----------|----------|
| `"full"` (default) | Run entire test suite on every commit | Small projects, CI-like safety |
| `"diff"` | Run only tests related to staged changes | Large projects, fast feedback |

**Backward compatibility:** Default is `"full"` to ensure existing users are not surprised by skipped tests. Users must explicitly opt into `"diff"` mode.

**Diff mode metadata:** When `test_mode == "diff"`, the `Tier1Result.extra` dict includes:

```python
{
    "test_mode": "diff",
    "test_targets": 3,  # Number of test files targeted
    "staged_count": 5,  # Number of staged source files
}
# Or if full suite triggered:
{
    "test_mode": "diff",
    "test_targets": None,  # Full suite triggered
}
```

---

### 8.10 LSP Guard (GR-050–GR-053, GR-063)

**Opt-in:** `guards.lsp: true` (default: `false`)

The LSP guard runs real language servers against staged files and reports diagnostics (undefined variables, type errors, unused imports, etc.). Multi-language support was added in GR-063.

**Configuration:**

```yaml
guards:
  lsp: true
  lsp_tools: [pylsp, gopls, rust-analyzer, typescript-language-server, clangd, jdtls, kotlin-language-server, csharp-ls, sourcekit-lsp, dart, elixir-ls, metals, ruby-lsp]
```

**Supported languages (14 total):**

| Language | LSP Server | File Extensions | Notes |
|----------|-----------|-----------------|-------|
| Python | pylsp (`python-lsp-server`) | `.py` | Requires pyflakes + pycodestyle for diagnostics |
| Go | gopls | `.go` | Requires `go mod init` for module resolution |
| Rust | rust-analyzer | `.rs` | Requires Cargo.toml for type checking |
| TypeScript/JavaScript | typescript-language-server | `.ts`, `.tsx`, `.js`, `.jsx` | |
| C/C++ | clangd | `.c`, `.cpp`, `.h`, `.hpp`, `.cc`, `.cxx` | Requires compile_commands.json for C++ |
| Java | jdtls | `.java` | |
| Kotlin | kotlin-language-server | `.kt`, `.kts` | |
| C# | csharp-ls / omnisharp-roslyn | `.cs` | Requires .csproj or .sln |
| Swift | sourcekit-lsp | `.swift` | macOS only (native tool) |
| Dart | dart | `.dart` | Requires pubspec.yaml |
| Elixir | elixir-ls | `.ex`, `.exs` | Requires mix.exs |
| Scala | metals | `.scala`, `.sc` | Requires build.sbt |
| Ruby | ruby-lsp / solargraph | `.rb` | |

**LSP integration tests:** 34+ tests covering tool discovery, language mapping, graceful skip when tools not installed, and end-to-end diagnostic collection.

**Architecture:** `engine/lsp.py` (~430 lines) — server spawning, stdin/stdout communication, OS-level pipe reading, absolute deadline-based timeout with per-file budget allocation.

**Process group isolation (GR-061):** LSP servers are killed via process group (`os.killpg()`) with PID validation — rejects non-integer/bool/pid≤1 values. Guards against init-kill bug.

### 8.11 Static Analysis Guard (GR-063)

**Opt-in:** `guards.static_analysis: true` (default: `false`)

Runs language-specific static analysis tools against staged files and collects structured diagnostics.

**Supported tools:**

| Language | Tool | Output Format |
|----------|------|---------------|
| Python | mypy, pyright | JSON |
| Go | staticcheck | Text (line regex) |
| C/C++ | cppcheck, clang-tidy | Text/XML |
| TypeScript/JS | eslint | JSON |
| Ruby | sorbet | Text |
| PHP | phpstan | Text |
| SQL | sqlfluff | Text |

**Tool configuration:** `guards.static_analysis_tools: [mypy, staticcheck, eslint, ...]`

All static analysis runs with `on_fail: continue` — surfaces issues without blocking commits. Diagnostics feed into Tier 2 evaluator for context-enhanced judging.

**Multi-language detection:** `engine/static_analysis.py` (~578 lines) — auto-detects language from file extensions, discovers available tools, and runs appropriate analyzers per staged file.

---

## 9. Error Taxonomy

| Error | Cause | Guard Behavior |
|-------|-------|----------------|
| `gitleaks not found` | Binary not in PATH | Falls back to built-in scanner |
| `ruff not found` | Binary not in PATH | Falls back to `flake8` |
| `flake8 not found` | Binary not in PATH | Returns "No linter found — skipped" |
| `pytest not found` | Binary not in PATH | Test command fails, guard reports failure |
| `go not found` | Binary not in PATH | Go guards return failure |
| `golangci-lint not found` | Binary not in PATH | Falls back to `go vet` |
| `skylos not found` | Binary not in PATH | Returns installation instructions |
| `No staged files` | Nothing staged for commit | Guard passes (nothing to check) |
| `Tests timed out` | Test suite > 120s | Guard fails with timeout message |
| `Secrets scan failed` | Exception during scan | Guard fails with error details |

---

## 10. Verification Checklist

| # | Check | Verification Method |
|---|-------|---------------------|
| 1 | GuardManager runs all enabled guards | `python -m pytest tests/test_guard_manager.py -v` |
| 2 | Secrets scanner catches hardcoded API keys | Add fake key to staged file, run `gitreins guard` |
| 3 | Secrets scanner ignores whitelisted patterns | Stage file with `os.getenv("API_KEY")`, verify pass |
| 4 | Diff-mode maps source → test files | Stage `engine/foo.py`, verify `tests/test_foo.py` targeted |
| 5 | Force-full triggers run full suite | Stage `pyproject.toml`, verify full suite runs |
| 6 | Go detection skips Python guards | Create `go.mod`, run guard, verify no ruff/pytest |
| 7 | Go guards run on Go projects | Stage `.go` file, verify `go vet` / `go test` run |
| 8 | Dead code detection finds unused imports | Stage file with unused import, verify detection |
| 9 | Skylos runs when enabled | Enable `guards.skylos: true`, verify `skylos` command executed |
| 10 | Test mode defaults to "full" | Fresh install, verify `guards.test_mode: full` in config |

---

## 11. Document Status

| Field | Value |
|-------|-------|
| **Version** | v0.6.0 |
| **Status** | Draft — specification only |
| **Last updated** | 2026-07-19 |
| **Author** | totalwindupflightsystems <totalwindupflightsystems@gmail.com> |
| **Co-author** | wojons <wojonstech@gmail.com> |

---

*End of 04-Guard-System.md*
