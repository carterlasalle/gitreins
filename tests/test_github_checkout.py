"""
R2.10 tests for engine/github/checkout — the ChangeSource abstraction
(DESIGN_v2.md §14).

Covers: Diff shape (text compatible with the orchestrator's diff_context),
WorkingTreeChangeSource (unstaged+staged diff, --unified=3, ACM filtered
changed_files, HEAD/empty-tree base, worktree head via stash create),
CommitRangeChangeSource (three-dot merge-base diff, ACM filtering, ref
resolution, missing-ref errors), and PullRequestChangeSource (gh pr
view/diff payloads, ACM status filtering from gh files JSON, base/head SHAs,
metadata caching, missing-gh and invalid-JSON error paths).

Hermetic: WorkingTree/CommitRange run against a real temp git repo created
with subprocess git init (local only — no network); PullRequestChangeSource
monkeypatches engine.github.checkout._run_gh — no live GitHub, no LLM.
"""

import json
import subprocess
from pathlib import Path

import pytest

from engine.github.checkout import (
    ChangeSourceError,
    CommitRangeChangeSource,
    Diff,
    PullRequestChangeSource,
    WorkingTreeChangeSource,
)

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(repo, *args):
    """Run a git command in the temp repo; return stdout, raise on failure."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=repo
    ).stdout.strip()


def head_sha(repo):
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A real temp git repo with one base commit (a.py, c.py)."""
    r = tmp_path / "repo"
    r.mkdir()
    git(str(r), "init", "-q", "-b", "main")
    git(str(r), "config", "user.email", "test@example.com")
    git(str(r), "config", "user.name", "Test Runner")
    (r / "a.py").write_text("x = 1\n")
    (r / "c.py").write_text("c = 3\n")
    git(str(r), "add", "a.py", "c.py")
    git(str(r), "commit", "-q", "-m", "base")
    return str(r)


def is_sha(text):
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


# ── Diff shape ───────────────────────────────────────────────────────────


class TestDiff:
    def test_text_is_str_compatible_with_diff_context(self):
        d = Diff(text="--- a/x.py\n+++ b/x.py\n")
        assert isinstance(d.text, str)
        assert d.files == []

    def test_defaults_empty(self):
        d = Diff()
        assert d.text == ""
        assert d.files == []


# ── WorkingTreeChangeSource (real temp git repo) ─────────────────────────


