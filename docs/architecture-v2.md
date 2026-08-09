# GitReins 2.0 — Review Ensemble Architecture (runbook)

> Branch `v2`. Full spec: [`DESIGN_v2.md`](../DESIGN_v2.md). This runbook
> documents the architecture **as built** through R2.16 (2026-08-09).

## Two independent AI lanes

GitReins 2.0 answers two different questions with two separate engines:

| | **Lane A — Requirements / completeness** | **Lane B — Defect discovery** |
|---|---|---|
| Question | *Did the dev implement all stated criteria?* | *Did the patch break something, regardless of requirements?* |
| Engine | `CriteriaEvaluator` (re-expressed `AgenticEvaluator`) | Review DAG (`ReviewOrchestrator`) |
| Input | Task criteria | Change (working tree / commit range / PR) + repo context |
| Output | Per-criterion PASS/FAIL | Verified, ranked findings |
| Entry point | `gitreins task complete <id>` | `gitreins review [--pr <n> \| --base <ref> --head <ref>]` |

## Module map (R2.1–R2.16 built)

```text
engine/
├── router.py              # ModelRouter.for_role(role) — per-role LLM clients
├── agents/                # AgentRunner generic bounded loop
│   ├── runner.py          #   caps, compaction, tool-dedup, schema parsing
│   ├── budget.py          #   iteration/wall-clock/token accounting
│   ├── tools.py           #   tool registry + dedup + bounded reads
│   └── schemas.py         #   output schemas (Pydantic-ish)
├── evaluator.py           # CriteriaEvaluator(AgentRunner) — Lane A
├── evidence/              # first-class Evidence
│   ├── models.py          #   Evidence(id, kind, source, file, lines, payload)
│   ├── store.py           #   append/query/provenance
│   ├── provenance.py
│   └── producers.py
├── analyzers/             # evidence producers
│   ├── semgrep.py  astgrep.py  trivy.py  gitleaks.py
├── codeintel/             # repository intelligence providers
│   ├── base.py            #   CodeIntelProvider Protocol (11 queries + get_cross_repo_impact)
│   ├── astgrep.py         #   structural pattern search
│   ├── ripgrep.py         #   lexical recall
│   ├── lsp.py             #   semantic queries (diagnostics, definition, refs)
│   ├── graph.py           #   hilo-style graph (interface; [NO_BACKEND] marker)
│   ├── serena.py          #   Serena MCP wrapper (lazy client)
│   └── tools.py           #   agent tool adapters
├── review/
│   ├── orchestrator.py    #   ReviewOrchestrator — DAG driver (R2.7)
│   ├── reviewers.py       #   runtime / contract / security reviewers (R2.7)
│   ├── verifier.py        #   adversarial verifier + deterministic BLOCK (R2.8)
│   ├── ranker.py          #   deterministic usefulness ranking (R2.9)
│   ├── dedup.py           #   evidence-overlap dedup (R2.9)
│   ├── writer.py          #   batched comment writer — GitHub/agent (R2.9)
│   ├── scout.py           #   cheap scout → retrieval plan (R2.6)
│   ├── context_builder.py #   deterministic evidence planner (R2.6)
│   ├── history.py         #   ReviewRunArchiver — per-SHA artifacts (R2.13)
│   └── learning.py        #   ReviewLearning — feedback → durable rules (R2.13)
├── github/
│   ├── checkout.py        #   ChangeSource Protocol + 3 sources (R2.10)
│   ├── publisher.py       #   PR/issue comments; never raises (R2.10)
│   ├── checks.py          #   status checks (R2.10)
│   ├── app.py             #   run_pr_review — PR-mode glue (R2.10)
│   └── sandbox.py         #   DockerSandbox — ephemeral PR sandbox (R2.11)
├── task_manager.py        # intent system: intent_context()/intent_prompt() (R2.12)
└── propagate.py           # PolicyPropagator.propagate_policy() (R2.14)
```

## The review DAG (R2.16 — complete, wired into the Pipeline)

```text
change_analysis
  → static_evidence (parallel: lsp / semgrep / typecheck / secrets / dependencies)
  → scout (cheap model → retrieval plan)
  → evidence_retrieval (deterministic execution of the plan)
  → reviewers (runtime / contract / security — parallel AgentRunner roles)
  → candidate_merge (dedup by evidence overlap)
  → verify_findings (adversarial — "disprove F<n>", per finding)
  → rank (deterministic factors + BLOCK policy, not LLM CVE scores)
  → criteria_eval (Lane A, when the task or --criteria has criteria)
  → publish (PR comments + status checks for --pr; no-op locally)
```

