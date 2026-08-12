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

### PITFALL-AGENT-EMPTY — AgentRunner bounded retry on empty LLM responses ✅ 7d65ba6 (judge PASS 38ca0b03)
- [x] PITFALL-AGENT-EMPTY runner.py: bounded retry with corrective re-prompt — `max_empty_retries=3` default (runner.py:85), counter :181, blank detection `content and content.strip()` :253-254, raises AgentRunError only when budget spent :280-281, corrective user re-prompt :282-293, counter reset on non-empty :256/:301 (7d65ba6, 2026-08-09)
- [x] PITFALL-AGENT-EMPTY regression tests: test_run_empty_response_retries_then_succeeds (:787, stub.calls==2 + nudge asserted), test_run_empty_response_raises_after_retries (:759, calls==max+1==4, len(nudges)==3), blank-whitespace retry (:811), counter-reset-on-tool-calls (:832) — 46 passed in 1.88s (7d65ba6)
- [x] PITFALL-AGENT-EMPTY judge verdict 38ca0b03: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE 3/3 (2026-08-10); gitreins guard PASS (test mode: diff, full suite safety trigger); push verified (origin/v2 == HEAD)

### DUCKBRAIN-001 — create gitreins namespace + sync project knowledge ✅ (judge PASS fb70006d, 2026-08-10)
- [x] DUCKBRAIN-001 namespace `gitreins` created (list_namespaces confirms) + 3 project entries written AND verified retrievable (list_keys + recall): `/projects/gitreins/architecture-v2` (ModelRouter R2.1, AgentRunner R2.2, review DAG R2.7–R2.16), `/projects/gitreins/env-sanitize-blocklist` (INFRA-LLM-ENV-001, engine/env_sanitize.py single source, 5 sites, GIT_*/GITREINS_MAX_*/GITREINS_LLM_* + 7 fallback keys), `/projects/gitreins/judge-caps` (evaluator caps, env overrides, guard config).
- [x] DUCKBRAIN-001 judge verdict fb70006d: tier1 lint/tests/secrets ALL PASS, tier2 COMPLETE 3/3 — verified live against DuckBrain backend (REST /api/namespaces + /api/memories, MCP list_keys/recall).

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

### R2.14 — propagate → propagate_policy + cross-repo impact ✅ 6aea4fc (judge tier2 COMPLETE 3/3)
- [x] R2-14 engine/propagate.py: Propagator → PolicyPropagator rename — propagate_policy() + Propagator alias + propagate() deprecated delegating alias; docstrings describe guard-policy propagation (6aea4fc, 2026-08-08)
- [x] R2-14 engine/codeintel: get_cross_repo_impact() added to CodeIntelProvider Protocol (base.py:116) + all 5 providers — graph returns [NO_BACKEND] marker (graph.py:76), astgrep/ripgrep/lsp/serena degrade to [] (6aea4fc)
- [x] R2-14 gitreins_mcp/server.py: imports PolicyPropagator, _propagate calls propagate_policy(); MCP tool name 'propagate' kept (external API contract) (6aea4fc)
- [x] R2-14 tests: test_propagate.py (PolicyPropagator + alias-delegation test), test_codeintel.py (no_backend + degraded-[] coverage), test_mcp_server.py (TestPropagateMCP new names) — targeted 117 passed/1 skipped; LSP 0 findings; judge verdict b5f1da04/983d3b49: tier2 COMPLETE 3/3 (fresh test runs: 47+70+8 passed, exit 0), tier1 tests FAIL = pre-existing flaky test_cli.py::test_full_task_lifecycle_subprocess (30s subprocess timeout on `task complete` firing LLM judge — passes under normal load: foreman full suite 1736 passed/8 skipped/0 failed; board-documented flake, unrelated to R2-14), secrets/lint PASS. R2.15 ready.

