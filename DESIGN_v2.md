# GitReins 2.0 — Architecture & Design Spec

> Scope: full rearchitecture of the gitreins fork into an **evidence-driven,
> cheap-model review ensemble** — not "GitReins with better prompts."
> GitReins 1.x stays as the harness/control plane; this spec builds the review
> brain on top, plus fixes the fail-open quality-gate bugs.
> Status: **design complete, build in progress** — 2026-08-08.

---

## 1. The mental model

```
                     GITREINS (control plane)
                ┌────────────────────────────┐
                │  Git / task / MCP / gates  │
                └──────────────┬─────────────┘
                               │  PR / staged diff
                               ▼
                ┌────────────────────────────┐
                │   Change Intelligence      │
                │  diff, symbols, contracts, │
                │  risk                      │
                └──────────────┬─────────────┘
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
 GitReins guards         Repo Intelligence      Intent
 (static analyzers,    (your graph + Serena/   (GitReins tasks,
  LSP, tests, CI,        LSP + ast-grep +        issues/PRs,
  security scans)        ripgrep + git history)   AGENTS.md)
        └──────────────────────┼──────────────────────┘
                               ▼
                       EVIDENCE STORE
                               │
                               ▼
                          QWEN SCOUT
                               │
                        retrieval plan
                               │
                               ▼
                 Compact evidence packets
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        DeepSeek runtime   DeepSeek contract  DeepSeek security
        reviewer           reviewer            reviewer
              └────────────────┼────────────────┘
                               ▼
                      candidate findings
                               ▼
                     QWEN/STEP VERIFIER
                               │
                    repo tools + execution
                               ▼
                   executable falsification
                               ▼
                    dedupe + usefulness rank
                               ▼
                          comment writer
                               ▼
                      GitHub / coding agent
```

**GitReins is not replaced. It moves underneath the review intelligence.**
Your repo graph is the "how the software actually works"; static/LSP tooling
supplies hard evidence; cheap specialized models do hypothesis generation; the
verifier does falsification.

---

## 2. Two independent AI lanes (the key idea)

| | **Lane A — Requirements / completeness** | **Lane B — Defect discovery** |
|---|---|---|
| Question | *Did Carter implement all stated requirements?* | *Regardless of requirements, did this patch break something?* |
| Engine | Keep `AgenticEvaluator` (criteria-based: TASK → criterion 1..n → verify each → COMPLETE/INCOMPLETE) | **New** review system (scout → evidence → multi-reviewer → verifier) |
| Input | Task criteria | PR diff + repo context |
| Output | Per-criterion PASS/FAIL | Verified findings |

```
                          PR
                           │
               ┌───────────┴───────────┐
               ▼                       ▼
      Requirements evaluator      Defect reviewer
      (GitReins native)           (new system)
               │                       │
               ▼                       ▼
      criteria PASS/FAIL         verified findings
               └───────────┬───────────┘
                           ▼
                     combined review
```

This is stronger than CodeRabbit: it answers *both* "did they do what was
asked" AND "what did they accidentally break."

---

## 3. Model routing — per-role, not per-pipeline

`Pipeline._llm` (one client owned by the pipeline) is replaced by a
`ModelRouter` that resolves a client **per role invocation**:

```python
class ModelRouter:
    def for_role(self, role: str) -> LLMClient:
        cfg = self.config["models"][role]
        return LLMClient(
            model=cfg["model"],
            base_url=cfg["base_url"],
            provider=cfg["provider"],
            llm_reasoning=cfg.get("reasoning", "disabled"),
        )
```

Config:

```yaml
review:
  models:
    scout:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: qwen/qwen3.7-flash
    runtime_reviewer:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: deepseek/deepseek-v4-flash-0731
    contract_reviewer:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: deepseek/deepseek-v4-flash-0731
    security_reviewer:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: deepseek/deepseek-v4-flash-0731
    verifier:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: qwen/qwen3.7-flash
    writer:
      provider: openai
      base_url: https://openrouter.ai/api/v1
      model: qwen/qwen3.7-flash
```

**The existing `LLMClient` transport is sufficiently generic** — it already
handles OpenAI-compatible endpoints, Anthropic Messages, tool calls, retries,
token accounting, and DeepSeek thinking control. Keep it; add routing.

---

## 4. `AgenticEvaluator` → generic agent runtime

`engine/evaluator.py` currently conflates: agent loop + tool runtime + budget +
compaction + criteria logic + verdict schema + context retrieval. **Extract the
good parts** (they are genuinely excellent):

