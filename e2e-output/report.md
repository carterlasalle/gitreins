# E2E-001 — Full GitReins CLI Lifecycle Against a Seeded Repo

**Date:** 2026-08-12 (session start 2026-08-11 22:09 UTC)
**Worker:** coding-hermes-worker (E2E-001, first E2E tick on this board)
**Harness under test:** gitreins 0.11.0 — editable install at `/home/hermes/gitreins2/.venv/bin/gitreins`
(engine resolves to `/home/hermes/gitreins2/engine/__init__.py`, i.e. THIS repo's code, not the pipx snapshot)
**Seeded repo:** `/tmp/gitreins-e2e-20260811-220946` (throwaway, outside the gitreins2 worktree)

## 1. Environment

| Item | Value |
|---|---|
| Host | Linux 6.8.0-137-generic |
| Python | 3.12.3 (system), pytest 9.1.1 (`/home/hermes/.local/bin/pytest`) |
| gitleaks | `/usr/bin/gitleaks` (present) |
| ruff | only inside `/home/hermes/gitreins2/.venv/bin/` (exported onto PATH for judge tier1) |
| semgrep / trivy | NOT installed (analyzers degrade gracefully — see §7) |
| Judge LLM | deepseek-v4-flash via OpenRouter; `GITREINS_LLM_*` sourced from `/home/hermes/.hermes/.env` |
| Judge caps | env overrides (highest priority): `GITREINS_MAX_INPUT_TOKENS=5000000`, `GITREINS_MAX_OUTPUT_TOKENS=1000000`, `GITREINS_MAX_ITERATIONS=120` |
| pipx copy | `/home/hermes/.local/bin/gitreins` also resolves engine to gitreins2 (editable) — stale-snapshot pitfall NOT present; task-mandated `.venv/bin/gitreins` used throughout |

## 2. Seeded-repo layout (initial commit c93b331)

```
.gitignore            __pycache__/, *.pyc, .venv/, .gitreins/tasks.yaml
calc.py               add(a, b) — the initial "real function"
test_calc.py          2 tests (test_add_positives, test_add_negatives)
pyproject.toml        name=gitreins-e2e-calc, [tool.pytest.ini_options] pythonpath=["."], testpaths=["test_calc.py"]
```

## 3. Step-by-step evidence

### Step 1 — Seed repo (exit 0)
```
git init -b master            → Initialized empty Git repository
pytest -x --tb=short          → 2 passed in 0.01s
git commit -m "chore: initial tiny Python project for GitReins E2E"
                              → c93b331, 4 files changed, 30 insertions(+)
```

### Step 2 — `gitreins init` + baseline commit (exit 0)
```
$ /home/hermes/gitreins2/.venv/bin/gitreins init
GitReins init: /tmp/gitreins-e2e-20260811-220946
  Language:    Python
  Packages:    1
  Test cmd:    pytest -x --tb=short
  Test mode:   full
  Eval cap:    15 iterations
  History:     enabled
  Static analysis: enabled (pyright)
Updated: guards, evaluator, history, pre-commit hook, .gitleaks.toml
EXIT=0
```
Notes:
- Generated `.gitleaks.toml` was verified against the real gitleaks: `gitleaks detect --source . --no-git --config .gitleaks.toml` → `no leaks found`, exit 0. The historical "invalid regex → gitleaks PANIC" pitfall (skill pitfall 2) does NOT reproduce on gitreins 0.11.0. No file edits needed.
- `init` installed the pre-commit hook (`.git/hooks/pre-commit`, 308B) — hook invokes bare `gitreins guard`; PATH exported to the editable install so the hook runs THIS repo's engine.
- Baseline commit `b846cb2` — the pre-commit hook ran the guard automatically:
```
Tier 1 Guards: PASS  (test mode: full)
  ✓ secrets — clean
  ✓ lint — ok
  ✓ tests (full)
  ✓ static_analysis
```

### Step 3 — Demo task with real criteria (exit 0)
```
$ gitreins task create E2E-DEMO 'add subtraction function' \
    'calc.py has a sub() function returning a-b' \
    'test_calc.py covers sub() incl. negative results'
Created task: E2E-DEMO — add subtraction function
  1. calc.py has a sub() function returning a-b
  2. test_calc.py covers sub() incl. negative results
$ gitreins task start E2E-DEMO        → Started: E2E-DEMO → in_progress
$ gitreins task list                  → ◐ E2E-DEMO  add subtraction function
```

### Step 4 — Real code change + full guard loop (GUARD_EXIT=0)
`calc.py` gained `sub(a, b) -> a - b` (calc.py:9-12); `test_calc.py` gained 3 tests
(`test_sub_positives`, `test_sub_negative_result` → sub(3,5)==-2, `test_sub_order_matters`).
```
$ git add calc.py test_calc.py        → 2 files changed, 18 insertions(+), 1 deletion(-)
$ gitreins guard
Tier 1 Guards: PASS  (test mode: full)
  ✓ secrets — clean
  ✓ lint — ok
  ✓ tests (full)
  ✓ static_analysis
GUARD_EXIT=0
$ pytest -q                           → 5 passed in 0.01s   (real suite, not vacuous)
```

### Step 5 — REAL Tier 2 judge (JUDGE_EXIT=0, Overall PASS ✓)
Env per task: `set -a; source /home/hermes/.hermes/.env; set +a;`
`export GITREINS_MAX_INPUT_TOKENS=5M GITREINS_MAX_OUTPUT_TOKENS=1M GITREINS_MAX_ITERATIONS=120`
plus `PATH="$HOME/gitreins2/.venv/bin:$PATH"` (tier1 subprocess runs bare `ruff check .`).
```
Completed: E2E-DEMO → complete
Stage tier1: PASS
    ✓ lint:      (exit 0)
  ✓ secrets:     gitleaks scan completed in 17ms — no leaks found (exit 0)
  ✓ tests:       pytest — collected 5 items, 5 passed in 0.02s (exit 0)
Stage tier2: PASS
  COMPLETE
  ✓ calc.py has a sub() function returning a-b: calc.py:9-12 defines
    `def sub(a: float, b: float) -> float:` with `return a - b`
    (search confirmed single match at calc.py:9)
  ✓ test_calc.py covers sub() incl. negative results: test_sub_positives (sub(5,3)==2),
    test_sub_negative_result (sub(3,5)==-2), test_sub_order_matters.
    `python -m pytest test_calc.py -v` output: 5 passed in 0.02s, all sub tests PASSED
Overall: PASS ✓
  📋 Verdict saved: 7ee0317a
JUDGE_EXIT=0
```
Wall time ≈ 45 s (tiny repo; tier2 completed within its first evaluation pass — no cap hits).

**verdict.json exists:** `.gitreins/history/2026-08-12/a83d2332/verdict.json` on the `gitreins`
branch (history storage = git). `gitreins report`:
```
Recent: 1 evaluations
Pass:   1 (100%)
Fail:   0 (0%)
  ✓ E2E-DEMO     add subtraction function
Storage: git (.../.gitreins/history)   Total entries: 1
```

### Step 6 — Review DAG, local change source (REVIEW_EXIT=0, ran clean)
`gitreins review` (no args → `WorkingTreeChangeSource`: staged+unstaged vs HEAD;
reviewed the staged sub() change, range `b846cb2b...de2a659f`).
```
GitReins review — review
Change: 2 file(s): calc.py, test_calc.py
Range: b846cb2b...de2a659f
Lane B — defect findings (0)
  (no findings — change looks clean)
Lane A — criteria evaluation (0 criterion/criteria)
  (no criteria provided — Lane A skipped)
REVIEW_EXIT=0
```
**DEFECT — no `review_runs/<sha>/` artifacts produced** (see §8 DEFECT-1).

### Step 7 — Commit the change in the seeded repo (exit 0, hook guard PASS)
```
$ git add calc.py test_calc.py && git commit -m "feat: add sub() function with tests (E2E-DEMO)"
Tier 1 Guards: PASS  (test mode: full)   ← pre-commit hook, real code change
[master 6aa4e94] feat: add sub() function with tests (E2E-DEMO)
```
Final log: `6aa4e94` feat → `b846cb2` baseline config → `c93b331` initial project.

## 4. Judge verdict summary (grep of verdict.json)

```
task_id:      E2E-DEMO
passed:       true
tier1:        passed=true, steps lint(0) secrets(0) tests(0) — real command output quoted
tier2:        passed=true, summary=COMPLETE
  criteria 1: PASS — calc.py:9-12 defines sub() returning a-b (search confirmed)
  criteria 2: PASS — tests cover sub() incl. negative results; pytest 5 passed
evaluated_at: 2026-08-12T05:12:26Z
```

## 5. Review DAG outcome

DAG ran every §17 stage (change_analysis → 5 parallel analyzers → scout → retrieval →
3 parallel reviewers → merge → verify → rank), printed the combined report, exited 0.
Lane B: 0 findings. Lane A skipped (no --criteria). Analyzer degradation (expected,
non-fatal): semgrep and trivy not on PATH → skipped with warning; pylsp emitted a
file-URI warning; three LLM structured-output schema parse retries
(`ChangedSymbol`/`RetrievalRequest`) recovered — see OBS-3.
**Artifacts missing**: `review_runs/` does not exist in the seeded repo after the run.

## 6. Defects found

| ID | Severity | Summary |
|---|---|---|
| DEFECT-1 | medium | `gitreins review` (local CLI path) does NOT persist §13 `review_runs/<sha>/` artifacts — archiver is only wired into the GitHub-App path |
| OBS-2 | low | `task complete`/judge run left previously staged files unstaged (index reset mid-run) |
| OBS-3 | low | review DAG LLM schema-parse warnings (recovered) — noisy agent tool-call protocol |
| OBS-4 | info | missing optional analyzers degrade gracefully; no crash |

## 7. Verification of "real" claims

- Editable install verified: `.venv/bin/python -c "import engine"` → `/home/hermes/gitreins2/engine/__init__.py`.
- Guard tests were NOT vacuous: files were staged before every guard run; direct `pytest -q` reproduced `5 passed`.
- Judge tier2 quoted real artifacts: `calc.py:9-12` source line and `python -m pytest test_calc.py -v` output.
- verdict.json read from the `gitreins` branch (history storage=git) and parsed (JSON, `passed: true`).
- No LLM keys appear in any log, report, or commit. Seeded repo stays in /tmp (untracked, throwaway).

## 8. DEFECT-1 detail — local `gitreins review` never archives review_runs/

- `gitreins review` (gitreins/cli.py:1794) → `Pipeline.run_review(task)` (engine/pipeline.py:152);
  the review pipeline config (`_default_review_pipeline`, cli.py:1607) contains no archive stage
  and `run_review` has no archiver parameter.
- The §13 `ReviewRunArchiver` (engine/review/history.py) is wired ONLY via
  `ReviewOrchestrator` (engine/review/orchestrator.py:145-147) and that orchestrator is
  instantiated ONLY in the GitHub-App path (engine/github/app.py:142). Local/CLI reviews
  therefore complete with exit 0 and findings but persist nothing under `review_runs/<sha>/`
  (manifest.json, change.json, static-evidence.json, scout.json, candidates.json,
  verification.json, final-findings.json, requirements.json, usage.json).
- Reproduction: any repo, `gitreins review` → after exit 0, `review_runs/` absent.
  `grep -rn "ReviewRunArchiver\|archiver" gitreins/cli.py engine/pipeline.py` → no hits.
- Impact: no persistent review history for local reviews; R2.13/R2.16 §13 behavior is
  CLI-unreachable. Tracked as E2E-001-DEFECT-001 in tasks.md.
