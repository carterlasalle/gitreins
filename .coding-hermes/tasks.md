# GitReins 2.0 — Task Board (branch `v2`)

> Foreman: deepseek-v4-flash @ openrouter | Repo: carterlasalle/gitreins (fork) | Workdir: ~/gitreins2
> Source of truth: DESIGN_v2.md (full architecture spec). Follow it phase by phase.
> Every commit: `gitreins guard` PASS → commit → **push** (verify remote HEAD).
> Judge: `gitreins task complete <id>` per task.

## Phase 0 — Fix fail-open quality-gate bugs (DONE in main, cherry-pick onto v2)
- [x] P0-01 fix: Judge._run_pipeline exception returns PASS regardless of pass_on_error (1673f84)
- [x] P0-02 fix: partial-verdict returns COMPLETE on any verified criterion (1673f84)
- [x] P0-03 fix: remove `|| true` from generated gating commands (1673f84)
- [x] P0-04 fix: `on_fail: continue` semantics (flow not pass) (1673f84)
> v2 branch must start from a state WITH these fixes. Cherry-pick 1673f84 if needed.

## v2 Active — GitReins 2.0 build

> Phases in dependency order. Read DESIGN_v2.md before each. Each phase: implement →
> tests → `gitreins guard` → commit → push → `gitreins task complete`.

### R2.1 — ModelRouter (per-role model routing) ✅ b0420b9
- [x] R2-1 engine/router.py ModelRouter.for_role(role) + review.models.<role> config + env fallback + Pipeline/Judge wiring (b0420b9, 2026-08-08)
- [x] R2-1 tests: 11 tests (per-role, fallback, caching, pipeline integration) — 96 router/pipeline/judge + 117 evaluator tests pass

### R2.2 — AgentRunner (generic bounded agent runtime) ✓ 1ec04a1
- [x] R2-2 engine/agents/runner.py AgentRunner.run(system_prompt, user_prompt, tools, output_schema, model_role, budget) + budget.py/tools.py/schemas.py (1ec04a1, 2026-08-08)
- [x] R2-2 tests: 43 tests (iteration-cap, wall-clock-cap, token-accounting, tool-dedup, bounded reads, sandbox, schema parsing, compaction) — 202 evaluator/pipeline/judge + 43 agent tests pass; judge verdict 791d8438 (tier2 4/4 PASS)
- [x] R2-2 lint: fixed pre-existing E501 in engine/persist.py (3 sites, e9f50cf) — blocks whole-repo tier1 lint in judge runs
Extract the good parts of `AgenticEvaluator` into `engine/agents/runner.py`:
- `AgentRunner.run(system_prompt, user_prompt, tools, output_schema, model_role, budget)`.
- Move: iteration caps, wall-clock caps, token accounting, tool-call weighting,
  context compaction, tool dedup, bounded file reads, sandbox scratch state, LLM tool calling.
- New: `engine/agents/budget.py`, `tools.py`, `schemas.py`.
- Keep `AgenticEvaluator` behavior working (it becomes `CriteriaEvaluator(AgentRunner)`).

### R2.3 — CriteriaEvaluator = Lane A (requirements/completeness) ✅ 3cd9b0e
- [x] R2-3 engine/evaluator.py re-expressed as CriteriaEvaluator(AgentRunner) — alias keeps pipeline/judge callers; criteria prompt + MANDATORY TEST VERIFICATION + scan_security retained (3cd9b0e, 2026-08-08)
- [x] R2-3 tests: 21 tests in test_criteria_evaluator.py (subclass/alias, run() delegation, budget wiring, compaction, hard-rule retention) — full suite 1314 passed, 8 skipped; judge verdict 30361ec4 (tier2 COMPLETE, 4/4 criteria PASS; tier1 lint FAIL = ruff-not-found judge-subprocess PATH artifact, tests+secrets PASS)

### R2.4 — Evidence store (first-class Evidence type)
- `engine/evidence/models.py`: `Evidence` dataclass (id, kind, source, file,
  line_start/end, payload) + kinds enum.
- `engine/evidence/store.py`: append/query/provenance.
- `engine/evidence/provenance.py`.
- Static-analysis findings become evidence producers (keep existing parsers;
  wrap mypy/pyright/semgrep/ast-grep/trivy/gitleaks/etc. output as Evidence).

### R2.5 — Code Intelligence providers
- `engine/codeintel/base.py`: `CodeIntelProvider` Protocol
  (definition/references/callers/callees/implementations/symbols/contract/impact/history/prs/cross_repo).