- iteration caps, wall-clock caps, token accounting, tool-call weighting
- context compaction
- tool dedup (stops cheap models going in circles)
- bounded file reads
- sandbox scratch state
- LLM tool calling

into:

```
engine/agents/
    runner.py      # AgentRunner — generic bounded loop
    budget.py      # cost/context caps
    tools.py       # tool registry + dedup + bounded reads
    schemas.py     # output schemas
```

```python
class AgentRunner:
    def run(
        self, *,
        system_prompt: str,
        user_prompt: str,
        tools: list[Tool],
        output_schema: type[T],
        model_role: str,
        budget: Budget,
    ) -> T: ...
```

Then concrete agents:

```
CriteriaEvaluator(AgentRunner)   # Lane A
ScoutAgent(AgentRunner)          # cheap classifier
ReviewAgent(AgentRunner)         # runtime / contract / security
VerifierAgent(AgentRunner)       # falsification
CommentWriter(AgentRunner)       # batched output
```

GitReins is no longer tied to one evaluator persona.

---

## 5. Code Intelligence — the biggest missing piece

Today the evaluator only gets `read_file / search_pattern / read_diff /
run_command`, and `file_scope = "changed"` (with fast-track narrowing to
changed lines) — great for cheap criterion eval, **bad for non-local
regressions**.

For review mode, give agents **small structured answers, not files**:

```
get_changed_symbols()      get_symbol_definition()
find_references()          get_callers()
get_callees()              get_implementations()
get_interface_contract()   get_tests_for_symbol()
get_change_impact()        get_related_history()
get_related_prs()          get_cross_repo_consumers()
ast_search()               text_search()
```

Define a provider interface and implement in priority order:

```python
class CodeIntelProvider(Protocol):
    def definition(...)     def references(...)
    def callers(...)        def callees(...)
    def implementations(...) def symbols(...)

# SerenaProvider    (LSP — precise current symbol semantics)
# RepoGraphProvider (YOUR graph — persistent topology / flows / architecture)
# AstGrepProvider   (structural pattern search)
# RipgrepProvider   (cheap lexical recall)
```

**Do NOT hand-roll LSP semantics inside GitReins.** The current `lsp.py`
already launches servers + consumes JSON-RPC diagnostics; extend it as an
*interface* but delegate deep semantics to Serena / your graph / ast-grep /
ripgrep.

---

## 6. Evidence as a first-class type

```python
@dataclass
class Evidence:
    id: str
    kind: Literal[
        "diff", "symbol", "reference", "call_edge",
        "static_analysis", "lsp", "test", "command",
        "history", "requirement", "repo_rule", "ci",
    ]
    source: str
    file: str | None
    line_start: int | None
    line_end: int | None
    payload: dict
```

Every claim from an agent references `E17 / E31 / E44` — never "assertions from
nowhere."

### Static analysis becomes evidence *producers* (CodeRabbit pattern)

Keep all existing normalized parsers (`mypy, pyright, Sorbet, SQLFluff,
PHPStan, cppcheck, staticcheck, Clippy, ESLint`). Add adapters for `OpenGrep /
Semgrep, ast-grep, OSV-Scanner, Trivy, gitleaks, actionlint, zizmor, Checkov,
Bandit`, plus language-native compiler diagnostics. **Don't post findings
directly** — the pipeline is:

```
Semgrep warning → Evidence E82 → review agent considers it
    → verifier tests the claim → possibly publish
```

---

## 7. The scout fits before the agent loop

`engine/review/scout.py` — a cheap Qwen prompt that produces a **retrieval
plan** (not a review):

```json
{
  "changed_symbols": [
    { "symbol": "SessionManager.rotate_token", "risk": "high" }
  ],
  "retrieval_requests": [
    { "type": "callers", "query": "SessionManager.rotate_token" },
    { "type": "references", "query": "refresh_token" }
  ],
  "review_lenses": ["state/concurrency", "authorization"]
}
```

Then an **evidence planner** (deterministic, not the LLM) executes those
requests. This is the reliability win: cheap models get much more reliable when
the system does `callers of foo → 3 callers` rather than giving the model
unrestricted shell access and hoping it discovers everything.

---

## 8. Three GitReins-style agent loops in parallel

The pipeline already supports parallel steps via `ThreadPoolExecutor`. Add new
stage types (`review_agent`, `verify_findings`):

```yaml
- id: reviewers
  parallel: true
  steps:
    - id: runtime
      type: review_agent
      role: runtime
    - id: contracts
      type: review_agent
      role: contracts
    - id: security_edges
      type: review_agent
      role: security_edges
```

