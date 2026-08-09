#!/usr/bin/env python3
"""
GitReins CLI — Human-usable command line.

Usage:
    gitreins install
    gitreins task create <id> <title> [criteria...]
    gitreins task start <id>
    gitreins task complete <id>
    gitreins task list [--status pending|in_progress|complete]
    gitreins task delete <id>
    gitreins guard run
    gitreins judge <id>
    gitreins commit <message>
    gitreins mcp-server
"""

import argparse
import logging
import os
import sys
import time
import yaml

from engine.version import __version__

DEFAULT_GITREINS_CONFIG = """\
# GitReins Configuration

# ── Global defaults (overrides engine.config.GitReinsDefaults) ─────
defaults:
  # model: deepseek-v4-flash            # default LLM
  max_iterations: 100                   # LLM turns (-1 = unlimited)
  # max_time: "30m"                     # wall clock
  max_input_tokens: "10M"               # 10 million
  max_output_tokens: "1M"               # 1 million
  tool_call_weight: 0.1                 # fraction per tool call
  check_for_updates: true               # check PyPI on each run
  update_check_ttl: "24h"               # re-check after this period

# ── Guards (Tier 1 static checks) ─────────────────────────────────
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"                      # "full" or "diff" (smart)
  test_command: "pytest -x --tb=short"
  # dead_code: true    # opt-in: Python dead-code detection (AST-based)
  # skylos: true       # opt-in: multi-language dead code + AI mistake detection

# ── Evaluator caps ───────────────────────────────────────────────
evaluator:
  max_iterations: 100

# ── Verdict history persistence ──────────────────────────────────
history:
  enabled: true           # false = don't save verdicts
  # path: ".gitreins/history"   # where to store (relative to repo)
  storage: "git"          # "git" = auto-commit to gitreins branch
                          # "filesystem" = write files only, no git
  max_verdicts: 1000      # auto-prune old entries past this limit
"""

PRE_COMMIT_HOOK = """\
#!/usr/bin/env bash
# GitReins pre-commit hook — runs Tier 1 guards on staged changes.

# Skip cleanly if the repo hasn't been initialised with a config.
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ ! -f "$REPO_ROOT/.gitreins/config.yaml" ]; then
    exit 0
fi

cd "$REPO_ROOT"
gitreins guard
exit $?
"""


def load_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml, returning {} if not found.

    WARNING: If the file exists but cannot be parsed (YAML syntax error,
    encoding issue, etc.), this function logs a warning and returns {}.
    Callers that write config back to disk (e.g. cmd_init) MUST check
    whether the original file existed before treating an empty return
    as "no config" — otherwise they will overwrite a broken-but-valuable
    config file with auto-generated defaults.
    """
    logger = logging.getLogger("gitreins")
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            return data
    except yaml.YAMLError as e:
        logger.warning(
            "Failed to parse %s: %s — config file exists but YAML is invalid. "
            "Callers: do NOT overwrite this file with defaults. "
            "User: fix the YAML syntax before running 'gitreins init'.",
            config_path,
            e,
        )
        return {}
    except Exception as e:
        logger.warning("Failed to load %s: %s", config_path, e)
        return {}


def _safe_overwrite(path: str, content_func) -> str | None:
    """Write content to path, backing up the original if it exists.

    Args:
        path: Absolute or relative path to write.
        content_func: Callable that writes to an open file handle, e.g.
            lambda f: f.write(text) or lambda f: yaml.dump(data, f).

    Creates parent directories if missing. Creates a .bak copy of the
    original file before overwriting. The caller is responsible for
    ensuring the content is valid before calling — this is a write
    safety net, not a content validator.

    Returns the bak_path if a backup was created, else None.
    """
    import shutil

    bak_path = None
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        bak_path = path + ".bak"
        shutil.copy2(path, bak_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        content_func(f)
    return bak_path


def get_workdir() -> str:
    """Find the git repo root."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return os.getcwd()


def _check_for_updates():
    """Check PyPI for a newer version. Prints notice to stderr if available."""
    try:
        from engine.config import check_for_update

        msg = check_for_update(workdir=get_workdir())
        if msg:
            print(f"  \033[33m{msg}\033[0m", file=sys.stderr)
    except Exception:
        pass  # never block on update check failures


def cmd_install(args):
    """One-command GitReins activation for the current repo.

    Creates:
      - .gitreins/config.yaml   (default config if missing)
      - .git/hooks/pre-commit   (runs `gitreins guard` on staged changes)
      - .gitignore              (adds .gitreins/tasks.yaml if not already present)

    For smarter auto-detection, use: gitreins init
    """

    workdir = get_workdir()
    git_dir = os.path.join(workdir, ".git")
    hooks_dir = os.path.join(git_dir, "hooks")
    gitreins_dir = os.path.join(workdir, ".gitreins")
    config_path = os.path.join(gitreins_dir, "config.yaml")
    hook_path = os.path.join(hooks_dir, "pre-commit")
    gitignore_path = os.path.join(workdir, ".gitignore")
    tasks_entry = ".gitreins/tasks.yaml"

    if not os.path.isdir(git_dir):
        print(f"Error: {workdir} is not a git repository (no .git directory).")
        print("Run `git init` first, then re-run `gitreins install`.")
        sys.exit(1)

    created = []
    skipped = []

    # 1. .gitreins/config.yaml
    os.makedirs(gitreins_dir, exist_ok=True)
    if os.path.isfile(config_path):
        skipped.append(config_path)
    else:
        with open(config_path, "w") as f:
            f.write(DEFAULT_GITREINS_CONFIG)
        created.append(config_path)

    # 2. .git/hooks/pre-commit
    os.makedirs(hooks_dir, exist_ok=True)
    hook_existed = os.path.isfile(hook_path)
    with open(hook_path, "w") as f:
        f.write(PRE_COMMIT_HOOK)
    os.chmod(hook_path, 0o755)
    created.append(hook_path + ("" if not hook_existed else " (overwritten)"))

    # 3. .gitignore — add .gitreins/tasks.yaml if not present
    existing_gitignore = ""
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r") as f:
            existing_gitignore = f.read()
    already_present = any(line.strip() == tasks_entry for line in existing_gitignore.splitlines())
    if already_present:
        skipped.append(f"{tasks_entry} already in .gitignore")
    else:
        with open(gitignore_path, "a") as f:
            if existing_gitignore and not existing_gitignore.endswith("\n"):
                f.write("\n")
            f.write(tasks_entry + "\n")
        created.append(f".gitignore (added {tasks_entry})")

    # 4. Success summary
    print(f"GitReins installed in {workdir}")
    print()
    print("Created:")
    for path in created:
        print(f"  + {path}")
    if skipped:
        print()
        print("Skipped:")
        for path in skipped:
            print(f"  - {path}")
    print()
    print("Next steps:")
    print("  - Run smart init:  gitreins init")
    print("  - Create a task:  gitreins task create <id> <title> [criteria...]")
    print("  - Run guards:     gitreins guard")
    print("  - Try the hook:   make a change, git add ., git commit -m 'test'")


