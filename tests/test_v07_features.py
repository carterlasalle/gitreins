"""
Integration tests for v0.7.0 features:
  - Verdict persistence (engine/persist.py)
  - gitreins report command
  - Task dependencies (--depends-on)
  - gitreins init command
  - Cleaner guard output
"""

import json
import os
import sys

import pytest

# Add gitreins-poc to path for direct imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════
# Verdict persistence
# ══════════════════════════════════════════════════════════════════


class TestVerdictPersistence:
    """Tests for engine/persist.py — verdict storage and reporting."""

    def test_config_defaults(self):
        """Verifies default history config values."""
        from engine.persist import DEFAULT_HISTORY_CONFIG

        assert DEFAULT_HISTORY_CONFIG["enabled"] is True
        assert DEFAULT_HISTORY_CONFIG["storage"] == "git"
        assert DEFAULT_HISTORY_CONFIG["max_verdicts"] == 1000
        assert ".gitreins/history" in DEFAULT_HISTORY_CONFIG["path"]

    def test_load_history_config_defaults(self, tmp_path):
        """load_history_config returns defaults when no config file exists."""
        from engine.persist import load_history_config

        config = load_history_config(str(tmp_path))
        assert config["enabled"] is True
        assert config["storage"] == "git"

    def test_load_history_config_disabled(self, tmp_path):
        """load_history_config respects disabled flag in config."""
        from engine.persist import load_history_config
        import yaml

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        config_path = os.path.join(tmp_path, ".gitreins", "config.yaml")
        with open(config_path, "w") as f:
            yaml.dump({"history": {"enabled": False, "storage": "filesystem"}}, f)

        config = load_history_config(str(tmp_path))
        assert config["enabled"] is False
        assert config["storage"] == "filesystem"

    def test_persist_writes_files(self, tmp_path):
        """persist() writes verdict.json and summary.md to the history dir."""
        from engine.persist import VerdictPersister

        persister = VerdictPersister(workdir=str(tmp_path))
        # Override storage to filesystem to avoid git ops
        persister.config["storage"] = "filesystem"

        verdict_data = {
            "task_id": "test-task",
            "task_title": "Test Task",
            "task_criteria": ["criteria 1", "criteria 2"],
            "passed": True,
            "items": [
                {"criterion": "criteria 1", "status": "PASS", "detail": "works"},
                {"criterion": "criteria 2", "status": "PASS", "detail": "also works"},
            ],
            "summary": "All good",
        }

        commit_hash = persister.persist("test-task", verdict_data)
        assert commit_hash in ("dry-run", "disabled") or len(commit_hash) == 8

        # Verify files exist
        history_dir = persister.history_dir
        assert os.path.isdir(history_dir)

        # Find the entry
        date_dirs = os.listdir(history_dir)
        assert len(date_dirs) > 0

        entry_dir = os.path.join(history_dir, date_dirs[0])
        hash_dirs = os.listdir(entry_dir)
        assert len(hash_dirs) > 0

        final_dir = os.path.join(entry_dir, hash_dirs[0])
        assert os.path.isfile(os.path.join(final_dir, "verdict.json"))
        assert os.path.isfile(os.path.join(final_dir, "summary.md"))

        # Verify verdict.json content
        with open(os.path.join(final_dir, "verdict.json")) as f:
            saved = json.load(f)
        assert saved["task_id"] == "test-task"
        assert saved["passed"] is True

    def test_persist_disabled_returns_disabled(self, tmp_path):
        """When history is disabled, persist returns 'disabled'."""
        from engine.persist import VerdictPersister

        persister = VerdictPersister(workdir=str(tmp_path))
        persister.config["enabled"] = False

        result = persister.persist("test", {"passed": True})
        assert result == "disabled"

    def test_list_verdicts(self, tmp_path):
        """list_verdicts returns persisted verdicts."""
        from engine.persist import VerdictPersister

        persister = VerdictPersister(workdir=str(tmp_path))
        persister.config["storage"] = "filesystem"

        # Persist two verdicts
        persister.persist(
            "task-a", {"task_id": "task-a", "task_title": "A", "passed": True, "items": []}
        )
        import time

        time.sleep(0.1)  # ensure different timestamps/hashes
        persister.persist(
            "task-b", {"task_id": "task-b", "task_title": "B", "passed": False, "items": []}
        )

        verdicts = persister.list_verdicts(n=10)
        assert len(verdicts) == 2

        # Filter by task_id
        a_only = persister.list_verdicts(n=10, task_id="task-a")
        assert len(a_only) == 1
        assert a_only[0]["task_id"] == "task-a"

    def test_build_report(self, tmp_path):
        """build_report returns a formatted string."""
        from engine.persist import VerdictPersister, build_report

        persister = VerdictPersister(workdir=str(tmp_path))
        persister.config["storage"] = "filesystem"
        persister.persist(
            "test-report",
            {"task_id": "test-report", "task_title": "Report Test", "passed": True, "items": []},
        )

        report = build_report(str(tmp_path), n=10)
        assert "test-report" in report
        assert "100%" in report
        assert "gitreins" not in report.lower() or "storage" in report.lower()


