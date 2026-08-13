"""Dedicated tests for verdict persistence and history reporting."""

import json
import os
import shutil
import subprocess
from unittest.mock import patch

from engine.persist import (
    DEFAULT_HISTORY_CONFIG,
    VerdictPersister,
    _pct,
    build_report,
)


# ── _pct ─────────────────────────────────────────────────────


def test_pct_formats_ratio():
    assert _pct(0, 10) == "0%"
    assert _pct(5, 10) == "50%"
    assert _pct(10, 10) == "100%"
    assert _pct(0, 0) == "0%"


# ── DEFAULT_HISTORY_CONFIG ───────────────────────────────────


def test_default_history_config_has_expected_keys():
    assert DEFAULT_HISTORY_CONFIG["enabled"] is True
    assert DEFAULT_HISTORY_CONFIG["storage"] == "git"
    assert DEFAULT_HISTORY_CONFIG["max_verdicts"] == 1000
    assert ".gitreins/history" in DEFAULT_HISTORY_CONFIG["path"]


# ── VerdictPersister init ────────────────────────────────────


def test_persister_uses_absolute_workdir(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert os.path.isabs(p.workdir)


def test_persister_enabled_defaults_true(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.enabled is True


def test_persister_history_dir_is_under_workdir_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.history_dir.startswith(str(tmp_path))


def test_persister_storage_mode_is_git_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.storage_mode == "git"


# ── persist (non-git path) ───────────────────────────────────


def test_persist_returns_disabled_when_history_disabled(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["enabled"] = False
    assert p.persist("task-1", {}) == "disabled"


def test_persist_creates_verdict_json_and_summary_md(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"  # skip git
    p.config["max_verdicts"] = 0  # no pruning

    result = p.persist("task-1", {"passed": True, "task_title": "Test Task"})
    assert result == "dry-run"

    # Find the verdict directory
    history = p.history_dir
    assert os.path.isdir(history)
    date_dirs = os.listdir(history)
    assert len(date_dirs) == 1
    hash_dirs = os.listdir(os.path.join(history, date_dirs[0]))
    assert len(hash_dirs) == 1

    entry = os.path.join(history, date_dirs[0], hash_dirs[0])
    assert os.path.isfile(os.path.join(entry, "verdict.json"))
    assert os.path.isfile(os.path.join(entry, "summary.md"))

    # Verdict JSON has task_id and evaluated_at
    with open(os.path.join(entry, "verdict.json")) as f:
        data = json.load(f)
    assert data["task_id"] == "task-1"
    assert "evaluated_at" in data

    # Summary markdown contains task title
    with open(os.path.join(entry, "summary.md")) as f:
        summary = f.read()
    assert "Test Task" in summary


# ── list_verdicts ────────────────────────────────────────────


def test_list_verdicts_returns_empty_when_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.list_verdicts() == []


def test_list_verdicts_returns_entries_newest_first(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-1", {"passed": True})
    p.persist("task-2", {"passed": False})

    entries = p.list_verdicts()
    assert len(entries) == 2
    # Ordering depends on directory entry order — both entries must exist
    task_ids = {e["task_id"] for e in entries}
    assert task_ids == {"task-1", "task-2"}


def test_list_verdicts_filters_by_task_id(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-a", {"passed": True})
    p.persist("task-b", {"passed": True})

    filtered = p.list_verdicts(task_id="task-a")
    assert len(filtered) == 1
    assert filtered[0]["task_id"] == "task-a"


def test_list_verdicts_limits_to_n(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(5):
        p.persist(f"task-{i}", {"passed": True})

    assert len(p.list_verdicts(n=2)) == 2


# ── count_verdicts ───────────────────────────────────────────


def test_count_verdicts_returns_zero_for_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.count_verdicts() == 0


def test_count_verdicts_counts_all_entries(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(3):
        p.persist(f"task-{i}", {"passed": True})

    assert p.count_verdicts() == 3


# ── build_report ─────────────────────────────────────────────


def test_build_report_returns_disabled_message_when_history_off(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        p.config["enabled"] = False
        result = build_report(str(tmp_path))
        assert "disabled" in result


def test_build_report_shows_no_history_when_empty(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        result = build_report(str(tmp_path))
        assert "No verdict history found" in result


def test_build_report_includes_summary_stats(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("pass-1", {"passed": True, "task_title": "Passing"})
    p.persist("fail-1", {"passed": False, "task_title": "Failing"})

    report = build_report(str(tmp_path))
    assert "pass-1" in report
    assert "fail-1" in report
    assert "Passing" in report or "Failing" in report


# ── _build_summary edge cases ────────────────────────────────


def test_build_summary_handles_dict_items(tmp_path):
    """Summary generation works with dict-format criteria items (MCP)."""
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "Dict Items",
        "items": [{"criterion": "Must pass", "status": "PASS", "detail": "ok"}],
        "verdict": None,
    }
    summary = p._build_summary("task-x", data)
    assert "✓" in summary
    assert "Must pass" in summary


def test_build_summary_handles_pipeline_stages(tmp_path):
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "With Stages",
        "verdict": None,
        "stages": {"tier1": {"passed": True, "summary": "guard ok"}},
    }
    summary = p._build_summary("task-x", data)
    assert "tier1" in summary


# ── git-branch fallback (DF-007) ─────────────────────────────


def _git_env() -> dict:
    """Env with a deterministic git identity for subprocess git calls."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Test Runner",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test Runner",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return env


def _make_gitreins_branch_repo(repo, verdicts):
    """Init a temp git repo with verdicts committed on a `gitreins` branch.

    verdicts: iterable of (date, hash, task_id, passed). After committing,
    the local .gitreins/history/ dir is removed from the working tree so the
    repo looks like a fresh clone — the branch holds the files, the working
    tree does not.
    """
    env = _git_env()
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "gitreins", str(repo)], check=True, env=env)
    for date, hash_, task_id, passed in verdicts:
        entry = repo / ".gitreins" / "history" / date / hash_
        entry.mkdir(parents=True)
        (entry / "verdict.json").write_text(
            json.dumps({"task_id": task_id, "passed": passed, "evaluated_at": f"{date}T00:00:00"})
        )
        (entry / "summary.md").write_text(f"# {task_id}")
    subprocess.run(
        ["git", "add", ".gitreins"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "verdicts"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    shutil.rmtree(repo / ".gitreins")
    return env


def test_list_verdicts_falls_back_to_gitreins_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "old-task", True),
            ("2026-06-22", "bbbb2222", "new-task", False),
        ],
    )

    p = VerdictPersister(str(repo))
    assert p.storage_mode == "git"
    assert not os.path.isdir(p.history_dir)  # fresh-clone shape

    entries = p.list_verdicts()
    assert [e["task_id"] for e in entries] == ["new-task", "old-task"]
    assert entries[0]["_date"] == "2026-06-22"
    assert entries[0]["_hash"] == "bbbb2222"
    assert entries[0]["passed"] is False
    assert entries[1]["_date"] == "2026-06-21"
    assert entries[1]["_hash"] == "aaaa1111"


def test_list_verdicts_branch_fallback_respects_n_limit(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(
        repo, [("2026-07-01", f"h000000{i}", f"task-{i}", True) for i in range(5)]
    )

    p = VerdictPersister(str(repo))
    entries = p.list_verdicts(n=2)
    assert len(entries) == 2
    assert [e["task_id"] for e in entries] == ["task-4", "task-3"]


def test_list_verdicts_branch_fallback_filters_by_task_id(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(
        repo,
        [
            ("2026-07-01", "h0000001", "task-a", True),
            ("2026-07-01", "h0000002", "task-b", True),
        ],
    )

    p = VerdictPersister(str(repo))
    assert [e["task_id"] for e in p.list_verdicts(task_id="task-b")] == ["task-b"]


def test_list_verdicts_branch_fallback_skips_non_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "gitreins", str(repo)], check=True, env=env)
    good = repo / ".gitreins" / "history" / "2026-06-21" / "aaaa1111"
    good.mkdir(parents=True)
    (good / "verdict.json").write_text(json.dumps({"task_id": "good-task", "passed": True}))
    bad = repo / ".gitreins" / "history" / "2026-06-22" / "bbbb2222"
    bad.mkdir(parents=True)
    (bad / "verdict.json").write_text("{not json")
    subprocess.run(
        ["git", "add", ".gitreins"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "verdicts"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    shutil.rmtree(repo / ".gitreins")

    p = VerdictPersister(str(repo))
    assert [e["task_id"] for e in p.list_verdicts()] == ["good-task"]


def test_list_verdicts_no_gitreins_branch_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "readme.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "readme.txt"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )

    p = VerdictPersister(str(repo))
    assert p.list_verdicts() == []
    assert p.count_verdicts() == 0
    assert "No verdict history found" in build_report(str(repo))


def test_list_verdicts_branch_fallback_graceful_without_git(tmp_path):
    p = VerdictPersister(str(tmp_path))  # no .git anywhere up the tree
    assert p.list_verdicts() == []
    assert p.count_verdicts() == 0
    assert "No verdict history found" in build_report(str(tmp_path))


def test_list_verdicts_local_entries_take_precedence_over_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(repo, [("2026-06-21", "aaaa1111", "branch-task", True)])
    # A later judge run wrote a local verdict with a different task.
    local = repo / ".gitreins" / "history" / "2026-08-03" / "cccc3333"
    local.mkdir(parents=True)
    (local / "verdict.json").write_text(json.dumps({"task_id": "local-task", "passed": True}))
    (local / "summary.md").write_text("# local-task")

    p = VerdictPersister(str(repo))
    with patch.object(p, "_list_branch_verdicts", return_value=[]) as mocked:
        entries = p.list_verdicts()
        mocked.assert_not_called()
    assert [e["task_id"] for e in entries] == ["local-task"]
    assert p.count_verdicts() == 1


def test_list_verdicts_filesystem_mode_never_consults_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(repo, [("2026-06-21", "aaaa1111", "branch-task", True)])

    p = VerdictPersister(str(repo))
    p.config["storage"] = "filesystem"
    with patch.object(p, "_list_branch_verdicts", return_value=[]) as mocked:
        assert p.list_verdicts() == []
        mocked.assert_not_called()
    assert p.count_verdicts() == 0


def test_count_verdicts_falls_back_to_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "t1", True),
            ("2026-06-22", "bbbb2222", "t2", False),
            ("2026-06-23", "cccc3333", "t3", True),
        ],
    )

    p = VerdictPersister(str(repo))
    assert p.count_verdicts() == 3


def test_build_report_reads_verdicts_from_gitreins_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_gitreins_branch_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "old-task", True),
            ("2026-06-22", "bbbb2222", "new-task", False),
        ],
    )

    report = build_report(str(repo))
    assert "No verdict history found" not in report
    assert "old-task" in report
    assert "new-task" in report
    assert "Total entries: 2" in report