def cmd_init(args):
    """Smart project initialization — detects language, size, and optimal config.

    Re-runnable: never overwrites existing config values, only adds missing sections.
    Use to upgrade config when new GitReins features ship.
    """
    workdir = get_workdir()
    gitreins_dir = os.path.join(workdir, ".gitreins")
    config_path = os.path.join(gitreins_dir, "config.yaml")

    # Detect project characteristics
    lang_info = _detect_language(workdir)
    test_cmd = _detect_test_command(workdir, lang_info)
    size = _detect_project_size(workdir, lang_info)
    static_tools = _detect_static_analysis_tools(workdir, lang_info)

    # Load existing config or start fresh.
    # CRITICAL: load_config returns {} for BOTH "file doesn't exist" AND
    # "YAML parse error". We must NOT overwrite a broken-but-existent config
    # with auto-generated defaults — that silently nukes user settings.
    config_exists = os.path.isfile(config_path) and os.path.getsize(config_path) > 0
    existing = load_config(workdir)
    if not existing:
        if config_exists:
            print(
                f"Error: {config_path} exists but could not be parsed.\n"
                f"Fix the YAML syntax in that file, then re-run 'gitreins init'.\n"
                f"To start fresh (discarding existing config), use --reset.",
                file=sys.stderr,
            )
            sys.exit(1)
        existing = {}

    # Build or update sections
    changed = []

    # Guards section
    if "guards" not in existing or args.reset:
        existing["guards"] = _build_guards_section(lang_info, test_cmd, static_tools)
        changed.append("guards")
    else:
        # Fill in missing guard keys
        guards = existing.setdefault("guards", {})
        updates = _fill_missing_guards(guards, lang_info, test_cmd)
        if updates:
            changed.append(f"guards (+{', '.join(updates)})")

    # Evaluator section — size-appropriate caps
    if "evaluator" not in existing or args.reset:
        existing["evaluator"] = _build_evaluator_section(size)
        changed.append("evaluator")
    else:
        evaluator = existing.setdefault("evaluator", {})
        if "max_iterations" not in evaluator:
            evaluator["max_iterations"] = size["max_iterations"]
            changed.append("evaluator.max_iterations")

    # History section
    if "history" not in existing or args.reset:
        existing["history"] = {
            "enabled": True,
            "storage": "git",
            "max_verdicts": 1000,
        }
        changed.append("history")

    # Write config
    os.makedirs(gitreins_dir, exist_ok=True)
    bak = _safe_overwrite(
        config_path,
        lambda f: yaml.dump(
            existing,
            f,
            default_flow_style=False,
            sort_keys=False,
        ),
    )
    if bak:
        changed.append(f"backup: {os.path.basename(bak)}")

    # Ensure pre-commit hook exists
    hook_path = os.path.join(workdir, ".git", "hooks", "pre-commit")
    hooks_dir = os.path.dirname(hook_path)
    if not os.path.isfile(hook_path) or args.reset:
        os.makedirs(hooks_dir, exist_ok=True)
        with open(hook_path, "w") as f:
            f.write(PRE_COMMIT_HOOK)
        os.chmod(hook_path, 0o755)
        changed.append("pre-commit hook")

    # Generate .gitleaks.toml if missing (prevents scanning node_modules/.venv/vendor)
    gitleaks_path = os.path.join(workdir, ".gitleaks.toml")
    if not os.path.isfile(gitleaks_path):
        _generate_gitleaks_config(workdir, lang_info, gitleaks_path)
        changed.append(".gitleaks.toml")

    # Summary
    print(f"GitReins init: {workdir}")
    print(f"  Language:    {lang_info['name']}")
    print(f"  Packages:    {size['packages']}")
    print(f"  Test cmd:    {test_cmd}")
    if test_cmd == "python3 -m pytest -x --tb=short":
        print(
            "  Note: using 'python3 -m pytest' — root-package layout without pytest pythonpath "
            'config; add [tool.pytest.ini_options] pythonpath = ["."] to pyproject.toml '
            "to use bare pytest"
        )
    print(f"  Test mode:   {existing['guards'].get('test_mode', 'full')}")
    print(f"  Eval cap:    {existing['evaluator'].get('max_iterations', 100)} iterations")
    print(
        f"  History:     {existing.get('history', {}).get('enabled', True) and 'enabled' or 'disabled'}"
    )
    sa_enabled = existing.get("guards", {}).get("static_analysis", False)
    sa_status = []
    if sa_enabled and static_tools:
        sa_status.append(f"enabled ({', '.join(static_tools)})")
    elif sa_enabled:
        # Build tool-specific install instructions for the detected language
        install_hints = []
        if lang_info["is_python"]:
            install_hints.append("pip install mypy")
        elif lang_info["is_ruby"]:
            install_hints.append("gem install sorbet && srb init")
        elif lang_info["is_php"]:
            install_hints.append("composer require --dev phpstan/phpstan")
        elif lang_info["has_sql"]:
            install_hints.append("pip install sqlfluff")
        hint = "; ".join(install_hints) if install_hints else "see docs for install instructions"
        sa_status.append(f"enabled (no tools detected — install: {hint})")
    else:
        sa_status.append("disabled (compiled language or explicitly off)")
    print(f"  Static analysis: {' '.join(sa_status)}")
    print()
    if changed:
        print(f"Updated: {', '.join(changed)}")
    else:
        print("No changes needed — config is up to date.")


def _looks_like_python(workdir: str) -> bool:
    """Narrow Python fallback for marker-less repos (DF-002).

    True when the repo root has any top-level *.py file, or any top-level
    package dir (contains __init__.py) outside well-known non-package dirs.
    Keeps the root-package probe in _detect_test_command reachable for
    fresh repos that have no pyproject.toml/setup.py/setup.cfg/
    requirements.txt yet.
    """
    excluded = {"tests", ".venv", "node_modules", ".git", ".gitreins", "__pycache__"}
    try:
        entries = os.listdir(workdir)
    except OSError:
        return False
    for entry in entries:
        full = os.path.join(workdir, entry)
        if os.path.isfile(full) and entry.endswith(".py"):
            return True
        if entry in excluded:
            continue
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            return True
    return False


def _detect_language(workdir: str) -> dict:
    """Detect project language(s). Returns {name, type, is_go, is_python, is_ts, ...}.

    Uses independent if-blocks (not elif) so multi-language projects are detected
    correctly — a repo with both pyproject.toml and package.json is Python AND TypeScript.
    'type' is the primary language (first detected), 'name' aggregates all found.
    """
    info = {
        "name": "unknown",
        "type": "unknown",
        "is_go": False,
        "is_python": False,
        "is_ts": False,
        "is_ruby": False,
        "is_php": False,
        "is_rust": False,
        "has_sql": False,
    }
    langs_found: list[str] = []

    if os.path.isfile(os.path.join(workdir, "go.mod")):
        info["is_go"] = True
        langs_found.append("Go")
        if info["type"] == "unknown":
            info["type"] = "go"

    if (
        os.path.isfile(os.path.join(workdir, "pyproject.toml"))
        or os.path.isfile(os.path.join(workdir, "setup.py"))
        or os.path.isfile(os.path.join(workdir, "setup.cfg"))
        or os.path.isfile(os.path.join(workdir, "requirements.txt"))
        # Narrow fallback (DF-002): root-package layouts without any of the
        # marker files above are still Python — a top-level *.py file or a
        # top-level package dir (contains __init__.py) is enough.
        or _looks_like_python(workdir)
    ):
        info["is_python"] = True
        langs_found.append("Python")
        if info["type"] == "unknown":
            info["type"] = "python"

    if os.path.isfile(os.path.join(workdir, "package.json")) or os.path.isfile(
        os.path.join(workdir, "tsconfig.json")
    ):
        info["is_ts"] = True
        langs_found.append("TypeScript")
        if info["type"] == "unknown":
            info["type"] = "typescript"

    if os.path.isfile(os.path.join(workdir, "Gemfile")) or any(
        f.endswith(".gemspec")
        for f in os.listdir(workdir)
        if os.path.isfile(os.path.join(workdir, f))
    ):
        info["is_ruby"] = True
        langs_found.append("Ruby")
        if info["type"] == "unknown":
            info["type"] = "ruby"

    if os.path.isfile(os.path.join(workdir, "composer.json")):
        info["is_php"] = True
        langs_found.append("PHP")
        if info["type"] == "unknown":
            info["type"] = "php"

    if os.path.isfile(os.path.join(workdir, "Cargo.toml")):
        info["is_rust"] = True
        langs_found.append("Rust")
        if info["type"] == "unknown":
            info["type"] = "rust"

    # SQL detection: check for .sql files or migrations dir
    try:
        for _root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules", ".gitreins")]
            if any(f.endswith(".sql") for f in files):
                info["has_sql"] = True
                langs_found.append("SQL")
                break
            if "migrations" in dirs:
                info["has_sql"] = True
                langs_found.append("SQL")
                break
    except PermissionError:
        pass

    if langs_found:
        info["name"] = " + ".join(langs_found)
    return info