The DAG is config-driven: `.gitreins/config.yaml` `review_pipeline:` overrides
the §17 default; the default lives in `gitreins/cli.py::_default_review_pipeline`.
Pipeline stage types: `change_analysis`, `analyzer`, `agent` (with `role`),
`evidence_retrieval`, `merge_findings`, `verify_findings`, `rank_findings`,
`criteria_eval` (conditional on `task.has_criteria`), `publish_review`.

| Stage | Type / role | Responsibility |
|---|---|---|
| change_analysis | stage | Diff → changed files, base/head SHAs |
| static_evidence | 5 parallel analyzers | LSP diagnostics, semgrep, typecheck, secrets, dependencies → `Evidence` |
| scout | `agent` role=scout | Cheap model emits a retrieval plan (symbols, requests, lenses) |
| retrieval | evidence_retrieval | Deterministically executes the scout's plan |
| reviewers | 3 parallel `agent` roles | runtime_reviewer / contract_reviewer / security_reviewer → candidate findings |
| candidate_merge | merge_findings | Dedup by evidence overlap (`ReviewFinding`s → one representative per group) |
| verify | verify_findings, role=verifier | Adversarial per-finding verification; output carries §12 structured factors |
| rank | rank_findings | Deterministic key: severity → confidence → execution-path-confirmed → developer relevance |
| requirements | criteria_eval (conditional) | Lane A — per-criterion PASS/FAIL when criteria exist |
| publish | publish_review | `--pr`: per-line comments + status check; locally a no-op |

## Change sources (R2.10)

`ChangeSource` is a runtime-checkable Protocol — the reviewer no longer reaches
into the worktree directly:

| Source | Change | Base/head |
|---|---|---|
| `WorkingTreeChangeSource` | `git diff` (unstaged + staged) | HEAD / worktree |
| `CommitRangeChangeSource` | `git diff base...head` | explicit refs |
| `PullRequestChangeSource` | `gh pr diff` / `gh pr view` | PR base / head SHAs |

All subprocess failures degrade to `ChangeSourceError` with a clear message
(missing `gh`, bad JSON, unknown refs). `gitreins review` maps flags to sources:
`--pr <n>` → PR (owner/repo from `--owner`/`--repo`, then `gh repo view`, then the
origin remote); `--base`+`--head` → commit range; default → working tree.

## PR mode (R2.10–R2.11)

`engine/github/app.py::run_pr_review` is thin glue: `PullRequestChangeSource` →
the **same** `ReviewOrchestrator.run` used locally → rank/dedupe →
`CommentWriter` → `publish_comments` (per-line PR review comments / issue
comments; NEVER raises — failures land in `PublishResult.failed`) + status check
(DAG error → error; crit/high findings → failure; else success).

**Sandbox** (`engine/github/sandbox.py::DockerSandbox`, docker CLI, no new deps):
ephemeral container that clones the PR with no prod credentials, no host fs, no
docker socket; `--network none` default-deny (explicit opt-in to open); resource
caps; `*_API_KEY`/credentials scrubbed from env; `--rm` + `--cidfile` teardown so
no ephemeral container outlives its run. Missing docker / dead daemon raise
`SandboxUnavailableError` (graceful degradation). The verifier routes its
`run_command` tool through any `Sandbox` in PR mode; direct execution stays the
default locally.

## Intent system (R2.12)

`TaskManager.intent_context(statuses=None)` is a read-only projection
`{id, title, criteria, status}` of **existing** tasks (never auto-created) +
`intent_prompt()` rendering a "Task intent (developer criteria)" block. The
pipeline feeds it to Lane A (requirements) and Lane B (defect review):
`ReviewAgent.run(intent_context=...)` renders it before the evidence store, and
`ReviewOrchestrator.run`/`run_from_plan`/`_run_one` thread it to every reviewer.

## Rich history + provenance + ReviewLearning (R2.13)

- **`ReviewRunArchiver`** persists `review_runs/<sha>/` JSON artifacts with the
  exact §13 filenames: manifest, change, static-evidence, scout, candidates,
  verification, final-findings, requirements, usage. Wired into the orchestrator
  (`archiver=` param); archive failure is logged and skipped, never raised.
- **Provenance**: `ReviewFinding.generated_by {role, model}` +
  `verified_by {model, verdict}` — every final finding names its producer and
  verifier (defaulted, backward compatible).
