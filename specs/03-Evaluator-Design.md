# 03-Evaluator-Design.md — Evaluator & Cap System

## 1. Mission

Document the agentic evaluator loop and the EvalCap budget system that together form the Tier 2 quality gate of GitReins. The evaluator is an LLM-powered agent that iteratively inspects code, runs tests, and searches the codebase until it has sufficient evidence to deliver a structured verdict on whether a task's criteria are met. The EvalCap system places configurable resource limits on this loop to prevent runaway consumption.

## 2. Scope

This specification covers:

- The `AgenticEvaluator` class (`engine/evaluator.py`) — its architecture, the iterative loop, tool definitions, and verdict delivery
- The 7 (plus 2 optional) evaluator tools exposed to the LLM — exact signatures, behavior, and return schemas
- The `EvalCap` dataclass (`engine/eval_cap.py`) — budget tracking, cap checking rules, and the priority resolution chain
- Deduplication tracking to prevent the LLM from repeating work
- Verdict parsing with a 3-strategy fallback chain
- Backward compatibility with legacy cap formats
- Cache token tracking (DeepSeek disk cache)

Out of scope: the LLM client itself (`engine/llm.py`), the guard manager (`engine/guard_manager.py`), the judge orchestrator (`engine/judge.py`), and the pipeline engine (`engine/pipeline.py`).

## 3. Inputs & Outputs

### Inputs

| Input | Source | Description |
|-------|--------|-------------|
| `task` dict | Task Manager | `{id, title, criteria: [...]}` — the task to evaluate |
| `llm` | LLMClient | Multi-provider LLM client (OpenAI-compatible + Anthropic native) |
| `workdir` | Constructor | Absolute path to the repository root |
| `eval_cap` | Constructor / Config / MCP | Resource budget (EvalCap object, string, or config-derived) |
| `max_iterations` | Constructor (legacy) | Simple integer iteration cap (backward compat) |

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| `Verdict` | dataclass | `{verdict: "COMPLETE"|"INCOMPLETE", items: [VerdictItem], summary: str}` |
| `VerdictItem` | dataclass | `{criterion: str, status: "PASS"|"FAIL", detail: str}` |

## 4. Operating Contract

### 4.1 What the Evaluator Does

1. Receives a task with criteria
2. Builds a system prompt + task prompt instructing the LLM to verify every criterion
3. Runs an iterative loop: LLM reasons → optionally calls tools → results appended → LLM reasons again
4. When the LLM stops calling tools, parses its response as a JSON verdict
5. If the loop hits iteration/time/token caps, returns `INCOMPLETE` with a summary explaining which cap was exceeded
6. Tracks deduplication to warn the LLM against re-reading files, re-running commands, or re-searching patterns

### 4.2 What the Evaluator Does NOT Do

- It does NOT write code, modify files, or make git commits
- It does NOT run the full test suite automatically — it only runs commands the LLM explicitly requests via `run_command`
- It does NOT evaluate code quality ("is this well-written?") — it evaluates criterion satisfaction ("does this code do what the criterion demands?")
- It does NOT persist evaluation history — the caller (judge orchestrator) handles that
- It does NOT enforce caps retroactively — caps are checked BEFORE each LLM call (lenient for iterations, hard for time/tokens)

### 4.3 Success Criteria

- Every task criterion receives a PASS or FAIL item in the verdict
- Verdict parsing succeeds even when the LLM wraps JSON in markdown fences or adds extra text
- Cap enforcement prevents runaway loops without false positives (lenient iteration check allows one final call at the boundary)
- Deduplication warnings reduce redundant tool calls by >50% in typical evaluations
- All legacy cap formats parse correctly

## 5. Assumptions & Dependencies

| # | Assumption | Risk if Violated |
|---|------------|------------------|
| 1 | LLM supports function calling / tool use | Evaluator cannot operate; falls back to INCOMPLETE |
| 2 | Repository is accessible at `workdir` | All file/command tools fail |
| 3 | `git` binary is available in PATH | `read_diff()` tool fails |
| 4 | LLM follows the system prompt's JSON format instruction | Verdict parsing falls back to keyword heuristic |
| 5 | Task criteria are specific and verifiable | LLM may deliver vague or unhelpful verdicts |
| 6 | `subprocess.run(shell=True)` is acceptable for the target environment | Security: commands run with shell expansion; only the LLM decides what to run |
| 7 | DeepSeek cache tokens are reported in the LLM response's usage field | Cache tracking is inaccurate; may undercount actual cost |

## 6. Architecture

### 6.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    AgenticEvaluator                           │
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐  │
│  │ System Prompt│    │ Task Prompt │    │ Message History │  │
│  └─────────────┘    └─────────────┘    └─────────────────┘  │
│         │                  │                  │               │
│         └──────────────────┴──────────────────┘               │
│                            │                                │
│                    ┌───────┴───────┐                        │
│                    │   LLM Call    │                        │
│                    │  (costs 1.0)  │                        │
│                    └───────┬───────┘                        │
│                            │                                │
│              ┌─────────────┴─────────────┐                  │
│              ▼                           ▼                  │
│    ┌─────────────────┐        ┌─────────────────┐           │
│    │  No tool calls  │        │  Tool calls     │           │
│    │  → Parse verdict│        │  → Execute each │           │
│    │  → Return       │        │  → Append results│          │
│    └─────────────────┘        │  → Loop back      │           │
│                               └─────────────────┘           │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐│
│  │ read_file   │  │ run_command │  │ search_pattern        ││
│  │ read_diff   │  │ get_task_item│  │ sandbox_write/read  ││
│  │ detect_dead_code│  │ skylos_scan │  │ (optional)           ││
│  └─────────────┘  └─────────────┘  └─────────────────────┘│
│                                                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ EvalCap — budget tracking (iterations, time, tokens)   ││
│  │  • record_llm_call() → +1.0 iterations                  ││
│  │  • record_tool_call() → +tool_call_weight iterations  ││
│  │  • check() → hard stop for time/token caps            ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### 6.2 State Lifecycle