class TestWorkingTreeChangeSource:
    def test_diff_covers_unstaged_and_staged(self, repo):
        repo = Path(repo)
        (repo / "a.py").write_text("x = 2\n")  # unstaged modification
        (repo / "b.py").write_text("b = 1\n")  # new file, staged below
        git(str(repo), "add", "b.py")
        git(str(repo), "rm", "-q", "c.py")  # unstaged deletion

        src = WorkingTreeChangeSource(str(repo))
        d = src.diff()

        # Both halves are present: a.py (unstaged) and b.py (staged).
        assert "diff --git a/a.py b/a.py" in d.text
        assert "diff --git a/b.py b/b.py" in d.text
        assert "-x = 1" in d.text and "+x = 2" in d.text
        assert "new file mode" in d.text
        # Deletions are part of the diff text…
        assert "diff --git a/c.py b/c.py" in d.text
        # …but the ACM filter excludes them from changed_files.
        assert d.files == ["a.py", "b.py"]
        assert src.changed_files() == ["a.py", "b.py"]

    def test_unified_3_context(self, repo):
        repo = Path(repo)
        # Commit a 7-line file, then modify a middle line unstaged.
        (repo / "a.py").write_text("l1\nl2\nl3\nl4\nl5\nl6\nl7\n")
        git(str(repo), "add", "a.py")
        git(str(repo), "commit", "-q", "-m", "seven lines")
        (repo / "a.py").write_text("l1\nl2\nl3\nl4\nCHANGED\nl6\nl7\n")

        text = WorkingTreeChangeSource(str(repo)).diff().text
        assert "@@" in text
        # --unified=3: the hunk carries 3 context lines before the change.
        hunk = text[text.index("@@") : text.index("@@") + 200]
        lines = [ln for ln in hunk.splitlines() if ln and ln[0] in "+- "]
        context_before = sum(1 for ln in lines[:3] if ln[0] == " ")
        assert context_before == 3

    def test_base_sha_is_head(self, repo):
        src = WorkingTreeChangeSource(repo)
        assert src.base_sha() == head_sha(repo)

    def test_head_sha_is_worktree_state(self, repo):
        (Path(repo) / "a.py").write_text("x = 9\n")
        src = WorkingTreeChangeSource(repo)
        sha = src.head_sha()
        assert is_sha(sha)
        assert sha != src.base_sha()  # worktree differs from HEAD

    def test_clean_tree_head_equals_base(self, repo):
        src = WorkingTreeChangeSource(repo)
        assert src.diff().text == ""
        assert src.changed_files() == []
        assert src.head_sha() == src.base_sha() == head_sha(repo)

    def test_staged_only_changes(self, repo):
        (Path(repo) / "new.py").write_text("n = 1\n")
        git(repo, "add", "new.py")
        src = WorkingTreeChangeSource(repo)
        assert src.changed_files() == ["new.py"]
        assert "new.py" in src.diff().text

    def test_no_head_repo_uses_empty_tree(self, tmp_path):
        r = tmp_path / "bare"
        r.mkdir()
        git(str(r), "init", "-q", "-b", "main")
        (r / "u.py").write_text("u = 1\n")  # untracked — not part of any diff

        src = WorkingTreeChangeSource(str(r))
        assert src.base_sha() == EMPTY_TREE_SHA
        assert src.head_sha() == EMPTY_TREE_SHA
        assert src.diff().text == ""  # untracked files are not in git diff
        assert src.changed_files() == []

    def test_not_a_repo_raises_clear_error(self, tmp_path):
        with pytest.raises(ChangeSourceError) as ei:
            WorkingTreeChangeSource(str(tmp_path)).diff()
        assert "git" in str(ei.value)


# ── CommitRangeChangeSource (real temp git repo) ─────────────────────────


