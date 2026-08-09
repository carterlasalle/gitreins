"""
R2.10 ChangeSource abstraction (DESIGN_v2.md §14) — the reviewer no longer
cares whether the change is a local working tree, a commit range, or a GitHub
pull request: every source exposes the same four-method surface

    diff()            -> Diff
    changed_files()   -> list[str]
    base_sha()        -> str
    head_sha()        -> str

``Diff.text`` serializes compatibly with the ``diff_context`` string
:class:`engine.review.orchestrator.ReviewOrchestrator.run` already accepts —
callers pass ``source.diff().text`` straight in, so the SAME review engine
runs locally and remotely (§14).

Three concrete sources:

- :class:`WorkingTreeChangeSource` — ``git diff`` (unstaged + staged, both
  halves combined) vs HEAD; ``--unified=3``.
- :class:`CommitRangeChangeSource` — ``git diff base_ref...head_ref``
  (three-dot → merge-base range).
- :class:`PullRequestChangeSource` — ``gh pr diff`` / ``gh pr view`` via the
  GitHub CLI subprocess (no PyGithub dependency).

All subprocess failures degrade to :class:`ChangeSourceError` with a clear
message (missing binary, unauthenticated CLI, non-zero exit, invalid JSON) —
never a raw stack trace. The ``_run_git`` / ``_run_gh`` / ``_gh_api`` helpers
are module-level so tests can monkeypatch them hermetically.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger("gitreins.github.checkout")

__all__ = [
    "ChangeSourceError",
    "ChangeSource",
    "Diff",
    "WorkingTreeChangeSource",
    "CommitRangeChangeSource",
    "PullRequestChangeSource",
    "_run_git",
    "_run_gh",
    "_gh_api",
    "EMPTY_TREE_SHA",
]

#: The git empty-tree object — the base of a repo with no commits yet.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: ``gh pr view --json files`` statuses that count as "changed" for review
#: (mirrors the ACM filter used for local git diffs). Uppercase per gh output.
_ACCEPTED_STATUSES = frozenset({"ADDED", "COPIED", "MODIFIED"})


class ChangeSourceError(Exception):
    """A change source could not be read (missing git/gh, bad ref, API fail).

    Raised instead of raw subprocess/OSError stack traces so callers get one
    clear, actionable message.
    """


@runtime_checkable
class ChangeSource(Protocol):
    """A change: its diff, the files it touches, and its base/head SHAs.

    Any object satisfying this protocol can drive the review DAG — local
    working tree, commit range, or GitHub pull request (§14).
    """

    def diff(self) -> Diff: ...

    def changed_files(self) -> list[str]: ...

    def base_sha(self) -> str: ...

    def head_sha(self) -> str: ...


@dataclass
class Diff:
    """A change's diff text plus the files it touches.

    ``text`` is a unified diff that serializes compatibly with the
    ``diff_context`` string ReviewOrchestrator.run accepts; ``files`` is the
    same ACM-filtered list as :meth:`ChangeSource.changed_files` (convenience
    so a diff call yields everything the DAG needs in one object).
    """

    text: str = ""
    files: list[str] = field(default_factory=list)


# ── Subprocess plumbing (module-level for hermetic monkeypatching) ───────


def _run_git(
    workdir: str, args: list[str], *, timeout: float = 30.0
) -> str:
    """Run ``git <args>`` in ``workdir``; raise ChangeSourceError on failure.

    Returns stdout. Errors carry the git stderr/rc in the message, never a
    stack trace.
    """
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir
        )
    except FileNotFoundError as exc:
        raise ChangeSourceError(
            f"git binary not found ({exc}) — git is required for this change source"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ChangeSourceError(
            f"git {' '.join(args)} timed out after {timeout}s"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ChangeSourceError(
            f"git {' '.join(args)} failed (rc={result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def _run_gh(
    args: list[str],
    *,
    gh_binary: str = "gh",
    timeout: float = 60.0,
    input: str | None = None,
) -> str:
    """Run a ``gh`` CLI command; raise ChangeSourceError on failure.

    Missing binary, unauthenticated CLI, and non-zero exits all become
    ChangeSourceError with the gh stderr in the message — no stack traces.
    ``input`` is written to the child's stdin (used by ``gh api --input -``).
    """
    cmd = [gh_binary, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError:
        raise ChangeSourceError(
            f"gh CLI not found ({gh_binary!r}) — install GitHub CLI "
            "(https://cli.github.com) or set GH_TOKEN for the requests fallback"
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise ChangeSourceError(
            f"gh {' '.join(args)} timed out after {timeout}s"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ChangeSourceError(
            f"gh {' '.join(args)} failed (rc={result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def _gh_api(
    method: str,
    path: str,
    fields: dict,
    *,
    gh_binary: str = "gh",
    timeout: float = 60.0,
) -> str:
    """POST a GitHub REST call through ``gh api --input -``.

    ``fields`` become the JSON request body (None values are dropped).
    Raises ChangeSourceError on failure — same graceful contract as
    :func:`_run_gh`.
    """
    body = json.dumps({k: v for k, v in fields.items() if v is not None})
    return _run_gh(
        ["api", "-X", method, path, "--input", "-"],
        gh_binary=gh_binary,
        timeout=timeout,
        input=body,
    )


def _rev_parse(workdir: str, ref: str, *, timeout: float = 30.0) -> str:
    """Resolve ``ref`` to a SHA; return "" when it does not exist."""
    try:
        return _run_git(workdir, ["rev-parse", ref], timeout=timeout).strip()
    except ChangeSourceError:
        return ""


# ── Sources ──────────────────────────────────────────────────────────────


class WorkingTreeChangeSource:
    """Local working-tree change: unstaged + staged edits vs HEAD.

    ``diff()`` combines ``git diff --unified=3`` (unstaged) with ``git diff
    --cached --unified=3`` (staged) — the full pre-commit picture, matching
    how GitReins' local MCP/pre-commit flow already treats the tree.

    ``base_sha()`` is HEAD, or the empty-tree SHA when the repo has no commits
    yet. ``head_sha()`` is the worktree state materialized by ``git stash
    create`` (a dangling commit object — harmless, never touches the working
    tree); when the tree is clean it falls back to HEAD, and to the
    empty-tree SHA when there is no HEAD at all.
    """

    def __init__(self, workdir: str = ".", *, timeout: float = 30.0):
        self.workdir = os.path.abspath(workdir)
        self._timeout = timeout

    def diff(self) -> Diff:
        unstaged = _run_git(
            self.workdir, ["diff", "--unified=3"], timeout=self._timeout
        )
        staged = _run_git(
            self.workdir, ["diff", "--cached", "--unified=3"], timeout=self._timeout
        )
        text = "\n".join(p for p in (unstaged, staged) if p.strip())
        return Diff(text=text, files=self.changed_files())

    def changed_files(self) -> list[str]:
        files: list[str] = []
        for args in (
            ["diff", "--name-only", "--diff-filter=ACM"],
            ["diff", "--cached", "--name-only", "--diff-filter=ACM"],
        ):
            files.extend(
                _run_git(self.workdir, args, timeout=self._timeout).splitlines()
            )
        return sorted(set(f for f in files if f))

    def base_sha(self) -> str:
        return _rev_parse(self.workdir, "HEAD", timeout=self._timeout) or EMPTY_TREE_SHA

    def head_sha(self) -> str:
        try:
            sha = _run_git(
                self.workdir, ["stash", "create"], timeout=self._timeout
            ).strip()
        except ChangeSourceError:
            sha = ""
        if not sha:
            sha = _rev_parse(self.workdir, "HEAD", timeout=self._timeout)
        return sha or EMPTY_TREE_SHA


class CommitRangeChangeSource:
    """Commit-range change: ``git diff base_ref...head_ref`` (three-dot range).

    ``diff()``/``changed_files()`` diff against the merge-base of the two refs
    (three-dot semantics — what the range actually changed). ``base_sha()`` /
    ``head_sha()`` resolve the refs as given and raise ChangeSourceError for
    refs that do not exist.
    """

    def __init__(
        self, workdir: str, base_ref: str, head_ref: str, *, timeout: float = 30.0
    ):
        self.workdir = os.path.abspath(workdir)
        self.base_ref = base_ref
        self.head_ref = head_ref
        self._timeout = timeout

    def _range(self) -> str:
        return f"{self.base_ref}...{self.head_ref}"

    def diff(self) -> Diff:
        text = _run_git(
            self.workdir, ["diff", "--unified=3", self._range()], timeout=self._timeout
        )
        return Diff(text=text, files=self.changed_files())

    def changed_files(self) -> list[str]:
        out = _run_git(
            self.workdir,
            ["diff", "--name-only", "--diff-filter=ACM", self._range()],
            timeout=self._timeout,
        )
        return [f for f in out.splitlines() if f]

    def base_sha(self) -> str:
        sha = _rev_parse(self.workdir, self.base_ref, timeout=self._timeout)
        if not sha:
            raise ChangeSourceError(
                f"ref {self.base_ref!r} does not exist in {self.workdir}"
            )
        return sha

    def head_sha(self) -> str:
        sha = _rev_parse(self.workdir, self.head_ref, timeout=self._timeout)
        if not sha:
            raise ChangeSourceError(
                f"ref {self.head_ref!r} does not exist in {self.workdir}"
            )
        return sha


class PullRequestChangeSource:
    """GitHub pull-request change, fetched via the gh CLI subprocess.

    ``gh pr diff <n> -R owner/repo`` supplies the diff text (GitHub's
    merge-base rendering) and one ``gh pr view --json
    baseRefOid,headRefOid,headRefName,files`` call supplies the SHAs and the
    file list (filtered to added/copied/modified — the ACM equivalent).
    Metadata and diff are fetched once and cached per instance.

    No PyGithub dependency; all gh failures (missing binary, unauthenticated
    CLI, non-zero exit, invalid JSON) raise ChangeSourceError with a clear
    message.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        gh_binary: str = "gh",
        timeout: float = 60.0,
    ):
        self.owner = owner
        self.repo = repo
        self.pr_number = int(pr_number)
        self._gh_binary = gh_binary
        self._timeout = timeout
        self._meta: dict | None = None
        self._diff_text: str | None = None

    @property
    def _repo_arg(self) -> str:
        return f"{self.owner}/{self.repo}"

    def _fetch_meta(self) -> dict:
        meta = self._meta
        if meta is None:
            raw = _run_gh(
                [
                    "pr",
                    "view",
                    str(self.pr_number),
                    "-R",
                    self._repo_arg,
                    "--json",
                    "baseRefOid,headRefOid,headRefName,files",
                ],
                gh_binary=self._gh_binary,
                timeout=self._timeout,
            )
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ChangeSourceError(
                    f"gh pr view returned invalid JSON for "
                    f"{self._repo_arg}#{self.pr_number}: {exc}"
                ) from exc
            if not isinstance(meta, dict):
                raise ChangeSourceError(
                    f"gh pr view returned unexpected JSON for "
                    f"{self._repo_arg}#{self.pr_number}: expected an object, "
                    f"got {type(meta).__name__}"
                )
            self._meta = meta
        return meta

    def diff(self) -> Diff:
        if self._diff_text is None:
            self._diff_text = _run_gh(
                ["pr", "diff", str(self.pr_number), "-R", self._repo_arg],
                gh_binary=self._gh_binary,
                timeout=self._timeout,
            )
        return Diff(text=self._diff_text, files=self.changed_files())

    def changed_files(self) -> list[str]:
        meta = self._fetch_meta()
        files = [
            str(entry["path"])
            for entry in meta.get("files") or []
            if str(entry.get("status", "")).upper() in _ACCEPTED_STATUSES
        ]
        return sorted(set(files))

    def base_sha(self) -> str:
        return str(self._fetch_meta().get("baseRefOid") or "")

    def head_sha(self) -> str:
        return str(self._fetch_meta().get("headRefOid") or "")