def _detect_static_analysis_tools(workdir: str, lang: dict) -> list[str]:
    """Return list of installed static analysis tools for all detected languages."""
    from engine.static_analysis import list_available_tools

    tools: list[str] = []
    if lang["is_python"]:
        tools.extend(list_available_tools("python"))
    if lang["is_ruby"]:
        tools.extend(list_available_tools("ruby"))
    if lang["is_php"]:
        tools.extend(list_available_tools("php"))
    if lang["has_sql"]:
        tools.extend(list_available_tools("sql"))
    if lang["is_ts"]:
        tools.extend(list_available_tools("typescript"))
    if lang["is_rust"]:
        tools.extend(list_available_tools("rust"))
    return tools


def _detect_root_package_layout(workdir: str) -> bool:
    """True when the repo root has one or more top-level package dirs.

    A top-level package dir is a directory directly under the repo root
    that contains __init__.py, excluding well-known non-package dirs
    (tests/, .venv, node_modules, .git, .gitreins, __pycache__).
    """
    excluded = {"tests", ".venv", "node_modules", ".git", ".gitreins", "__pycache__"}
    try:
        entries = os.listdir(workdir)
    except OSError:
        return False
    for entry in entries:
        if entry in excluded:
            continue
        full = os.path.join(workdir, entry)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            return True
    return False


def _has_pytest_pythonpath_config(workdir: str) -> bool:
    """True when the project already configures pytest's pythonpath.

    Checks the standard locations pytest reads: pyproject.toml
    ([tool.pytest.ini_options] pythonpath), pytest.ini / tox.ini
    ([pytest] pythonpath), and setup.cfg ([tool:pytest] pythonpath).
    """
    pyproject = os.path.join(workdir, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            try:
                import tomllib  # Python 3.11+
            except ImportError:  # pragma: no cover — Python 3.10 fallback
                import tomli as tomllib  # type: ignore[no-redef]

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            ini_options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
            if "pythonpath" in ini_options:
                return True
        except Exception:
            pass  # unparseable pyproject — treat as unconfigured

    import configparser

    for path, section in (
        (os.path.join(workdir, "pytest.ini"), "pytest"),
        (os.path.join(workdir, "tox.ini"), "pytest"),
        (os.path.join(workdir, "setup.cfg"), "tool:pytest"),
    ):
        if not os.path.isfile(path):
            continue
        try:
            parser = configparser.ConfigParser()
            parser.read(path)
            if parser.has_option(section, "pythonpath"):
                return True
        except Exception:
            pass
    return False


def _needs_python_module_pytest(workdir: str) -> bool:
    """True when bare `pytest` cannot import the project's root packages.

    pytest 9 importlib mode no longer adds the repo root to sys.path, so a
    root-package layout (top-level __init__.py dirs) with a tests/ dir only
    works under `python3 -m pytest` — unless the user already configured
    pytest's pythonpath (pyproject.toml / pytest.ini / setup.cfg).
    """
    if not os.path.isdir(os.path.join(workdir, "tests")):
        return False
    if not _detect_root_package_layout(workdir):
        return False
    if _has_pytest_pythonpath_config(workdir):
        return False
    return True


def _detect_test_command(workdir: str, lang: dict) -> str:
    """Detect the right test command for the project."""
    if lang["is_go"]:
        # Check for Makefile first
        makefile = os.path.join(workdir, "Makefile")
        if os.path.isfile(makefile):
            with open(makefile) as f:
                content = f.read()
            if "go test" in content:
                return "go test -short -count=1 ./..."
        return "go test -short -count=1 ./..."
    elif lang["is_python"]:
        if _needs_python_module_pytest(workdir):
            return "python3 -m pytest -x --tb=short"
        return "pytest -x --tb=short"
    elif lang["is_ts"]:
        # Check package.json for test script
        pkg = os.path.join(workdir, "package.json")
        if os.path.isfile(pkg):
            try:
                import json

                with open(pkg) as f:
                    data = json.load(f)
                if data.get("scripts", {}).get("test"):
                    return "npm test"
            except Exception:
                pass
        return "npx vitest run"
    return "pytest -x --tb=short"


def _detect_project_size(workdir: str, lang: dict) -> dict:
    """Estimate project size for evaluator cap recommendations."""
    packages = 0
    if lang["is_go"]:
        # Count Go packages
        go_files = set()
        for root, dirs, files in os.walk(workdir):
            # Skip vendor, .git, node_modules
            dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules", ".gitreins")]
            for f in files:
                if f.endswith(".go"):
                    go_files.add(os.path.relpath(os.path.dirname(os.path.join(root, f)), workdir))
        packages = len(go_files)
    elif lang["is_python"]:
        py_pkgs = set()
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [
                d
                for d in dirs
                if d not in (".git", ".venv", "node_modules", ".gitreins", "__pycache__")
            ]
            if "__init__.py" in files:
                py_pkgs.add(os.path.relpath(root, workdir))
        packages = len(py_pkgs) or 1

    # Cap recommendations
    if packages <= 3:
        max_iter = 15
    elif packages <= 10:
        max_iter = 25
    elif packages <= 25:
        max_iter = 50
    else:
        max_iter = 100

    return {
        "packages": packages,
        "max_iterations": max_iter,
        "test_mode": "full" if packages <= 5 else "diff",
    }


def _build_guards_section(lang: dict, test_cmd: str, static_tools: list[str] | None = None) -> dict:
    """Build guards section optimized for the detected language."""
    if static_tools is None:
        static_tools = []

    section: dict
    if lang["is_go"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "go": {"build": True, "lint": True, "tests": True},
        }
    elif lang["is_python"]:
        section = {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
            "static_analysis": True,  # ON: dynamic language, no compiler
        }
        if static_tools:
            section["static_analysis_tools"] = {"python": static_tools}
        return section
    elif lang["is_ts"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
            "static_analysis": False,  # OFF: tsc --noEmit covers this
        }
    elif lang["is_ruby"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "bundle exec rspec",
            "static_analysis": True,  # ON: dynamic language, no compiler
        }
    elif lang["is_php"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "vendor/bin/phpunit",
            "static_analysis": True,  # ON: dynamic language, no compiler
        }
    elif lang["is_rust"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "cargo test",
            "static_analysis": False,  # OFF: cargo check covers this
        }
    elif lang["has_sql"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "echo 'No SQL test runner configured'",
            "static_analysis": True,  # ON: no compiler for SQL
        }
    else:
        return {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
        }


def _fill_missing_guards(guards: dict, lang: dict, test_cmd: str) -> list[str]:
    """Fill in missing guard keys without overwriting existing values. Returns keys added."""
    added = []
    defaults = _build_guards_section(lang, test_cmd)

    for key, val in defaults.items():
        if key not in guards:
            guards[key] = val
            added.append(key)

    # For Go projects, ensure go: section exists
    if lang["is_go"] and "go" not in guards:
        guards["go"] = {"build": True, "lint": True, "tests": True}
        added.append("go")

    return added


def _build_evaluator_section(size: dict) -> dict:
    """Build evaluator section with size-appropriate caps."""
    return {
        "max_iterations": size["max_iterations"],
        "static_analysis_diagnostics": False,  # OFF by default, visible for opt-in
    }


def _glob_to_regex(path: str) -> str:
    """Convert a plain-glob path into a valid Go (RE2) regexp.

    gitleaks compiles every [allowlist] paths entry as a Go regexp, so bare
    globs like '*.log' panic with 'missing argument to repetition operator'
    (DF-001). Glob '*' becomes regex '.*' and literal regex metacharacters are
    escaped: '.venv/' -> '\\.venv/', '*.egg-info/' -> '.*\\.egg-info/',
    'apps/*/node_modules/' -> 'apps/.*/node_modules/'.
    """
    out: list[str] = []
    for ch in path:
        if ch == "*":
            out.append(".*")
        elif ch in ".+()[]{}^$|\\?":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _generate_gitleaks_config(workdir: str, lang: dict, target_path: str) -> None:
    """Generate a .gitleaks.toml with sensible path exclusions.

    Prevents gitleaks from scanning dependency directories (node_modules,
    .venv, vendor, etc.) which can cause 30-second timeouts on large projects.
    Never overwrites existing .gitleaks.toml — caller checks before invoking.
    Allowlist path entries are emitted as Go regexps (via _glob_to_regex) —
    gitleaks compiles each entry as a regexp, not a glob.
    """
    # Universal exclusions for all projects
    paths = [
        ".git/",
        ".gitreins/",
        ".gitreins/history/",
        "*.log",
    ]

    # Language/ecosystem-specific exclusions
    if lang["is_python"]:
        paths.extend(
            [
                ".venv/",
                "venv/",
                "__pycache__/",
                "*.egg-info/",
                "dist/",
                "build/",
                ".mypy_cache/",
                ".pytest_cache/",
                ".ruff_cache/",
            ]
        )
    elif lang["is_ts"]:
        paths.extend(
            [
                "node_modules/",
                ".pnpm-store/",
                "apps/*/node_modules/",
                "packages/*/node_modules/",
                "apps/*/dist/",
                "packages/*/dist/",
                "dist/",
                "build/",
                ".turbo/",
                ".next/",
                "coverage/",
            ]
        )
    elif lang["is_go"]:
        paths.extend(
            [
                "vendor/",
            ]
        )
    elif lang["is_ruby"]:
        paths.extend(
            [
                "vendor/bundle/",
                ".bundle/",
            ]
        )
    elif lang["is_rust"]:
        paths.extend(
            [
                "target/",
            ]
        )

    # Documentation/spec files (often contain example keys)
    paths.extend(
        [
            "specs/",
            "docs/",
            "*.spec.md",
            "*.md",
        ]
    )

    # Build TOML
    lines = [
        "# GitReins — gitleaks configuration",
        "# Auto-generated by 'gitreins init'. Edit to add project-specific exclusions.",
        "# See: https://github.com/gitleaks/gitleaks#configuration",
        "",
        "# Extend the default gitleaks ruleset so AWS/GitHub/GitLab/etc. patterns still run",
        "[extend]",
        "useDefault = true",
        "",
        "# Catch all sk- prefixed API keys (OpenAI, OpenRouter, DeepSeek, etc.)",
        "# 20+ chars — lower than gitleaks' default 32-char minimum",
        "[[rules]]",
        'id = "sk-api-key"',
        'description = "OpenAI/OpenRouter/DeepSeek API key (sk- prefix)"',
        "regex = '''(?i)sk-[a-zA-Z0-9_-]{20,}'''",
        "",
        "[allowlist]",
        'description = "Auto-generated by gitreins init — skip dependency dirs, docs, specs"',
        "",
        "paths = [",
    ]
    for p in paths:
        lines.append(f"  '''{_glob_to_regex(p)}''',")
    lines.append("]")
    lines.append("")

    toml_content = "\n".join(lines) + "\n"

    # Use safe overwrite (creates .bak)
    _safe_overwrite(target_path, lambda f: f.write(toml_content))


def cmd_task_create(args):
    from engine.task_manager import TaskManager

    tm = TaskManager(get_workdir())
    criteria = args.criteria if args.criteria else []
    depends_on = args.depends_on if hasattr(args, "depends_on") and args.depends_on else []
    task = tm.create(args.id, args.title, criteria, depends_on=depends_on)
    print(f"Created task: {task.id} — {task.title}")
    if task.depends_on:
        print(f"  Depends on: {', '.join(task.depends_on)}")
    for i, c in enumerate(task.criteria, 1):
        print(f"  {i}. {c}")


def cmd_task_start(args):
    from engine.task_manager import TaskManager

    tm = TaskManager(get_workdir())
    task = tm.start(args.id)
    print(f"Started: {task.id} → {task.status}")


def cmd_task_complete(args):
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

    workdir = get_workdir()
    tm = TaskManager(workdir)

    force = getattr(args, "force", False)

    # Check dependencies (unless forced)
    if not force:
        blocked = tm.check_dependencies(args.id)
        if blocked:
            print(
                f"Cannot complete '{args.id}' — depends on incomplete tasks: {', '.join(blocked)}"
            )
            print("Complete those tasks first, or use --force to skip dependency checks.")
            sys.exit(1)

    task = tm.complete(args.id, force=force)
    print(f"Completed: {task.id} → {task.status}")

    print("\nEvaluating...")
    llm = LLMClient()
    judge = Judge(llm, workdir)
    result = judge.evaluate_task(task)
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)


