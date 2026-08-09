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

### R2.4 — Evidence store (first-class Evidence type) ✅ 552ec26 + 5a0dc5f
- [x] R2-4 engine/evidence/models.py: `Evidence` dataclass (id, kind, source, file, line_start/end, payload) + kinds enum (552ec26, 2026-08-08)
- [x] R2-4 engine/evidence/store.py: append/query/provenance + engine/evidence/provenance.py (552ec26)
- [x] R2-4 evidence producers wrapping analyzers: engine/analyzers/{semgrep,ast-grep,trivy,gitleaks}.py → Evidence (5a0dc5f)
- [x] R2-4 tests: 33 evidence + 26 analyzer tests pass; guard PASS; judge verdict 08066aff/ef6fc473 (tier2 COMPLETE 4/4)

### R2.5 — Code Intelligence providers ✅ ae62a0c
- [x] R2-5 engine/codeintel/base.py: CodeIntelProvider Protocol — 11 query methods (definition/references/callers/callees/implementations/symbols/contract/impact/history/prs/cross_repo) + CodeIntelUnavailableError (ae62a0c, 2026-08-08)
- [x] R2-5 engine/codeintel/astgrep.py + ripgrep.py: structural pattern search + lexical recall providers (ae62a0c)
- [x] R2-5 engine/codeintel/lsp.py: LSP semantic queries (diagnostics + definition/references/symbols) extending engine/lsp (ae62a0c)
- [x] R2-5 engine/codeintel/graph.py + serena.py: graph provider interface (hilo/gitnexus-style) + serena MCP wrapper (lazy ClientSession, SerenaUnavailableError when binary absent) (ae62a0c)
- [x] R2-5 agent tools registered via codeintel_tools() (engine/agents/tools.py, exported in agents/__init__.py): ast_search, text_search, get_symbol_definition, find_references, get_callers, get_callees, get_implementations, get_change_impact — small structured answers (500-char snippet cap, limit cap, errors degrade to []) (ae62a0c)
- [x] R2-5 tests: 30 codeintel + 43 agents tests pass; full suite 1229 passed/5 skipped (judge run); judge verdict 53537562 (tier2 COMPLETE 5/5 PASS; tier1 lint FAIL = ruff-not-found judge-subprocess PATH artifact, tests+secrets PASS). test_lsp.py hang is pre-existing (reproduced on parent d870e1b), unrelated to R2.5.

### R2.6 — Scout agent + evidence planner ✅ 05eb02d (judge COMPLETE 7119237d)
- [x] `engine/review/scout.py`: cheap Qwen prompt → {changed_symbols, retrieval_requests,
  review_lenses} JSON (retrieval PLAN, not a review). (05eb02d, 2026-08-08)
- [x] `engine/review/context_builder.py`: deterministic evidence planner executes the
  retrieval_requests (callers of foo → 3 callers) — NOT the LLM. (05eb02d)
- [x] R2-6 judge: verified COMPLETE 7119237d (ScoutAgent(AgentRunner) produces ScoutPlan
  JSON via schema, model_role='scout'). Tier1 fixes (ruff/LSP/ast-grep/gitleaks PATH +
  .venv allowlist) committed 24f238a/3f946f4/f66edfd.
- [x] DOCS-000 docs pass (re-run 2026-08-08): README v2 architecture section +
  docs/architecture-v2.md runbook (cc86d1a).

### R2.7 — Review agents (Lane B: defect discovery) ✅ 45add46 (judge COMPLETE e92bac86)
- [x] R2-7 engine/review/orchestrator.py: ReviewOrchestrator runs the review DAG —
  diff evidence → ScoutAgent → EvidencePlanner (when provider set) → parallel
  reviewers via ThreadPoolExecutor, per-lane error capture, findings merged (45add46, 2026-08-08)
- [x] R2-7 engine/review/reviewers.py: RuntimeReviewer / ContractReviewer /
  SecurityReviewer — each AgentRunner subclass (MODEL_ROLE + distinct OBJECTIVE),
  same EvidenceStore serialized into each lane's user prompt (45add46)
- [x] R2-7 pipeline stage type `review_agent` (with `role`) wired into
  engine/pipeline.py — dispatches runtime/contracts/security_edges roles,
  findings plumbed onto task['findings'], parallel via existing ThreadPoolExecutor (45add46)
- [x] R2-7 tests: 65 hermetic tests (StubLLM, per-role routers, zero LLM calls) —
  targeted 162 passed; judge verdict e92bac86 (tier2 COMPLETE 4/4; tier1 FAIL =
  judge-subprocess PATH/env artifact, ruff-not-found + GITREINS_MAX_* leaking into
  subprocess pytest — identical on parent commit)

### R2.8 — Adversarial verifier ✅ e68b9d3 (judge tier2 COMPLETE 4/4 eda00fdb)
- [x] `engine/review/verifier.py`: `VerifierAgent(AgentRunner)` (MODEL_ROLE=verifier);
  falsification prompt "Your job is to DISPROVE F<n>" — CONFIRMED only if the
  claim survives the agent's attempts to break it. Processes each candidate
  finding independently via VerifierCandidate (finding_id, file, line, claim,
  trigger, evidence refs, verification_plan). Executes run_command /
  read_file / search_pattern + the 8 R2.5 codeintel tools. (e68b9d3, 2026-08-08)