```
1. evaluate(task) called
   → _sandbox.clear(), _files_read.clear(), _commands_run.clear(), _searches_done.clear()
   → _task_index[task.id] = task
   → Build task prompt with numbered criteria
   → messages = [system, user(task_prompt)]
   → eval_cap.start() — begin wall-clock timer

2. Loop (for iteration in range(iter_limit)):
   a. eval_cap.check() → hard caps (time/tokens) → INCOMPLETE if exceeded
   b. LLM call → response (content + tool_calls + usage)
   c. eval_cap.record_llm_call(usage) → +1.0 iterations, check caps
   d. No tool_calls? → _parse_verdict(response.content) → return Verdict
   e. Has tool_calls? → execute each, record_tool_call() per tool → append to messages
   f. Loop back to (a)

3. Iteration cap hit → return INCOMPLETE with cap summary
```

## 7. Core Design Decisions

### Decision 1: Lenient Iteration Check (Checked BEFORE Call)

The iteration cap is checked BEFORE each LLM call, not after. This means at 99.9/100 iterations, a full 1.0-cost LLM call is still allowed, bringing the total to 100.9. This prevents the evaluator from being one call short of a verdict when the cap is nearly exhausted.

**Rationale:** The iteration cap is a budget, not a hard wall. The LLM needs a final reasoning turn to synthesize evidence into a verdict. Time and token caps are hard stops because they represent real resource consumption.

**Trade-off:** Slightly over the configured cap in edge cases. Acceptable because the alternative is an INCOMPLETE verdict due to cap exhaustion rather than actual evidence.

### Decision 2: Fractional Tool-Call Weighting

Tool calls cost `tool_call_weight` (default 0.1) iterations, while LLM reasoning turns cost 1.0. This reflects the real cost ratio: a tool call is cheap (local file read, grep, shell command) compared to an LLM API call.

**Rationale:** Without fractional weighting, the evaluator would burn through its iteration budget on cheap local operations, leaving no room for the LLM to reason about results. With 0.1 weighting, a 100-iteration budget supports ~10 tool calls + ~90 reasoning turns, or ~50 tool calls + ~50 reasoning turns.

**Trade-off:** The weight is a heuristic. A `run_command` that triggers a full test suite is more expensive than a `read_file`. The default 0.1 works well for typical usage; users can adjust via `tool_call_weight` config.

### Decision 3: 3-Strategy Verdict Parsing

LLMs often fail to output pure JSON even when instructed. The parser uses three strategies in order:

1. **Markdown fence stripping** — remove ` ```json ... ``` ` wrappers
2. **JSON boundary extraction** — find first `{` and last `}`, parse the substring
3. **Keyword fallback** — search for `"complete"` or `"all criteria" + "pass"` in raw text

**Rationale:** Strict JSON parsing would cause frequent INCOMPLETE verdicts due to formatting issues, not actual criterion failures. The fallback chain maximizes verdict extraction success.

**Trade-off:** Keyword fallback may misclassify. The parser defaults to INCOMPLETE when uncertain, which is the safe direction (false negative, not false positive).

### Decision 4: Deduplication Warnings (Not Hard Blocks)

When the LLM repeats a tool call, the tool still executes but a `_dedup_warning` field is injected into the result. The system prompt also instructs the LLM not to repeat.

**Rationale:** Hard-blocking dedup could prevent legitimate re-reads (e.g., reading a file at different offsets). Warning-based dedup gives the LLM the information it needs to self-correct without being overly restrictive.

**Trade-off:** Some redundant calls still occur. The warning is usually sufficient for well-instructed models.

### Decision 5: Cap Priority Chain (9 Levels)

Caps resolve through a 9-level priority chain, from most specific to most general:

1. MCP individual params (`judge.evaluate(id, max_iterations=50, ...)`)
2. MCP legacy `eval_cap` string param
3. `GITREINS_EVAL_CAP` environment variable
4. `.gitreins/config.yaml` `evaluator.*` individual keys
5. `config.yaml` `evaluator.cap` legacy combined string
6. `config.yaml` `defaults:` section
7. `max_iterations` kwarg to `AgenticEvaluator()` constructor
8. `GitReinsDefaults` hardcoded defaults
9. Ultimate fallback: 100 iterations

**Rationale:** More specific contexts override more general ones. An MCP tool call from a primary agent should be able to override repo defaults. Environment variables allow CI/CD to set caps without modifying repo files.

**Trade-off:** Complex priority chain can be surprising. Documented clearly in the spec and code.

## 8. Detailed Design

### 8.1 AgenticEvaluator Class

**File:** `engine/evaluator.py` (~725 lines)

**Constructor:**

```python
AgenticEvaluator(
    llm: LLMClient,
    workdir: str = ".",
    max_iterations: int | None = None,      # legacy
    eval_cap: str | EvalCap | None = None,  # primary
)
```

**Cap resolution (in constructor):**

```
if eval_cap is EvalCap → use directly
elif eval_cap is str → parse_eval_cap(eval_cap)
elif max_iterations is not None and > 0 → EvalCap(max_iterations=..., source="max_iterations=...")
else → read .gitreins/config.yaml → eval_cap_from_config(config)
```

**Primary method:**

```python
def evaluate(self, task: dict) -> Verdict
```

**State fields (reset per evaluation):**

| Field | Type | Purpose |
|-------|------|---------|
| `_sandbox` | `dict[str, str]` | In-memory scratch space for the LLM |
| `_task_index` | `dict[str, dict]` | Registered tasks (typically one per evaluation) |
| `_files_read` | `set[str]` | Paths already read via `read_file` |
| `_commands_run` | `set[str]` | Commands already executed via `run_command` |
| `_searches_done` | `set[str]` | Regex patterns already searched via `search_pattern` |

### 8.2 The Loop (Step-by-Step)

**Step 1 — Load and Reset:**
- Clear all state sets and sandbox
- Register the task in `_task_index`
- Build the task prompt with numbered criteria
- Initialize messages: `[system, user(task_prompt)]`
- Call `eval_cap.start()` to begin wall-clock timer

**Step 2 — Determine Iteration Limit:**
- `iter_limit = eval_cap.max_iterations_int` (converts -1/unlimited to safety max of 10,000)

**Step 3 — Iteration Loop:**

For each iteration `i` in `range(iter_limit)`:

a. **Pre-call cap check:** `eval_cap.check()` → checks time and token hard caps. If exceeded, return `INCOMPLETE` with cap message.