Each receives the same evidence store, different objective.

---

## 9. The verifier — a separate step, adversarial

```yaml
- id: verification
  type: verify_findings
  model_role: verifier
```

Each candidate finding processed independently:

```json
{
  "finding_id": "F19",
  "file": "auth/session.py",
  "line": 182,
  "claim": "Deleted users can reach this dereference with user=None.",
  "trigger": "refresh token belonging to deleted account",
  "evidence": ["E12", "E33", "E52"],
  "verification_plan": [
    "inspect caller guard",
    "inspect get_user return contract",
    "run targeted regression test"
  ]
}
```

**Verifier prompt: "Your job is to disprove F19."** It gets execution tools
(`run_command / read_file / search_pattern` — already present) plus codeintel.

### ⚠️ Execution sandbox requirement

`_tool_run_command()` does `subprocess.run(cmd, shell=True, cwd=self.workdir)`
**directly on the host**. That's acceptable for local trusted coding, but
**absolutely not** for random external GitHub PRs. For **PR service mode**:

```
review job → ephemeral container/microVM → clone PR
  → NO production credentials, NO host filesystem, NO docker socket
  → network default-deny → resource caps
  → GitReins agent tools
Then let the verifier go wild.
```

---

## 10. GitReins tasks as the intent system

When a PR links an issue, create a synthetic task:

```
GitHub Issue #842 "Allow promotional orders with zero-dollar total"
  id: github-842
  criteria:
    - zero-dollar promotional orders are accepted
    - ordinary zero-dollar orders remain rejected
    - Stripe is not contacted for free orders
    - order fulfillment still runs
```

So review gets **intent context** (what the dev tried to do) **and** **defect
context** (what they accidentally broke).

---

## 11. `propagate` → rename + add real cross-repo impact

Current `propagate` only copies missing config keys between sibling repos.
Rename conceptually to `propagate_policy`. Add a real `get_cross_repo_impact()`
through your graph (backend API changed → inspect web + mobile).

---

## 12. Don't trust LLM "CVE scores" as the gate

Replace the numeric `1.0–10.0` threshold blocking with **structured factors +
deterministic policy**:

```json
{
  "impact": "high",
  "patch_causality": "confirmed",
  "reproducible": true,
  "execution_path_confirmed": true,
  "verifier_confidence": 0.94,
  "developer_relevance": "high"
}
```

```python
BLOCK = (
    finding.impact in {"critical", "high"}
    and finding.patch_causality == "confirmed"
    and finding.execution_path_confirmed
    and finding.verifier_confidence >= 0.90
)
```

A model can't reliably distinguish `7.9` from `8.1`; structured factors + a
deterministic policy can.

---

## 13. Rich review history + provenance

```
review_runs/<commit-sha>/
    manifest.json          change.json          static-evidence.json
    scout.json             candidates.json       verification.json
    final-findings.json    requirements.json     usage.json
```

Every final finding has full provenance:

```json
{
  "id": "F19",
  "claim": "...",
  "evidence": [
    "static:semgrep:17",
    "graph:caller:Session.refresh",
    "source:auth/session.py:181",
    "test:verification-F19"
  ],
  "generated_by": { "role": "runtime_reviewer", "model": "deepseek-v4-flash" },
  "verified_by":  { "model": "qwen3.7-flash", "verdict": "confirmed" }
}
```

### Developer responses → real memory (`ReviewLearning`)

```json
{
  "scope": { "repo": "mockingbird-dsp", "paths": ["asr/**"] },
  "rule": "Unknown radio street names must never be normalized to dictionary words.",
  "source": { "type": "developer_rejection", "finding": "F821" },
  "confidence": 0.94,
  "support_count": 3
}
```

Retrieve that only when reviewing relevant code. This is where your broader
memory work (DuckBrain) plugs in.

---

## 14. GitHub/PR mode — new `ChangeSource` abstraction

Today GitReins is fundamentally *local repo / staged changes / pre-commit /
MCP*, repeatedly shelling out to `git diff --cached`. Add:

```python
class ChangeSource:
    def diff(self) -> Diff: ...
    def changed_files(self) -> list[str]: ...
    def base_sha(self) -> str: ...
    def head_sha(self) -> str: ...

# WorkingTreeChangeSource
# PullRequestChangeSource
# CommitRangeChangeSource
```

Then the reviewer doesn't care whether it's a `git commit` or `GitHub PR #419` —
**the same engine runs locally and remotely**:

```
CODING AGENT → MCP → GitReins → pre-commit review (finds most defects)
    → git push → GitHub PR review → SAME REVIEW ENGINE (deeper, full repo)
    → human reviewer
```