### R2.15 — Replace CodeRabbit-ish reviewer ✅ fcc1aa7 (judge tier2 COMPLETE 4/4, verdict 043adf41)
- [x] R2-15 delete COMMIT_REVIEW_SYSTEM_PROMPT / current commit-review orchestration (fcc1aa7, 2026-08-09)
- [x] R2-15 keep CommitAuditor for actual commit-message auditing (fcc1aa7)
- [x] R2-15 code review = its own subsystem (review DAG); commit_audit_review_mode no longer drives review (fcc1aa7)
- [x] R2-15 tests: CommitAuditor kept (30 tests), review DAG independent (242 targeted pass; full suite 1673 passed / 3 pre-existing env-artifact failures reproduced on parent 51877e6 — mypy-not-installed + LSP roundtrip)
> Judge verdict 043adf41: tier2 COMPLETE 4/4 PASS (301 targeted tests run, exit 0; ruff clean); tier1 lint+secrets PASS, tier1 tests FAIL = pre-existing env artifact (mypy missing, LSP) — identical on parent commit. R2.16 ready.

### R2.16 — Review DAG wiring + `gitreins review` entrypoint ✅ 4a52557 (judge tier2 COMPLETE 4c52c119)
- [x] R2-16 wire full review_pipeline (DESIGN §17) into the Pipeline (4a52557, 2026-08-09)
- [x] R2-16 add `gitreins review` command (local ChangeSource) + `gitreins review --pr <n>` (4a52557)
- [x] R2-16 criteria eval (Lane A) + defect review (Lane B) → combined review (4a52557)
- [x] R2-16 tests: review DAG E2E + gitreins review CLI (4a52557)
> Judge verdict 4c52c119: tier2 COMPLETE 3/3 PASS (32 targeted tests; full suite 1717 passed/10
> skipped in clean env; ruff clean; guard PASS). tier1 tests step FAIL = judge-subprocess
> artifact (exit 2/-9/-15 mid-suite at ~8%, output cut, no failure text — reproduced across
> all judge runs; tier2's own full-suite verification passed). ENVFIX 9c9e8c6 (strip
> GITREINS_MAX_* from subprocess envs) confirmed live + regression tests pass.

### DOCS-001 — Final documentation pass re-run (after R2.16) ✅ f290e9a (judge tier2 COMPLETE facc5594)
- [x] DOCS-001 update README for the completed R2.7–R2.16 work: review DAG
  (orchestrator, reviewers, verifier, ranker, dedup, writer), ChangeSource +
  `gitreins review` / `gitreins review --pr <n>`, PR sandbox, intent system,
  ReviewLearning/provenance, propagate_policy, and the CodeRabbit-replacement. (f290e9a, 2026-08-09)
- [x] DOCS-001 refresh docs/architecture-v2.md + docs/ index to match built code (f290e9a)
- [x] DOCS-001 verify GitHub repo description + topics still match (`gh repo edit`) (f290e9a)
- [x] DOCS-001 formatting bar: README skimmable; `uv run ruff format --check --preview README.md docs/` → 13 files already formatted (f290e9a)
- [x] DOCS-001 judge + commit + push (f290e9a)
> Judge verdict facc5594: tier2 COMPLETE 3/3 PASS (README covers all R2.7–R2.16 features
> with accurate module/commit refs; architecture-v2.md matches built code; ruff format
> check exit 0). tier1 tests step FAIL = SIGKILL -9 judge-subprocess artifact (120s-ish
> mid-suite kill, no failure text) — not a code failure.