b. **LLM call:** `llm.chat(messages, tools=EVALUATOR_TOOLS)` → returns `response` with `.content`, `.tool_calls`, `.usage`

c. **Record LLM cost:** `eval_cap.record_llm_call(prompt_tokens, completion_tokens, cache_read, cache_write)` → adds 1.0 to `iteration_credit`, accumulates tokens. If cap exceeded, return INCOMPLETE.

d. **No tool calls?** → LLM is delivering verdict. Call `_parse_verdict(response.content)` → return `Verdict`.

e. **Has tool calls?** → For each `ToolCall`:
   - Execute via `_execute_tool_with_dedup(tc)` → returns `(result_dict, was_duplicate)`
   - `eval_cap.record_tool_call()` → adds `tool_call_weight` (default 0.1) to `iteration_credit`
   - If `was_duplicate`, inject `_dedup_warning` into result dict
   - Append assistant message with tool calls to `messages`
   - Append tool result messages to `messages`

f. **Loop back to (a)**

**Step 4 — Cap Exhaustion:**
- If the loop exits without a verdict (iteration limit reached), return `INCOMPLETE` with a summary showing current cap usage and suggesting to increase caps or split criteria.

### 8.3 The 7 Evaluator Tools

All tools are defined as OpenAI function-calling schema in `EVALUATOR_TOOLS` list. The LLM receives these definitions and can call any combination in a single turn.

#### Tool 1: `read_file(path, offset?, limit?)`

**Purpose:** Read any file in the working tree with optional line-range support.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | yes | Path relative to repo root |
| `offset` | integer | no | Start line (1-indexed). 0 = from beginning. |
| `limit` | integer | no | Max lines to return. 0 = no limit. |

**Behavior:**
- Path safety: `os.path.realpath` check rejects paths escaping the working tree
- Large file handling: If no offset/limit requested and total chars > 12,000, auto-truncates to first 400 lines with a notice showing `total_lines` and `total_chars`
- Offset validation: If `offset > total_lines`, returns error

**Return schema:**

```json
{
  "path": "src/routes.py",
  "content": "...",
  "total_lines": 340,
  "total_chars": 12300,
  "shown_lines": 50,
  "has_more": true
}
```

**Error returns:**

```json
{"error": "File not found: missing.py"}
{"error": "Path outside working tree: ../../etc/passwd"}
{"error": "Offset 500 exceeds file length (340 lines)", "path": "src/routes.py", "total_lines": 340}
```

**Dedup tracking:** Tracked in `_files_read` by `path` string. Repeated reads get `_dedup_warning`.

---

#### Tool 2: `run_command(cmd)`

**Purpose:** Run a shell command (tests, lint, build verification).

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `cmd` | string | yes | Shell command to execute |

**Behavior:**
- Executed via `subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=workdir)`
- Output capped at 4,000 chars; `[truncated]` notice appended if exceeded
- Returns both stdout and stderr combined in `output`

**Return schema:**

```json
{"cmd": "pytest tests/", "exit_code": 0, "output": "===== 5 passed in 0.45s ====="}
{"cmd": "pytest tests/", "exit_code": 1, "output": "FAILED test_auth.py::test_login ... AssertionError"}
{"cmd": "sleep 60", "error": "Command timed out after 30s"}
```

**Dedup tracking:** Tracked in `_commands_run` by `cmd` string. Repeated commands get `_dedup_warning`.

---

#### Tool 3: `search_pattern(regex, file_glob?)`

**Purpose:** Grep the codebase for a Python regex pattern.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `regex` | string | yes | Python regex pattern |
| `file_glob` | string | no | `fnmatch` filter (e.g., `"*.py"`) |

**Behavior:**
- Uses `os.walk` over the working tree
- Skip directories: `.git`, `venv`, `.venv`, `node_modules`, `__pycache__`, `.gitreins-sandbox`, `.pytest_cache`, and any hidden directories (starting with `.`)
- Skip files > 500KB
- 200-match cap: results truncated at 200 matches with `[truncated]` notice
- Regex compiled with `re.compile(regex)` — invalid regex returns error

**Return schema:**

```json
{
  "regex": "def handle_",
  "matches": [
    "src/handlers.py:12: def handle_login():",
    "src/handlers.py:45: def handle_logout():"
  ],
  "count": 2
}
```

**Error returns:**

```json
{"regex": "(invalid", "error": "Invalid regex: (invalid"}
```

**Dedup tracking:** Tracked in `_searches_done` by `regex` string. Repeated searches get `_dedup_warning`.

---

#### Tool 4: `read_diff()`

**Purpose:** Show staged and unstaged git diff summaries.

**Parameters:** None.

**Behavior:**
- Runs `git diff --cached --stat` (staged) and `git diff --stat` (unstaged)
- 10-second timeout per command

**Return schema:**

```json
{
  "staged": "src/routes.py | 3 ++-\n1 file changed, 2 insertions(+), 1 deletion(-)",
  "unstaged": "(no unstaged changes)"
}
```

**Dedup tracking:** Not tracked (idempotent and cheap).

---

#### Tool 5: `get_task_item(id)`

**Purpose:** Fetch the full task definition including all criteria.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Task ID to fetch |

**Behavior:**
- Looks up in `_task_index` (registered at the start of `evaluate()`)

**Return schema:**

```json
{
  "id": "task-42",
  "title": "Add login endpoint",
  "criteria": [
    "POST /login returns 200 on valid credentials",
    "POST /login returns 401 on invalid credentials",
    "Password stored as bcrypt hash"
  ]
}
```

**Dedup tracking:** Not tracked (idempotent and cheap).

---

#### Tool 6: `sandbox_write(key, content)`

**Purpose:** Write to the evaluator's in-memory scratch space.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | yes | Storage key |
| `content` | string | yes | Content to store |

**Behavior:**
- Writes to `self._sandbox[key] = content`
- Cleared at the start of every `evaluate()` call

**Return schema:**

```json
{"key": "checked-ready", "written": 5}
```

**Dedup tracking:** Not tracked (intentionally overwritable).

---

#### Tool 7: `sandbox_read(key)`

**Purpose:** Read from the evaluator's in-memory scratch space.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `key` | string | yes | Key to read |

**Behavior:**
- Values > 4,000 chars are truncated with `[truncated]` notice

**Return schema:**