Cheap inner loop: Qwen + DeepSeek. Deep PR loop: more evidence, more
verification, optional frontier escalation.

---

## 15. Model-per-role call budget

| Role | Model | Calls |
|---|---|---|
| Changed-code classification | Qwen Flash | 1 |
| Retrieval / scout | Qwen Flash | 1 |
| Runtime reviewer | DeepSeek V4 Flash | 1 |
| Contract/state reviewer | DeepSeek V4 Flash | 1 |
| Security/edge reviewer | DeepSeek V4 Flash | 1 |
| Verification | Qwen/Step | per candidate |
| Comment writer | Qwen Flash | batched |
| Hard-case escalation | Sol/Opus/etc. | rare |

---

## 16. Package structure (fork → v2)

```
gitreins/
├── engine/
│   ├── agents/          runner.py budget.py tools.py schemas.py
│   ├── review/          orchestrator.py scout.py context_builder.py
│   │                    reviewers.py verifier.py ranker.py dedup.py writer.py
│   ├── evidence/        models.py store.py provenance.py
│   ├── codeintel/       base.py graph.py serena.py lsp.py astgrep.py ripgrep.py
│   ├── analyzers/       semgrep.py osv.py trivy.py gitleaks.py ...
│   ├── github/          app.py checkout.py publisher.py checks.py
│   ├── evaluator.py     # requirement evaluator remains (Lane A)
│   ├── guard_manager.py
│   ├── static_analysis.py
│   ├── task_manager.py
│   └── llm.py
```

---

## 17. The pipeline (v2 review DAG)

```
review_pipeline:
  - id: change_analysis
    type: change_analysis

  - id: static_evidence
    parallel: true
    steps:
      - { type: analyzer, analyzer: lsp }
      - { type: analyzer, analyzer: semgrep }
      - { type: analyzer, analyzer: typecheck }
      - { type: analyzer, analyzer: secrets }
      - { type: analyzer, analyzer: dependencies }

  - id: scout
    type: agent
    role: scout

  - id: retrieval
    type: evidence_retrieval

  - id: reviewers
    parallel: true
    steps:
      - { type: agent, role: runtime_reviewer }
      - { type: agent, role: contract_reviewer }
      - { type: agent, role: security_reviewer }

  - id: candidate_merge
    type: merge_findings

  - id: verify
    type: verify_findings
    parallel: true
    role: verifier

  - id: rank
    type: rank_findings

  - id: requirements
    type: criteria_eval
    condition: task.has_criteria

  - id: publish
    type: publish_review
```

---

## 18. Keep / Refactor / Replace summary

**Keep essentially intact:** task lifecycle, MCP interface, LLM transport,
token/cost caps, tool dedup, context compaction ideas, normalized static
diagnostics, LSP startup/diagnostics, git-native local workflow, verdict/history
concept.

**Refactor heavily:** `AgenticEvaluator` → `AgentRunner` + `CriteriaEvaluator`;
`Pipeline` → true typed review DAG; `GuardManager` → `EvidenceCollectors`;
LLM config → `ModelRouter`; LSP → full semantic code-intelligence interface;
history → provenance + feedback memory.

**Replace:** `COMMIT_REVIEW_SYSTEM_PROMPT`; current commit-review orchestration;
LLM CVE-score blocking; 15K-diff review; changed-files-only context for defect
discovery; host-shell execution in service mode.

---

## 19. Pre-build fixes (already applied to this repo)

1. ✅ `Judge._run_pipeline` fail-open bug — exception returned PASS regardless
   of `pass_on_error`; fixed to gate the pass.
2. ✅ Partial-verdict COMPLETE bug — `_extract_partial_verdict` returned
   COMPLETE on any verified criterion; fixed to `all(status == "PASS")`.
3. ✅ Removed `|| true` from generated gating commands.
4. ✅ `on_fail: continue` semantics — now controls flow, not pass status.
5. ✅ Per-stage model routing (new `ModelRouter`).
6. ✅ Pipeline `prompt_template` now actually wires to the evaluator prompt.

---

## 20. Adoption verdict

> **"GitReins + your repository intelligence layer + evidence-driven cheap-model
> review ensemble."** GitReins handles when agents must prove work is good. Your
> repo graph handles how the software actually works. Static/LSP tooling supplies
> hard evidence. Cheap specialized models handle hypothesis generation. The
> verifier handles falsification. The same engine works inside Claude/Hermes/OMP
> before commit and on GitHub after push.