- `engine/codeintel/serena.py`: wraps oraios/serena via MCP client.
- `engine/codeintel/lsp.py`: extend current LSP (diagnostics + semantic queries).
- `engine/codeintel/astgrep.py`: structural pattern search (already have ast-grep).
- `engine/codeintel/ripgrep.py`: lexical recall.
- `engine/codeintel/graph.py`: Hilo-type / gitnexus / codegraph integration (interface).
- Tools to agents: get_changed_symbols, get_symbol_definition, find_references,
  get_callers, get_callees, get_implementations, get_interface_contract,
  get_tests_for_symbol, get_change_impact, get_related_history, get_related_prs,
  get_cross_repo_consumers, ast_search, text_search — **small structured answers, not files**.

### R2.6 — Scout agent + evidence planner
- `engine/review/scout.py`: cheap Qwen prompt → {changed_symbols, retrieval_requests,
  review_lenses} JSON (retrieval PLAN, not a review).
- `engine/review/context_builder.py`: deterministic evidence planner executes the
  retrieval_requests (callers of foo → 3 callers) — NOT the LLM.

### R2.7 — Review agents (Lane B: defect discovery)
- `engine/review/orchestrator.py`: runs the review DAG.
- `engine/review/reviewers.py`: runtime/contract/security review agents, each an
  AgentRunner with a distinct objective, given the same evidence store.
- Parallel via ThreadPoolExecutor (pipeline already supports it).
- New pipeline stage types: `review_agent` (with `role`), `verify_findings`.

### R2.8 — Adversarial verifier
- `engine/review/verifier.py`: `VerifierAgent(AgentRunner)`; prompt = "disprove F<n>".
  Processes each candidate finding independently (finding_id, claim, evidence,
  verification_plan). Executes run_command/read_file/search_pattern + codeintel.
- Structured finding shape (impact, patch_causality, reproducible,
  execution_path_confirmed, verifier_confidence, developer_relevance) + **deterministic
  BLOCK policy** (NOT LLM CVE scores). DESIGN §12.

### R2.9 — Ranker + dedup + comment writer
- `engine/review/ranker.py`, `dedup.py`, `writer.py`.
- Writer batched; findings deduped by evidence overlap + usefulness.

### R2.10 — ChangeSource abstraction (local + GitHub PR)
- `engine/github/checkout.py` + `ChangeSource` Protocol (diff/changed_files/base_sha/head_sha).
- WorkingTreeChangeSource, PullRequestChangeSource, CommitRangeChangeSource.
- Same reviewer runs locally (pre-commit) and on GitHub PR.
- `engine/github/app.py`, `publisher.py`, `checks.py` (PR review surface).

### R2.11 — PR service-mode sandbox
- Review job runs in ephemeral container/microVM: clone PR, NO prod credentials,
  NO host fs, NO docker socket, network default-deny, resource caps.
- `engine/github/sandbox.py`. Verifier runs inside.

### R2.12 — Intent system (consume GitReins tasks)
- `engine/task_manager.py`: when a PR links an issue and a task exists, consume it
  as intent context (criteria). Do NOT auto-create. REVIEW gets both intent + defect context.
- Feed task criteria into Lane A (requirements) alongside Lane B.

### R2.13 — Rich review history + provenance + ReviewLearning
- `review_runs/<sha>/` artifacts (manifest, change, static-evidence, scout,
  candidates, verification, final-findings, requirements, usage).
- Final findings carry generated_by + verified_by provenance.
- `ReviewLearning`: developer accept/reject → durable rule scoped by repo+paths,
  retrieved only for relevant code.

### R2.14 — propagate → propagate_policy + cross-repo impact
- Rename conceptual purpose; add `get_cross_repo_impact()` via graph provider.

### R2.15 — Replace CodeRabbit-ish reviewer
- Delete `COMMIT_REVIEW_SYSTEM_PROMPT` / current commit-review orchestration.
- Keep `CommitAuditor` for actual commit-message auditing.
- Code review = its own subsystem (the review DAG above). `commit_audit_review_mode`
  no longer drives review.

### R2.16 — Review DAG wiring + `gitreins review` entrypoint
- Wire the full `review_pipeline` (DESIGN §17) into the Pipeline.
- Add `gitreins review` command (local ChangeSource) + `gitreins review --pr <n>`.
- Criteria eval (Lane A) + defect review (Lane B) → combined review.

## NEVER-DONE (audit — run only when board is empty)
- Full regression: `gitreins guard`, evaluator suite, review DAG E2E against a
  seeded repo, PR-mode smoke.
- CI/CD health, docs gate (README_v2, AGENTS, SECURITY, runbooks, repo description/topics).
