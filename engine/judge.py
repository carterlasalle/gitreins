"""
Judge Orchestrator — Runs the evaluation pipeline.

Now delegate to the Pipeline engine for configurable multi-stage evaluation.
"""

import logging
import re

from engine.evaluator import AgenticEvaluator
from engine.guard_manager import GuardManager, Tier1Result
from engine.llm import LLMClient
from engine.eval_cap import EvalCap
from engine.pipeline import Pipeline, load_pipeline_config
from engine.task_manager import Task

logger = logging.getLogger("gitreins.judge")


class Judge:
    """Run the evaluation pipeline against a task."""

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        guard_config: dict | None = None,
        eval_cap: "str | EvalCap | None" = None,
    ):
        self.workdir = workdir
        self.llm = llm
        self.guard_config = guard_config or {}
        self.guard_manager = GuardManager(workdir, self.guard_config)
        self.eval_cap = eval_cap

    def evaluate_task(self, task: Task, skip_tier2: bool = False) -> "JudgeResult":
        """Run the pipeline against a task.

        Uses the configurable pipeline from .gitreins/config.yaml.
        Falls back to the simple Tier 1 → Tier 2 if no pipeline defined.

        When ``pass_on_error`` is True in config, Tier 2 (LLM evaluator)
        is skipped if the LLM is unavailable — the task passes on Tier 1
        alone.  Useful for CI environments or when the LLM provider is down.

        When ``skip_tier2`` is True, the ``task.skip_tier2`` flag is set on
        the task dict before the pipeline runs.  Pipeline stages with the
        condition ``not task.skip_tier2`` will be skipped (GR-064c).
        """
        config = load_pipeline_config(self.workdir)
        self._pass_on_error = self._read_pass_on_error()

        if config.get("pipeline", {}).get("stages"):
            return self._run_pipeline(task, config, skip_tier2=skip_tier2)
        else:
            if skip_tier2:
                # Legacy path doesn't use the condition system — just run
                # Tier 1 and return a pass-on-tier1 result.
                return self._run_legacy_skip_tier2(task)
            return self._run_legacy(task)

    def _read_pass_on_error(self) -> bool:
        """Read pass_on_error from .gitreins/config.yaml, defaulting to False."""
        import os

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.exists(config_path):
            try:
                import yaml

                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                return bool((cfg.get("defaults") or {}).get("pass_on_error", False))
            except Exception:
                pass
        return False

    def _run_pipeline(self, task: Task, config: dict, skip_tier2: bool = False) -> "JudgeResult":
        """Run the configurable pipeline."""
        from engine.pipeline import Pipeline

        # When skip_tier2 is requested via CLI/trailer, force every stage
        # whose type is ai_eval to be skipped by overriding its condition.
        # This works regardless of how the user's config defines the
        # tier2 stage (condition: "true" or condition: "stage.tier1.any_failed").
        if skip_tier2:
            stages = config.get("pipeline", {}).get("stages", [])
            for stage in stages:
                if stage.get("type") == "ai_eval":
                    stage["condition"] = "false"

        pipeline = Pipeline(config, self.workdir, llm=self.llm)

        task_dict: dict[str, object] = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
            "status": task.status,
        }
        if skip_tier2:
            # Propagate flag to task dict so pipeline conditions
            # like "not task.skip_tier2" can skip the tier2 stage.
            task_dict["skip_tier2"] = True

        try:
            result = pipeline.run(task_dict, trigger="pre-eval")

            # Extract tier1 guard results from pipeline stages
            tier1_stage = result.get("stages", {}).get("tier1", {})
            tier1 = Tier1Result(
                passed=tier1_stage.get("passed", True),
                results=[],
                extra={
                    "pipeline": True,
                    "summary": tier1_stage.get("summary", ""),
                    "any_failed": tier1_stage.get("any_failed", False),
                },
            )

            return JudgeResult(
                task_id=task.id,
                passed=result.get("passed", False),
                tier1=tier1,
                pipeline_result=result,
            )
        except Exception as e:
            if self._pass_on_error:
                logger.warning(
                    "Pipeline execution failed but pass_on_error=True — returning pass",
                )
                return JudgeResult(
                    task_id=task.id,
                    passed=True,
                    tier1=Tier1Result(
                        passed=True,
                        results=[],
                        extra={"pass_on_error": True},
                    ),
                    pipeline_result={"error": str(e), "pass_on_error": True},
                )
            logger.exception("Pipeline execution failed")
            return JudgeResult(task_id=task.id, passed=False, pipeline_result={"error": str(e)})

    def _run_legacy(self, task: Task) -> "JudgeResult":
        """Fallback: simple Tier 1 → Tier 2."""

        result = JudgeResult(task_id=task.id)

        print("  Tier 1: Running static guards...")
        tier1 = self.guard_manager.run_all()
        result.tier1 = tier1

        # Extract LSP diagnostics from Tier 1 results and pass to evaluator
        tier1_diagnostics = self._extract_lsp_diagnostics(tier1)

        if not tier1.passed:
            print("  Tier 1 FAILED — skipping evaluator")
            result.passed = False
            return result

        print("  Tier 1 PASSED")
        print("  Tier 2: Running agentic evaluator...")

        evaluator = AgenticEvaluator(self.llm, self.workdir, eval_cap=self.eval_cap)
        task_dict: dict[str, object] = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
        }
        if tier1_diagnostics:
            task_dict["tier1_diagnostics"] = tier1_diagnostics
        try:
            tier2 = evaluator.evaluate(task_dict)
            result.tier2 = tier2
            result.passed = tier2.verdict == "COMPLETE"
            result.verdict = tier2
        except Exception:
            if self._pass_on_error:
                logger.warning("Tier 2 failed but pass_on_error=True — task passes on Tier 1 alone")
                result.passed = True
            else:
                logger.exception("Tier 2 evaluator failed")
                result.passed = False
        return result

    def _run_legacy_skip_tier2(self, task: Task) -> "JudgeResult":
        """Legacy path when --skip-tier2 is set but no pipeline config exists.

        Runs Tier 1 only and returns a pass-on-tier1 result without
        invoking the AgenticEvaluator.  Mirrors the spirit of the
        pipeline condition ``not task.skip_tier2``.
        """

        result = JudgeResult(task_id=task.id)
        print("  Tier 1: Running static guards...")
        tier1 = self.guard_manager.run_all()
        result.tier1 = tier1
        if not tier1.passed:
            print("  Tier 1 FAILED — skipping evaluator")
            result.passed = False
            return result
        print("  Tier 1 PASSED")
        print("  Tier 2 SKIPPED (--skip-tier2 flag)")
        result.passed = True
        result.pipeline_result = {"skipped_tier2": True, "tier1_passed": True}
        return result

    def _extract_lsp_diagnostics(self, tier1: Tier1Result) -> list[dict]:
        """Extract structured LSP diagnostics from Tier 1 guard results.

        Parses the GuardResult output for the 'lsp' guard back into
        structured diagnostic dicts for the evaluator to consume.

        Returns list of dicts with keys: file, line, severity, message, tool.
        """
        for guard_result in tier1.results:
            if guard_result.name == "lsp" and guard_result.output:
                return self._parse_lsp_output(guard_result.output)
        return []

    @staticmethod
    def _parse_lsp_output(output: str) -> list[dict]:
        """Parse GuardResult output text back into structured diagnostics.

        The GuardResult._check_lsp output format is:
          ✗ file:line [tool] message
          ⚠ file:line [tool] message
          tool — clean
        """
        diags: list[dict] = []
        pattern = re.compile(r"^  [✗⚠] (.+?):(\d+) \[(.+?)\] (.+)")
        for line in output.split("\n"):
            m = pattern.match(line)
            if m:
                severity = "error" if line.startswith("  ✗") else "warning"
                diags.append(
                    {
                        "file": m.group(1),
                        "line": int(m.group(2)),
                        "severity": severity,
                        "message": m.group(4),
                        "tool": m.group(3),
                    }
                )
        return diags

    def run_precommit(self) -> bool:
        """Run pre-commit pipeline stages only. Returns True if commit should proceed."""
        config = load_pipeline_config(self.workdir)
        pipeline = Pipeline(config, self.workdir, llm=self.llm)
        result = pipeline.run(
            {"id": "_precommit", "title": "pre-commit", "criteria": []}, trigger="pre-commit"
        )
        return result.get("passed", True)


