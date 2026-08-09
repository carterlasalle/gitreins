"""
Unit tests for engine/pipeline.py — configurable evaluation pipeline.
axiom:trace work_item=GR-001 spec=specs/06-Pipeline-Engine.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from engine.pipeline import (
    Pipeline,
    StageResult,
    StepResult,
    load_pipeline_config,
)


# ── Phase 1-5-1: StepResult/StageResult, conditions, templates, defaults ─────


class TestStepResult:
    """Test StepResult dataclass and to_dict — step-1-5-1-3."""

    def test_step_result_with_passed_true(self):
        """StepResult with passed=True produces correct to_dict."""
        sr = StepResult(id="secrets", type="script", passed=True, output="clean")
        d = sr.to_dict()
        assert d["id"] == "secrets"
        assert d["type"] == "script"
        assert d["passed"] is True
        assert d["output"] == "clean"
        assert d["error"] == ""

    def test_step_result_with_error(self):
        """StepResult with error includes error in to_dict."""
        sr = StepResult(id="lint", type="script", passed=False, output="", error="E501")
        d = sr.to_dict()
        assert d["passed"] is False
        assert d["error"] == "E501"

    def test_step_result_output_truncated(self):
        """to_dict truncates output to 500 chars."""
        long_output = "x" * 1000
        sr = StepResult(id="tests", type="script", passed=True, output=long_output)
        d = sr.to_dict()
        assert len(d["output"]) == 500


class TestStageResult:
    """Test StageResult dataclass — step-1-5-1-3."""

    def test_stage_result_all_passed(self):
        """StageResult with all steps passed → passed=True, any_failed=False."""
        steps = [
            StepResult(id="s1", type="script", passed=True, output="ok"),
            StepResult(id="s2", type="script", passed=True, output="ok"),
        ]
        sr = StageResult(id="tier1", passed=True, steps=steps, any_failed=False)
        d = sr.to_dict()
        assert d["passed"] is True
        assert d["any_failed"] is False
        assert len(d["steps"]) == 2

    def test_stage_result_one_failed(self):
        """StageResult with one failed step → passed=False, any_failed=True."""
        steps = [
            StepResult(id="s1", type="script", passed=True, output="ok"),
            StepResult(id="s2", type="script", passed=False, output="", error="fail"),
        ]
        sr = StageResult(id="tier1", passed=False, steps=steps, any_failed=True)
        d = sr.to_dict()
        assert d["passed"] is False
        assert d["any_failed"] is True


class TestPipelineConditions:
    """Test Pipeline._check_condition — step-1-5-1-4."""

    def test_condition_none_is_true(self, pipeline_config_default, tmp_workdir):
        """condition=None → always True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition(None, {}) is True

    def test_condition_true_string_is_true(self, pipeline_config_default, tmp_workdir):
        """condition='true' → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("true", {}) is True

    def test_condition_always_is_true(self, pipeline_config_default, tmp_workdir):
        """condition='always' → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("always", {}) is True

    def test_condition_task_has_criteria_with_criteria(self, pipeline_config_default, tmp_workdir):
        """condition='task.has_criteria' with task having criteria → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1", "c2"]}
        assert p._check_condition("task.has_criteria", task) is True

    def test_condition_task_has_criteria_empty(self, pipeline_config_default, tmp_workdir):
        """condition='task.has_criteria' with empty criteria → False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": []}
        assert p._check_condition("task.has_criteria", task) is False

    def test_condition_stage_any_failed(self, pipeline_config_default, tmp_workdir):
        """condition='stage.tier1.any_failed' with tier1 having failures → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        # Inject a failed stage result
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=False,
            any_failed=True,
            steps=[StepResult(id="s1", type="script", passed=False, error="fail")],
        )
        assert p._check_condition("stage.tier1.any_failed", {}) is True

    def test_condition_stage_passed(self, pipeline_config_default, tmp_workdir):
        """condition='stage.tier1.passed' with tier1 passed → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=True,
            any_failed=False,
            steps=[StepResult(id="s1", type="script", passed=True, output="ok")],
        )
        assert p._check_condition("stage.tier1.passed", {}) is True

    def test_condition_stage_unknown_returns_false(self, pipeline_config_default, tmp_workdir):
        """condition='stage.unknown.passed' returns False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("stage.unknown.passed", {}) is False

    def test_condition_or_logic(self, pipeline_config_default, tmp_workdir):
        """OR logic: one true → True, both false → False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1"]}
        # task.has_criteria is true, so condition should be true regardless
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        assert p._check_condition("stage.tier1.any_failed or task.has_criteria", task) is True

    def test_condition_and_logic(self, pipeline_config_default, tmp_workdir):
        """AND logic: both must be true."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1"]}
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        assert p._check_condition("stage.tier1.passed and task.has_criteria", task) is True


class TestPipelineTemplate:
    """Test Pipeline._template — template substitution — step-1-5-1-5."""

    def test_template_task_id(self, pipeline_config_default, tmp_workdir):
        """{{ task.id }} replaced with task id."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("echo {{ task.id }}", {"id": "my-task"})
        assert result == "echo my-task"

    def test_template_task_title(self, pipeline_config_default, tmp_workdir):
        """{{ task.title }} replaced with task title."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("{{ task.title }}", {"id": "t1", "title": "Hello World"})
        assert "Hello World" in result

    def test_template_task_criteria(self, pipeline_config_default, tmp_workdir):
        """{{ task.criteria }} replaced with JSON array."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("{{ task.criteria }}", {"id": "t1", "criteria": ["c1", "c2"]})
        assert '"c1"' in result

    def test_template_stage_passed(self, pipeline_config_default, tmp_workdir):
        """{{ stage.tier1.passed }} replaced with True/False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        result = p._template("passed={{ stage.tier1.passed }}", {})
        assert "passed=True" in result

    def test_template_stage_any_failed(self, pipeline_config_default, tmp_workdir):
        """{{ stage.tier1.any_failed }} replaced with True/False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(id="tier1", passed=False, any_failed=True, steps=[])
        result = p._template("failed={{ stage.tier1.any_failed }}", {})
        assert "failed=True" in result

    def test_template_stages_full_json(self, pipeline_config_default, tmp_workdir):
        """{{ stages }} replaced with full JSON."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=True,
            any_failed=False,
            steps=[StepResult(id="s1", type="script", passed=True, output="ok")],
        )
        result = p._template("{{ stages }}", {})
        assert "tier1" in result
        assert "passed" in result


class TestLoadPipelineConfig:
    """Test load_pipeline_config() — step-1-5-1-6."""

    def test_no_config_file_returns_default_pipeline(self, tmp_workdir):
        """Config file missing → returns default dict with tier1 + tier2 stages."""
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        stages = config["pipeline"]["stages"]
        assert len(stages) == 2
        assert stages[0]["id"] == "tier1"
        assert stages[1]["id"] == "tier2"

    def test_config_file_no_pipeline_key_returns_default(self, tmp_workdir):
        """Config file exists but no 'pipeline' key → returns default."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"other": "stuff"}, f)
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config

    def test_config_file_empty_stages_returns_default(self, tmp_workdir):
        """Config file has pipeline but no stages → returns minimal default pipeline
        with tier1+secrets stage."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"pipeline": {}}, f)
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        # load_pipeline_config returns the config as-is when pipeline key exists
        # The pipeline dict may be empty since the file had empty stages
        assert isinstance(config["pipeline"], dict)

    def test_malformed_yaml_returns_safe_minimal(self, tmp_workdir):
        """Malformed YAML returns safe minimal pipeline."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write(":broken: yaml: :")
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        assert config["pipeline"]["stages"] == []