```json
{"key": "checked-ready", "content": "... evidence ..."}
```

**Error returns:**

```json
{"error": "Key not found: checked-ready"}
```

**Dedup tracking:** Not tracked (intentionally re-readable).

---

#### Optional Tools (8-9)

These tools are defined in `EVALUATOR_TOOLS` but are opt-in depending on environment:

**Tool 8: `detect_dead_code()`**
- Runs Python AST-based dead code detection via `engine.dead_code.DeadCodeDetector`
- Returns per-file findings with line numbers, categorized by issue type
- Returns `{"error": "Dead code detector unavailable"}` if import fails

**Tool 9: `skylos_scan()`**
- Runs external `skylos` CLI for multi-language dead code and AI-mistake detection
- Returns grade, total findings, and categorized findings (unused functions, imports, dead symbols)
- Returns `{"error": "skylos not installed — pip install skylos"}` if binary not found

### 8.4 Deduplication System

**Tracking:**

| Tool | Dedup Key | Storage |
|------|-----------|---------|
| `read_file` | `path` string | `self._files_read: set[str]` |
| `run_command` | `cmd` string | `self._commands_run: set[str]` |
| `search_pattern` | `regex` string | `self._searches_done: set[str]` |

**Behavior on duplicate:**
- The tool still executes (no hard block)
- A `_dedup_warning` field is injected into the result dict:

```json
{
  "path": "src/routes.py",
  "content": "...",
  "_dedup_warning": "You already used read_file with these arguments. See previous result above. Move on to unchecked criteria."
}
```

**System prompt reinforcement:**

> Do not re-read the same file twice. Do not re-run the same command. Do not search for the same pattern twice.

**Non-tracked tools:** `read_diff`, `get_task_item`, `sandbox_write`, `sandbox_read`, `detect_dead_code`, `skylos_scan` — these are either idempotent or intentionally overwritable.

### 8.5 Verdict Format & Parsing

**Expected LLM output format:**

```json
{
  "verdict": "COMPLETE",
  "items": [
    {"criterion": "POST /login returns 200 on valid credentials", "status": "PASS", "detail": "Confirmed at routes.py:42-68"},
    {"criterion": "POST /login returns 401 on invalid credentials", "status": "FAIL", "detail": "No 401 handler — routes.py:65 missing"},
    {"criterion": "Password stored as bcrypt hash", "status": "PASS", "detail": "bcrypt used at auth.py:23"}
  ],
  "summary": "2 of 3 criteria pass — missing 401 error handling"
}
```

**System prompt instruction:**

> Output ONLY the JSON object. Nothing before. Nothing after. "verdict" must be "COMPLETE" or "INCOMPLETE". "status" must be "PASS" or "FAIL". EVERY criterion must have a corresponding item. PASS requires concrete evidence (file path, line number, or test output). FAIL requires explaining exactly what's missing.

**Parser: `_parse_verdict(content: str) → Verdict`**

**Strategy 1 — Markdown Fence Stripping:**
- If content starts with ` ``` `, remove opening fence (optional `json` language tag)
- Remove closing ` ``` ` fence
- Pass cleaned content to Strategy 2

**Strategy 2 — JSON Boundary Extraction:**
- Find first `{` and last `}` in cleaned string
- Extract substring and attempt `json.loads()`
- Validate: `verdict` field present, `items` field present
- Normalize `verdict` to `"COMPLETE"` or `"INCOMPLETE"` (default `"INCOMPLETE"` if invalid)
- Normalize each item's `status` to `"PASS"` or `"FAIL"` (default `"FAIL"` if invalid)
- Build `Verdict` with `VerdictItem` list

**Strategy 3 — Keyword Fallback:**
- Search raw text (case-insensitive) for:
  - `"complete"` or `verdict":"complete"` → `COMPLETE`
  - `"all criteria"` + `"pass"` → `COMPLETE`
  - Everything else → `INCOMPLETE`
- Log warning: `Falling back to keyword parse: verdict=...`
- Return `Verdict` with empty items and auto-generated summary including first 300 chars of raw text

**Validation rules:**
- Missing `verdict` or `items` → Strategy 2 fails, falls through to Strategy 3
- Invalid `verdict` value → normalized to `"INCOMPLETE"`
- Invalid `status` value → normalized to `"FAIL"`
- Missing `criterion` → defaults to `"unknown"`
- Missing `detail` → defaults to `""`

### 8.6 EvalCap System

**File:** `engine/eval_cap.py` (~412 lines)

**Dataclass:**

```python
@dataclass
class EvalCap:
    max_iterations: float = -1.0  # -1 = unlimited
    max_seconds: float = -1.0  # -1 = unlimited
    max_input_tokens: int = -1  # -1 = unlimited
    max_output_tokens: int = -1  # -1 = unlimited
    tool_call_weight: float = 0.1  # fraction per tool call

    # Runtime tracking (mutable, updated during evaluation)
    iteration_credit: float = 0.0
    start_time: float = 0.0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_read: int = 0
    cumulative_cache_write: int = 0

    source: str = ""  # Where this cap came from (for debugging)
```

#### Cap Checking Rules

**Iteration cap (lenient):**
- Checked in `record_llm_call()` and `record_tool_call()` BEFORE adding cost
- At `iteration_credit >= max_iterations`, the call is blocked
- This means at 99.9/100, a 1.0 call is still allowed → 100.9 total
- Rationale: prevents being one call short of a verdict

**Time cap (hard):**
- Checked in `_check_hard_caps()`
- If `elapsed >= max_seconds`, return error immediately
- No leniency — real wall-clock time is consumed

**Input token cap (hard):**
- Checked in `_check_hard_caps()`
- If `cumulative_input_tokens >= max_input_tokens`, return error
- Input tokens = prompt_tokens + cache_read_tokens + cache_write_tokens
- No leniency — token budget is a hard cost limit

**Output token cap (hard):**
- Checked in `_check_hard_caps()`
- If `cumulative_output_tokens >= max_output_tokens`, return error
- No leniency

#### Cost Tracking Methods

**`start()`:**
- Sets `start_time = time.time()`
- Called once at the beginning of `evaluate()`