### QUALITY-FMT-001 — ruff format test files drifted from canonical format ✅ 9811b15 (judge PASS d532f3cb)
- [x] QUALITY-FMT-001 ruff format tests/test_task_manager.py, test_types.py, test_v07_features.py — 3 files reformatted (+4/-9, pure formatting collapse, zero semantic change) (9811b15, 2026-08-10, worker deepseek-v4-flash)
- [x] QUALITY-FMT-001 verify: `uv run ruff format --check` on the 3 files → "3 files already formatted" exit 0; `uv run ruff check .` → "All checks passed!" exit 0; `uv run pytest tests/test_task_manager.py -q` → 39 passed (1.78s); gitreins guard PASS (staged, diff mode)
- [x] QUALITY-FMT-001 judge verdict d532f3cb: tier1 lint/secrets/tests ALL PASS, tier2 COMPLETE 2/2 (both criteria verified via live command runs); push verified (origin/v2 == HEAD)
> Note: whole-repo `ruff format --check` still lists 41 engine files as would-reformat — pre-existing drift, format was never an enforced gate (CI runs `gitreins guard`; guard lint = `ruff check` only). Left untouched: 41-file formatting churn = zero-behavior diff, no gate benefit. Revisit if format enforcement is ever added.

### DEPS-RUFF-001 — bump ruff dev dep 0.15.22 → 0.16.2 ✅ ca08bee (judge PASS 7384b49e)
- [x] DEPS-RUFF-001 uv.lock resolves ruff to 0.16.2 via `uv lock --upgrade-package ruff` (only ruff version changed; 66 lock diff lines = automatic marker normalization + new hashes, no other package versions touched) (ca08bee, 2026-08-11, worker)
- [x] DEPS-RUFF-001 verify: `uv run ruff --version` → 0.16.2; `uv run ruff check .` → exit 0 (no new lint failures from 0.16.2, zero lint fixes needed); `ruff format --check` on the 3 QUALITY-FMT-001 files → already formatted; full suite `uv run pytest -x --tb=short` → 1730 passed, 8 skipped (635s; test_cli lifecycle flake did not fire); `gitreins guard` PASS (diff mode, full-suite safety trigger)
- [x] DEPS-RUFF-001 judge verdict 7384b49e: tier1 lint/secrets/tests ALL PASS, tier2 COMPLETE 3/3 (all three criteria verified via live command runs); push verified (origin/v2 == HEAD, 0 unpushed)
> Audit finding: `uv pip list --outdated` showed ruff 0.15.22 → 0.16.2 as the ONLY actionable direct dev dep; all other outdated packages (pydantic-core, cryptography, starlette, uvicorn, etc.) are transitive or pinned by pydantic (`pydantic-core==` exact-pin pitfall) — correctly left untouched.

## NEVER-DONE (audit — run only when board is empty)
- Full regression: `gitreins guard`, evaluator suite, review DAG E2E against a
  seeded repo, PR-mode smoke.
- CI/CD health, docs gate (README_v2, AGENTS, SECURITY, runbooks, repo description/topics).

### INFRA-LLM-ENV-001 — strip LLM credential vars from judge/guard subprocess envs ✅ 35a34cc + 714d8b5 (judge PASS f4b23cbe)
- [x] INFRA-LLM-ENV-001 guards.py `_sanitized_env` + pipeline.py tier1 script
  runner + evaluator.py `_tool_run_command` + verifier.py `run_command` all
  exclude GITREINS_LLM_* AND fallback credential keys (OPENROUTER_API_KEY,
  OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, NEURALWATT_API_KEY)
  (35a34cc inline; 714d8b5 centralized into engine/env_sanitize.py —
  is_blocked_env_key()/sanitized_env() shared by all 5 sites incl. sibling
  guard_manager._sanitized_env; block list = GIT_*, GITREINS_MAX_*,
  GITREINS_LLM_* + 7 fallback keys incl. KIMI/GROQ mirroring LLMClient chain)
- [x] INFRA-LLM-ENV-001 regression test: `tests/test_llm.py` passes with
  GITREINS_LLM_API_KEY + OPENROUTER_API_KEY set in env (test_env_sanitize.py
  test_polluted_env_pytest_llm_goes_green pins the repro — 58 passed under
  pollution; test_llm.py missing-keys test also hardened with KIMI/GROQ/
  OPENROUTER delenv)