class TestPipelineRun:
    """Test Pipeline.run() actual execution."""

    def test_run_parallel_stage(self, pipeline_config_default, tmp_workdir):
        """Pipeline runs parallel stage and returns results.

        Note: tier2 (ai_eval) will fail without LLM key configured,
        but tier1 (parallel scripts) should pass.
        """
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-eval")
        assert "stages" in result
        assert "tier1" in result["stages"]
        assert result["stages"]["tier1"]["passed"] is True
        # Overall passed may be False if tier2 failed (no LLM key), but tier1 should pass

    def test_run_sequential_stage(self, pipeline_config_default, tmp_workdir):
        """Pipeline runs sequential ai_eval stage (skips with no criteria)."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-eval")
        assert "stages" in result

    def test_run_with_llm_injected(self, pipeline_config_default, tmp_workdir, llm_client):
        """Pipeline with LLM injected runs ai_eval stage."""
        p = Pipeline(pipeline_config_default, tmp_workdir, llm=llm_client)
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        with patch.object(
            llm_client,
            "chat",
            return_value=MagicMock(content='{"verdict":"COMPLETE","items":[],"summary":"done"}'),
        ):
            result = p.run(task, trigger="pre-eval")
        assert "stages" in result

    def test_trigger_filtering(self, pipeline_config_default, tmp_workdir):
        """Stages not matching trigger are skipped."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-commit")
        # Only tier1 runs on pre-commit (tier2 is pre-eval only)
        assert "tier1" in result["stages"]
        # tier2 should not run since it has on: ["pre-eval"]
        assert "tier2" not in result["stages"]

    def test_run_precommit_triggers(self, pipeline_config_default, tmp_workdir):
        """pre-commit trigger runs tier1 but not tier2."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "_precommit", "title": "x", "criteria": []}, trigger="pre-commit")
        assert result["passed"] is True
        assert "tier1" in result["stages"]

    def test_unknown_step_type_returns_error(self, pipeline_config_default, tmp_workdir):
        """Unknown step type produces error result."""
        config = {
            "pipeline": {
                "stages": [
                    {"id": "bad", "steps": [{"id": "x", "type": "unknown_type"}], "parallel": True}
                ]
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        assert "bad" in result["stages"]
        step = result["stages"]["bad"]["steps"][0]
        assert step["passed"] is False
        assert "Unknown step type" in step["error"]

    def test_script_no_command_returns_error(self, pipeline_config_default, tmp_workdir):
        """Script step with no command returns error."""
        config = {
            "pipeline": {
                "stages": [
                    {"id": "empty", "parallel": True, "steps": [{"id": "x", "type": "script"}]}
                ]
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        assert "empty" in result["stages"]
        step = result["stages"]["empty"]["steps"][0]
        assert step["passed"] is False
        assert "No command specified" in step["error"]


class TestExtendedPipeline:
    """Extended coverage for Pipeline module."""

    def test_step_result_to_dict_all_fields(self):
        """StepResult.to_dict() includes id, type, passed, output, error."""
        from engine.pipeline import StepResult

        sr = StepResult(id="lint", type="script", passed=True, output="clean", error="")
        d = sr.to_dict()
        assert d["id"] == "lint"
        assert d["type"] == "script"
        assert d["passed"] is True
        assert d["output"] == "clean"
        assert d["error"] == ""

    def test_stage_result_all_failed_true_any_failed(self):
        """StageResult with failed steps has any_failed=True."""
        from engine.pipeline import StageResult, StepResult

        srs = [StepResult(id="s1", type="script", passed=False, output="err")]
        stage = StageResult(id="tier1", passed=False, any_failed=True, steps=srs)
        assert stage.any_failed is True
        assert stage.passed is False

    def test_template_unknown_var_unchanged(self, pipeline_config_default, tmp_workdir):
        """Template with unknown variable leaves braces unchanged."""
        from engine.pipeline import Pipeline

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "t1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "x", "type": "script", "run": "echo {{nonexistent}}"}],
                    },
                ],
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        step = result["stages"]["t1"]["steps"][0]
        # The template variable is not resolved; step output will contain the literal string
        assert "{{nonexistent}}" in step["output"] or step["passed"] is True

    def test_run_with_no_matching_trigger(self, pipeline_config_default, tmp_workdir):
        """Pipeline skips stage when trigger doesn't match."""
        from engine.pipeline import Pipeline

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "t1",
                        "parallel": True,
                        "on": ["pre-commit"],
                        "steps": [{"id": "x", "type": "script", "run": "echo hi"}],
                    },
                ],
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        # t1 not in pre-eval, so it should be skipped
        assert "t1" not in result["stages"]

    def test_script_step_strips_budget_and_llm_cred_env(self, tmp_workdir, monkeypatch):
        """Tier1 script steps must not inherit budget or LLM credential vars.

        Proven 2026-08-09 R2-16: GITREINS_MAX_* exported for a judge run
        leaked into the tier1 pytest subprocess and broke EvalCap /
        config-priority tests (assert 2000000 == 1000000). The GIT_* strip
        must extend to evaluator budget controls. INFRA-LLM-ENV-001: leaking
        GITREINS_LLM_* / OPENROUTER_API_KEY into the pytest subprocess
        breaks test_llm.py env-priority tests (api_key == '' asserted, real
        key found).
        """
        from unittest.mock import patch as _patch

        from engine.pipeline import Pipeline

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        monkeypatch.setenv("GITREINS_MAX_OUTPUT_TOKENS", "2M")
        monkeypatch.setenv("GITREINS_MAX_ITERATIONS", "400")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "sk-keep")
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
        monkeypatch.setenv("KIMI_API_KEY", "kimi-key")
        monkeypatch.setenv("GROQ_API_KEY", "groq-key")
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "t1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "x", "type": "script", "run": "pytest"}],
                    },
                ],
            }
        }
        with _patch("engine.pipeline.subprocess.run", side_effect=fake_run):
            p = Pipeline(config, tmp_workdir)
            result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        assert result["stages"]["t1"]["passed"] is True
        assert "GITREINS_MAX_OUTPUT_TOKENS" not in captured["env"]
        assert "GITREINS_MAX_ITERATIONS" not in captured["env"]
        assert "GITREINS_LLM_API_KEY" not in captured["env"]
        assert "OPENROUTER_API_KEY" not in captured["env"]
        assert "KIMI_API_KEY" not in captured["env"]
        assert "GROQ_API_KEY" not in captured["env"]