@pytest.fixture
def range_repo(repo):
    """repo + three commits: modify a.py + add b.py; modify b.py + add c2.py;
    then delete c2.py."""
    r = repo
    (Path(r) / "a.py").write_text("x = 2\n")
    (Path(r) / "b.py").write_text("b = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "commit2")
    (Path(r) / "b.py").write_text("b = 2\n")
    (Path(r) / "c2.py").write_text("c2 = 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "commit3")
    git(r, "rm", "-q", "c2.py")
    git(r, "commit", "-q", "-m", "commit4")
    return r


class TestCommitRangeChangeSource:
    def test_three_dot_diff_from_merge_base(self, range_repo):
        # HEAD~2 is commit2; merge-base(commit2, HEAD) is commit2, so the
        # range covers commit3+commit4: b.py modified, c2.py added+deleted
        # (net zero — absent), a.py untouched.
        src = CommitRangeChangeSource(range_repo, "HEAD~2", "HEAD")
        d = src.diff()
        assert "diff --git a/b.py b/b.py" in d.text
        assert "diff --git a/a.py b/a.py" not in d.text
        assert "c2.py" not in d.text  # added then deleted → not in the range
        assert d.files == ["b.py"]
        assert src.changed_files() == ["b.py"]

    def test_range_across_more_commits(self, range_repo):
        src = CommitRangeChangeSource(range_repo, "HEAD~3", "HEAD")
        # From commit1: a.py modified, b.py added+modified; c2.py net zero.
        assert src.changed_files() == ["a.py", "b.py"]

    def test_base_head_shas_resolve_refs(self, range_repo):
        src = CommitRangeChangeSource(range_repo, "HEAD~2", "HEAD")
        assert src.base_sha() == git(range_repo, "rev-parse", "HEAD~2")
        assert src.head_sha() == git(range_repo, "rev-parse", "HEAD")

    def test_missing_ref_raises_clear_error(self, range_repo):
        src = CommitRangeChangeSource(range_repo, "does-not-exist", "HEAD")
        with pytest.raises(ChangeSourceError) as ei:
            src.diff()
        assert "does-not-exist" in str(ei.value)
        with pytest.raises(ChangeSourceError):
            src.base_sha()


# ── PullRequestChangeSource (monkeypatched gh) ───────────────────────────

PR_META = {
    "baseRefOid": "b" * 40,
    "headRefOid": "h" * 40,
    "headRefName": "feature/x",
    "files": [
        {"path": "a.py", "status": "MODIFIED"},
        {"path": "b.py", "status": "ADDED"},
        {"path": "c.py", "status": "REMOVED"},
        {"path": "old.py", "status": "RENAMED"},
        {"path": "x.py", "status": "UNCHANGED"},
    ],
}

PR_DIFF = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


class FakeGH:
    """A _run_gh stand-in: dispatches pr view / pr diff, records calls."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, args, *, gh_binary="gh", timeout=60.0, input=None):
        self.calls.append((list(args), input))
        if self.fail is not None:
            raise self.fail
        if args[:2] == ["pr", "view"]:
            return json.dumps(PR_META)
        if args[:2] == ["pr", "diff"]:
            return PR_DIFF
        raise AssertionError(f"unexpected gh call: {args}")


class TestPullRequestChangeSource:
    def test_diff_text_and_files(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        src = PullRequestChangeSource("acme", "widgets", 42)

        d = src.diff()
        assert isinstance(d, Diff)
        assert d.text == PR_DIFF
        # Only ADDED/MODIFIED survive the ACM filter (REMOVED/RENAMED/UNCHANGED out).
        assert d.files == ["a.py", "b.py"]
        assert src.changed_files() == ["a.py", "b.py"]

    def test_base_head_shas_from_gh(self, monkeypatch):
        monkeypatch.setattr("engine.github.checkout._run_gh", FakeGH())
        src = PullRequestChangeSource("acme", "widgets", 42)
        assert src.base_sha() == "b" * 40
        assert src.head_sha() == "h" * 40

    def test_gh_args_target_repo_and_pr(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        src = PullRequestChangeSource("acme", "widgets", 42)
        src.diff()
        view_call = next(c for c in fake.calls if c[0][:2] == ["pr", "view"])
        diff_call = next(c for c in fake.calls if c[0][:2] == ["pr", "diff"])
        assert view_call[0][2:] == [
            "42",
            "-R",
            "acme/widgets",
            "--json",
            "baseRefOid,headRefOid,headRefName,files",
        ]
        assert diff_call[0][2:] == ["42", "-R", "acme/widgets"]

    def test_metadata_fetched_once_and_cached(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        src = PullRequestChangeSource("acme", "widgets", 42)
        src.changed_files()
        src.changed_files()
        src.base_sha()
        src.head_sha()
        src.diff()
        view_calls = [c for c in fake.calls if c[0][:2] == ["pr", "view"]]
        assert len(view_calls) == 1

    def test_missing_gh_raises_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.github.checkout._run_gh",
            FakeGH(fail=ChangeSourceError("gh CLI not found ('gh') — install GitHub CLI")),
        )
        src = PullRequestChangeSource("acme", "widgets", 42)
        with pytest.raises(ChangeSourceError) as ei:
            src.diff()
        assert "gh" in str(ei.value)

    def test_unauthenticated_gh_raises_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.github.checkout._run_gh",
            FakeGH(fail=ChangeSourceError("gh pr diff 42 failed (rc=1): HTTP 401")),
        )
        src = PullRequestChangeSource("acme", "widgets", 42)
        with pytest.raises(ChangeSourceError) as ei:
            src.changed_files()
        assert "401" in str(ei.value)

    def test_invalid_json_raises_clear_error(self, monkeypatch):
        def bad_json(args, **kw):
            return "not json at all"

        monkeypatch.setattr("engine.github.checkout._run_gh", bad_json)
        src = PullRequestChangeSource("acme", "widgets", 42)
        with pytest.raises(ChangeSourceError) as ei:
            src.changed_files()
        assert "invalid JSON" in str(ei.value)