# ══════════════════════════════════════════════════════════════════
# Task dependencies
# ══════════════════════════════════════════════════════════════════


class TestTaskDependencies:
    """Tests for task dependencies (--depends-on flag)."""

    def test_task_has_depends_on_field(self):
        """Task dataclass includes depends_on field."""
        from engine.task_manager import Task

        task = Task(id="test", title="Test", depends_on=["other-task"])
        assert task.depends_on == ["other-task"]

    def test_task_saved_with_depends_on(self, tmp_path):
        """Tasks with depends_on are persisted and loaded back."""
        import yaml
        from engine.task_manager import TaskManager

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        tm.create("dep-a", "Dependency A", [])
        tm.complete("dep-a")

        task = tm.create("main-task", "Main Task", ["do something"], depends_on=["dep-a"])
        assert task.depends_on == ["dep-a"]

        # Verify saved to YAML
        with open(os.path.join(tmp_path, ".gitreins", "tasks.yaml")) as f:
            data = yaml.safe_load(f)
        saved = [t for t in data["tasks"] if t["id"] == "main-task"][0]
        assert saved["depends_on"] == ["dep-a"]

        # Load fresh and verify
        tm2 = TaskManager(workdir=str(tmp_path))
        loaded = tm2.get("main-task")
        assert loaded.depends_on == ["dep-a"]

    def test_check_dependencies_passes_when_deps_complete(self, tmp_path):
        """check_dependencies returns empty when all deps are complete."""
        from engine.task_manager import TaskManager

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        tm.create("dep-1", "Dep 1", [])
        tm.complete("dep-1")
        tm.create("main", "Main", [], depends_on=["dep-1"])

        blocked = tm.check_dependencies("main")
        assert blocked == []

    def test_check_dependencies_blocks_when_deps_pending(self, tmp_path):
        """check_dependencies returns blocked list when deps not complete."""
        from engine.task_manager import TaskManager

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        tm.create("dep-1", "Dep 1", [])
        # dep-1 is still pending — not completed
        tm.create("main", "Main", [], depends_on=["dep-1"])

        blocked = tm.check_dependencies("main")
        assert "dep-1" in blocked

    def test_complete_blocks_on_dependencies(self, tmp_path):
        """complete() raises DependencyError when deps not met."""
        from engine.task_manager import TaskManager, DependencyError

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        tm.create("dep-1", "Dep 1", [])
        tm.create("main", "Main", [], depends_on=["dep-1"])

        with pytest.raises(DependencyError, match="dep-1"):
            tm.complete("main")

    def test_complete_force_bypasses_dependencies(self, tmp_path):
        """complete(force=True) skips dependency checks."""
        from engine.task_manager import TaskManager

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        tm.create("dep-1", "Dep 1", [])
        tm.create("main", "Main", [], depends_on=["dep-1"])

        # Should succeed with force
        task = tm.complete("main", force=True)
        assert task.status == "complete"

    def test_no_depends_on_defaults_to_empty(self, tmp_path):
        """Tasks without --depends-on default to empty list."""
        from engine.task_manager import TaskManager

        os.makedirs(os.path.join(tmp_path, ".gitreins"), exist_ok=True)
        tm = TaskManager(workdir=str(tmp_path))

        task = tm.create("standalone", "No deps", [])
        assert task.depends_on == []
        assert tm.check_dependencies("standalone") == []


# ══════════════════════════════════════════════════════════════════
# gitreins init
# ══════════════════════════════════════════════════════════════════


