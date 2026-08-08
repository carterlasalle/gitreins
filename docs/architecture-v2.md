# GitReins 2.0 — Review Ensemble Architecture (runbook)

> Branch `v2`. Full spec: [`DESIGN_v2.md`](../DESIGN_v2.md). This runbook
> documents the architecture **as built** through R2.6 (2026-08-08).

## Two independent AI lanes

GitReins 2.0 answers two different questions with two separate engines:

| | **Lane A — Requirements / completeness** | **Lane B — Defect discovery** |
|---|---|---|
| Question | *Did the dev implement all stated criteria?* | *Did the patch break something, regardless of requirements?* |
| Engine | `CriteriaEvaluator` (re-expressed `AgenticEvaluator`) | New review DAG |
| Input | Task criteria | PR diff + repo context |
| Output | Per-criterion PASS/FAIL | Verified findings |

## Module map (R2.1–R2.6 built)

```
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
│   └── provenance.py
├── analyzers/             # evidence producers
│   ├── semgrep.py  astgrep.py  trivy.py  gitleaks.py
├── codeintel/             # repository intelligence providers
│   ├── base.py            #   CodeIntelProvider Protocol (11 query methods)
│   ├── astgrep.py         #   structural pattern search
│   ├── ripgrep.py         #   lexical recall
│   ├── lsp.py             #   semantic queries (diagnostics, definition, refs)
│   ├── graph.py           #   hilo/gitnexus-style graph (interface)
│   └── serena.py          #   Serena MCP wrapper (lazy client)
└── review/
    ├── scout.py           # cheap Qwen scout → retrieval plan
    └── context_builder.py # deterministic evidence planner
```

## The review pipeline (R2.7+, in progress)

```text
change_analysis
  → static_evidence (parallel: lsp/semgrep/typecheck/secrets/dependencies)
  → scout (cheap model → retrieval plan)
  → evidence_retrieval (deterministic execution of the plan)
  → reviewers (runtime/contract/security — parallel AgentRunner roles)
  → candidate_merge
  → verify_findings (adversarial — "disprove F<n>", per finding)
  → rank (structured factors + deterministic BLOCK policy, not LLM CVE scores)
  → criteria_eval (Lane A, when the task has criteria)
  → publish (GitHub / agent)
```

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

## Tier-1 guard PATH fixes (2026-08-08)

The judge's `run_command` runs with a bare `shell=True` subprocess that does
not inherit the project venv PATH. Explicit config + provider fixes:
- `guards.lint_command: uv run ruff check .` (ruff lives in `.venv/bin`)
- `AstGrepProvider` prefers `ast-grep` over `sg` — `/usr/bin/sg` is GNU screen,
  not ast-grep, and was silently returning 0 matches
- `find_lsp_tool(tool, workdir)` checks `<workdir>/.venv/bin/<tool>`

## Progress

| Phase | Status |
|---|---|
| R2.1 ModelRouter | ✅ |
| R2.2 AgentRunner | ✅ |
| R2.3 CriteriaEvaluator (Lane A) | ✅ |
| R2.4 Evidence store + producers | ✅ |
| R2.5 Code Intelligence | ✅ |
| R2.6 Scout + evidence planner | ✅ (judge COMPLETE 7119237d) |
| R2.7–R2.16 | pending |

Board: `.coding-hermes/tasks.md` (branch `v2`).