**`record_llm_call(prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens) → str | None`:**
1. Check iteration cap BEFORE adding (lenient)
2. If exceeded, return error message
3. Add 1.0 to `iteration_credit`
4. Add `prompt_tokens + cache_read + cache_write` to `cumulative_input_tokens`
5. Add `completion_tokens` to `cumulative_output_tokens`
6. Add `cache_read_tokens` to `cumulative_cache_read`
7. Add `cache_write_tokens` to `cumulative_cache_write`
8. Call `_check_hard_caps()` → return error if any hard cap exceeded

**`record_tool_call() → str | None`:**
1. Check iteration cap BEFORE adding (lenient)
2. If exceeded, return error message
3. Add `tool_call_weight` (default 0.1) to `iteration_credit`
4. Call `_check_hard_caps()` → return error if any hard cap exceeded

**`check() → str | None`:**
- Alias for `_check_hard_caps()` — used before starting a new LLM call
- Checks time and token caps only (iteration checked in record methods)

#### Summary Display

**`summary() → str`:**

Returns a human-readable string showing current usage vs. caps:

```
iterations: 12.3/100, time: 45s/30m, in: 45k/200k (cache-hit 12k), out: 3k/50k
```

- Unlimited caps are shown as "unlimited" or omitted
- Cache tokens shown in parentheses when non-zero
- Time formatted as `Xs`, `XmXs`, `XhXm` as appropriate
- Token counts formatted as `Xk` or `X.XM` when >= 1,000

#### `max_iterations_int` Property

Converts the float `max_iterations` to an integer for the Python `range()` loop:
- If `max_iterations <= 0` (unlimited): returns 10,000 (safety maximum)
- Otherwise: returns `int(max_iterations)`

This prevents an infinite loop when iterations are unlimited while still allowing essentially unbounded evaluation.

### 8.7 Cap Priority Chain

Caps are resolved through a 9-level priority chain, from highest to lowest precedence:

```
Level 1: MCP individual params
  → judge.evaluate(id="task", max_iterations=50, max_time="10m",
                   max_input_tokens="200k", max_output_tokens="50k")

Level 2: MCP legacy eval_cap string
  → judge.evaluate(id="task", eval_cap="50/10m/200k/50k")

Level 3: GITREINS_EVAL_CAP environment variable
  → export GITREINS_EVAL_CAP="100/30m/200k/50k"

Level 4: .gitreins/config.yaml evaluator.* individual keys
  → evaluator:
       max_iterations: 100
       max_time: "30m"
       max_input_tokens: "200k"
       max_output_tokens: "50k"
       tool_call_weight: 0.1

Level 5: .gitreins/config.yaml evaluator.cap legacy combined string
  → evaluator:
       cap: "100/30m/200k/50k"

Level 6: .gitreins/config.yaml defaults: section
  → defaults:
       max_iterations: 50

Level 7: max_iterations kwarg to AgenticEvaluator constructor
  → AgenticEvaluator(llm, max_iterations=20)

Level 8: GitReinsDefaults hardcoded defaults
  → Defined in engine/config.py

Level 9: Ultimate fallback
  → max_iterations = 100
```

**Resolution in `AgenticEvaluator.__init__`:**

```python
if isinstance(eval_cap, EvalCap):
    self.eval_cap = eval_cap  # Level 1 (pre-built)
elif isinstance(eval_cap, str):
    self.eval_cap = parse_eval_cap(eval_cap)  # Level 2
elif max_iterations is not None and max_iterations > 0:
    self.eval_cap = EvalCap(max_iterations=...)  # Level 7
else:
    config = self._load_config()  # Levels 3-6
    self.eval_cap = eval_cap_from_config(config)
```

**Resolution in `eval_cap_from_config(config)`:**

```python
# Start with GitReinsDefaults (Level 8)
gd = GitReinsDefaults().overlay(config)
cap = EvalCap(..., source=gd._source)

# Check GITREINS_EVAL_CAP env var (Level 3) — handled by GitReinsDefaults

# Try legacy combined string (Level 5)
cap_str = ev.get("cap", "") or config.get("guards", {}).get("eval_cap", "")
if cap_str:
    cap = parse_eval_cap(str(cap_str))

# Override with individual keys (Level 4)
if "max_iterations" in ev:
    ...
if "max_time" in ev:
    ...
if "max_input_tokens" in ev:
    ...
if "max_output_tokens" in ev:
    ...
if "tool_call_weight" in ev:
    ...
```

### 8.8 Cache Token Tracking

DeepSeek's disk cache provides significant cost savings. The evaluator tracks cache tokens separately from regular tokens:

| Field | Description | Cost Impact |
|-------|-------------|-------------|
| `cumulative_input_tokens` | Total input tokens (regular + cache read + cache write) | Counted against input token cap |
| `cumulative_cache_read` | Tokens served from existing cache entries | Free (DeepSeek pricing) |
| `cumulative_cache_write` | Tokens written to new cache entries | Discounted (DeepSeek pricing) |
| `cumulative_output_tokens` | Completion tokens generated by the model | Counted against output token cap |

**Tracking flow:**

1. LLM response includes `usage` with `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`
2. `record_llm_call()` receives all four values
3. `all_input = prompt_tokens + cache_read_tokens + cache_write_tokens` added to `cumulative_input_tokens`
4. `cache_read_tokens` added to `cumulative_cache_read`
5. `cache_write_tokens` added to `cumulative_cache_write`
6. `completion_tokens` added to `cumulative_output_tokens`

**Summary display:**

When cache tokens are non-zero, the summary includes them:

```
in: 45k/200k (cache-hit 12k, cache-write 3k), out: 3k/50k
```

### 8.9 Backward Compatibility

**Legacy `eval_cap` string format:**

The combined string `"100/30m/200k/50k"` is still parsed by `parse_eval_cap(raw)`:

| Component | Example | Parsed As |
|-----------|---------|-----------|
| Iterations | `100`, `-1`, `unlimited` | `max_iterations` |
| Time | `30m`, `2h`, `45s` | `max_seconds` |
| Input tokens | `200k`, `1.5M` | `max_input_tokens` |
| Output tokens | `50k`, `0.1M` | `max_output_tokens` |

**Parsing rules:**
- Order is flexible — the parser identifies each component by type
- Time components are recognized by suffix (`s`, `m`, `h`)
- Token components are recognized by suffix (`k`, `m`) or slash separator (`200k/50k`)
- Single value without slash: interpreted as iterations if numeric, time if has suffix, tokens if has `k`/`m` suffix
- Unrecognized strings fall back to default 100 iterations with a warning