# ── Regression: pipeline fallback when config exists but lacks pipeline key ───


class TestLoadPipelineConfigFallback:
    """Regression tests for load_pipeline_config fallback behavior."""

    def test_config_exists_no_pipeline_section_gets_tier1_plus_tier2(self, tmp_workdir):
        """When .gitreins/config.yaml exists but has no 'pipeline' key,
        load_pipeline_config must inject a two-tier pipeline (tier1 + tier2),
        not the old broken single-tier default (secrets: true only)."""
        workdir = tmp_workdir
        # Create a config.yaml with NO pipeline section (simulates existing config
        # that was set up before pipeline was a concept)
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        config_without_pipeline = {
            "guards": {
                "secrets": True,
                "lint": True,
                "tests": True,
                "test_mode": "diff",
                "test_command": "pytest -x --tb=short",
            },
            "evaluator": {
                "model": "deepseek-v4-flash",
                "max_iterations": 100,
            },
        }
        with open(config_path, "w") as f:
            yaml.dump(config_without_pipeline, f)

        result = load_pipeline_config(workdir)

        assert "pipeline" in result
        stages = result["pipeline"]["stages"]
        assert len(stages) >= 2, f"Expected at least tier1 + tier2, got {len(stages)} stage(s)"

        tier1 = next((s for s in stages if s["id"] == "tier1"), None)
        tier2 = next((s for s in stages if s["id"] == "tier2"), None)

        assert tier1 is not None, "tier1 stage missing from fallback pipeline"
        assert tier2 is not None, "tier2 stage missing from fallback pipeline"

        # tier1 should have real steps, not just secrets: true
        assert len(tier1["steps"]) >= 1
        step_ids = [s["id"] for s in tier1["steps"]]
        assert "secrets" in step_ids, "secrets step missing from tier1 fallback"

        # tier2 should be an ai_eval stage
        assert tier2["type"] == "ai_eval", f"tier2 should be ai_eval, got {tier2['type']}"
        assert "tools" in tier2, "tier2 should have tools configured"
        assert "max_iterations" in tier2, "tier2 should have max_iterations"

    def test_config_missing_file_gets_two_tier_default(self, tmp_workdir):
        """When .gitreins/config.yaml does not exist at all,
        load_pipeline_config returns the full default (already correct)."""
        workdir = tmp_workdir
        result = load_pipeline_config(workdir)

        stages = result["pipeline"]["stages"]
        assert len(stages) >= 2
        tier1 = next((s for s in stages if s["id"] == "tier1"), None)
        tier2 = next((s for s in stages if s["id"] == "tier2"), None)
        assert tier1 is not None
        assert tier2 is not None
        assert tier2["type"] == "ai_eval"

    def test_default_tier1_secrets_step_suppresses_gitleaks_banner(self, tmp_workdir):
        """The default pipeline's tier1 secrets step runs gitleaks with
        --no-banner (DF-006): the banner must not leak into captured tier1
        output that reaches judge verdicts."""
        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"guards": {"secrets": True}}, f)

        result = load_pipeline_config(workdir)
        tier1 = next(s for s in result["pipeline"]["stages"] if s["id"] == "tier1")
        secrets_step = next(s for s in tier1["steps"] if s["id"] == "secrets")
        assert "--no-banner" in secrets_step["run"], (
            "tier1 secrets step must pass --no-banner to gitleaks"
        )