def cmd_task_list(args):
    from engine.task_manager import TaskManager

    tm = TaskManager(get_workdir())
    tasks = tm.list_tasks(args.status)
    if not tasks:
        print("No tasks found.")
        return
    for t in tasks:
        status_icon = {"pending": "○", "in_progress": "◐", "complete": "●"}.get(t.status, "?")
        print(f"  {status_icon} {t.id:<20} {t.title}")


def cmd_task_delete(args):
    from engine.task_manager import TaskManager

    tm = TaskManager(get_workdir())
    tm.delete(args.id)
    print(f"Deleted: {args.id}")


def _persist_result(workdir: str, task, result) -> None:
    """Save evaluation verdict to history. Non-fatal — logs on failure."""
    try:
        from engine.persist import VerdictPersister

        persister = VerdictPersister(workdir)
        if not persister.enabled:
            return

        # Build verdict data from result
        verdict_data = {
            "task_id": task.id,
            "task_title": task.title,
            "task_criteria": task.criteria,
            "passed": result.passed,
        }

        # Extract items from verdict or pipeline result
        if result.verdict and hasattr(result.verdict, "items"):
            verdict_data["items"] = [
                {"criterion": item.criterion, "status": item.status, "detail": item.detail}
                for item in result.verdict.items
            ]
        else:
            verdict_data["items"] = []

        # Pipeline stages
        if result.pipeline_result:
            verdict_data["stages"] = result.pipeline_result.get("stages", {})

        # Summary text
        verdict_data["summary"] = result.summary

        commit_hash = persister.persist(task.id, verdict_data)
        if commit_hash == "disabled":
            pass  # user opted out
        elif commit_hash == "dry-run":
            print("  ⚠ Verdict saved to disk but not committed (git unavailable)", file=sys.stderr)
        else:
            print(f"  📋 Verdict saved: {commit_hash}")

    except Exception:
        print("  ⚠ Failed to persist verdict (non-fatal)", file=sys.stderr)


def cmd_report(args):
    """Show recent verdict history."""
    from engine.persist import build_report

    workdir = get_workdir()
    n = args.n if hasattr(args, "n") else 10

    # Interactive TUI mode
    if args.interactive:
        _cmd_report_tui(workdir, n)
        return

    report = build_report(workdir, n=n)
    print(report)