**Legacy `max_iterations` kwarg:**

The `AgenticEvaluator(llm, max_iterations=20)` constructor is still accepted. When provided and positive, it creates an `EvalCap` with only `max_iterations` set, ignoring config file values.

**Legacy `evaluator.cap` config key:**

The combined string under `evaluator.cap` in `.gitreins/config.yaml` is still parsed. Individual keys (`evaluator.max_iterations`, etc.) take priority over the combined string.

## 9. Filesystem Layout

```
engine/
├── evaluator.py          # AgenticEvaluator class, 7 tools, verdict parser, loop
│   ├── Verdict           # dataclass: verdict, items[], summary
│   ├── VerdictItem       # dataclass: criterion, status, detail
│   ├── AgenticEvaluator  # main class
│   ├── EVALUATOR_SYSTEM_PROMPT  # instructions to LLM
│   ├── EVALUATOR_TOOLS   # OpenAI function schema for 7+ tools
│   └── _parse_verdict()  # 3-strategy JSON parser
│
└── eval_cap.py           # EvalCap dataclass, parsing, config resolution
    ├── EvalCap           # dataclass with caps + runtime tracking
    ├── parse_eval_cap()  # legacy combined string parser
    ├── eval_cap_from_config()  # config.yaml resolution
    └── _parse_time(), _parse_tokens(), _fmt_*()  # helpers
```

## 10. Error Taxonomy

| Error | Source | Handling |
|-------|--------|----------|
| Iteration cap exceeded | `record_llm_call()`, `record_tool_call()` | Return INCOMPLETE with cap summary |
| Time cap exceeded | `_check_hard_caps()` | Return INCOMPLETE with elapsed time |
| Input token cap exceeded | `_check_hard_caps()` | Return INCOMPLETE with token usage |
| Output token cap exceeded | `_check_hard_caps()` | Return INCOMPLETE with token usage |
| LLM call failure | `llm.chat()` exception | Return INCOMPLETE with error message |
| Path escape attempt | `read_file` tool | Return `{"error": "Path outside working tree"}` |
| File not found | `read_file` tool | Return `{"error": "File not found"}` |
| Command timeout | `run_command` tool | Return `{"error": "Command timed out after 30s"}` |
| Invalid regex | `search_pattern` tool | Return `{"error": "Invalid regex"}` |
| Task not found | `get_task_item` tool | Return `{"error": "Task not found"}` |
| Sandbox key missing | `sandbox_read` tool | Return `{"error": "Key not found"}` |
| Dead code detector unavailable | `detect_dead_code` tool | Return `{"error": "Dead code detector unavailable"}` |
| Skylos not installed | `skylos_scan` tool | Return `{"error": "skylos not installed"}` |
| JSON parse failure | `_parse_verdict()` | Fall back to keyword heuristic |
| Keyword parse failure | `_parse_verdict()` | Default to INCOMPLETE with raw text |

## 11. Test Strategy

### Unit Tests (eval_cap.py)

- `test_default_cap` — unlimited by default, `is_unlimited` returns True
- `test_parse_time` — `30m` → 1800s, `2h` → 7200s, `45s` → 45s
- `test_parse_tokens` — `200k` → 200000, `1.5M` → 1500000
- `test_parse_eval_cap_combined` — `"100/30m/200k/50k"` parses all four caps
- `test_parse_eval_cap_single_iteration` — `"50"` → max_iterations=50
- `test_parse_eval_cap_unlimited` — `"-1"`, `"unlimited"`, `"none"` → unlimited
- `test_record_llm_call_increments_credit` — +1.0 per call
- `test_record_tool_call_increments_credit` — +0.1 per call (default weight)
- `test_iteration_cap_lenient` — at 99.9/100, 1.0 call allowed → 100.9
- `test_time_cap_hard` — elapsed >= max_seconds → hard stop
- `test_token_cap_hard` — cumulative >= max → hard stop
- `test_summary_format` — verify human-readable output format
- `test_eval_cap_from_config_individual` — config.yaml individual keys
- `test_eval_cap_from_config_legacy` — config.yaml combined string
- `test_eval_cap_from_config_priority` — individual keys override combined string

### Unit Tests (evaluator.py)