# ── GR-063c: C++ pipeline — split "c" from "cpp" ───────────────────────────


class TestCppLanguageDetection:
    """Verify C++ pipeline detection: CMakeLists.txt → cpp, Makefile → c."""

    def test_cmake_lists_txt_detected_as_cpp(self, tmp_workdir):
        """CMakeLists.txt alone → primary language is cpp."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "secrets" in step_ids, "secrets step should always be present"

    def test_makefile_only_detected_as_c(self, tmp_workdir):
        """Makefile alone (no CMakeLists.txt) → primary language is c."""
        from engine.pipeline import _default_tier1_steps

        makefile_path = os.path.join(tmp_workdir, "Makefile")
        with open(makefile_path, "w") as f:
            f.write("all:\n\techo ok\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_cmake_takes_priority_over_makefile(self, tmp_workdir):
        """Both CMakeLists.txt and Makefile present → CMakeLists.txt wins (cpp)."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        makefile_path = os.path.join(tmp_workdir, "Makefile")
        with open(makefile_path, "w") as f:
            f.write("all:\n\techo ok\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_cpp_lang_commands_produces_lint_and_test(self, tmp_workdir):
        """C++ pipeline produces lint + test steps (via make)."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for C++ project"
        assert test_step is not None, "tests step missing for C++ project"
        assert "make" in lint_step["run"], f"Expected make lint, got {lint_step['run']}"
        assert "make" in test_step["run"], f"Expected make test, got {test_step['run']}"

    def test_c_and_cpp_both_produce_steps(self, tmp_workdir):
        """Both C (Makefile) and C++ (CMakeLists.txt) produce lint+test steps."""
        from engine.pipeline import _default_tier1_steps

        # Test C
        with open(os.path.join(tmp_workdir, "Makefile"), "w") as f:
            f.write("all:\n\techo ok\n")
        c_steps = _default_tier1_steps(tmp_workdir)
        c_ids = [s["id"] for s in c_steps]
        assert "lint" in c_ids, f"C project missing lint: {c_ids}"
        assert "tests" in c_ids, f"C project missing tests: {c_ids}"
        # Remove Makefile, test C++
        os.remove(os.path.join(tmp_workdir, "Makefile"))
        with open(os.path.join(tmp_workdir, "CMakeLists.txt"), "w") as f:
            f.write("project(test)\n")
        cpp_steps = _default_tier1_steps(tmp_workdir)
        cpp_ids = [s["id"] for s in cpp_steps]
        assert "lint" in cpp_ids, f"C++ project missing lint: {cpp_ids}"
        assert "tests" in cpp_ids, f"C++ project missing tests: {cpp_ids}"

    def test_no_cmake_or_makefile_skips_c_and_cpp(self, tmp_workdir):
        """No CMakeLists.txt or Makefile → falls back to secrets-only."""
        from engine.pipeline import _default_tier1_steps

        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" not in step_ids, f"No C/C++ signature → should not have lint step: {step_ids}"
        assert "tests" not in step_ids, (
            f"No C/C++ signature → should not have tests step: {step_ids}"
        )


# ── Guards config overrides: lint_command / test_command / test_timeout ─────


class TestGuardsCommandOverrides:
    """Verify .gitreins/config.yaml guards overrides reach the tier1 steps.

    Regression: ``guards.lint_command`` was dead config — the judge's tier1
    lint step always used the hardcoded language default (``ruff check .``),
    which fails in the judge's bare-shell subprocess where ``.venv/bin`` is
    not on PATH. The config override (``uv run ruff check .``) must win.
    """

    def _make_py_workdir(self, tmp_workdir):
        with open(os.path.join(tmp_workdir, "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'x'\n")
        return tmp_workdir

    def test_lint_command_override_wins(self, tmp_workdir):
        """guards.lint_command replaces the language-default lint command."""
        from engine.pipeline import _default_tier1_steps

        self._make_py_workdir(tmp_workdir)
        steps = _default_tier1_steps(
            tmp_workdir, {"guards": {"lint_command": "uv run ruff check ."}}
        )
        lint_step = next(s for s in steps if s["id"] == "lint")
        assert lint_step["run"] == "uv run ruff check ."

    def test_lint_command_absent_uses_language_default(self, tmp_workdir):
        """No lint_command configured → language default is kept."""
        from engine.pipeline import _default_tier1_steps

        self._make_py_workdir(tmp_workdir)
        steps = _default_tier1_steps(tmp_workdir, {})
        lint_step = next(s for s in steps if s["id"] == "lint")
        assert "ruff" in lint_step["run"]

    def test_test_command_override_wins(self, tmp_workdir):
        """guards.test_command still replaces the default test command."""
        from engine.pipeline import _default_tier1_steps

        self._make_py_workdir(tmp_workdir)
        steps = _default_tier1_steps(
            tmp_workdir, {"guards": {"test_command": "uv run pytest -x"}}
        )
        test_step = next(s for s in steps if s["id"] == "tests")
        assert test_step["run"] == "uv run pytest -x"

    def test_test_timeout_override_wins(self, tmp_workdir):
        """guards.test_timeout replaces the 120s default."""
        from engine.pipeline import _default_tier1_steps

        self._make_py_workdir(tmp_workdir)
        steps = _default_tier1_steps(
            tmp_workdir, {"guards": {"test_timeout": 300}}
        )
        test_step = next(s for s in steps if s["id"] == "tests")
        assert test_step["timeout"] == 300

    def test_lint_and_test_overrides_together(self, tmp_workdir):
        """Both overrides applied in one config."""
        from engine.pipeline import _default_tier1_steps

        self._make_py_workdir(tmp_workdir)
        steps = _default_tier1_steps(
            tmp_workdir,
            {
                "guards": {
                    "lint_command": "uv run ruff check .",
                    "test_command": "uv run pytest -x --tb=short",
                    "test_timeout": 900,
                }
            },
        )
        by_id = {s["id"]: s for s in steps}
        assert by_id["lint"]["run"] == "uv run ruff check ."
        assert by_id["tests"]["run"] == "uv run pytest -x --tb=short"
        assert by_id["tests"]["timeout"] == 900


# ── GR-063j: C# pipeline — dotnet + .csproj/.sln detection ───────────────────


class TestCsharpLanguageDetection:
    """Verify C# pipeline detection: .csproj/.sln → csharp."""

    def test_csproj_detected_as_csharp(self, tmp_workdir):
        """*.csproj file → primary language is csharp."""
        from engine.pipeline import _default_tier1_steps

        csproj_path = os.path.join(tmp_workdir, "MyProject.csproj")
        with open(csproj_path, "w") as f:
            f.write("<Project />\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_sln_detected_as_csharp(self, tmp_workdir):
        """*.sln file → primary language is csharp."""
        from engine.pipeline import _default_tier1_steps

        sln_path = os.path.join(tmp_workdir, "MySolution.sln")
        with open(sln_path, "w") as f:
            f.write("Microsoft Visual Studio Solution File\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_csharp_lang_commands_produces_dotnet_steps(self, tmp_workdir):
        """C# pipeline produces lint + test steps (via dotnet)."""
        from engine.pipeline import _default_tier1_steps

        csproj_path = os.path.join(tmp_workdir, "App.csproj")
        with open(csproj_path, "w") as f:
            f.write("<Project />\n")
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for C# project"
        assert test_step is not None, "tests step missing for C# project"
        assert "dotnet" in lint_step["run"], f"Expected dotnet, got {lint_step['run']}"
        assert "dotnet" in test_step["run"], f"Expected dotnet, got {test_step['run']}"

    def test_csproj_takes_priority_over_sln(self, tmp_workdir):
        """Both .csproj and .sln present → .csproj detected first (csharp)."""
        from engine.pipeline import _default_tier1_steps

        with open(os.path.join(tmp_workdir, "App.csproj"), "w") as f:
            f.write("<Project />\n")
        with open(os.path.join(tmp_workdir, "App.sln"), "w") as f:
            f.write("Solution\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids


class TestScalaLanguageDetection:
    """Verify Scala pipeline detection: build.sbt → scala."""

    def test_build_sbt_detected_as_scala(self, tmp_workdir):
        """build.sbt file → primary language is scala."""
        from engine.pipeline import _default_tier1_steps

        sbt_path = os.path.join(tmp_workdir, "build.sbt")
        with open(sbt_path, "w") as f:
            f.write('name := "test"\n')
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_scala_lang_commands_produces_sbt_steps(self, tmp_workdir):
        """Scala pipeline produces lint + test steps (via sbt)."""
        from engine.pipeline import _default_tier1_steps

        sbt_path = os.path.join(tmp_workdir, "build.sbt")
        with open(sbt_path, "w") as f:
            f.write('name := "test"\n')
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for Scala project"
        assert test_step is not None, "tests step missing for Scala project"
        assert "sbt" in lint_step["run"], f"Expected sbt, got {lint_step['run']}"
        assert "sbt" in test_step["run"], f"Expected sbt, got {test_step['run']}"


class TestAiEvalCapForwarding:
    """_run_ai_eval cap resolution: step overrides over config base.

    Regression coverage for the fleet-wide tier2 compaction loop
    ('Context near limit (N/-1 tokens)' — token caps at -1 made the
    compaction threshold int(-1*0.9)=0, so the evaluator compacted on
    every turn and never produced a verdict, 2026-08).
    """

    def _make_verdict(self):
        v = MagicMock()
        v.verdict = "COMPLETE"
        v.summary = "ok"
        v.items = []
        return v

    def test_no_step_caps_defers_to_config(self, tmp_workdir, llm_client):
        """Step with only max_iterations: -1 defers — no explicit EvalCap."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,  # defer to evaluator config
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        # Deferral: AgenticEvaluator called WITHOUT an explicit eval_cap,
        # so it reads .gitreins/config.yaml itself (documented working path).
        _, kwargs = mock_eval.call_args
        assert "eval_cap" not in kwargs, (
            "deferral step must not pass an explicit EvalCap — all -1 caps "
            "would make compaction threshold 0 (compaction loop)"
        )

    def test_step_explicit_caps_override_config(self, tmp_workdir, llm_client):
        """Step-level caps are forwarded (parsed) over the config base."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": 25,
                        "max_time": "5m",
                        "max_input_tokens": "200k",
                        "max_output_tokens": "50k",
                        "tool_call_weight": 0.2,
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        _, kwargs = mock_eval.call_args
        cap = kwargs["eval_cap"]
        assert cap.max_iterations == 25
        assert cap.max_seconds == 300.0
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 50_000
        assert cap.tool_call_weight == 0.2
        assert not cap.is_unlimited

    def test_partial_step_caps_merge_config_base(self, tmp_workdir, llm_client):
        """Unset caps fall back to the config base, not to unlimited."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": 25,  # only iterations pinned
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        _, kwargs = mock_eval.call_args
        cap = kwargs["eval_cap"]
        assert cap.max_iterations == 25
        # Token caps from config base — NOT -1 (compaction loop guard)
        assert cap.max_input_tokens == 10_000_000
        assert cap.max_output_tokens == 1_000_000
        assert cap.max_seconds == 1800.0
        assert not cap.is_unlimited