def _cmd_report_tui(workdir: str, n: int = 20):
    """Interactive TUI for verdict browsing (requires textual)."""
    from engine.persist import build_report, VerdictPersister

    try:
        from importlib.util import find_spec

        if not find_spec("textual"):
            raise ImportError
    except ImportError:
        print("Interactive mode requires 'textual'. Install with: pip install textual")
        print("Falling back to text mode...")
        print()
        print(build_report(workdir, n=n))
        return

    persister = VerdictPersister(workdir)

    if not persister.enabled:
        print("History is disabled (history.enabled = false in config).")
        return

    entries = persister.list_verdicts(n=n)
    if not entries:
        print("No verdict history found.")
        return

    # Textual TUI
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Header, Footer, Static

    verdict_lines = []
    for entry in entries:
        icon = "✓" if entry.get("passed") else "✗"
        task_id = entry.get("task_id", "?")
        date = entry.get("_date", "?")
        title = entry.get("task_title", task_id)
        items = entry.get("items", [])
        criteria = ""
        if items:
            parts = []
            for item in items:
                if isinstance(item, dict):
                    s = "✓" if item.get("status") == "PASS" else "✗"
                else:
                    s = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                parts.append(s)
            criteria = f" [{''.join(parts)}]"
        verdict_lines.append(f"{icon} {task_id:<24} {date}  {criteria}")
        if title and title != task_id:
            verdict_lines.append(f"   {title}")

    class VerdictApp(App):
        CSS = """
        Screen { background: #0d1117; }
        Static { color: #c9d1d9; }
        Static.green { color: #3fb950; }
        Static.red { color: #f85149; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            yield VerticalScroll(Static("\n".join(verdict_lines), markup=False))
            yield Footer()

    app = VerdictApp()
    app.run()


def cmd_guard_run(args):
    _check_for_updates()
    from engine.guard_manager import GuardManager

    workdir = get_workdir()
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    result = gm.run_all(force_dead_code=getattr(args, "dead_code", False))

    # Build mode note
    mode = gm.test_mode
    extra = result.extra
    mode_note = f"  (test mode: {mode}"
    if extra.get("test_targets"):
        mode_note += f", {extra['test_targets']} test file(s)"
    elif extra.get("test_targets") is None and mode == "diff":
        mode_note += ", full suite — safety trigger"
    mode_note += ")"

    print(f"Tier 1 Guards: {'PASS' if result.passed else 'FAIL'}{mode_note}")
    print(result.summary)

    if result.warnings:
        print()
        for warning in result.warnings:
            print(f"\033[33m⚠ {warning}\033[0m", file=sys.stderr)

    if not result.passed:
        print()
        print("Fix the issues above and re-run: gitreins guard")
        sys.exit(1)


def cmd_judge(args):
    """Evaluate a task — sync (default), or dispatch a background job.

    ``--async`` detaches a worker process and returns a job id; the job
    record lives in the shared disk store, so it survives this CLI
    exiting and can be polled with ``gitreins judge --status <job_id>``
    (or the MCP ``judge.status`` tool). ``--run-job`` is the internal
    worker mode executed by the detached child.
    """
    if getattr(args, "status", False):
        _cmd_judge_status(args.id)
        return
    if getattr(args, "run_job", False):
        _cmd_judge_worker(args.id)
        return
    if getattr(args, "async_dispatch", False):
        _cmd_judge_async(args.id)
        return

    _check_for_updates()
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.get(args.id)
    if not task:
        print(f"Task not found: {args.id}")
        sys.exit(1)

    if getattr(args, "skip_tier2", False):
        print("Tier 2 skipped (--skip-tier2 flag)")

    llm = LLMClient()
    config = load_config(workdir)
    judge = Judge(llm, workdir, guard_config=config)
    result = judge.evaluate_task(task, skip_tier2=getattr(args, "skip_tier2", False))
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)


def _cmd_judge_async(task_id: str) -> None:
    """Dispatch the evaluation as a detached background job (DF-006).

    The worker is a detached child process that survives this CLI
    exiting; the job record lives in the shared disk store, so it can be
    polled with ``gitreins judge --status <job_id>`` and from the MCP
    server's ``judge.status`` tool.
    """
    import subprocess

    from engine.job_store import job_log_path, make_job, save_job
    from engine.task_manager import TaskManager

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.get(task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    job = make_job(task_id, workdir)
    save_job(job)

    log_path = job_log_path(job["id"])
    try:
        logf = open(log_path, "ab")
    except OSError as e:
        print(f"Could not open job log {log_path}: {e}")
        sys.exit(1)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gitreins.cli", "judge", "--run-job", job["id"]],
            cwd=workdir,
            start_new_session=True,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        logf.close()
        job["status"] = "error"
        job["error"] = f"worker spawn failed: {e}"
        job["finished_at"] = time.time()
        save_job(job)
        print(f"Could not start background job: {e}")
        sys.exit(1)

    job["pid"] = proc.pid
    save_job(job)
    print(f"Async job dispatched: {job['id']}")
    print(f"  task:    {task_id}")
    print(f"  workdir: {workdir}")
    print(f"  log:     {log_path}")
    print(f"  poll:    gitreins judge --status {job['id']}")


def _cmd_judge_worker(job_id: str) -> None:
    """Internal worker mode: run the evaluation for a disk job record.

    Executed by the detached child spawned from ``judge --async`` (and
    directly by tests). Writes the result back to the job record and
    persists the verdict exactly like a synchronous run.
    """
    from engine.job_store import load_job, save_job
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge, judge_result_to_dict

    job = load_job(job_id)
    if job is None:
        print(f"Job not found: {job_id}")
        sys.exit(1)
    if job["status"] != "running":
        print(f"Job {job_id} already {job['status']} — nothing to do")
        sys.exit(0)

    job["pid"] = os.getpid()
    save_job(job)

    wd = job["workdir"]
    tm = TaskManager(wd)
    task = tm.get(job["task_id"])
    if not task:
        job["status"] = "error"
        job["error"] = f"task {job['task_id']} not found in {wd}"
        job["finished_at"] = time.time()
        save_job(job)
        print(job["error"])
        sys.exit(1)

    try:
        llm = LLMClient()
        config = load_config(wd)
        judge = Judge(llm, wd, guard_config=config)
        result = judge.evaluate_task(task)
        job["result"] = judge_result_to_dict(job["task_id"], wd, result)
        job["status"] = "complete"
        job["finished_at"] = time.time()
        save_job(job)
        # Same verdict persistence as a synchronous run
        _persist_result(wd, task, result)
        print(result.summary)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["finished_at"] = time.time()
        save_job(job)
        print(f"Evaluation failed: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_judge_status(job_id: str) -> None:
    """Print the status/result of a background job.

    Exit codes: 0 complete, 2 still running, 1 error/not found.
    """
    from engine.job_store import job_log_path, load_job

    job = load_job(job_id)
    if job is None:
        print(f"Job not found: {job_id}")
        sys.exit(1)

    status = job["status"]
    print(f"Job:      {job['id']}")
    print(f"Status:   {status}")
    print(f"Task:     {job['task_id']}")
    print(f"Workdir:  {job['workdir']}")
    if job.get("pid"):
        print(f"Pid:      {job['pid']}")
    if job.get("started_at"):
        print("Started:  " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["started_at"])))
    if job.get("finished_at"):
        print("Finished: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["finished_at"])))
    print()

    if status == "running":
        print(f"Job is still running (pid {job.get('pid') or 'unknown'}). Poll again later.")
        sys.exit(2)
    if status == "error":
        print(f"Error: {job.get('error')}")
        log = job_log_path(job_id)
        if os.path.exists(log):
            print(f"Log: {log}")
        sys.exit(1)

    result = job.get("result") or {}
    print(f"Result:   {'PASS ✓' if result.get('passed') else 'FAIL ✗'}")
    if result.get("tier1_passed") is not None:
        print(f"Tier 1:   {'PASS' if result['tier1_passed'] else 'FAIL'}")
    if result.get("verdict"):
        print(f"Verdict:  {result['verdict']}")
        for item in result.get("items", []):
            mark = "✓" if item.get("status") == "PASS" else "✗"
            print(f"  {mark} {item.get('criterion')}: {item.get('detail')}")
    if result.get("summary"):
        print()
        print(result["summary"])
    sys.exit(0)


def cmd_commit(args):
    import subprocess
    from engine.guard_manager import GuardManager

    workdir = get_workdir()
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    tier1 = gm.run_all()

    if not tier1.passed:
        print("Tier 1 FAILED — cannot commit:")
        print(tier1.summary)
        sys.exit(1)

    if getattr(args, "skip_tier2", False):
        print("Tier 1 PASSED — Tier 2 skipped (--skip-tier2 flag) — committing...")
    else:
        print("Tier 1 PASSED — committing...")
    result = subprocess.run(
        ["git", "commit", "-m", args.message],
        capture_output=True,
        text=True,
        cwd=workdir,
    )
    print(result.stdout + result.stderr)


def cmd_commit_audit(args):
    """Validate a commit message against the staged diff using LLM.

    Reads the message from ``args.message`` or falls back to the git
    commit message file.  Runs the commit_audit pipeline stage and
    exits non-zero if configured to block on bad messages.
    """
    import os
    from engine.pipeline import Pipeline, load_pipeline_config
    from engine.llm import LLMClient

    workdir = get_workdir()
    message = args.message or ""

    # Fallback: read from git COMMIT_EDITMSG
    if not message:
        msg_path = os.path.join(workdir, ".git", "COMMIT_EDITMSG")
        if os.path.exists(msg_path):
            with open(msg_path, "r") as f:
                raw = f.read().strip()
            message = "\n".join(
                line for line in raw.split("\n") if not line.startswith("#")
            ).strip()

    if not message:
        print("No commit message to audit.")
        sys.exit(0)

    # Check for gitreins.skip-tier2 trailer before running audit
    from engine.commit_audit import has_skip_tier2_trailer

    if has_skip_tier2_trailer(message):
        print("Commit audit skipped (gitreins.skip-tier2 trailer)")
        sys.exit(0)

    # Load config and run commit_audit stage
    config = load_pipeline_config(workdir)
    llm = LLMClient()
    pipeline = Pipeline(config, workdir, llm=llm)

    task = {
        "id": "_commit_msg",
        "title": "Commit message audit",
        "criteria": [],
        "commit_message": message,
    }

    result = pipeline.run(task, trigger="commit-msg")

    # Check if audit stage blocked
    audit_stage = result.get("stages", {}).get("commit_audit", {})
    if audit_stage and not audit_stage.get("passed", True):
        print("\n" + audit_stage.get("summary", "Commit message rejected."))
        sys.exit(1)

    if audit_stage:
        print(audit_stage.get("summary", "✓ Commit message OK."))
    sys.exit(0)


def cmd_security_scan(args):
    """Run the Antares CVE localization scanner (GR-117f).

    Default target: staged Python files (``git diff --cached``). With
    ``--directory DIR`` the scanner recurses into DIR instead. Output
    is human-readable by default; ``--output json`` emits a
    machine-readable report. ``--force-ml`` requires the optional
    ``huggingface_hub``/``transformers`` stack — if either is missing
    the command exits non-zero instead of falling back to the
    keyword heuristic.

    Exit codes:
        0  — clean (no findings)
        1  — one or more findings produced
        2  — forced ML mode but dependencies missing
    """
    import json as _json

    workdir = get_workdir()
    config = load_config(workdir)
    security_cfg = (config or {}).get("defaults", {}).get("security_scan", {}) or {}

    force_ml = bool(getattr(args, "force_ml", False))
    output_fmt = getattr(args, "output", "text") or "text"
    directory = getattr(args, "directory", None)

    try:
        from engine.antares import AntaresScanner
    except ImportError as exc:
        print(f"Antares scanner unavailable: {exc}", file=sys.stderr)
        if force_ml:
            sys.exit(2)
        sys.exit(1)

    model_map = {
        "antares-1b": "fdtn-ai/antares-1b",
        "antares-350m": "fdtn-ai/antares-350m",
    }
    model_id = model_map.get(security_cfg.get("model", "antares-1b"), "fdtn-ai/antares-1b")

    if force_ml:
        # When ML is required, both download and inference deps must
        # be present. We check huggingface_hub (for the snapshot) and
        # transformers (for inference). The scanner itself enforces
        # this contract; here we just pre-flight a friendlier error.
        for mod_name, install_hint in (
            ("huggingface_hub", "pip install huggingface_hub"),
            ("transformers", "pip install transformers"),
        ):
            try:
                __import__(mod_name)
            except ImportError as exc:
                print(
                    f"Antares ML mode requires {mod_name}: {exc}\nInstall with: {install_hint}",
                    file=sys.stderr,
                )
                sys.exit(2)

    scanner = AntaresScanner(workdir, model_id=model_id, use_ml=force_ml)

    try:
        if directory:
            findings = scanner.scan_directory(directory)
        else:
            findings = scanner.scan_staged_files()
    except ImportError as exc:
        # Raised by the scanner when force_ml=True and the ML stack
        # couldn't be reached. Already covered above, but stay safe
        # in case the failure happens after pre-flight.
        print(f"Antares ML dependencies missing: {exc}", file=sys.stderr)
        if force_ml:
            sys.exit(2)
        findings = []
    except Exception as exc:  # noqa: BLE001
        logger = logging.getLogger("gitreins")
        logger.warning("Antares scan failed: %s", exc)
        print(f"Antares scan failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if output_fmt == "json":
        payload = [
            f.to_dict()
            if hasattr(f, "to_dict")
            else {
                "file": getattr(f, "file", ""),
                "line": getattr(f, "line", 0),
                "cve_id": getattr(f, "cve_id", ""),
                "confidence": getattr(f, "confidence", 0.0),
                "description": getattr(f, "description", ""),
            }
            for f in findings
        ]
        print(_json.dumps(payload, indent=2))
    else:
        if not findings:
            target = directory or "staged files"
            print(f"Antares: clean — no findings in {target}")
        else:
            target = directory or "staged files"
            print(f"Antares: {len(findings)} potential finding(s) in {target}:")
            for f in findings:
                print(f"  • {f.file}:{f.line} [{f.cve_id} conf={f.confidence:.2f}] {f.description}")

    sys.exit(1 if findings else 0)


def cmd_setup_tools(args):
    """Show available static analysis tools and install instructions for missing ones."""
    from engine.static_analysis import find_tool, _TOOL_INSTALL_GUIDE

    workdir = get_workdir()
    lang = _detect_language(workdir)

    lang_tools_map = {
        "python": ["mypy", "pyright"],
        "ruby": ["sorbet"],
        "sql": ["sqlfluff"],
        "php": ["phpstan"],
    }
    tools = lang_tools_map.get(lang["type"], [])

    if not tools:
        print(f"No static analysis tools are tracked for {lang['name']}.")
        return

    print(f"Static Analysis Tools for {lang['name']}:")
    found = 0
    missing = 0
    for tool in tools:
        path = find_tool(tool)
        if path:
            found += 1
            display = path.split("/")[-1] if "/" in path else path
            print(f"  {tool:<12} ✓ found  ({display})")
        else:
            missing += 1
            install = _TOOL_INSTALL_GUIDE.get(
                tool,
                f"Install {tool} from your package manager",
            )
            print(f"  {tool:<12} ✗ not installed — install: {install}")

    print()
    print(f"{found} tools available, {missing} missing.")


def cmd_mcp_server(args):
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gitreins_mcp.server import GitReinsMCPServer

    server = GitReinsMCPServer(get_workdir())
    server.run_stdio()


# ── review (R2.16) — the §17 review DAG from the CLI ─────────────────────────


def _default_review_pipeline() -> dict:
    """The DESIGN_v2.md §17 review DAG, as pipeline config stages.

    Lane B (defect review): change_analysis → parallel analyzers → scout →
    evidence_retrieval → parallel reviewers → merge (dedup) → verify →
    rank. Lane A (criteria evaluation) runs when the task has criteria;
    publish_review is a no-op locally and posts comments for --pr.
    """
    return {
        "review_pipeline": [
            {"id": "change_analysis", "type": "change_analysis"},
            {
                "id": "static_evidence",
                "parallel": True,
                "steps": [
                    {"id": "lsp", "type": "analyzer", "analyzer": "lsp"},
                    {"id": "semgrep", "type": "analyzer", "analyzer": "semgrep"},
                    {"id": "typecheck", "type": "analyzer", "analyzer": "typecheck"},
                    {"id": "secrets", "type": "analyzer", "analyzer": "secrets"},
                    {"id": "dependencies", "type": "analyzer", "analyzer": "dependencies"},
                ],
            },
            {"id": "scout", "type": "agent", "role": "scout"},
            {"id": "retrieval", "type": "evidence_retrieval"},
            {
                "id": "reviewers",
                "parallel": True,
                "steps": [
                    {"id": "runtime", "type": "agent", "role": "runtime_reviewer"},
                    {"id": "contracts", "type": "agent", "role": "contract_reviewer"},
                    {"id": "security", "type": "agent", "role": "security_reviewer"},
                ],
            },
            {"id": "candidate_merge", "type": "merge_findings"},
            {
                "id": "verify",
                "type": "verify_findings",
                "parallel": True,
                "role": "verifier",
            },
            {"id": "rank", "type": "rank_findings"},
            {
                "id": "requirements",
                "type": "criteria_eval",
                "condition": "task.has_criteria",
            },
            {"id": "publish", "type": "publish_review"},
        ]
    }


def _review_config(config: dict) -> dict:
    """Pipeline config for the review DAG: the config's review_pipeline, or
    the §17 default when the config has none."""
    if config.get("review_pipeline"):
        return config
    merged = dict(config)
    merged["review_pipeline"] = _default_review_pipeline()["review_pipeline"]
    return merged


def _make_review_router(config):
    """Build the ModelRouter for the review DAG (module-level for test injection)."""
    from engine.router import ModelRouter

    return ModelRouter(config)


def _default_github_repo(workdir: str) -> tuple[str, str]:
    """Best-effort owner/repo for ``--pr``: gh default repo, then origin remote.

    Raises ValueError when neither yields ``owner/repo``.
    """
    import json as _json

    try:
        from engine.github.checkout import _run_gh

        out = _run_gh(["repo", "view", "--json", "nameWithOwner"]).strip()
        name = _json.loads(out).get("nameWithOwner", "")
        if "/" in name:
            return tuple(name.split("/", 1))  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — fall through to git remote
        pass
    try:
        from engine.github.checkout import _run_git

        url = _run_git(workdir, ["remote", "get-url", "origin"]).strip()
        if url.endswith(".git"):
            url = url[:-4]
        for sep in ("github.com:", "github.com/"):
            if sep in url:
                slug = url.split(sep, 1)[1].rstrip("/")
                owner, _, repo = slug.partition("/")
                if owner and repo:
                    return owner, repo
    except Exception:  # noqa: BLE001 — fall through to error
        pass
    raise ValueError(
        "cannot determine GitHub owner/repo for --pr — pass --owner/--repo, "
        "or run inside a repo with a GitHub origin remote"
    )


def _make_change_source(args, workdir: str):
    """Build the ChangeSource for the review command.

    ``--pr <n>`` → PullRequestChangeSource (owner/repo from --owner/--repo or
    gh/git fallbacks); ``--base`` + ``--head`` → CommitRangeChangeSource;
    otherwise the local working tree (WorkingTreeChangeSource).
    """
    from engine.github.checkout import (
        CommitRangeChangeSource,
        PullRequestChangeSource,
        WorkingTreeChangeSource,
    )

    pr_number = getattr(args, "pr", None)
    if pr_number:
        owner = getattr(args, "owner", None)
        repo = getattr(args, "repo", None)
        if not owner or not repo:
            owner, repo = _default_github_repo(workdir)
        return PullRequestChangeSource(owner, repo, int(pr_number))
    base = getattr(args, "base", None)
    head = getattr(args, "head", None)
    if base and head:
        return CommitRangeChangeSource(workdir, base, head)
    return WorkingTreeChangeSource(workdir)


def _format_review_report(task: dict, result: dict) -> str:
    """Combine Lane B (defect findings) + Lane A (criteria verdicts) into one
    human-readable report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"GitReins review — {task.get('id', 'review')}")
    changed = task.get("changed_files") or []
    preview = ", ".join(changed[:8]) + (" …" if len(changed) > 8 else "")
    lines.append(f"Change: {len(changed)} file(s): {preview}")
    base = task.get("base_sha", "")
    head = task.get("head_sha", "")
    if base and head:
        lines.append(f"Range: {base[:8]}...{head[:8]}")
    lines.append("=" * 72)

    stages = result.get("stages", {})
    rank_stage = stages.get("rank", {})
    ranked = rank_stage.get("data", {}).get("ranked") if rank_stage else None
    findings = ranked if ranked is not None else list(task.get("findings") or [])

    lines.append("")
    lines.append(f"Lane B — defect findings ({len(findings)})")
    if not findings:
        lines.append("  (no findings — change looks clean)")
    else:
        for f in findings:
            loc = f.get("file") or "<repo>"
            if isinstance(f.get("line"), int):
                loc = f"{loc}:{f['line']}"
            sev = f.get("severity") or f.get("impact") or "?"
            conf = f.get("verifier_confidence")
            conf_s = f" conf={conf:.2f}" if isinstance(conf, (int, float)) else ""
            verdict = f.get("verdict")
            v_s = f" [{verdict}]" if verdict else ""
            claim = str(f.get("claim") or "")
            lines.append(f"  [{sev}]{conf_s}{v_s} {loc}: {claim}")

    criteria = task.get("criteria") or []
    verdict_items = task.get("criteria_verdicts") or []
    lines.append("")
    lines.append(f"Lane A — criteria evaluation ({len(criteria)} criterion/criteria)")
    if not criteria:
        lines.append("  (no criteria provided — Lane A skipped)")
    elif not verdict_items:
        lines.append("  (criteria evaluation did not run)")
    else:
        for item in verdict_items:
            mark = "✓" if item.get("status") == "PASS" else "✗"
            detail = str(item.get("detail", ""))[:200]
            lines.append(f"  {mark} {item.get('criterion', '?')}: {detail}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def cmd_review(args):
    """Run the §17 review DAG against a change (R2.16).

    Change sources: local working tree (default), commit range (--base/--head),
    or GitHub pull request (--pr <n>). The DAG runs Lane B defect review
    (scout → retrieval → parallel reviewers → dedup → adversarial verify →
    rank) plus Lane A criteria evaluation when ``--criteria`` is given, and
    prints one combined report. Exit code 0 when the DAG ran; 1 on machinery
    errors (findings and failed criteria are reported, not exits).
    """
    workdir = get_workdir()
    config = load_config(workdir)

    try:
        source = _make_change_source(args, workdir)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        diff = source.diff()
        changed_files = source.changed_files()
        base_sha = source.base_sha()
        head_sha = source.head_sha()
    except Exception as exc:  # noqa: BLE001 — change-source boundary
        print(f"Error: could not read change: {exc}", file=sys.stderr)
        sys.exit(1)

    criteria = list(getattr(args, "criteria", None) or [])
    task = {
        "id": "review",
        "title": "Change review",
        "criteria": criteria,
        "changed_files": changed_files,
        "diff": diff.text,
        "diff_context": diff.text,
        "change_source": source,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    pr_number = getattr(args, "pr", None)
    if pr_number:
        task["pr_number"] = int(pr_number)
        task["owner"] = getattr(source, "owner", "")
        task["repo"] = getattr(source, "repo", "")

    if not changed_files:
        print("No changed files to review — the working tree is clean.")
        sys.exit(0)

    from engine.pipeline import Pipeline

    router = _make_review_router(config)
    pipeline = Pipeline(_review_config(config), workdir, router=router)
    result = pipeline.run_review(task)

    print(_format_review_report(task, result))

    failed = [sid for sid, s in result.get("stages", {}).items() if not s.get("passed", True)]
    if failed:
        print(
            f"\nReview pipeline errors in stage(s): {', '.join(failed)}",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="GitReins — Git-Native Agent Co-Harness")
    parser.add_argument("--version", action="version", version=f"gitreins {__version__}")
    sub = parser.add_subparsers(dest="command")

    # install
    sub.add_parser("install", help="Install GitReins hooks and config in the current repo")

    # init
    init_p = sub.add_parser("init", help="Smart init — detect language, size, optimal config")
    init_p.add_argument("--reset", action="store_true", help="Reset config to smart defaults")

    # task
    task_p = sub.add_parser("task", help="Task management")
    task_sub = task_p.add_subparsers(dest="subcommand")

    create_p = task_sub.add_parser("create", help="Create a task")
    create_p.add_argument("id")
    create_p.add_argument("title")
    create_p.add_argument("criteria", nargs="*")
    create_p.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="Task ID that must complete first (repeatable)",
    )

    start_p = task_sub.add_parser("start", help="Start a task")
    start_p.add_argument("id")

    complete_p = task_sub.add_parser("complete", help="Complete and evaluate a task")
    complete_p.add_argument("id")
    complete_p.add_argument("--force", "-f", action="store_true", help="Skip dependency checks")

    list_p = task_sub.add_parser("list", help="List tasks")
    list_p.add_argument("--status", choices=["pending", "in_progress", "complete"])

    delete_p = task_sub.add_parser("delete", help="Delete a task")
    delete_p.add_argument("id")

    # guard
    guard_p = sub.add_parser("guard", help="Run Tier 1 guards")
    guard_p.add_argument(
        "--dead-code",
        action="store_true",
        help="Enable Python dead-code detection (overrides config)",
    )

    # judge
    judge_p = sub.add_parser("judge", help="Evaluate a task")
    judge_p.add_argument("id")
    judge_p.add_argument(
        "--skip-tier2", action="store_true", help="Skip Tier 2 LLM evaluation; Tier 1 guards only"
    )
    judge_p.add_argument(
        "--async",
        dest="async_dispatch",
        action="store_true",
        help="Dispatch the evaluation as a detached background job (survives this CLI exiting); poll with `gitreins judge --status <job_id>`",
    )
    judge_p.add_argument(
        "--status",
        action="store_true",
        help="Show the status/result of a background job (id = job id, not task id)",
    )
    judge_p.add_argument(
        "--run-job",
        dest="run_job",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # commit
    commit_p = sub.add_parser("commit", help="Commit with guard checks")
    commit_p.add_argument("message")
    commit_p.add_argument(
        "--skip-tier2", action="store_true", help="Skip any Tier 2 processing; Tier 1 guards only"
    )

    # commit-audit
    audit_p = sub.add_parser(
        "commit-audit", help="Validate commit message against staged diff (commit-msg hook)"
    )
    audit_p.add_argument(
        "message", nargs="?", help="Commit message (reads from COMMIT_EDITMSG if omitted)"
    )

    # mcp-server
    sub.add_parser(
        "mcp-server",
        help="Run MCP stdio server",
        description="Run the MCP stdio server (no flags required).",
        epilog=(
            "Configuration is via environment variables:\n"
            "  GITREINS_LLM_API_KEY   API key for the LLM provider\n"
            "  GITREINS_LLM_BASE_URL  Base URL of the LLM API (default: https://api.openai.com/v1)\n"
            "  GITREINS_LLM_MODEL     Model name (default varies by provider)\n"
            "  GITREINS_LLM_REASONING Reasoning mode: 'enabled' or 'disabled' (default: disabled)\n"
            "\n"
            "The MCP tool mcp_gitreins_configure can hot-reload the LLM config at runtime.\n"
            "\n"
            "Example:\n"
            "  export GITREINS_LLM_API_KEY=sk-...\n"
        ),
    )

    # security-scan
    security_p = sub.add_parser(
        "security-scan",
        help="Run the Antares CVE localization scanner (opt-in)",
    )
    security_p.add_argument(
        "--directory",
        "-d",
        help="Scan a directory recursively instead of staged files",
    )
    security_p.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    security_p.add_argument(
        "--force-ml",
        action="store_true",
        help="Require ML inference (fails if huggingface_hub/transformers missing)",
    )

    # setup-tools
    sub.add_parser(
        "setup-tools", help="Show available static analysis tools and install instructions"
    )

    # report
    report_p = sub.add_parser("report", help="Show verdict history")
    report_p.add_argument("-n", type=int, default=10, help="Number of recent verdicts to show")
    report_p.add_argument("--interactive", "-i", action="store_true", help="Interactive TUI mode")

    # review — the §17 review DAG from the CLI (R2.16)
    review_p = sub.add_parser(
        "review",
        help="Run the §17 review DAG against the change (local tree, commit range, or PR)",
    )
    review_p.add_argument(
        "--pr", type=int, help="Review GitHub pull request <n> (PullRequestChangeSource)"
    )
    review_p.add_argument("--owner", help="GitHub owner for --pr (default: gh default repo, then origin remote)")
    review_p.add_argument("--repo", help="GitHub repo for --pr")
    review_p.add_argument("--base", help="Base ref for a commit-range review (with --head)")
    review_p.add_argument("--head", help="Head ref for a commit-range review (with --base)")
    review_p.add_argument(
        "--criteria",
        action="append",
        default=[],
        help="Task criteria for Lane A evaluation (repeatable)",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.WARNING,  # Only show warnings+ by default
        format="%(name)s: %(levelname)s: %(message)s",
    )

    if args.command == "install":
        cmd_install(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "task":
        if args.subcommand == "create":
            cmd_task_create(args)
        elif args.subcommand == "start":
            cmd_task_start(args)
        elif args.subcommand == "complete":
            cmd_task_complete(args)
        elif args.subcommand == "list":
            cmd_task_list(args)
        elif args.subcommand == "delete":
            cmd_task_delete(args)
        else:
            parser.print_help()
    elif args.command == "guard":
        cmd_guard_run(args)
    elif args.command == "judge":
        cmd_judge(args)
    elif args.command == "commit":
        cmd_commit(args)
    elif args.command == "commit-audit":
        cmd_commit_audit(args)
    elif args.command == "mcp-server":
        cmd_mcp_server(args)
    elif args.command == "security-scan":
        cmd_security_scan(args)
    elif args.command == "setup-tools":
        cmd_setup_tools(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "review":
        cmd_review(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