- `test_evaluate_complete_verdict` — mock LLM returns JSON verdict → parsed correctly
- `test_evaluate_incomplete_verdict` — mock LLM returns INCOMPLETE → correct items
- `test_parse_verdict_markdown_fence` — strips ` ```json ` wrappers
- `test_parse_verdict_json_boundaries` — extracts JSON from surrounding text
- `test_parse_verdict_keyword_fallback` — "complete" in text → COMPLETE
- `test_parse_verdict_invalid_status` — invalid status → normalized to FAIL
- `test_read_file_path_safety` — rejects `../../etc/passwd`
- `test_read_file_large_file_truncation` — >12KB chars → first 400 lines
- `test_read_file_offset_limit` — offset=10, limit=5 returns correct lines
- `test_run_command_success` — exit_code 0, output captured
- `test_run_command_timeout` — >30s → timeout error
- `test_run_command_output_truncation` — >4000 chars → truncated
- `test_search_pattern_skip_dirs` — `.git`, `venv` skipped
- `test_search_pattern_large_file_skip` — >500KB skipped
- `test_search_pattern_200_cap` — >200 matches → truncated
- `test_search_pattern_invalid_regex` — bad regex → error
- `test_dedup_read_file` — second read_file gets `_dedup_warning`
- `test_dedup_run_command` — second run_command gets `_dedup_warning`
- `test_dedup_search_pattern` — second search gets `_dedup_warning`
- `test_read_diff_staged_unstaged` — both git diff commands run
- `test_get_task_item` — returns registered task
- `test_sandbox_write_read` — write then read returns content
- `test_sandbox_read_missing` — missing key → error
- `test_cap_exceeded_iteration` — mock cap at limit → INCOMPLETE
- `test_cap_exceeded_time` — mock elapsed time → INCOMPLETE
- `test_forced_final_prompt` — max iterations hit → INCOMPLETE with cap message
- `test_empty_llm_response` — no content → INCOMPLETE

### Integration Tests

- `test_real_llm_evaluation` — requires real LLM API key; runs full loop with 2-3 tool calls
- `test_evaluator_with_git_repo` — uses actual git repo for `read_diff` tool
- `test_end_to_end_task_evaluation` — create task → evaluate → verify verdict structure

## 12. Observability

### Logging

| Logger | Level | Events |
|--------|-------|--------|
| `gitreins.evaluator` | DEBUG | Iteration progress, tool call counts, message history length |
| `gitreins.evaluator` | WARNING | Cap exceeded, JSON parse failure, keyword fallback |
| `gitreins.evaluator` | ERROR | LLM call failure, tool execution failure |
| `gitreins.eval_cap` | WARNING | Unrecognized cap string, fallback to default |

### Key Log Messages

```
"Eval cap exceeded: Iteration cap (100) reached (100.9 used). Increase max_iterations or split criteria."
"Evaluator iteration %d: %d tool calls, %d messages"
"JSON parse failed: %s"
"Falling back to keyword parse: verdict=%s"
"LLM call failed on iteration %d: %s"
"Tool %s failed"
```

### Metrics (Future)

- `evaluator.iterations` — histogram of iterations per evaluation
- `evaluator.tool_calls` — counter by tool name
- `evaluator.dedup_warnings` — counter of redundant calls prevented
- `evaluator.verdict_parse_strategy` — counter (1=JSON, 2=boundary, 3=keyword)
- `eval_cap.exceeded` — counter by cap type (iteration, time, input, output)

## 13. Implementation Status

| Component | Status | File | Lines |
|-----------|--------|------|-------|
| AgenticEvaluator class | ✅ Implemented | `engine/evaluator.py` | ~725 |
| EvalCap dataclass | ✅ Implemented | `engine/eval_cap.py` | ~412 |
| 7 core tools | ✅ Implemented | `engine/evaluator.py` | inline |
| Deduplication tracking | ✅ Implemented | `engine/evaluator.py` | `_execute_tool_with_dedup` |
| Verdict parser (3-strategy) | ✅ Implemented | `engine/evaluator.py` | `_parse_verdict` |
| Legacy cap string parser | ✅ Implemented | `engine/eval_cap.py` | `parse_eval_cap` |
| Config resolution | ✅ Implemented | `engine/eval_cap.py` | `eval_cap_from_config` |
| Cache token tracking | ✅ Implemented | `engine/eval_cap.py` | `record_llm_call` |
| Unit tests (eval_cap) | ✅ Implemented | `tests/test_eval_cap.py` | 39 tests |
| Unit tests (evaluator) | ✅ Implemented | `tests/test_evaluator.py` | ~45 tests |
| Integration tests (real LLM) | ✅ Implemented | `tests/test_evaluator_integration.py` | 3 tests |

## 14. Verification Checklist

- [ ] `AgenticEvaluator` constructor accepts all three cap input types (EvalCap, string, None)
- [ ] Cap priority chain resolves correctly: MCP params > env var > config individual > config legacy > defaults
- [ ] Iteration cap is lenient (checked before call, allows going slightly over)
- [ ] Time and token caps are hard stops
- [ ] Tool calls cost `tool_call_weight` (default 0.1) iterations
- [ ] LLM calls cost 1.0 iterations
- [ ] Deduplication warnings are injected for repeated `read_file`, `run_command`, `search_pattern`
- [ ] Non-tracked tools (`read_diff`, `get_task_item`, `sandbox_*`) do not trigger dedup warnings
- [ ] Verdict parser handles markdown fences, JSON boundaries, and keyword fallback
- [ ] Invalid verdict values normalize to `INCOMPLETE`
- [ ] Invalid status values normalize to `FAIL`
- [ ] `read_file` rejects path escape attempts
- [ ] `read_file` auto-truncates large files (>12KB chars, no range requested)
- [ ] `run_command` times out after 30 seconds
- [ ] `run_command` truncates output at 4,000 chars
- [ ] `search_pattern` skips hidden dirs, `venv`, `node_modules`, `.git`
- [ ] `search_pattern` caps at 200 matches
- [ ] `search_pattern` skips files > 500KB
- [ ] Cache tokens (`cache_read`, `cache_write`) are tracked separately and shown in summary
- [ ] Legacy `"100/30m/200k/50k"` string parses correctly
- [ ] `max_iterations` kwarg still accepted (backward compat)
- [ ] `evaluator.cap` legacy config key still parsed
- [ ] Summary format is human-readable: `iterations: 12.3/100, time: 45s/30m, in: 45k/200k, out: 3k/50k`

## 15. Example Outputs

### Example 1: Complete Evaluation

```
$ gitreins judge login-endpoint

Evaluating task: login-endpoint
  Criteria: 3 items

  Iteration 1: LLM calls get_task_item → reads criteria
  Iteration 2: LLM calls read_file("src/routes.py") → finds login handler
  Iteration 3: LLM calls run_command("pytest tests/test_login.py") → 3 passed
  Iteration 4: LLM calls search_pattern("bcrypt") → found in auth.py
  Iteration 5: LLM delivers verdict

Verdict: COMPLETE
  PASS: POST /login returns 200 on valid credentials — routes.py:42
  PASS: POST /login returns 401 on invalid credentials — routes.py:65
  PASS: Password stored as bcrypt hash — auth.py:23
  Summary: All 3 criteria verified

Caps: iterations: 5.0/100, time: 12s/30m, in: 8.2k/200k, out: 1.1k/50k
```

### Example 2: Incomplete Evaluation (Missing Tests)

```
$ gitreins judge login-endpoint

Verdict: INCOMPLETE
  PASS: POST /login returns 200 on valid credentials — routes.py:42
  FAIL: POST /login returns 401 on invalid credentials — no 401 handler found
  FAIL: Password stored as bcrypt hash — uses plain text at auth.py:23
  Summary: 1 of 3 criteria pass — missing error handling and secure storage

Caps: iterations: 8.2/100, time: 18s/30m, in: 12k/200k, out: 2.3k/50k
```

### Example 3: Cap Exceeded

```
$ gitreins judge complex-refactor

Verdict: INCOMPLETE
  Summary: Cap exceeded: Iteration cap (50) reached (50.3 used). Increase caps or split criteria.