class JudgeResult:
    """Result of running the judge pipeline."""

    def __init__(
        self,
        task_id: str,
        passed: bool = False,
        pipeline_result: dict = None,
        verdict=None,
        tier1=None,
        tier2=None,
    ):
        self.task_id = task_id
        self.passed = passed
        self.pipeline_result = pipeline_result or {}
        self.verdict = verdict
        self.tier1 = tier1  # Tier1Result from guard_manager.run_all()
        self.tier2 = tier2  # Verdict from AgenticEvaluator.evaluate()

    @property
    def summary(self) -> str:
        lines = [f"Judge Result: {self.task_id}"]

        if self.pipeline_result:
            stages = self.pipeline_result.get("stages", {})
            for stage_id, stage in stages.items():
                status = "PASS" if stage.get("passed") else "FAIL"
                lines.append(f"\nStage {stage_id}: {status}")
                summary = stage.get("summary", "")
                if summary:
                    lines.append(f"  {summary}")

        # Legacy verdict items
        if self.verdict:
            lines.append(f"\nTier 2 (Agentic Evaluator): {self.verdict.verdict}")
            for item in self.verdict.items:
                status = "✓" if item.status == "PASS" else "✗"
                lines.append(f"  {status} {item.criterion}: {item.detail}")

        lines.append(f"\nOverall: {'PASS ✓' if self.passed else 'FAIL ✗'}")
        return "\n".join(lines)


def judge_result_to_dict(task_id: str, workdir: str, result) -> dict:
    """Standard judge result dict shared by MCP judge.status and CLI --status.

    Shape: task_id / passed / workdir / tier1_passed / verdict / items /
    summary (items only present when a Tier 2 verdict exists). Handles
    both result shapes: the legacy path (``result.tier2``) and the
    pipeline path (verdict buried in ``pipeline_result.stages[].steps[].data``).
    """
    d = {
        "task_id": task_id,
        "passed": result.passed,
        "workdir": workdir,
        "tier1_passed": result.tier1.passed if result.tier1 else None,
    }
    tier2 = result.tier2
    if tier2 is None and result.pipeline_result:
        tier2 = _find_pipeline_verdict(result.pipeline_result)
    if tier2:
        d["verdict"] = tier2.verdict
        d["items"] = [
            {"criterion": i.criterion, "status": i.status, "detail": i.detail} for i in tier2.items
        ]
        d["summary"] = tier2.summary
    return d


def _find_pipeline_verdict(pipeline_result: dict):
    """Extract the structured ai_eval verdict from a compiled pipeline result."""
    from types import SimpleNamespace

    for stage in (pipeline_result.get("stages") or {}).values():
        for step in stage.get("steps") or []:
            data = step.get("data") or {}
            if data.get("verdict"):
                items = [
                    SimpleNamespace(
                        criterion=i.get("criterion"),
                        status=i.get("status"),
                        detail=i.get("detail"),
                    )
                    for i in (data.get("items") or [])
                ]
                return SimpleNamespace(
                    verdict=data.get("verdict"),
                    items=items,
                    summary=data.get("summary", ""),
                )
    return None