class TestInit:
    """Tests for gitreins init command."""

    def test_detect_go_project(self, tmp_path):
        """Detects Go project from go.mod."""
        from gitreins.cli import _detect_language

        (tmp_path / "go.mod").write_text("module example.com/test")
        info = _detect_language(str(tmp_path))
        assert info["is_go"]
        assert info["name"] == "Go"

    def test_detect_python_project(self, tmp_path):
        """Detects Python project from pyproject.toml."""
        from gitreins.cli import _detect_language

        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        info = _detect_language(str(tmp_path))
        assert info["is_python"]
        assert info["name"] == "Python"

    def test_detect_ts_project(self, tmp_path):
        """Detects TypeScript project from package.json."""
        from gitreins.cli import _detect_language

        (tmp_path / "package.json").write_text('{"name":"test"}')
        info = _detect_language(str(tmp_path))
        assert info["is_ts"]
        assert info["name"] == "TypeScript"

    def test_detect_unknown_project(self, tmp_path):
        """Returns unknown for unrecognized project types."""
        from gitreins.cli import _detect_language

        info = _detect_language(str(tmp_path))
        assert info["name"] == "unknown"
        assert not any([info["is_go"], info["is_python"], info["is_ts"]])

    def test_detect_python_from_requirements(self, tmp_path):
        """Detects Python from requirements.txt as fallback."""
        from gitreins.cli import _detect_language

        (tmp_path / "requirements.txt").write_text("requests>=2.28\n")
        info = _detect_language(str(tmp_path))
        assert info["is_python"]
        assert info["name"] == "Python"

    def test_detect_python_from_root_package_layout(self, tmp_path):
        """Detects Python from a root package dir with __init__.py (no pyproject)."""
        from gitreins.cli import _detect_language

        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")
        info = _detect_language(str(tmp_path))
        assert info["is_python"]

    def test_detect_python_from_top_level_py_file(self, tmp_path):
        """Detects Python from a top-level *.py file (no pyproject)."""
        from gitreins.cli import _detect_language

        (tmp_path / "main.py").write_text("print('hi')\n")
        info = _detect_language(str(tmp_path))
        assert info["is_python"]

    def test_tests_dir_alone_is_not_python(self, tmp_path):
        """A lone tests/ dir is NOT enough for Python detection (fallback is narrow)."""
        from gitreins.cli import _detect_language

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        info = _detect_language(str(tmp_path))
        assert not info["is_python"]

    def test_python_root_package_with_tests_uses_python3_m_pytest(self, tmp_path):
        """Root-package + tests/ + no pytest pythonpath config → python3 -m pytest.

        pytest 9 importlib mode doesn't put the repo root on sys.path, so
        bare `pytest` cannot import the root package. (DF-002)
        """
        from gitreins.cli import _detect_language, _detect_test_command

        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")
        (tmp_path / "todo_stats" / "todo.py").write_text("def add(): pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_todo.py").write_text("def test_add(): pass\n")

        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "python3 -m pytest -x --tb=short"

    def test_python_root_package_with_pythonpath_config_uses_bare_pytest(self, tmp_path):
        """Root-package + tests/ but pyproject configures pytest pythonpath → bare pytest."""
        from gitreins.cli import _detect_language, _detect_test_command

        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_todo.py").write_text("def test_add(): pass\n")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='todo'\n[tool.pytest.ini_options]\npythonpath = [\".\"]\n"
        )

        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "pytest -x --tb=short"

    def test_pythonpath_detection_falls_back_to_tomli_on_py310(self, monkeypatch, tmp_path):
        """Simulate Python 3.10 (no tomllib): the tomli backport must be used.

        Regression for DF-009: on 3.10 the old code swallowed ImportError and
        skipped the pyproject.toml pythonpath check, so _detect_test_command
        wrongly returned 'python3 -m pytest ...' for pythonpath-configured
        projects (CI 3.10 job went red on the bare-pytest assertion).
        """
        import builtins
        import sys
        import types

        try:
            import tomllib as real_tomllib
        except ImportError:  # pragma: no cover — real Python 3.10
            import tomli as real_tomllib

        # Provide a fake tomli so the fallback resolves even on 3.11+, where
        # the backport is not installed (marker: python_version < "3.11").
        fake_tomli = types.ModuleType("tomli")
        setattr(fake_tomli, "load", real_tomllib.load)
        monkeypatch.setitem(sys.modules, "tomli", fake_tomli)

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError("No module named 'tomllib' (simulated Python 3.10)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        from gitreins.cli import _has_pytest_pythonpath_config

        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='todo'\n[tool.pytest.ini_options]\npythonpath = [\".\"]\n"
        )
        assert _has_pytest_pythonpath_config(str(tmp_path)) is True

    def test_python_root_package_with_pytest_ini_pythonpath_uses_bare_pytest(self, tmp_path):
        """Root-package + tests/ but pytest.ini configures pythonpath → bare pytest."""
        from gitreins.cli import _detect_language, _detect_test_command

        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_todo.py").write_text("def test_add(): pass\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "pytest -x --tb=short"

    def test_python_root_package_without_tests_uses_bare_pytest(self, tmp_path):
        """Root-package layout but NO tests/ dir → bare pytest (nothing to import)."""
        from gitreins.cli import _detect_language, _detect_test_command

        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")

        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "pytest -x --tb=short"

    def test_pyproject_only_no_root_package_uses_bare_pytest(self, tmp_path):
        """pyproject-based project without root package dirs → bare pytest."""
        from gitreins.cli import _detect_language, _detect_test_command

        (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")

        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "pytest -x --tb=short"

    def test_detect_ts_from_tsconfig(self, tmp_path):
        """Detects TypeScript from tsconfig.json (no package.json)."""
        from gitreins.cli import _detect_language

        (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{}}')
        info = _detect_language(str(tmp_path))
        assert info["is_ts"]
        assert info["name"] == "TypeScript"

    def test_detect_ruby_from_gemspec(self, tmp_path):
        """Detects Ruby from .gemspec file (no Gemfile)."""
        from gitreins.cli import _detect_language

        (tmp_path / "mygem.gemspec").write_text("Gem::Specification.new do |s| end")
        info = _detect_language(str(tmp_path))
        assert info["is_ruby"]
        assert "Ruby" in info["name"]

    def test_detect_multiple_langs(self, tmp_path):
        """Multi-language project: pyproject.toml + package.json → both detected."""
        from gitreins.cli import _detect_language

        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        (tmp_path / "package.json").write_text('{"name":"test"}')
        info = _detect_language(str(tmp_path))
        assert info["is_python"]
        assert info["is_ts"]
        assert "Python" in info["name"]
        assert "TypeScript" in info["name"]
        # 'type' is first detected language
        assert info["type"] == "python"

    def test_detect_sql_even_with_python(self, tmp_path):
        """SQL detection runs regardless of other languages (was gated behind not is_python)."""
        from gitreins.cli import _detect_language

        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        (tmp_path / "migrations").mkdir()
        info = _detect_language(str(tmp_path))
        assert info["is_python"]
        assert info["has_sql"], "SQL should be detected even when Python is present"
        assert "SQL" in info["name"]

    def test_detect_all_flags_multi_lang(self, tmp_path):
        """Go + Python + Rust project — all flags set, no elif shadowing."""
        from gitreins.cli import _detect_language

        (tmp_path / "go.mod").write_text("module example.com/test")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        (tmp_path / "Cargo.toml").write_text("[package]\nname='test'")
        info = _detect_language(str(tmp_path))
        assert info["is_go"]
        assert info["is_python"]
        assert info["is_rust"]
        assert "Go" in info["name"]
        assert "Python" in info["name"]
        assert "Rust" in info["name"]
        # 'type' is first detected
        assert info["type"] == "go"

    def test_detect_static_analysis_tools_multi_lang(self, tmp_path):
        """Multi-language detection feeds all languages to tool discovery."""
        from gitreins.cli import _detect_language, _detect_static_analysis_tools
        from unittest.mock import patch

        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")

        def fake_list_available(lang):
            return [f"tool-for-{lang}"]

        with patch("engine.static_analysis.list_available_tools", side_effect=fake_list_available):
            lang_info = _detect_language(str(tmp_path))
            tools = _detect_static_analysis_tools(str(tmp_path), lang_info)

        assert "tool-for-python" in tools
        assert "tool-for-ruby" in tools

    def test_build_guards_go(self):
        """Go project guards include go: section and disable Python-only guards."""
        from gitreins.cli import _build_guards_section

        lang = {"is_go": True, "is_python": False, "is_ts": False}
        guards = _build_guards_section(lang, "go test ./...")
        assert guards["secrets"] is True
        assert guards["lint"] is False  # Python-only
        assert guards["tests"] is False  # Python-only
        assert guards["test_mode"] == "full"  # new projects start full
        assert guards["go"]["build"] is True
        assert guards["go"]["lint"] is True
        assert guards["go"]["tests"] is True

    def test_build_guards_python(self):
        """Python project guards use pytest and full test mode default."""
        from gitreins.cli import _build_guards_section

        lang = {"is_go": False, "is_python": True, "is_ts": False}
        guards = _build_guards_section(lang, "pytest")
        assert guards["secrets"] is True
        assert guards["lint"] is True
        assert guards["tests"] is True
        assert guards["test_mode"] == "full"

    def test_fill_missing_guards_adds_missing(self):
        """_fill_missing_guards adds missing keys without overwriting existing."""
        from gitreins.cli import _fill_missing_guards

        lang = {"is_go": True, "is_python": False, "is_ts": False}
        existing = {"secrets": False}  # explicit override
        _fill_missing_guards(existing, lang, "go test ./...")
        # secrets should NOT be overwritten
        assert existing["secrets"] is False
        # missing keys should be added
        assert "lint" in existing
        assert "go" in existing

    def test_detect_project_size_small(self, tmp_path):
        """Small projects get low cap recommendations."""
        from gitreins.cli import _detect_project_size

        lang = {"is_python": True, "is_go": False}
        (tmp_path / "__init__.py").write_text("")
        size = _detect_project_size(str(tmp_path), lang)
        assert size["packages"] == 1
        assert size["max_iterations"] == 15
        assert size["test_mode"] == "full"

    def test_detect_project_size_large(self, tmp_path):
        """Large projects get higher caps and diff mode recommendation."""
        from gitreins.cli import _detect_project_size

        lang = {"is_python": True, "is_go": False, "is_ts": False}
        # Create 6 packages
        for i in range(6):
            pkg = tmp_path / f"pkg{i}"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")

        size = _detect_project_size(str(tmp_path), lang)
        assert size["packages"] >= 6
        assert size["max_iterations"] == 25  # 6 packages → 6 ≤ 10 → 25
        assert size["test_mode"] == "diff"


# ══════════════════════════════════════════════════════════════════
# Cleaner guard output
# ══════════════════════════════════════════════════════════════════


class TestGuardOutput:
    """Tests for the cleaner guard output formatting."""

    def test_guard_result_pass_detail(self):
        """GuardResult._pass_detail returns appropriate short details."""
        from engine.guard_manager import GuardResult

        secrets = GuardResult(name="secrets", passed=True, output="gitleaks: clean")
        assert "clean" in secrets._pass_detail()

        lint = GuardResult(name="lint", passed=True)
        assert "ok" in lint._pass_detail()

        go_lint = GuardResult(name="go_lint", passed=True)
        assert "ok" in go_lint._pass_detail()

        tests = GuardResult(name="tests", passed=True, output="10 passed")
        assert "passed" in tests._pass_detail()

        # Unknown guard types return empty
        unknown = GuardResult(name="custom", passed=True)
        assert unknown._pass_detail() == ""

    def test_tier1_result_summary_with_failures(self):
        """Tier1Result.summary shows failure counts."""
        from engine.guard_manager import Tier1Result, GuardResult

        result = Tier1Result(
            passed=False,
            results=[
                GuardResult(name="secrets", passed=True, output="clean"),
                GuardResult(
                    name="tests", passed=False, output="FAILED test_a\nFAILED test_b\nFAILED test_c"
                ),
            ],
        )
        summary = result.summary
        assert "✓ secrets" in summary
        assert "✗ tests" in summary
        assert "failure" in summary.lower()

    def test_tier1_result_extra_populated(self):
        """Tier1Result.extra contains test_mode info."""
        from engine.guard_manager import Tier1Result, GuardResult

        result = Tier1Result(
            passed=True,
            results=[GuardResult(name="secrets", passed=True)],
            extra={"test_mode": "diff", "test_targets": 3, "staged_count": 5},
        )
        assert result.extra["test_mode"] == "diff"
        assert result.extra["test_targets"] == 3