- [x] Structured finding shape VerifierFinding (impact, patch_causality,
  reproducible, execution_path_confirmed, verifier_confidence,
  developer_relevance) + verdict + **deterministic BLOCK policy** (pure fn,
  no LLM): impact in {critical,high} AND causality==confirmed AND
  execution_path_confirmed AND confidence>=0.90. DESIGN §12 exact. (e68b9d3)
- [x] R2-8 tests: 40 hermetic (StubLLM, zero LLM calls) — exhaustive 120-row
  BLOCK truth table + 0.90/0.89 boundary, confirmed/refuted runs, tool belt,
  schema parse; review suites 81 + agents/pipeline 97 pass; ruff clean.
  Judge verdict eda00fdb (tier2 COMPLETE 4/4; tier1 lint FAIL = ruff-not-found
  judge-subprocess artifact — root-caused + FIXED below).
- [x] INFRA-LINT-CMD fix (073ae19, judge PASS da467d23): `guards.lint_command`
  was dead config — `_default_tier1_steps` hardcoded `ruff check .` and only
  honored test_command/test_timeout, so every judge run since R2.5 failed
  tier1 lint ("ruff: not found" in bare-shell subprocess). Now honors
  `lint_command` (uv run ruff check .) mirroring test_command; 5 regression
  tests (TestGuardsCommandOverrides). Next judge run = clean tier1.

### R2.9 — Ranker + dedup + comment writer ✅ a0bffe0 (judge PASS 8b8b7b2e, tier1 clean)
- [x] R2-9 engine/review/ranker.py — findings ranked by evidence overlap + usefulness
- [x] R2-9 engine/review/dedup.py — dedupe findings by evidence overlap
- [x] R2-9 engine/review/writer.py — batched comment writer (GitHub/agent)
- [x] R2-9 tests: ranker/dedup/writer hermetic tests
> rank_findings deterministic LLM-free key (severity, confidence, exec-path,
> developer-relevance, file/line/claim tie-break); dedupe_findings union-find
> grouping by shared evidence refs OR same-file line proximity (10 lines),
> most-useful representative kept, identity preserved; CommentWriter(AgentRunner)
> MODEL_ROLE=writer — ONE batched LLM call → CommentBatch, review.models.writer
> fallback never raises. 55 hermetic tests (StubLLM); full suite 1583 passed /
> 15 skipped (5 pre-existing: static_analysis mypy env artifact reproduced on
> parent HEAD, cli lifecycle flaky passes in isolation, lsp hang pre-existing);
> ruff clean. Judge verdict 8b8b7b2e: tier1 lint/tests/secrets ALL PASS (first
> clean tier1 since lint_command fix 073ae19), tier2 COMPLETE 4/4. R2.10 ready.

### R2.10 — ChangeSource abstraction (local + GitHub PR) ✅ e3d2eae (judge PASS 50f00f11)
- [x] R2-10 engine/github/checkout.py — ChangeSource Protocol (diff/changed_files/base_sha/head_sha, runtime-checkable) + Diff dataclass + WorkingTreeChangeSource / CommitRangeChangeSource / PullRequestChangeSource (gh CLI subprocess, NO new deps, ChangeSourceError on missing gh/bad JSON) (e3d2eae, 2026-08-08)
- [x] R2-10 engine/github/publisher.py — publish_comments (per-line PR review comments / issue comments, requests fallback, NEVER raises → PublishResult.failed) + checks.py — set_status_check (pending/success/failure/error, ValueError on bad state) + app.py — run_pr_review: PullRequestChangeSource → SAME ReviewOrchestrator.run as local → rank/dedupe → CommentWriter → publish + status check (DAG error→error, crit/high→failure, else success) (e3d2eae)
- [x] R2-10 tests: 43 hermetic (21 checkout + 22 app) — diff text correctness (--unified=3), ACM filtering, base/head SHAs (HEAD/empty-tree/worktree stash/gh refs), missing-gh + invalid-JSON error paths, publisher never-raises + one-failure-doesn't-block-others, checks state mapping, PR smoke (stubbed orchestrator + StubLLM writer). Full suite 1604 passed/7 skipped (1 pre-existing flaky cli lifecycle, passes in isolation); ruff clean; gitreins guard PASS. Judge verdict 50f00f11: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE. R2.11 ready.

### R2.11 — PR service-mode sandbox ✅ df8c452 (judge PASS 089fdf31)
- [x] R2-11 engine/github/sandbox.py — ephemeral container/microVM (clone PR, NO prod credentials, NO host fs, NO docker socket, network default-deny, resource caps) (df8c452, 2026-08-08)
- [x] R2-11 verifier runs inside the sandbox (df8c452)
- [x] R2-11 tests: sandbox isolation + verifier-in-sandbox — 49 hermetic tests (protocol, cmd-building, env-scrub, denied-mounts, unavailable, teardown, clone-pr, verifier-in-sandbox, docker-smoke); judge verdict 089fdf31: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE 3/3