Caps: iterations: 50.3/50, time: 45s/2m, in: 89k/200k, out: 12k/50k
```

### Example 4: Verdict with Cache Tokens

```
Verdict: COMPLETE
  PASS: All 5 criteria verified
  Summary: Dead code scan clean, tests pass, no AI hallucinations detected

Caps: iterations: 12.4/100, time: 34s/30m, in: 45k/200k (cache-hit 32k, cache-write 8k), out: 4.2k/50k
```

## 16. Package Structure

```
gitreins/
└── engine/
    ├── __init__.py
    ├── evaluator.py          # This spec's primary module
    ├── eval_cap.py           # Cap system
    ├── llm.py                # LLM client (dependency)
    ├── config.py             # GitReinsDefaults (dependency)
    ├── dead_code.py          # DeadCodeDetector (optional dependency)
    └── task_manager.py       # Task storage (dependency)
```

## 17. Commit Audit & CodeRabbit-Style Review (GR-065, GR-066)

**File:** `engine/commit_audit.py`

The commit audit system (Tier 2) was expanded in v0.10.0 from message-only validation to full CodeRabbit-style code review. v0.11.0 added CVE-style scored severity.

### Review Modes

| Mode | Latency | Iterations | Description |
|------|---------|-----------|-------------|
| `message` | <2s | 1 | Validate commit message vs diff (pre-v0.10.0 behavior) |
| `review` | <10s | 1 | Single-pass code review — structured findings |
| `agent` | ~30s | 3–5 | Multi-turn LLM with read_file/search_pattern tools |

### Review Checks

Configurable categories in `commit_audit.review_checks`:

| Check | Default | What it catches |
|-------|---------|-----------------|
| `bugs` | `true` | Logic errors, edge cases, nil pointer, race conditions |
| `security` | `true` | Injection, hardcoded keys, auth bypass, unsafe deserialization |
| `anti_patterns` | `true` | Mutable defaults, bare except, god objects |
| `style` | `false` | Deferred to linter |
| `performance` | `false` | N+1 queries, O(n²), unnecessary allocations |

### CVE-Style Severity Scoring (GR-066)

Every review issue gets a numeric 1–10 score. Configurable threshold + per-model offset multiplier:

```yaml
commit_audit:
  review_score_threshold: 8.0    # issues ≥ threshold BLOCK
  review_score_offset: 1.0       # multiplier: 0.5 dampens, 2.0 amplifies
```

| Range | Severity | Description |
|-------|----------|-------------|
| 9.0–10.0 | critical | RCE, data breach, auth bypass, guaranteed outage |
| 7.0–8.9 | high | User-visible bug, data corruption, incident-likely |
| 4.0–6.9 | medium | Code smell, anti-pattern, maintenance burden |
| 1.0–3.9 | low | Style nit, naming, readability |
| 0.0 | info | Observation, not a problem |

**Effective score:** `issue.score × review_score_offset`. Compared against threshold:
- ≥ threshold → 🚫 BLOCK
- ≥ threshold × 0.75 → ⚠️ WARN
- < threshold × 0.75 → ℹ️ INFO

**Per-model calibration:** boost lenient models (DeepSeek: `1.2`), dampen aggressive ones (GLM: `0.65`).

**Result structure:** `CommitReviewResult` with `{issues: [{file, line, severity, category, message, suggestion, score}], summary, message_valid, message_issues, overall_score}`.

### Review Output (commit-msg hook)

```
⚠ Commit review — 2 issue(s) found (overall: 9.0/8.0)

src/auth.py:42 [security] [🔴 CRITICAL] 🚫 BLOCK (score: 9.0) — SQL injection in login
  Fix: use parameterized query

src/utils.py:15 [anti_patterns] [🟡 MEDIUM] ℹ️ INFO (score: 5.0) — Mutable default arg
```

## 18. DeepSeek Prompt Caching (GR-068)

**Implemented:** v0.8.2+

DeepSeek V4 auto-caches repeated prompt prefixes. The evaluator's system prompt + code context is identical across multiple judge calls — subsequent evaluations on the same codebase only pay for the diff.

**Cache telemetry:** `LLMResponse.usage` includes `cache_read_tokens` and `cache_write_tokens`. Evaluator output shows cache savings:

```
Caps: iterations: 12.4/100, time: 34s/30m, in: 45k/200k (cache-hit 32k, cache-write 8k), out: 4.2k/50k
```

**Expected savings:** 50–90% on cache hits (system prompt + code context typically 80%+ of input tokens). Evaluator costs drop from ~$0.02/judge to ~$0.003–$0.01/judge.

**Reasoning flag:** Evaluator sets `reasoning_effort` via `llm.reasoning` config key (default: off for coding tasks, on for design tasks).

## 19. Large-Repo Hardening (GR-064)

Hardening from real-world use on dexdat-memory (147 Go packages):

**Fast-track mode:** Skips full call-graph analysis on large repos. Config: `evaluator.fast_track` (default: auto-detect based on package count > 50).

**Aggressive timeout respect:** Returns partial findings when deadline hits instead of failing silently. `max_time` wired into the tool-call loop — `read_file`/`search_pattern` check remaining budget before executing.

**Skip Tier 2 flag:** `gitreins judge --skip-tier2` or `gitreins.skip-tier2` trailer in commit message body. For config/docs/ops commits that don't need LLM evaluation.

**Token budget overflow protection:** Cap individual `read_file` results proportional to remaining budget. Config: `max_file_bytes` (default: 128KB per file in evaluator context).

**Pre-commit hook timeout:** Configurable `hook_timeout` (default: 120s). If exceeded, fail open with warning rather than blocking the push indefinitely.

**Config:** `evaluator.fast_track`, `evaluator.max_file_bytes`, `hook_timeout`, `--skip-tier2` flag.

---

## 20. Document Status
|-------|-------|
| **Version** | v0.6.0 |
| **Status** | Active — fully implemented |
| **Last updated** | 2026-07-19 |
| **Author** | totalwindupflightsystems <totalwindupflightsystems@gmail.com> |
| **Co-author** | wojons <wojonstech@gmail.com> |
| **Related specs** | 00-PRD.md, 01-Architecture-Overview.md |
| **Related docs** | docs/evaluator-loop.md, docs/architecture.md |
