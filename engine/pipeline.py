"""
Pipeline Engine — Configurable evaluation pipelines.

Pipelines are defined in .gitreins/config.yaml as a list of stages.
Each stage can be sequential or parallel. Results pipe between stages.

Key features:
    - Nested lists = parallel groups (items in a parallel list run concurrently)
    - Flat lists = sequential stages
    - Conditional execution (skip AI if scripts pass)
    - Result piping (failures from Tier 1 feed into Tier 2 AI context)
    - Script stages, AI evaluation stages, output stages

YAML schema:

pipeline:
  stages:
    - id: tier1
      parallel: true
      on: [pre-commit, pre-eval]   # When to run
      steps:
        - id: secrets
          type: script
          run: "gitleaks detect --source . --no-git"
          on_fail: continue          # continue | block | skip_remaining

        - id: lint
          type: script
          run: "ruff check ."

    - id: tier2
      type: ai_eval
      condition: "stage.tier1.any_failed"
      max_iterations: -1  # Defer to evaluator config
      tools: [read_file, run_command, search_pattern, read_diff, sandbox]
      prompt_template: |
        Evaluate task completeness.
        Tier 1 results: {{ stage.tier1 }}
        Criteria: {{ task.criteria }}

    - id: verdict
      type: output
"""

import concurrent.futures
import glob
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger("gitreins.pipeline")


@dataclass
class StepResult:
    id: str
    type: str  # script | ai_eval | output | review_agent | agent | commit_audit
    #        | change_analysis | analyzer | evidence_retrieval | merge_findings
    #        | verify_findings | rank_findings | criteria_eval | publish_review
    passed: bool = True
    output: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            "output": self.output[:500],
            "error": self.error,
            "data": self.data,
        }


@dataclass
class StageResult:
    id: str
    passed: bool = True
    steps: list[StepResult] = field(default_factory=list)
    any_failed: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "passed": self.passed,
            "any_failed": self.any_failed,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }


class Pipeline:
    """Execute a pipeline of stages against a task."""

    def __init__(self, config: dict, workdir: str = ".", llm=None, router=None):
        self.workdir = os.path.abspath(workdir)
        self.config = config
        self.stages: list[dict] = config.get("pipeline", {}).get("stages", [])
        # DESIGN_v2.md §17 review DAG — a flat list of stage dicts (parallel
        # groups use ``parallel: true`` + ``steps``), executed by run_review().
        self.review_stages: list[dict] = config.get("review_pipeline") or []
        self._stage_results: dict[str, StageResult] = {}
        self._llm = llm  # Can be injected by Judge
        self._router = router  # ModelRouter — resolves a client per role (R2.1)

    def run(self, task: dict, trigger: str = "pre-eval") -> dict:
        """Run all stages that match the trigger.

        Args:
            task: Task dict with id, title, criteria, status.
            trigger: "pre-commit" or "pre-eval" — filters which stages run.

        Returns:
            Dict with overall verdict and per-stage results.
        """
        self._stage_results = {}

        for stage_def in self.stages:
            # Check if this stage should run for this trigger
            stage_on = stage_def.get("on", ["pre-eval", "pre-commit"])
            if trigger not in stage_on:
                logger.debug(
                    "Skipping stage %s (trigger mismatch: %s)", stage_def.get("id"), trigger
                )
                continue

            # Check condition
            if not self._check_condition(stage_def.get("condition"), task):
                logger.debug("Skipping stage %s (condition not met)", stage_def.get("id"))
                continue

            stage_id = stage_def.get("id", f"stage_{len(self._stage_results)}")
            logger.info("Running stage: %s", stage_id)

            if stage_def.get("parallel"):
                result = self._run_parallel_stage(stage_id, stage_def, task)
            else:
                result = self._run_sequential_stage(stage_id, stage_def, task)

            self._stage_results[stage_id] = result

        return self._compile_results()

    def run_review(self, task: dict) -> dict:
        """Run the DESIGN_v2.md §17 review DAG stages (R2.16).

        Stages come from the config ``review_pipeline`` key: a flat list of
        stage dicts where parallel groups use ``parallel: true`` + ``steps``
        and every §17 stage type is dispatched (change_analysis, parallel
        analyzer steps, agent roles scout/reviewers/verifier,
        evidence_retrieval, merge_findings, verify_findings, rank_findings,
        criteria_eval, publish_review). Conditions (e.g. ``task.has_criteria``
        gating the criteria_eval stage) are honored exactly like the eval
        pipeline; the trigger filter does not apply.

        Findings are plumbed through ``task["findings"]`` (one dict per
        finding) so later stages and the caller consume the same list; Lane A
        verdicts land on ``task["criteria_verdicts"]``.
        """
        self._stage_results = {}

        for stage_def in self.review_stages:
            if not self._check_condition(stage_def.get("condition"), task):
                logger.debug(
                    "Skipping review stage %s (condition not met)", stage_def.get("id")
                )
                continue

            stage_id = stage_def.get("id", f"stage_{len(self._stage_results)}")
            logger.info("Running review stage: %s", stage_id)

            if stage_def.get("parallel"):
                result = self._run_parallel_stage(stage_id, stage_def, task)
            else:
                result = self._run_sequential_stage(stage_id, stage_def, task)

            self._stage_results[stage_id] = result

        return self._compile_results()

    def _check_condition(self, condition: str | None, task: dict) -> bool:
        """Evaluate a condition expression.

        Supported:
            - None/empty → always true
            - "stage.X.any_failed" → true if stage X had failures
            - "stage.X.passed" → true if stage X passed
            - "task.has_criteria" → true if task has criteria
            - "task.skip_tier2" → true if task has skip_tier2 flag set
            - "not task.skip_tier2" → true if task does NOT have skip_tier2 flag
            - "true" / "always" → always true
            - "false" → always false
            - Expressions with AND/OR: "stage.tier1.any_failed or task.has_criteria"
        """
        if not condition:
            return True
        if condition in ("true", "always"):
            return True
        if condition == "false":
            return False

        # Parse simple expressions
        condition = condition.strip()

        # Handle OR
        if " or " in condition:
            parts = condition.split(" or ")
            return any(self._check_condition(p.strip(), task) for p in parts)

        # Handle AND
        if " and " in condition:
            parts = condition.split(" and ")
            return all(self._check_condition(p.strip(), task) for p in parts)

        # Handle individual predicates
        if condition == "task.has_criteria":
            return bool(task.get("criteria"))
        if condition == "task.skip_tier2":
            return bool(task.get("skip_tier2", False))
        if condition == "not task.skip_tier2":
            return not bool(task.get("skip_tier2", False))
        if condition.startswith("stage."):
            # stage.tier1.any_failed
            parts = condition.split(".")
            if len(parts) == 3:
                stage_id = parts[1]
                prop = parts[2]
                stage = self._stage_results.get(stage_id)
                if stage:
                    if prop == "any_failed":
                        return stage.any_failed
                    elif prop == "passed":
                        return stage.passed
            return False

        return True

    def _run_parallel_stage(self, stage_id: str, stage_def: dict, task: dict) -> StageResult:
        """Run all steps in parallel."""
        steps = stage_def.get("steps", [])
        result = StageResult(id=stage_id)

        if not steps and stage_def.get("type"):
            # ``parallel: true`` with no step list — the stage type itself
            # parallelizes internally (verify_findings fans out over candidate
            # findings). Run it as a single typed step (§17).
            step_result = self._run_step(stage_def, task)
            result.steps.append(step_result)
            result.any_failed = not step_result.passed
            result.passed = step_result.passed
            result.summary = step_result.output or step_result.error
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as executor:
            futures = {executor.submit(self._run_step, step, task): step for step in steps}
            for future in concurrent.futures.as_completed(futures):
                step_result = future.result()
                result.steps.append(step_result)

        # Check results
        result.any_failed = any(not s.passed for s in result.steps)
        result.passed = not result.any_failed
        result.summary = self._summarize_stage(result)
        return result

    def _run_sequential_stage(self, stage_id: str, stage_def: dict, task: dict) -> StageResult:
        """Run steps sequentially — in order, no concurrency."""
        result = StageResult(id=stage_id)

        steps = stage_def.get("steps", [])
        if steps:
            # Multi-step sequential stage (e.g. tier1 with secrets→lint→tests)
            for step_def in steps:
                step_result = self._run_step(step_def, task)
                result.steps.append(step_result)
                if not step_result.passed and step_def.get("on_fail") != "continue":
                    # Stop at first hard failure — later steps won't change the verdict
                    break
            result.any_failed = any(not s.passed for s in result.steps)
            result.passed = not result.any_failed
            result.summary = self._summarize_stage(result)
            return result

        # Single typed stage (script, ai_eval, review DAG types, ...)
        step_result = self._dispatch(stage_def, task)

        result.steps.append(step_result)
        result.passed = step_result.passed
        result.any_failed = not step_result.passed
        result.summary = step_result.output or step_result.error
        return result

    def _run_step(self, step_def: dict, task: dict) -> StepResult:
        """Run a single step (used by parallel stages)."""
        return self._dispatch(step_def, task)

    #: step ``type`` → dispatch method name (R2.16: full §17 review DAG).
    _STAGE_DISPATCHERS: dict[str, str] = {
        "script": "_run_script_step",
        "ai_eval": "_run_ai_eval",
        "commit_audit": "_run_commit_audit",
        # §17 uses ``type: agent``; ``review_agent`` stays for backward compat.
        "review_agent": "_run_review_agent",
        "agent": "_run_review_agent",
        "output": "_run_output",
        # ── DESIGN_v2.md §17 review_pipeline stage types (R2.16) ──
        "change_analysis": "_run_change_analysis",
        "analyzer": "_run_analyzer",
        "evidence_retrieval": "_run_evidence_retrieval",
        "merge_findings": "_run_merge_findings",
        "verify_findings": "_run_verify_findings",
        "rank_findings": "_run_rank_findings",
        "criteria_eval": "_run_criteria_eval",
        "publish_review": "_run_publish_review",
    }

    def _dispatch(self, step_def: dict, task: dict) -> StepResult:
        """Route one step def to its runner by ``type``; unknown → error step."""
        step_type = step_def.get("type", "script")
        method_name = self._STAGE_DISPATCHERS.get(step_type)
        if method_name is None:
            return StepResult(
                id=step_def.get("id", "unnamed"),
                type=step_type,
                passed=False,
                error=f"Unknown step type: {step_type}",
            )
        return getattr(self, method_name)(step_def, task)

    def _run_script_step(self, step_def: dict, task: dict) -> StepResult:
        """Execute a shell command."""
        step_id = step_def.get("id", "unnamed")
        cmd = step_def.get("run", "")

        if not cmd:
            return StepResult(id=step_id, type="script", passed=False, error="No command specified")

        # Template substitution
        cmd = self._template(cmd, task)

        logger.debug("Running script: %s", cmd)
        try:
            # Strip GIT_* env vars (GIT_INDEX_FILE etc.) leaked by the
            # pre-commit hook — they poison nested git commands in tests
            # (same class as DF-008; guards.py got this in 3cad082). Also
            # strip GITREINS_MAX_* budget controls — they leak into the
            # pytest subprocess and break EvalCap/config-priority tests when
            # a judge run exports caps (proven 2026-08-09 R2-16). Also strip
            # LLM credential vars (GITREINS_LLM_* + provider fallback keys):
            # leaking them into the pytest subprocess breaks tests/test_llm.py
            # env-priority tests that assert api_key == '' with no keys set
            # (proven 2026-08-09 INFRA-LLM-ENV-001: tier1 1 failed, 58 passed
            # with them stripped).
            sanitized_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("GIT_")
                and not k.startswith("GITREINS_MAX_")
                and not k.startswith("GITREINS_LLM_")
                and k
                not in {
                    "OPENROUTER_API_KEY",
                    "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY",
                    "DEEPSEEK_API_KEY",
                    "NEURALWATT_API_KEY",
                }
            }
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=step_def.get("timeout", 120),
                cwd=self.workdir,
                env=sanitized_env,
            )
            output = (result.stdout + result.stderr)[:2000]
            # A non-zero exit is a hard failure regardless of on_fail. on_fail
            # only controls whether later steps still run; it must never turn a
            # failed lint/test into a pass (previously `on_fail: continue` and
            # generated `cmd || true` both zeroed the failure). 2026-08-08.
            passed = result.returncode == 0

            return StepResult(
                id=step_id,
                type="script",
                passed=passed,
                output=output,
                data={"exit_code": result.returncode},
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                id=step_id, type="script", passed=False, error="Command timed out"
            )
        except Exception as e:
            return StepResult(
                id=step_id, type="script", passed=False, error=str(e)
            )

    def _run_ai_eval(self, step_def: dict, task: dict) -> StepResult:
        """Run the AI evaluator as a pipeline step."""
        step_id = step_def.get("id", "ai_eval")
        model = step_def.get("model")
        max_iterations = step_def.get("max_iterations", -1)

        # Lazy init LLM client. When a ModelRouter is present and the step
        # declares a role, resolve that role's client per invocation (R2.1);
        # otherwise keep the legacy behavior (injected llm or env defaults).
        llm = self._llm
        if self._router is not None:
            role = step_def.get("role")
            if role:
                llm = self._router.for_role(role)
        if llm is None:
            from engine.llm import LLMClient

            if model:
                llm = LLMClient(model=model)
            else:
                llm = LLMClient()
            self._llm = llm

        from engine.evaluator import AgenticEvaluator
        from engine.eval_cap import (
            EvalCap,
            _parse_time,
            _parse_tokens,
            eval_cap_from_config,
        )

        # Cap resolution: config.yaml evaluator: section is the base;
        # caps EXPLICITLY set in the step config override it. A step that
        # sets nothing (or only max_iterations: -1 = "defer to evaluator
        # config", the default) passes eval_cap=None so AgenticEvaluator
        # reads the config itself — the documented working path (helix
        # tick 60: -1 → full caps from config, zero compaction cycles).
        #
        # Do NOT build an explicit EvalCap with -1 defaults for token
        # caps: compaction threshold int(-1*0.9)=0 → the evaluator
        # compacts on every turn and never produces a verdict (fleet-wide
        # tier2 INCOMPLETE 'Context near limit (N/-1 tokens)', 2026-08).
        explicit_caps: dict[str, float | int] = {}
        if max_iterations not in (None, -1):
            explicit_caps["max_iterations"] = max_iterations
        max_time = step_def.get("max_time")
        if max_time:
            max_seconds = _parse_time(str(max_time))
            if max_seconds is not None:
                explicit_caps["max_seconds"] = float(max_seconds)
        for key in ("max_input_tokens", "max_output_tokens"):
            raw = step_def.get(key)
            if raw in (None, -1):
                continue
            if isinstance(raw, str):
                parsed = _parse_tokens(raw)
                if parsed is None:
                    logger.warning("Unparseable %s=%r in step %s — ignoring", key, raw, step_id)
                    continue
                explicit_caps[key] = parsed
            else:
                explicit_caps[key] = int(raw)
        if step_def.get("tool_call_weight") is not None:
            explicit_caps["tool_call_weight"] = float(step_def["tool_call_weight"])

        if explicit_caps:
            # Merge step overrides over the config base so unset caps
            # never fall back to unlimited.
            base = eval_cap_from_config(self.config)
            eval_cap = EvalCap(
                max_iterations=float(explicit_caps.get("max_iterations", base.max_iterations)),
                max_seconds=float(explicit_caps.get("max_seconds", base.max_seconds)),
                max_input_tokens=int(explicit_caps.get("max_input_tokens", base.max_input_tokens)),
                max_output_tokens=int(
                    explicit_caps.get("max_output_tokens", base.max_output_tokens)
                ),
                tool_call_weight=float(
                    explicit_caps.get("tool_call_weight", base.tool_call_weight)
                ),
            )
            evaluator = AgenticEvaluator(llm, self.workdir, eval_cap=eval_cap)
        else:
            # Nothing set in the step — defer to .gitreins/config.yaml
            evaluator = AgenticEvaluator(llm, self.workdir)

        # Build prompt with template substitution — the custom prompt_template
        # (if any) is passed to the evaluator as its system-prompt override so
        # it actually becomes the evaluation prompt. Previously this branch set
        # _pipeline_context then did nothing with the template (2026-08-08).
        prompt_template = step_def.get("prompt_template", "")
        if prompt_template:
            pipeline_context = self._get_pipeline_context()
            import json as _json
            ctx_str = _json.dumps(pipeline_context.get("stages", {}), default=str)[:4000]
            rendered = prompt_template.replace(
                "{{ pipeline_context }}", ctx_str,
            )
            task["_system_prompt_override"] = rendered
            task["_pipeline_context"] = pipeline_context

        try:
            verdict = evaluator.evaluate(task)
            passed = verdict.verdict == "COMPLETE"

            items_output = "\n".join(
                f"  {'✓' if i.status == 'PASS' else '✗'} {i.criterion}: {i.detail}"
                for i in verdict.items
            )

            return StepResult(
                id=step_id,
                type="ai_eval",
                passed=passed,
                output=f"{verdict.verdict}\n{items_output}\n{verdict.summary}",
                data={
                    "verdict": verdict.verdict,
                    "items": [
                        {"criterion": i.criterion, "status": i.status, "detail": i.detail}
                        for i in verdict.items
                    ],
                    "summary": verdict.summary,
                },
            )
        except Exception as e:
            logger.exception("AI eval failed")
            return StepResult(id=step_id, type="ai_eval", passed=False, error=str(e))

    def _run_commit_audit(self, step_def: dict, task: dict) -> StepResult:
        """Run the commit message auditor as a pipeline step.

        Reads the commit message from ``task["commit_message"]`` and the
        staged diff from git.  Uses the CommitAuditor to validate the
        message against the diff, with optional LLM exploration
        (configured via ``max_iterations`` in the step or config).

        Config keys (from .gitreins/config.yaml):
          ``commit_audit.mode`` — "warn" (default) | "block" | "suggest"
          ``commit_audit.strictness`` — "lenient" | "standard" (default) | "strict"
          ``commit_audit.max_iterations`` — int, default 3
          ``commit_audit.suggest_message`` — bool, default True
        """
        step_id = step_def.get("id", "commit_audit")

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            self._llm = LLMClient()

        from engine.commit_audit import CommitAuditor

        # Read config for commit_audit settings
        config = self._load_commit_audit_config()

        auditor = CommitAuditor(
            self._llm,
            self.workdir,
            strictness=config.get("strictness", "standard"),
            max_iterations=config.get("max_iterations", 3),
            suggest_message=config.get("suggest_message", True),
        )

        message = task.get("commit_message", "")
        if not message:
            # Try reading from git commit message file
            msg_path = os.path.join(self.workdir, ".git", "COMMIT_EDITMSG")
            if os.path.exists(msg_path):
                try:
                    with open(msg_path, "r") as f:
                        raw = f.read().strip()
                    # Strip comment lines
                    message = "\n".join(
                        line for line in raw.split("\n") if not line.startswith("#")
                    ).strip()
                except Exception:
                    pass

        if not message:
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output="No commit message to audit.",
            )

        try:
            result = auditor.audit(message)
        except Exception as e:
            logger.warning("Commit audit failed: %s", e)
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output=f"Audit error (passing): {e}",
            )

        mode = config.get("mode", "warn")
        passed = result.valid or mode != "block"

        output_lines: list[str] = []
        # ── Message audit ──
        if result.valid:
            output_lines.append("✓ Commit message looks good.")
        else:
            output_lines.append("⚠ Commit message issues:")
            for issue in result.issues:
                output_lines.append(f"  - {issue}")
            if result.suggested_message:
                output_lines.append(f"\nSuggested message: {result.suggested_message}")
            if mode == "block":
                output_lines.append(
                    "\n(Commit BLOCKED — fix message or set commit_audit.mode=warn)"
                )
            elif mode == "warn":
                output_lines.append("\n(Warning only — commit will proceed)")

        return StepResult(
            id=step_id,
            type="commit_audit",
            passed=passed,
            output="\n".join(output_lines),
            data={
                "valid": result.valid,
                "issues": result.issues,
                "suggested_message": result.suggested_message,
                "mode": mode,
                "iterations_used": result.iterations_used,
            },
        )

    def _load_commit_audit_config(self) -> dict:
        """Read commit_audit section from .gitreins/config.yaml."""
        import yaml

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                return cfg.get("commit_audit", {})
            except Exception:
                pass
        return {}

    def _run_review_agent(self, step_def: dict, task: dict) -> StepResult:
        """Run one Lane-B review agent as a pipeline step (R2.7).

        Config keys:
          ``role`` — required; one of runtime | contracts | security_edges
          (or the ``*_reviewer`` spellings). Maps to a ReviewAgent subclass
          via ``ROLE_TO_REVIEWER``.
          ``budget`` — optional ``{max_iterations, max_time, ...}`` dict
          overriding the evaluator block for this step.

        The agent reads the task's evidence (``task["evidence_store"]`` — an
        EvidenceStore instance — or ``task["evidence"]`` — a list of Evidence
        dicts) plus ``changed_files`` / ``diff`` context, and runs with the
        pipeline's ModelRouter (per-role model routing, R2.1). Findings are
        attached to ``task["findings"]`` so later stages can consume them.
        A reviewer failure is captured as an error StepResult and never
        propagates — parallel lanes keep running.
        """
        step_id = step_def.get("id", "review_agent")
        role = step_def.get("role")
        if not role:
            return StepResult(
                id=step_id,
                type=step_def.get("type", "review_agent"),
                passed=False,
                error="review_agent step requires a 'role' (runtime, contracts, security_edges)",
            )

        # §17 ``scout`` role — the cheap retrieval-plan classifier (R2.6).
        # Runs BEFORE the reviewers and feeds the evidence_retrieval stage.
        if role in ("scout", "scout_agent"):
            return self._run_scout(step_def, task)

        from engine.agents import Budget
        from engine.review import ROLE_TO_REVIEWER

        reviewer_cls = ROLE_TO_REVIEWER.get(role)
        if reviewer_cls is None:
            return StepResult(
                id=step_id,
                type=step_def.get("type", "review_agent"),
                passed=False,
                error=(
                    f"Unknown review_agent role: {role!r} "
                    "(expected runtime, contracts, or security_edges)"
                ),
            )
        model_role = reviewer_cls.MODEL_ROLE

        try:
            router = self._router
            if router is None:
                from engine.router import ModelRouter

                router = ModelRouter(self.config)
            agent = reviewer_cls(router=router, workdir=self.workdir)

            store = self._task_evidence_store(task)
            changed_files = task.get("changed_files") or task.get("files")
            diff_context = task.get("diff") or task.get("diff_context") or ""
            lenses = task.get("review_lenses")

            # Task intent (DESIGN_v2.md §10) — feed developer criteria into
            # Lane B. An explicit intent_context wins; otherwise wrap the
            # task's own criteria into a single intent block. Both forms may
            # be a rendered string or {id, title, criteria, status} dicts.
            intent_context: str | list[dict] | None = task.get("intent_context")
            if not intent_context:
                criteria = task.get("criteria")
                if criteria:
                    from engine.task_manager import intent_to_prompt

                    intent_context = intent_to_prompt(
                        [
                            {
                                "id": task.get("id", ""),
                                "title": task.get("title", ""),
                                "criteria": list(criteria),
                                "status": task.get("status", ""),
                            }
                        ]
                    )

            step_budget_cfg = step_def.get("budget") or {}
            budget = Budget.from_config(self.config)
            if step_budget_cfg:
                for key, value in step_budget_cfg.items():
                    if hasattr(budget, key):
                        setattr(budget, key, value)

            findings = agent.run(
                store,
                changed_files=changed_files,
                diff_context=diff_context,
                lenses=lenses,
                intent_context=intent_context,
                budget=budget,
            )
            items = list(findings.findings) if findings is not None else []
            findings_dicts = [f.to_dict() for f in items]

            # Findings plumbing: later stages consume task["findings"].
            task.setdefault("findings", []).extend(findings_dicts)

            summary = findings.summary if findings is not None else ""
            output = f"{model_role}: {len(items)} finding(s)"
            if summary:
                output += f" — {summary[:200]}"
            return StepResult(
                id=step_id,
                type="review_agent",
                passed=True,
                output=output,
                data={
                    "role": model_role,
                    "count": len(items),
                    "findings": findings_dicts,
                },
            )
        except Exception as e:  # noqa: BLE001 — step boundary: report, don't crash the DAG
            logger.exception("review_agent step %s failed", step_id)
            return StepResult(
                id=step_id, type="review_agent", passed=False, error=str(e)
            )

    def _task_evidence_store(self, task: dict):
        """Resolve the task's evidence store, or None when the task carries none.

        Accepts an ``EvidenceStore`` instance under ``task["evidence_store"]``
        or a list of Evidence dicts under ``task["evidence"]`` (rebuilt via
        ``Evidence.from_dict``). Reviewers fall back to an empty store when
        this returns None.
        """
        from engine.evidence import Evidence, EvidenceStore

        store = task.get("evidence_store")
        if isinstance(store, EvidenceStore):
            return store
        items = task.get("evidence")
        if isinstance(items, list):
            rebuilt = EvidenceStore()
            for d in items:
                if isinstance(d, dict):
                    try:
                        rebuilt.append(Evidence.from_dict(d))
                    except (KeyError, ValueError):
                        continue
            return rebuilt
        return None

    # ── R2.16: DESIGN_v2.md §17 review_pipeline stage types ────────────────

    def _ensure_store(self, task: dict):
        """Return the task's EvidenceStore, creating + attaching one when absent."""
        from engine.evidence import EvidenceStore

        store = task.get("evidence_store")
        if not isinstance(store, EvidenceStore):
            store = self._task_evidence_store(task) or EvidenceStore()
            task["evidence_store"] = store
        return store

    def _append_evidence(self, task: dict, evidence_items: list) -> list[str]:
        """Append Evidence items to the task's store; return their ids.

        Keeps the ``task[\"evidence\"]`` dict-list view in sync so consumers
        that read the list (rather than the store instance) see the additions.
        """
        store = self._ensure_store(task)
        ids: list[str] = []
        for ev in evidence_items:
            ids.append(store.append(ev).id)
        task["evidence"] = [e.to_dict() for e in store.all()]
        return ids

    def _run_change_analysis(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``change_analysis`` — resolve the change being reviewed.

        When the task carries a ChangeSource instance (``task[\"change_source\"]``
        — engine/github/checkout.py), its diff/changed_files/base_sha/head_sha
        populate the task. Explicit task fields win over the source so callers
        can pre-seed context. Fills ``task[\"changed_files\"]``,
        ``task[\"diff\"]`` and ``task[\"diff_context\"]`` for later stages.
        """
        step_id = step_def.get("id", "change_analysis")
        source = task.get("change_source")
        changed_files = list(task.get("changed_files") or [])
        diff_text = task.get("diff") or task.get("diff_context") or ""
        base_sha = task.get("base_sha", "")
        head_sha = task.get("head_sha", "")

        if source is not None:
            try:
                diff = source.diff()
                if diff_text is None or not diff_text.strip():
                    diff_text = diff.text or ""
                if not changed_files:
                    changed_files = list(diff.files or [])
                base_sha = source.base_sha() or base_sha
                head_sha = source.head_sha() or head_sha
            except Exception as exc:  # noqa: BLE001 — change-source boundary
                return StepResult(
                    id=step_id,
                    type="change_analysis",
                    passed=False,
                    error=f"change source failed: {exc}",
                )

        task["changed_files"] = changed_files
        task["diff"] = diff_text
        task.setdefault("diff_context", diff_text)
        task["base_sha"] = base_sha
        task["head_sha"] = head_sha

        if not changed_files:
            return StepResult(
                id=step_id,
                type="change_analysis",
                passed=True,
                output="No changed files detected — clean tree.",
                data={"files": [], "base_sha": base_sha, "head_sha": head_sha},
            )
        output = f"{len(changed_files)} file(s) changed; diff {len(diff_text)} chars"
        return StepResult(
            id=step_id,
            type="change_analysis",
            passed=True,
            output=output,
            data={"files": changed_files, "base_sha": base_sha, "head_sha": head_sha},
        )

    def _run_analyzer(self, step_def: dict, task: dict) -> StepResult:
        """§17 parallel ``analyzer`` step — static evidence producers.

        ``analyzer`` names: lsp | semgrep | typecheck | secrets | dependencies.
        Each producer degrades to [] when its binary is missing (the engines
        already do this); produced Evidence is appended to the task's store so
        reviewers and the verifier can cite it. A producer failure is captured
        as an error step — parallel lanes keep running.
        """
        step_id = step_def.get("id", "analyzer")
        name = step_def.get("analyzer")
        if not name:
            return StepResult(
                id=step_id,
                type="analyzer",
                passed=False,
                error=(
                    "analyzer step requires an 'analyzer' name "
                    "(lsp, semgrep, typecheck, secrets, dependencies)"
                ),
            )
        try:
            evidence_items = self._run_analyzer_producer(name, task)
            ids = self._append_evidence(task, evidence_items)
        except Exception as exc:  # noqa: BLE001 — analyzer boundary: report, don't crash
            logger.exception("analyzer step %s (%s) failed", step_id, name)
            return StepResult(id=step_id, type="analyzer", passed=False, error=str(exc))
        output = f"{name}: {len(evidence_items)} finding(s)"
        return StepResult(
            id=step_id,
            type="analyzer",
            passed=True,
            output=output,
            data={"analyzer": name, "count": len(evidence_items), "evidence_ids": ids},
        )

    def _run_analyzer_producer(self, name: str, task: dict) -> list:
        """Run one §17 analyzer producer; returns Evidence objects.

        All external tools skip gracefully when not installed (logged note,
        no evidence). ``lsp`` runs the configured LSP tools over the changed
        files; ``typecheck`` runs the configured static-analysis tools.
        """
        from engine.evidence import Evidence

        workdir = self.workdir
        if name == "semgrep":
            from engine.analyzers.semgrep import run_semgrep

            return run_semgrep(workdir)
        if name == "secrets":
            from engine.analyzers.gitleaks import run_gitleaks

            return run_gitleaks(workdir)
        if name == "dependencies":
            from engine.analyzers.trivy import run_trivy

            return run_trivy(workdir)
        if name == "lsp":
            from engine.lsp import run_lsp_check

            guards = self.config.get("guards", {})
            tools = list(guards.get("lsp_tools") or ["pylsp"])
            changed = task.get("changed_files")
            result: list[Evidence] = []
            for tool in tools:
                diags = run_lsp_check(tool, workdir, files=changed)
                result.extend(self._lsp_diags_to_evidence(diags, tool))
            return result
        if name == "typecheck":
            from engine.evidence.producers import static_findings_to_evidence
            from engine.static_analysis import run_static_check

            guards = self.config.get("guards", {})
            lang_tools = guards.get("static_analysis_tools") or {}
            tools = list(lang_tools.get("python") or ["mypy", "pyright"])
            result = []
            for tool in tools:
                findings = run_static_check(tool, workdir)
                result.extend(static_findings_to_evidence(findings, tool))
            return result
        raise ValueError(
            f"unknown analyzer name: {name!r} "
            "(expected lsp, semgrep, typecheck, secrets, dependencies)"
        )

    def _lsp_diags_to_evidence(self, diags: list[dict], tool: str) -> list:
        """Wrap LSP diagnostic dicts ({file,line,severity,message,code,tool})
        as kind='lsp' Evidence so the review store carries them like any other
        static evidence."""
        from engine.evidence import Evidence

        result = []
        for d in diags:
            result.append(
                Evidence(
                    id="",
                    kind="lsp",
                    source=tool,
                    file=d.get("file"),
                    line_start=d.get("line"),
                    line_end=d.get("line"),
                    payload={
                        "code": d.get("code", ""),
                        "message": d.get("message", ""),
                        "severity": d.get("severity", ""),
                        "tool": tool,
                    },
                )
            )
        return result

    def _run_scout(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``agent`` role ``scout`` — cheap retrieval-plan classifier (R2.6).

        The ScoutAgent plans retrievals for the evidence_retrieval stage; the
        plan is stashed on ``task[\"scout_plan\"]`` and its lenses on
        ``task[\"review_lenses\"]`` so reviewers and later stages consume it.
        """
        step_id = step_def.get("id", "scout")
        from engine.agents import Budget
        from engine.review.scout import ScoutAgent

        router = self._router
        if router is None:
            from engine.router import ModelRouter

            router = ModelRouter(self.config)
        try:
            scout = ScoutAgent(router=router, workdir=self.workdir)
            plan = scout.run(
                list(task.get("changed_files") or []),
                task.get("diff") or task.get("diff_context") or "",
                budget=Budget.from_config(self.config),
            )
            task["scout_plan"] = plan
            task["review_lenses"] = list(plan.review_lenses or [])
            requests = [r.__dict__ for r in plan.retrieval_requests]
            symbols = [s.__dict__ for s in plan.changed_symbols]
            output = (
                f"scout: {len(requests)} retrieval request(s), "
                f"{len(task['review_lenses'])} lens(es)"
            )
            return StepResult(
                id=step_id,
                type="agent",
                passed=True,
                output=output,
                data={
                    "role": "scout",
                    "changed_symbols": symbols,
                    "retrieval_requests": requests,
                    "review_lenses": task["review_lenses"],
                },
            )
        except Exception as exc:  # noqa: BLE001 — agent boundary: report, don't crash
            logger.exception("scout step %s failed", step_id)
            return StepResult(id=step_id, type="agent", passed=False, error=str(exc))

    def _run_evidence_retrieval(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``evidence_retrieval`` — execute the scout's retrieval plan.

        Uses ``task[\"codeintel_provider\"]`` (an R2.5 CodeIntelProvider
        instance) with the plan from the scout stage. Degrades to a no-op when
        no provider or plan is available — the DAG never crashes on retrieval.
        """
        step_id = step_def.get("id", "evidence_retrieval")
        plan = task.get("scout_plan")
        provider = task.get("codeintel_provider")
        if provider is None:
            return StepResult(
                id=step_id,
                type="evidence_retrieval",
                passed=True,
                output="Evidence retrieval skipped — no code-intel provider configured.",
                data={"retrieved": 0},
            )
        if plan is None:
            return StepResult(
                id=step_id,
                type="evidence_retrieval",
                passed=True,
                output="Evidence retrieval skipped — no scout plan.",
                data={"retrieved": 0},
            )
        from engine.review.context_builder import EvidencePlanner

        store = self._ensure_store(task)
        planner = EvidencePlanner(provider, store, default_limit=int(step_def.get("limit", 3)))
        appended = planner.execute(plan)
        task["evidence"] = [e.to_dict() for e in store.all()]
        return StepResult(
            id=step_id,
            type="evidence_retrieval",
            passed=True,
            output=f"Retrieved {len(appended)} evidence item(s)",
            data={"retrieved": len(appended), "ids": [e.id for e in appended]},
        )

    def _run_merge_findings(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``merge_findings`` — merge + dedup the reviewers' candidate list.

        The three reviewers run in parallel (R2.7) and routinely surface the
        same defect; deterministic dedup (engine/review/dedup.py) collapses
        evidence-overlapping / same-region findings to one representative.
        """
        step_id = step_def.get("id", "merge_findings")
        from engine.review.dedup import dedupe_findings

        findings = list(task.get("findings") or [])
        before = len(findings)
        deduped = dedupe_findings(findings)
        task["findings"] = list(deduped)
        output = f"Merged {before} finding(s) → {len(deduped)} after dedup"
        return StepResult(
            id=step_id,
            type="merge_findings",
            passed=True,
            output=output,
            data={"input_count": before, "output_count": len(deduped)},
        )

    def _verify_one(self, router, finding_dict: dict, index: int):
        """Run one VerifierAgent against one candidate finding dict (R2.8)."""
        from engine.agents import Budget
        from engine.review.verifier import VerifierAgent, VerifierCandidate

        candidate = VerifierCandidate(
            finding_id=str(finding_dict.get("finding_id") or f"F{index + 1}"),
            file=str(finding_dict.get("file", "")),
            claim=str(finding_dict.get("claim", "")),
            line=finding_dict.get("line")
            if isinstance(finding_dict.get("line"), int)
            else None,
            trigger=str(finding_dict.get("trigger", "")),
            evidence=[str(r) for r in (finding_dict.get("evidence") or [])],
            verification_plan=[
                str(p) for p in (finding_dict.get("verification_plan") or [])
            ],
        )
        agent = VerifierAgent(router=router, workdir=self.workdir)
        verdicts = agent.run(candidate, budget=Budget.from_config(self.config))
        return list(verdicts.findings) if verdicts is not None else []

    def _run_verify_findings(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``verify_findings`` — adversarial verification of candidates.

        One VerifierAgent per candidate finding (R2.8), fanned out across a
        thread pool (the ``parallel: true`` marker on the §17 stage). Each
        finding dict gains the verifier's structured factors (verdict,
        verifier_confidence, execution_path_confirmed, impact, ...); REFUTED
        findings are dropped from the list, CONFIRMED/UNVERIFIED stay for
        ranking and the report. Per-candidate failures degrade to UNVERIFIED
        — the DAG never crashes on a verifier error.
        """
        step_id = step_def.get("id", "verify_findings")
        candidates = list(task.get("findings") or [])
        if not candidates:
            return StepResult(
                id=step_id,
                type="verify_findings",
                passed=True,
                output="No findings to verify.",
                data={"candidates": 0, "confirmed": 0, "refuted": 0, "unverified": 0},
            )
        router = self._router
        if router is None:
            from engine.router import ModelRouter

            router = ModelRouter(self.config)

        verdicts_by_index: dict[int, list] = {}
        errors: dict[int, str] = {}
        max_workers = min(len(candidates), max(1, int(step_def.get("workers", 4))))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._verify_one, router, cand, i): i
                for i, cand in enumerate(candidates)
            }
            for future in concurrent.futures.as_completed(futures):
                idx = futures[future]
                try:
                    verdicts_by_index[idx] = future.result()
                except Exception as exc:  # noqa: BLE001 — verifier boundary
                    logger.warning("verifier for candidate %d failed: %s", idx, exc)
                    errors[idx] = str(exc)

        confirmed = refuted = unverified = 0
        kept: list[dict] = []
        for i, finding in enumerate(candidates):
            verdicts = verdicts_by_index.get(i, [])
            v = verdicts[0] if verdicts else None
            if v is None:
                unverified += 1
                finding["verdict"] = "UNVERIFIED"
                finding["verifier_confidence"] = 0.0
                if i in errors:
                    finding["verifier_error"] = errors[i]
                kept.append(finding)
                continue
            d = v.to_dict() if hasattr(v, "to_dict") else dict(v)
            finding["verdict"] = d.get("verdict", "UNVERIFIED")
            finding["impact"] = d.get("impact", "")
            finding["patch_causality"] = d.get("patch_causality", "")
            finding["reproducible"] = bool(d.get("reproducible", False))
            finding["execution_path_confirmed"] = bool(
                d.get("execution_path_confirmed", False)
            )
            finding["verifier_confidence"] = d.get("verifier_confidence", 0.0)
            finding["developer_relevance"] = d.get("developer_relevance", "medium")
            finding["notes"] = d.get("notes", "")
            finding["verified_by"] = {"role": "verifier", "verdict": d.get("verdict", "")}
            if finding["verdict"] == "REFUTED":
                refuted += 1
                continue  # a refuted claim is not reported
            if finding["verdict"] == "CONFIRMED":
                confirmed += 1
            else:
                unverified += 1
            kept.append(finding)

        task["findings"] = kept
        output = (
            f"Verified {len(candidates)} candidate(s): {confirmed} confirmed, "
            f"{refuted} refuted, {unverified} unverified"
        )
        return StepResult(
            id=step_id,
            type="verify_findings",
            passed=True,
            output=output,
            data={
                "candidates": len(candidates),
                "confirmed": confirmed,
                "refuted": refuted,
                "unverified": unverified,
                "errors": errors,
            },
        )

    def _run_rank_findings(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``rank_findings`` — deterministic usefulness ranking (R2.9).

        Sorts the verified findings most-useful-first by the LLM-free key in
        engine/review/ranker.py (severity, verifier confidence, execution
        path, developer relevance, then file/line/claim).
        """
        step_id = step_def.get("id", "rank_findings")
        from engine.review.ranker import finding_to_dict, rank_findings

        findings = list(task.get("findings") or [])
        ranked = rank_findings(findings)
        task["findings"] = list(ranked)
        return StepResult(
            id=step_id,
            type="rank_findings",
            passed=True,
            output=f"Ranked {len(ranked)} finding(s)",
            data={
                "count": len(ranked),
                "ranked": [finding_to_dict(f) for f in ranked],
            },
        )

    def _run_criteria_eval(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``criteria_eval`` — Lane A: criteria evaluation (R2.3).

        Runs the CriteriaEvaluator (engine/evaluator.py) over the task's
        criteria when present (the §17 stage carries ``condition:
        task.has_criteria``; this runner also guards for direct calls).
        Verdicts land on ``task[\"criteria_verdicts\"]`` for the combined
        report. A verdict is a review result, not a DAG failure: the step
        passes once the evaluation ran (criteria FAILs are reported, not
        crashes).
        """
        step_id = step_def.get("id", "criteria_eval")
        criteria = task.get("criteria") or []
        if not criteria:
            return StepResult(
                id=step_id,
                type="criteria_eval",
                passed=True,
                output="No criteria — Lane A skipped.",
                data={"items": []},
            )

        llm = self._llm
        router = self._router
        if router is not None:
            llm = router.for_role("evaluator")
        if llm is None:
            from engine.llm import LLMClient

            llm = LLMClient()
            self._llm = llm

        from engine.evaluator import CriteriaEvaluator

        evaluator = CriteriaEvaluator(llm, self.workdir, router=router)
        try:
            verdict = evaluator.evaluate(task)
        except Exception as exc:  # noqa: BLE001 — evaluation boundary
            logger.exception("criteria_eval step %s failed", step_id)
            return StepResult(id=step_id, type="criteria_eval", passed=False, error=str(exc))

        items = [
            {"criterion": i.criterion, "status": i.status, "detail": i.detail}
            for i in verdict.items
        ]
        task["criteria_verdicts"] = items
        output = "\n".join(
            f"  {'✓' if i['status'] == 'PASS' else '✗'} {i['criterion']}: {i['detail']}"
            for i in items
        )
        return StepResult(
            id=step_id,
            type="criteria_eval",
            passed=True,
            output=f"{verdict.verdict}\n{output}\n{verdict.summary}",
            data={"verdict": verdict.verdict, "items": items, "summary": verdict.summary},
        )

    def _run_publish_review(self, step_def: dict, task: dict) -> StepResult:
        """§17 ``publish_review`` — batched comments to GitHub, no-op locally.

        With PR context on the task (``pr_number`` + ``owner``/``repo``) the
        CommentWriter (R2.9) renders one batched CommentBatch from the ranked
        findings and engine/github/publisher.py posts it — publishing never
        raises. Without PR context the step is a local no-op that summarizes
        the findings for the combined report.
        """
        step_id = step_def.get("id", "publish_review")
        findings = list(task.get("findings") or [])
        pr_number = task.get("pr_number")
        if not pr_number:
            return StepResult(
                id=step_id,
                type="publish_review",
                passed=True,
                output=(
                    f"Local review — {len(findings)} finding(s); "
                    "publish skipped (no PR context)."
                ),
                data={"mode": "local", "published": 0, "failed": 0},
            )

        router = self._router
        if router is None:
            from engine.router import ModelRouter

            router = ModelRouter(self.config)
        try:
            from engine.github.publisher import publish_comments
            from engine.review.writer import CommentWriter

            writer = CommentWriter(router=router, workdir=self.workdir)
            batch = writer.run(
                findings,
                changed_files=task.get("changed_files"),
                diff_context=task.get("diff") or "",
            )
            owner = str(task.get("owner") or "")
            repo = str(task.get("repo") or "")
            result = publish_comments(owner, repo, int(pr_number), batch)
            posted, failed = len(result.posted), len(result.failed)
            output = (
                f"Published {posted}/{len(batch.comments)} comment(s) to "
                f"{owner}/{repo}#{pr_number}"
                + (f"; {failed} failed" if failed else "")
            )
            return StepResult(
                id=step_id,
                type="publish_review",
                passed=result.ok,
                output=output,
                data={
                    "mode": "github",
                    "comments": len(batch.comments),
                    "published": posted,
                    "failed": failed,
                    "errors": result.errors,
                },
            )
        except Exception as exc:  # noqa: BLE001 — publishing never breaks the DAG
            logger.exception("publish_review step %s failed", step_id)
            return StepResult(id=step_id, type="publish_review", passed=False, error=str(exc))

    def _run_output(self, step_def: dict, task: dict) -> StepResult:
        """Compile output from all stages."""
        step_id = step_def.get("id", "output")
        fmt = step_def.get("format", "{{ stages }}")

        output = self._template(fmt, task)
        return StepResult(id=step_id, type="output", passed=True, output=output)

    def _template(self, text: str, task: dict) -> str:
        """Simple template substitution with {{ var }} syntax.

        Available vars:
            {{ task.id }}, {{ task.title }}, {{ task.criteria }}
            {{ stage.<id>.passed }}, {{ stage.<id>.any_failed }}
            {{ stage.<id>.summary }}
            {{ stages }} — full stage results as JSON
        """
        # Task vars
        text = text.replace("{{ task.id }}", str(task.get("id", "")))
        text = text.replace("{{ task.title }}", str(task.get("title", "")))
        text = text.replace("{{ task.criteria }}", json.dumps(task.get("criteria", []), indent=2))

        # Stage vars
        for stage_id, stage in self._stage_results.items():
            prefix = f"{{{{ stage.{stage_id}"
            text = text.replace(f"{prefix}.passed }}}}", str(stage.passed))
            text = text.replace(f"{prefix}.any_failed }}}}", str(stage.any_failed))
            text = text.replace(f"{prefix}.summary }}}}", str(stage.summary))
            text = text.replace(f"{prefix} }}}}", json.dumps(stage.to_dict(), indent=2))

        # All stages
        stages_json = json.dumps(
            {sid: s.to_dict() for sid, s in self._stage_results.items()},
            indent=2,
        )
        text = text.replace("{{ stages }}", stages_json)

        return text

    def _get_pipeline_context(self) -> dict:
        """Get context from previous stages to inject into AI evaluation."""
        return {
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }

    def _summarize_stage(self, stage: StageResult) -> str:
        """Create a summary string for a stage."""
        lines = []
        for step in stage.steps:
            status = "✓" if step.passed else "✗"
            lines.append(
                f"  {status} {step.id}: {step.output[:100] if step.output else step.error[:100]}"
            )
        return "\n".join(lines)

    def _compile_results(self) -> dict:
        """Compile final results from all stages."""
        all_passed = all(s.passed for s in self._stage_results.values())
        return {
            "passed": all_passed,
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }


def _normalize_yaml_bool_keys(obj):
    """Recursively convert boolean keys to their string equivalents.

    PyYAML 1.1 parses unquoted ``on``, ``off``, ``yes``, ``no``, ``true``,
    ``false`` as Python bools.  When those appear as mapping keys they break
    lookups: ``stage_def.get("on")`` returns None because the real key is
    ``True``.  This walker converts them back to the lowercase string form.
    """
    if isinstance(obj, dict):
        fixed = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = str(k).lower()  # True→"true", False→"false"
            fixed[k] = _normalize_yaml_bool_keys(v)
        return fixed
    if isinstance(obj, list):
        return [_normalize_yaml_bool_keys(i) for i in obj]
    return obj


# Mapping of Python bool → YAML 1.1 boolean keyword that would have
# produced it when used as a plain scalar key.
_YAML_BOOL_KEY_MAP: dict[bool, str] = {
    True: "on",
    False: "off",
}


def _fix_on_key(obj):
    """Post-processor specifically for the ``on`` / ``off`` key pitfall.

    YAML 1.1 interprets ``on: [...]`` as ``True: [...]``.  This second pass
    converts bool-to-string using the most-common-intent mapping
    (True→"on", False→"off") rather than the generic True→"true".
    """
    if isinstance(obj, dict):
        fixed = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = _YAML_BOOL_KEY_MAP.get(k, str(k).lower())
            fixed[k] = _fix_on_key(v)
        return fixed
    if isinstance(obj, list):
        return [_fix_on_key(i) for i in obj]
    return obj


def _has_sig_file(workdir: str, sig_file: str) -> bool:
    """Check if a signature file exists, supporting wildcard patterns."""
    if any(c in sig_file for c in "*?["):
        matches = glob.glob(os.path.join(workdir, sig_file))
        return len(matches) > 0
    return os.path.isfile(os.path.join(workdir, sig_file))


def _default_tier1_steps(workdir: str, config: dict | None = None) -> list[dict]:
    """Return language-appropriate default Tier 1 pipeline steps.

    Detects the project language(s) by checking for ecosystem files
    (go.mod, pyproject.toml, Cargo.toml, package.json, etc.) and
    returns lint + test commands for the primary language found.
    Falls back to a secrets-only step when no language is detected.

    Honors .gitreins/config.yaml overrides: ``guards.lint_command``
    replaces the language-default lint command, and ``guards.test_command``
    + ``guards.test_timeout`` replace the language-default test command and
    the 120s script timeout (large Go suites exceed 120s).

    NOTE: the lint override exists because the judge subprocess runs steps in
    a bare shell (``.venv/bin`` NOT on its PATH) — ``uv run ruff check .``
    resolves ruff through uv while a bare ``ruff`` does not.
    """
    guards_cfg = (config or {}).get("guards", {})
    configured_lint_cmd = guards_cfg.get("lint_command")
    configured_test_cmd = guards_cfg.get("test_command")
    test_timeout = int(guards_cfg.get("test_timeout", 120))
    steps: list[dict] = [
        {
            "id": "secrets",
            "type": "script",
            "run": (
                "gitleaks detect --source . --no-git --no-banner || "
                'python3 -c "from engine.guard_manager import GuardManager; '
                "import sys; gm = GuardManager('.'); "
                'r = gm._check_secrets(); sys.exit(0 if r.passed else 1)"'
            ),
            "on_fail": "continue",
        },
    ]

    # Lint + test commands per language ecosystem. NOTE: no `2>/dev/null ||
    # true` suffix — appending it would zero the exit code and make a failing
    # lint/test report as a pass (2026-08-08 fix; _run_script_step now treats
    # non-zero exit as a hard failure regardless of on_fail).
    _LANG_COMMANDS: dict[str, tuple[str, str]] = {
        "go": ("go vet ./...", "go test ./..."),
        "rust": (
            "cargo clippy -- -D warnings",
            "cargo test --no-fail-fast",
        ),
        "python": (
            "ruff check . --quiet",
            "pytest -x --tb=short",
        ),
        "js": ("npx eslint .", "npm test"),
        "java": ("mvn checkstyle:check", "mvn test -q"),
        "c": ("make lint", "make test"),
        "cpp": ("make lint", "make test"),
        "ruby": ("rubocop", "bundle exec rspec"),
        "php": (
            "php vendor/bin/phpcs",
            "php vendor/bin/phpunit",
        ),
        "kotlin": ("./gradlew lint", "./gradlew test"),
        "csharp": (
            "dotnet format --verify-no-changes",
            "dotnet test",
        ),
        "scala": ("sbt scalafmtCheck", "sbt test"),
    }

    # Detection order — first match becomes the primary language
    _SIGNATURE_FILES: list[tuple[str, str]] = [
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("requirements.txt", "python"),
        ("package.json", "js"),
        ("pom.xml", "java"),
        ("settings.gradle.kts", "kotlin"),
        ("build.gradle", "java"),
        ("CMakeLists.txt", "cpp"),
        ("Makefile", "c"),
        ("Gemfile", "ruby"),
        ("composer.json", "php"),
        ("*.csproj", "csharp"),
        ("*.sln", "csharp"),
        ("build.sbt", "scala"),
    ]

    primary = None
    for sig_file, lang in _SIGNATURE_FILES:
        if _has_sig_file(workdir, sig_file):
            primary = lang
            break

    if primary is not None:
        lint_cmd, test_cmd = _LANG_COMMANDS[primary]
        lint_step: dict = {"id": "lint", "type": "script", "run": lint_cmd}
        if configured_lint_cmd:
            lint_step["run"] = configured_lint_cmd
        steps.append(lint_step)
        test_step: dict = {"id": "tests", "type": "script", "run": test_cmd}
        if configured_test_cmd:
            test_step["run"] = configured_test_cmd
        if test_timeout > 0:
            test_step["timeout"] = test_timeout
        steps.append(test_step)

    return steps


def load_pipeline_config(workdir: str = ".") -> dict:
    """Load pipeline configuration from .gitreins/config.yaml."""
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.exists(config_path):
        # Return default pipeline
        return {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": _default_tier1_steps(workdir),
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                ]
            }
        }

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
        # Fix YAML 1.1 boolean-key pitfall: unquoted ``on:`` / ``off:``
        # are parsed as ``True:`` / ``False:`` and break key lookups.
        config = _fix_on_key(config)
        if "pipeline" not in config:
            config["pipeline"] = {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": _default_tier1_steps(workdir, config),
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                ]
            }
        return config
    except Exception as e:
        logger.warning("Failed to load pipeline config: %s", e)
        return {"pipeline": {"stages": []}}