### R2.12 — Intent system (consume GitReins tasks) ✅ 43af82e (judge PASS 661e954c)
- [x] R2-12 engine/task_manager.py — consume existing tasks as intent context (do NOT auto-create): `TaskManager.intent_context(statuses=None)` read-only projection {id,title,criteria,status} + `intent_prompt()` + module-level `intent_to_prompt()` render block "Task intent (developer criteria)" (43af82e, 2026-08-08)
- [x] R2-12 feed task criteria into Lane A (requirements) alongside Lane B: pipeline `_run_review_agent` builds intent block from task criteria (explicit `task.intent_context` wins), ReviewAgent.run(intent_context=...) renders it BEFORE the evidence store; ReviewOrchestrator.run/run_from_plan/_run_one thread it to every reviewer (43af82e)
- [x] R2-12 tests: task-consume + intent/defect context merge — 23 new tests (7 task_manager consume/read-only, 8 reviewer prompt-merge, 4 orchestrator threading, 4 pipeline); targeted 120 passed, ruff clean, LSP 0 findings; judge verdict 661e954c: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE 3/3. R2.13 ready.

### R2.13 — Rich review history + provenance + ReviewLearning ✅ ecc6752 (judge PASS 781eef2c)
- [x] R2-13 engine/review/history.py — ReviewRunArchiver persists review_runs/<sha>/ JSON artifacts: manifest, change, static-evidence, scout, candidates, verification, final-findings, requirements, usage (exact §13 filenames); wired into ReviewOrchestrator (archiver= param, base_dir=workdir, archives stages when data exists); never raises on archive failure (ecc6752, 2026-08-08)
- [x] R2-13 final findings carry provenance — ReviewFinding.generated_by {role, model} + verified_by {model, verdict} (defaulted, backward compatible; §13 shape) (ecc6752)
- [x] R2-13 engine/review/learning.py — ReviewLearning: developer accept/reject → durable rule {scope: {repo, paths}, rule, source: {type, finding}, confidence, support_count}, JSONL-persisted (.gitreins/review_learning.jsonl), repo+fnmatch path-glob retrieval, thread-safe append, I/O failure never raises (ecc6752)
- [x] R2-13 tests: 24 hermetic (9 test_review_history: 9 artifacts, clean re-run, failure-never-raises, provenance in final-findings, orchestrator wiring; 15 test_review_learning: accept/reject rules, repo+path scoping, §13 round-trip, support_count, persistence, corrupt-line skip) — StubLLM/zero LLM; full suite 1722 passed/7 skipped/3 deselected (rust-analyzer hang pre-existing at parent); ruff clean; guard PASS; judge verdict 781eef2c: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE 4/4. R2.14 ready.

### R2.14 — propagate → propagate_policy + cross-repo impact
- [ ] R2-14 rename conceptual purpose of propagate to propagate_policy
- [ ] R2-14 add get_cross_repo_impact() via graph provider
- [ ] R2-14 tests: policy propagate + cross-repo impact

### R2.15 — Replace CodeRabbit-ish reviewer
- [ ] R2-15 delete COMMIT_REVIEW_SYSTEM_PROMPT / current commit-review orchestration
- [ ] R2-15 keep CommitAuditor for actual commit-message auditing
- [ ] R2-15 code review = its own subsystem (review DAG); commit_audit_review_mode no longer drives review
- [ ] R2-15 tests: CommitAuditor kept, review DAG independent

### R2.16 — Review DAG wiring + `gitreins review` entrypoint
- [ ] R2-16 wire full review_pipeline (DESIGN §17) into the Pipeline
- [ ] R2-16 add `gitreins review` command (local ChangeSource) + `gitreins review --pr <n>`
- [ ] R2-16 criteria eval (Lane A) + defect review (Lane B) → combined review
- [ ] R2-16 tests: review DAG E2E + gitreins review CLI

### DOCS-001 — Final documentation pass re-run (after R2.16)
- [ ] DOCS-001 update README for the completed R2.7–R2.16 work: review DAG
  (orchestrator, reviewers, verifier, ranker, dedup, writer), ChangeSource +
  `gitreins review` / `gitreins review --pr <n>`, PR sandbox, intent system,
  ReviewLearning/provenance, propagate_policy, and the CodeRabbit-replacement.
- [ ] DOCS-001 refresh docs/architecture-v2.md + docs/ index to match built code
- [ ] DOCS-001 verify GitHub repo description + topics still match (`gh repo edit`)
- [ ] DOCS-001 formatting bar: README still skimmable (title + badges + nav +
  diagram + tables); `uv run ruff format --check .` passes (lints README/docs
  Python blocks)
- [ ] DOCS-001 judge + commit + push

## NEVER-DONE (audit — run only when board is empty)
- Full regression: `gitreins guard`, evaluator suite, review DAG E2E against a
  seeded repo, PR-mode smoke.
- CI/CD health, docs gate (README_v2, AGENTS, SECURITY, runbooks, repo description/topics).