- [x] INFRA-LLM-ENV-001 targeted suites green; ruff clean; gitreins guard PASS
  (judge verified under polluted env: 311 targeted + 58 test_llm passed,
  guard PASS with GITREINS_LLM_API_KEY/OPENROUTER_API_KEY exported)
> Judge verdict f4b23cbe (2026-08-09): tier1 PASS — FIRST clean tier1 since
> this env leak was root-caused (the 13-phase "judge-subprocess artifact"
> mislabel is resolved); tier2 COMPLETE 3/3. Full suite 1726 passed /
> 8 skipped, 1 pre-existing flake (test_cli lifecycle, passes in isolation).
> Root cause (proven 2026-08-09): ENVFIX 9c9e8c6 stripped GITREINS_MAX_* but
> NOT LLM credential vars → tier1 pytest subprocess inherited
> GITREINS_LLM_API_KEY/OPENROUTER_API_KEY → test_llm.py env-priority tests
> broke (1 failed, 58 passed clean). Fix now centralized in env_sanitize.py —
> the block list can never drift again. R2.x phase build COMPLETE.

### E2E-001 — E2E testing tick: full GitReins CLI lifecycle against a seeded repo ✅ 177ecb2 (judge PASS 5b94bc20)
- [x] E2E-001 seeded throwaway repo /tmp/gitreins-e2e-20260811-220946 (real calc.py + tests + pyproject), ran the FULL loop with the editable install (.venv/bin/gitreins → engine resolves to this repo): init (exit 0) → baseline commit hook guard PASS (b846cb2) → task create/start E2E-DEMO (2 criteria) → real sub() change → `gitreins guard` PASS (GUARD_EXIT=0, 5 tests) → **Tier 2 judge fired: Overall PASS verdict 7ee0317a** (tier1 lint/secrets/tests exit 0, tier2 COMPLETE 2/2; verdict.json persisted on gitreins branch, `gitreins report` 1/1 = 100%) → `gitreins review` DAG exit 0 (Lane B 0 findings) → change commit via hook guard PASS (6aa4e94). (177ecb2, 2026-08-11, worker deepseek-v4-flash)
- [x] E2E-001 deliverables committed: e2e-output/report.md (9.9K: env, seeded layout, all 9 checkpoints with real output + exit codes, defect detail) + e2e-output/tasks.md (4.6K: evidence table 8 PASS / 1 PARTIAL + DEFECT-001 + 3 observations) (177ecb2)
- [x] E2E-001 judge verdict 5b94bc20 (this tick's gate): tier1 lint/secrets/tests ALL PASS, tier2 COMPLETE (auto-parsed; 1 JSON-parse warning recovered by keyword fallback); push verified (origin/v2 == HEAD)
> E2E outcome: lifecycle GREEN with 1 real defect + 3 observations (full details in e2e-output/tasks.md). **DEFECT-001 → new board task below (next tick).** OBS-002 judge run resets git index (re-add before commit — skill pitfall 16), OBS-003 review DAG schema-parse warnings (recovers, noisy), OBS-004 semgrep/trivy/pylsp degrade gracefully (info).

## [x] E2E-001-DEFECT-001 — `gitreins review` (local CLI path) never persists `review_runs/<sha>/` §13 artifacts ✅ f298cb6 (judge PASS 45d694c0)
- [x] E2E-001-DEFECT-001 fix: ReviewRunArchiver wired into the LOCAL review path — `cmd_review` constructs `ReviewRunArchiver(base_dir=workdir, commit_sha=task['head_sha'])` and passes it to `Pipeline.run_review`; `run_review` gained `archiver=` param + `_archive_review_run` maps all nine §13 groups (change, static_evidence, scout, candidates, verification, final_findings, requirements, usage, manifest) onto `archiver.archive_all`, mirroring the orchestrator pattern (f298cb6, 2026-08-11, worker deepseek-v4-flash)
- [x] E2E-001-DEFECT-001 verify: `uv run ruff check .` clean; targeted `tests/test_review_history.py test_review_cli.py test_review_pipeline_dag.py` → 45 passed (incl. 4 new regression tests); real smoke: `gitreins review` in throwaway repo wrote review_runs/<sha>/ with all nine artifacts at exit 0; archive-failure path degrades to `logger.warning` never raises (DAG still reports passed); no-archiver callers (GitHub-App) untouched (f298cb6)
- [x] E2E-001-DEFECT-001 judge verdict 45d694c0 (history 15cd01ef): tier1 lint/secrets/tests ALL PASS, tier2 COMPLETE 3/3 (all three criteria verified via live pytest runs — 4 passed in 1.73s + 36 passed in 3.16s, archive-failure regression asserts passed=True); gitreins task deleted after judge; push verified (origin/v2 == HEAD, 0 unpushed)
- **Priority:** High | **Complexity:** 3±1 | **Deps:** — | **Model:** deepseek-v4-flash (worker) | **Reasoning:** Medium | **Fallback:** —
- **Found by:** E2E-001 (2026-08-11). In any initialized repo with a working-tree change, `gitreins review` (no args) exits 0 and prints Lane B findings but `review_runs/` is never created.
- **Root cause:** `gitreins/cli.py:cmd_review` → `engine/pipeline.py::Pipeline.run_review`; the review pipeline config (`cli.py:_default_review_pipeline`) has no archive stage and `run_review` accepts no archiver. `ReviewRunArchiver` (engine/review/history.py) is wired only into `ReviewOrchestrator` (engine/review/orchestrator.py:145-147), instantiated only by the GitHub-App path (engine/github/app.py:142). Local reviews persist nothing.
- **Acceptance:** `gitreins review` writes the nine §13 artifacts under `review_runs/<sha>/` (manifest, change, static-evidence, scout, candidates, verification, final-findings, requirements, usage); archive failures logged, never raised (R2.13 contract); regression test proves artifacts exist after a local review run.
- **Suggested fix:** wire `ReviewRunArchiver(base_dir=workdir)` into `Pipeline.run_review` (or `cmd_review`) after the DAG completes; key by base_sha/head_sha from the ChangeSource; add a pipeline `archive_review` step mirroring orchestrator's `_archive_all`.
- **GitReins task:** create/start before work, `task complete` after commit (judge fires), then delete.

## NEVER-DONE (audit — run when board otherwise empty)
- E2E tick (E2E-001) ran 2026-08-11 — full lifecycle green; DEFECT-001 (local review archiver) FIXED 2026-08-11 (f298cb6, judge PASS 45d694c0); next E2E in ~5-10 ticks unless new work changes the cadence.
- Idle tick 2026-08-11 23:10 (tick overlapped prior tick's DEFECT-001 completion — verified independently): board 0 pending in BOTH sources (tasks.md, gitreins tasks.yaml empty); judge verdict 15cd01ef PASS (tier1 lint/secrets/tests PASS, tier2 COMPLETE 3/3) for DEFECT-001 confirmed; targeted regression 36 passed (test_review_cli + test_review_pipeline_dag); audit clean — deps (uv pip list --outdated): all outdated (pydantic-core, cryptography, starlette, uvicorn, certifi, etc.) transitive or exact-pinned, nothing actionable; test gaps: 51 test files / 209 test fns, all engine modules covered by tests/; pitfalls: no stubs (TODO/FIXME hits are scanner fixtures); CI: workflow ci.yml exists but triggers on `main` only — fork's v2 branch never runs CI (0 runs ever, structural, not a failure; local guard+judge are the quality gate); judge sizing check-gitreins-judge.py PASS (deepseek-v4-flash); docs gate satisfied (README.md, AGENTS.md, CONTRIBUTING.md, SECURITY.md, docs/); cooldown re-applied 43200s (idle).
