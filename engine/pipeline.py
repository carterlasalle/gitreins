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
    type: str  # "script" | "ai_eval" | "output"
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

    def __init__(self, config: dict, workdir: str = ".", llm=None):
        self.workdir = os.path.abspath(workdir)
        self.config = config
        self.stages: list[dict] = config.get("pipeline", {}).get("stages", [])
        self._stage_results: dict[str, StageResult] = {}
        self._llm = llm  # Can be injected by Judge

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

        if stage_def.get("type") == "ai_eval":
            step_result = self._run_ai_eval(stage_def, task)
        elif stage_def.get("type") == "commit_audit":
            step_result = self._run_commit_audit(stage_def, task)
        elif stage_def.get("type") == "output":
            step_result = self._run_output(stage_def, task)
        else:
            # Treat as a single script step
            step_result = self._run_script_step(stage_def, task)

        result.steps.append(step_result)
        result.passed = step_result.passed
        result.any_failed = not step_result.passed
        result.summary = step_result.output or step_result.error
        return result

    def _run_step(self, step_def: dict, task: dict) -> StepResult:
        """Run a single step (used by parallel stages)."""
        step_type = step_def.get("type", "script")
        step_id = step_def.get("id", "unnamed")

        if step_type == "script":
            return self._run_script_step(step_def, task)
        elif step_type == "ai_eval":
            return self._run_ai_eval(step_def, task)
        elif step_type == "commit_audit":
            return self._run_commit_audit(step_def, task)
        elif step_type == "output":
            return self._run_output(step_def, task)
        else:
            return StepResult(
                id=step_id, type=step_type, passed=False, error=f"Unknown step type: {step_type}"
            )

    def _run_script_step(self, step_def: dict, task: dict) -> StepResult:
        """Execute a shell command."""
        step_id = step_def.get("id", "unnamed")
        cmd = step_def.get("run", "")
        on_fail = step_def.get("on_fail", "block")

        if not cmd:
            return StepResult(id=step_id, type="script", passed=False, error="No command specified")

        # Template substitution
        cmd = self._template(cmd, task)

        logger.debug("Running script: %s", cmd)
        try:
            # Strip GIT_* env vars (GIT_INDEX_FILE etc.) leaked by the
            # pre-commit hook — they poison nested git commands in tests
            # (same class as DF-008; guards.py got this in 3cad082).
            sanitized_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            if model:
                self._llm = LLMClient(model=model)
            else:
                self._llm = LLMClient()

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
            evaluator = AgenticEvaluator(self._llm, self.workdir, eval_cap=eval_cap)
        else:
            # Nothing set in the step — defer to .gitreins/config.yaml
            evaluator = AgenticEvaluator(self._llm, self.workdir)

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
          ``commit_audit.review_score_threshold`` — float, default 8.0 (GR-066)
          ``commit_audit.review_score_offset`` — float, default 1.0 (GR-066)
        """
        step_id = step_def.get("id", "commit_audit")

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            self._llm = LLMClient()

        from engine.commit_audit import CommitAuditor

        # Read config for commit_audit settings
        config = self._load_commit_audit_config()

        score_threshold = float(config.get("review_score_threshold", 8.0))
        score_offset = float(config.get("review_score_offset", 1.0))

        auditor = CommitAuditor(
            self._llm,
            self.workdir,
            strictness=config.get("strictness", "standard"),
            max_iterations=config.get("max_iterations", 3),
            suggest_message=config.get("suggest_message", True),
            review_mode=config.get("review_mode", "message"),
            review_checks=config.get("review_checks", None),
            review_severity=config.get("review_severity", "standard"),
            review_suggest_fix=config.get("review_suggest_fix", True),
            review_score_threshold=score_threshold,
            review_score_offset=score_offset,
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
        # ── CVE-style scoring (GR-066) ──
        all_review_issues = getattr(result, "review_issues", [])
        if all_review_issues:
            # Determine highest effective score
            max_effective = 0.0
            for ri in all_review_issues:
                raw_score = ri.get("score", 0.0)
                effective = raw_score * score_offset
                ri["effective_score"] = effective
                if effective > max_effective:
                    max_effective = effective

            sev_marker = {
                "critical": "🔴 CRITICAL",
                "high": "🟠 HIGH",
                "medium": "🟡 MEDIUM",
                "low": "🟢 LOW",
                "info": "ℹ️ INFO",
            }
            output_lines.append(
                f"⚠ Commit review — {len(all_review_issues)} issue(s) found (overall: {max_effective:.1f}/{score_threshold:.1f})"
            )
            review_summary = getattr(result, "review_summary", "")
            if review_summary:
                output_lines.append(f"   {review_summary}")
            output_lines.append("")

            blocked = False
            warn_issues = False
            for ri in all_review_issues:
                sev = sev_marker.get(ri.get("severity", "info"), "ℹ️ INFO")
                cat = ri.get("category", "unknown")
                file_ref = f"{ri.get('file', '')}:{ri.get('line', 0)}"
                title = ri.get("title", "")
                desc = ri.get("description", "")
                sugg = ri.get("suggestion", "")
                effective = ri.get("effective_score", 0.0)

                # Score-based action marker (GR-066)
                if effective >= score_threshold:
                    action_mark = "🚫 BLOCK"
                    blocked = True
                elif effective >= score_threshold * 0.75:
                    action_mark = "⚠️ WARN"
                    warn_issues = True
                else:
                    action_mark = "ℹ️ INFO"

                output_lines.append(
                    f"  {file_ref} [{cat}] [{sev}] {action_mark} (score: {effective:.1f}) — {title}"
                )
                if desc:
                    output_lines.append(f"    {desc}")
                if sugg:
                    output_lines.append(f"    Fix: {sugg}")
                output_lines.append("")

            # Apply scoring to pass/fail
            if blocked and mode == "block":
                passed = False
            elif blocked and mode == "warn":
                output_lines.append("(Warning: issues above threshold — review recommended)")
            elif warn_issues:
                output_lines.append("(Warning: issues in warning range — review recommended)")

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
                "review_issues": getattr(result, "review_issues", []),
                "review_summary": getattr(result, "review_summary", ""),
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

    Honors .gitreins/config.yaml overrides: ``guards.test_command`` and
    ``guards.test_timeout`` replace the language-default test command and
    the 120s script timeout (large Go suites exceed 120s).
    """
    guards_cfg = (config or {}).get("guards", {})
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
        steps.append({"id": "lint", "type": "script", "run": lint_cmd})
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