- **`ReviewLearning`**: developer accept/reject → durable rule
  `{scope: {repo, paths}, rule, source: {type, finding}, confidence,
  support_count}`, JSONL-persisted at `.gitreins/review_learning.jsonl`,
  repo + fnmatch path-glob retrieval, thread-safe append, I/O failure never
  raises.

## propagate (R2.14)

`Propagator` → `PolicyPropagator`; the entry point is now
`propagate_policy()` (guard-policy config propagation across sibling repos),
with `Propagator`/`propagate()` kept as backward-compatible aliases. The MCP
tool name `propagate` is unchanged (external API contract).
`get_cross_repo_impact()` joined the `CodeIntelProvider` Protocol: the graph
provider returns a `[NO_BACKEND]` marker, astgrep/ripgrep/lsp/serena degrade to
`[]`.

## CodeRabbit-replacement (R2.15)

The CodeRabbit-ish commit-review loop is **deleted** from `CommitAuditor`
(`COMMIT_REVIEW_SYSTEM_PROMPT` and its orchestration are gone). `CommitAuditor`
keeps actual commit-message auditing; code review is its own subsystem — the
review DAG — and `commit_audit_review_mode` no longer drives review.

## Key behavioral notes

- **Model routing is per-role.** `review.models.<role>` in `.gitreins/config.yaml`
  (scout, runtime_reviewer, contract_reviewer, security_reviewer, verifier,
  writer). The pipeline no longer owns one client.
- **`scan_security` is diff-scoped.** It runs ast-grep (CodeRabbit essentials,
  184 rules) against only the changed files (`git diff HEAD` + staged), not the
  whole repo — this was a major judge-cost regression and is now bounded.
- **`find_lsp_tool` falls back to the project `.venv/bin`.** LSP tools like
  `pylsp` live in the project venv, not on the bare judge subprocess PATH.
- **gitleaks allowlists `.venv/.git/node_modules/caches`.** The secrets guard
  was scanning the entire virtualenv (false-positive "leaks" in third-party
  packages + 20-43s per run).
- **The judge strips `GITREINS_MAX_*` budget vars from subprocess envs** — a
  leaked `EvalCap` broke tier1/tier2 pytest runs inside judge subprocesses.

## Tier-1 guard PATH fixes (2026-08-08)

The judge's `run_command` runs with a bare `shell=True` subprocess that does
not inherit the project venv PATH. Explicit config + provider fixes:
- `guards.lint_command: uv run ruff check .` (ruff lives in `.venv/bin`)
- `AstGrepProvider` prefers `ast-grep` over `sg` — `/usr/bin/sg` is GNU screen,
  not ast-grep, and was silently returning 0 matches
- `find_lsp_tool(tool, workdir)` checks `<workdir>/.venv/bin/<tool>`

## Progress

| Phase | Status | Built (commit) | Judge |
|---|---|---|---|
| R2.1 ModelRouter | ✅ | b0420b9 | 4347df2 |
| R2.2 AgentRunner | ✅ | 1ec04a1 | 791d8438 |
| R2.3 CriteriaEvaluator (Lane A) | ✅ | 3cd9b0e | 30361ec4 |
| R2.4 Evidence store + producers | ✅ | 552ec26 + 5a0dc5f | 08066aff / ef6fc473 |
| R2.5 Code Intelligence | ✅ | ae62a0c | 53537562 |
| R2.6 Scout + evidence planner | ✅ | 05eb02d | 7119237d |
| R2.7 Review agents + orchestrator | ✅ | 45add46 | e92bac86 |
| R2.8 Adversarial verifier + BLOCK policy | ✅ | e68b9d3 | eda00fdb |
| R2.9 Ranker + dedup + comment writer | ✅ | a0bffe0 | 8b8b7b2e |
| R2.10 ChangeSource + publisher + app | ✅ | e3d2eae | 50f00f11 |
| R2.11 PR sandbox | ✅ | df8c452 | 089fdf31 |
| R2.12 Intent system | ✅ | 43af82e | 661e954c |
| R2.13 Review history + provenance + ReviewLearning | ✅ | ecc6752 | 781eef2c |
| R2.14 propagate_policy + cross-repo impact | ✅ | 6aea4fc | b5f1da04 / 983d3b49 |
| R2.15 CodeRabbit-replacement strip | ✅ | fcc1aa7 | 043adf41 |
| R2.16 Review DAG in pipeline + `gitreins review` CLI | ✅ built | 4a52557 | pending (after DOCS-001) |

Board: `.coding-hermes/tasks.md` (branch `v2`).
