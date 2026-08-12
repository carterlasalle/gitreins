# E2E-001 Findings Board

Source: full GitReins CLI lifecycle E2E against seeded repo `/tmp/gitreins-e2e-20260811-220946`
(evidence in `e2e-output/report.md`). Overall lifecycle verdict: **GREEN with 1 defect + 3 observations.**

## Evidence table (lifecycle checkpoints)

| # | Checkpoint | Result | Evidence |
|---|---|---|---|
| 1 | `gitreins init` (defaults) | PASS | exit 0; config.yaml written; hook + .gitleaks.toml generated |
| 2 | gitleaks vs generated config | PASS | `no leaks found`, exit 0 (old "invalid regex" pitfall not reproduced on 0.11.0) |
| 3 | Baseline commit via pre-commit hook | PASS | `Tier 1 Guards: PASS` (secrets/lint/tests(full)/static_analysis) — b846cb2 |
| 4 | `task create` + `task start` | PASS | E2E-DEMO → in_progress; 2 criteria stored |
| 5 | Real change + `gitreins guard` (staged) | PASS | `Tier 1 Guards: PASS`, GUARD_EXIT=0; `pytest -q` → 5 passed |
| 6 | Tier 2 judge `task complete` | PASS | Overall: PASS ✓; verdict 7ee0317a; both criteria ✓; tier1 lint/secrets/tests exit 0 |
| 7 | verdict.json persisted | PASS | `.gitreins/history/2026-08-12/a83d2332/verdict.json` on `gitreins` branch; `gitreins report` → 1/1 pass (100%) |
| 8 | Review DAG (`gitreins review`, local source) | PARTIAL | exit 0, 0 findings, all stages ran — but §13 artifacts NOT archived (DEFECT-1) |
| 9 | Commit of the change (hook guard) | PASS | `Tier 1 Guards: PASS`, 6aa4e94 |

## Tasks

### E2E-001-DEFECT-001 — `gitreins review` (local CLI path) never persists `review_runs/<sha>/` artifacts (DESIGN_v2 §13)

- **Severity:** medium (feature/behavior gap; DAG itself works)
- **Status:** open
- **Repro:** in any initialized repo with a working-tree change, run `gitreins review` (no args).
  It exits 0, prints Lane B findings, but `review_runs/` is never created.
- **Root cause:** `gitreins/cli.py:cmd_review` → `engine/pipeline.py::Pipeline.run_review`;
  the review pipeline config (`gitreins/cli.py:_default_review_pipeline`) has no archive stage
  and `run_review` accepts no archiver. `ReviewRunArchiver` (engine/review/history.py) is wired
  only into `ReviewOrchestrator` (engine/review/orchestrator.py:145-147), instantiated only by
  the GitHub-App path (engine/github/app.py:142). Local reviews therefore persist nothing.
- **Acceptance:** `gitreins review` writes the nine §13 artifacts under `review_runs/<sha>/`
  (manifest, change, static-evidence, scout, candidates, verification, final-findings,
  requirements, usage); archive failures logged, never raised (R2.13).
- **Suggested fix:** wire `ReviewRunArchiver(base_dir=workdir)` into `Pipeline.run_review`
  (or `cmd_review`) after the DAG completes; key by `base_sha`/`head_sha` from the ChangeSource;
  add a pipeline `archive_review` step mirroring the orchestrator's `_archive_all` behavior.

### E2E-001-OBS-002 — judge `task complete` run resets the git index (staged changes become unstaged)

- **Severity:** low
- **Observed:** after `git add calc.py test_calc.py` (staged) and the judge run,
  `git status` showed both files unstaged (` M`), index empty. Guard/judge ran fine regardless;
  a subsequent `git add` + commit was unaffected.
- **Likely source:** a git-stash-create / history-commit interaction in the judge or history
  persistence path. Worth a follow-up to confirm it is deliberate (WorkingTreeChangeSource
  head_sha materialization uses `git stash create`, which is read-only) or a side effect of the
  verdict commit on the `gitreins` branch.
- **Impact:** low — user must re-`git add` before committing after a judge run.

### E2E-001-OBS-003 — review DAG emits LLM structured-output schema-parse warnings (recovers)

- **Severity:** low
- **Observed:** 3 warnings in the review run:
  `Output schema parse failed: Missing required field 'symbol' for ChangedSymbol`,
  `Missing required field 'query' for RetrievalRequest`, `Missing required field 'type' for RetrievalRequest`.
  The DAG completed with exit 0 and 0 findings — agents recovered on retry.
- **Impact:** noisy logs; suggests the agent tool-call schema validation is stricter than what the
  model emits for these tool types. Consider tolerant defaults or prompt-side schema emphasis.

### E2E-001-OBS-004 — optional analyzers degrade gracefully (semgrep, trivy, pylsp)

- **Severity:** info
- **Observed:** `semgrep not found on PATH — skipping scan`, `trivy not found — skipping fs scan`,
  `LSP tool 'pylsp' error: relative path can't be expressed as a file URI`. None crashed the DAG.
- **Impact:** none for this run; documented as expected graceful degradation. The pylsp URI error
  is a candidate for a relative-path handling fix if pylsp is a supported analyzer target.
