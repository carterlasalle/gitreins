"""
Verdict Persister — Save verdicts to .gitreins/history/ with configurable storage.

Storage modes:
    "git"        — auto-commit to a `gitreins` orphan branch (default)
    "filesystem" — write files to .gitreins/history/ only, no git commits

Reads: list_verdicts()/count_verdicts() prefer the local .gitreins/history/
files. When the local dir is missing or holds no entries and storage is
"git", they fall back to the verdicts committed on the `gitreins` branch —
so a fresh clone (whose working tree has no .gitreins/history/, since it is
gitignored) can still browse the full verdict history via `gitreins report`.

Config (.gitreins/config.yaml):
    history:
      enabled: true               # false = no persistence at all
      path: ".gitreins/history"   # relative to repo root, or absolute
      storage: "git"              # "git" or "filesystem"
      max_verdicts: 1000          # auto-prune old verdicts

Usage:
    persister = VerdictPersister(workdir="/path/to/repo")
    commit_hash = persister.persist(task_id, verdict_data)
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

logger = logging.getLogger("gitreins.persist")


# ── Persistence config defaults ────────────────────────────────

DEFAULT_HISTORY_CONFIG = {
    "enabled": True,
    "path": ".gitreins/history",
    "storage": "git",
    "max_verdicts": 1000,
}


def load_history_config(workdir: str) -> dict:
    """Load history section from .gitreins/config.yaml, merged with defaults."""
    config = {}
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            import yaml

            with open(config_path, "r") as f:
                raw = yaml.safe_load(f) or {}
            config = raw.get("history", {})
        except Exception:
            pass

    merged = dict(DEFAULT_HISTORY_CONFIG)
    if isinstance(config, dict):
        for key in merged:
            if key in config:
                merged[key] = config[key]
    return merged


# ── Persister ──────────────────────────────────────────────────


class VerdictPersister:
    """Persist verdict results and provide history lookup for reports."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self.config = load_history_config(self.workdir)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def history_dir(self) -> str:
        path = self.config.get("path", ".gitreins/history")
        if os.path.isabs(path):
            return path
        return os.path.join(self.workdir, path)

    @property
    def storage_mode(self) -> str:
        return self.config.get("storage", "git")

    # ── Save ────────────────────────────────────────────────

    def persist(self, task_id: str, verdict_data: dict) -> str:
        """Save verdict to history. Returns commit hash or "dry-run" or "disabled"."""
        if not self.enabled:
            return "disabled"

        verdict_data["task_id"] = task_id
        verdict_data["evaluated_at"] = datetime.utcnow().isoformat()

        # Generate deterministic short hash
        hash_input = f"{task_id}:{verdict_data['evaluated_at']}"
        short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]

        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        entry_dir = os.path.join(self.history_dir, date_str, short_hash)
        os.makedirs(entry_dir, exist_ok=True)

        # Write verdict.json
        verdict_path = os.path.join(entry_dir, "verdict.json")
        with open(verdict_path, "w") as f:
            json.dump(verdict_data, f, indent=2, default=str)

        # Write summary.md
        summary_path = os.path.join(entry_dir, "summary.md")
        summary = self._build_summary(task_id, verdict_data)
        with open(summary_path, "w") as f:
            f.write(summary)

        # Git commit if configured
        commit_hash = "dry-run"
        if self.storage_mode == "git":
            commit_hash = self._git_commit(entry_dir, task_id, verdict_data.get("passed", False))

        # Prune old verdicts if over max
        self._prune_old()

        return commit_hash

    # ── List / Report ────────────────────────────────────────

    def list_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Return recent verdicts as a list of dicts, newest first.

        Local filesystem entries take precedence. When the local history
        dir is missing or holds no verdict.json files at all and storage
        mode is "git", falls back to verdicts committed on the `gitreins`
        branch — fresh clones (no local .gitreins/history/, it is
        gitignored) can still browse history via `gitreins report`.
        """
        entries = self._list_local_verdicts(n=n, task_id=task_id)
        if entries:
            return entries
        # Local pass yielded nothing: fall back to the `gitreins` branch
        # only when git storage is active AND the local dir holds no
        # verdict.json files at all (a task_id filter that matches nothing
        # locally must not pull branch entries in — sources are never merged).
        if self.storage_mode == "git" and not self._local_history_has_entries():
            return self._list_branch_verdicts(n=n, task_id=task_id)
        return []

    def _list_local_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Read verdict entries from the local filesystem (original behavior)."""
        if not os.path.isdir(self.history_dir):
            return []

        entries = []
        for date_dir in sorted(os.listdir(self.history_dir), reverse=True):
            date_path = os.path.join(self.history_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for hash_dir in sorted(os.listdir(date_path), reverse=True):
                entry_dir = os.path.join(date_path, hash_dir)
                verdict_path = os.path.join(entry_dir, "verdict.json")
                if not os.path.isfile(verdict_path):
                    continue
                try:
                    with open(verdict_path) as f:
                        data = json.load(f)
                    if task_id and data.get("task_id") != task_id:
                        continue
                    data["_date"] = date_dir
                    data["_hash"] = hash_dir
                    entries.append(data)
                    if len(entries) >= n:
                        return entries
                except (json.JSONDecodeError, OSError):
                    continue

        return entries

    def _local_history_has_entries(self) -> bool:
        """True when the local history dir holds at least one verdict.json."""
        if not os.path.isdir(self.history_dir):
            return False
        for date_dir in os.listdir(self.history_dir):
            date_path = os.path.join(self.history_dir, date_dir)
            if not os.path.isdir(date_path):
                continue
            for hash_dir in os.listdir(date_path):
                if os.path.isfile(os.path.join(date_path, hash_dir, "verdict.json")):
                    return True
        return False

    def _branch_history_prefix(self) -> str:
        """History path relative to workdir, git-style (forward slashes).

        Verdicts are committed to the `gitreins` branch at the path
        relative to the repo root (persist() commits os.path.relpath of the
        entry dir), so this is the prefix to enumerate on the branch.
        """
        return os.path.relpath(self.history_dir, self.workdir).replace(os.sep, "/")

    def _list_branch_verdicts(self, n: int = 20, task_id: str | None = None) -> list[dict]:
        """Read verdict entries committed to the `gitreins` branch.

        Enumerates verdict.json files under the history path with
        `git ls-tree -r --name-only gitreins -- <prefix>` and reads each
        via `git show gitreins:<path>`. Returns [] on any git failure
        (branch absent, not a git repo, timeout) — callers degrade to
        "No verdict history found." exactly as before.
        """
        prefix = self._branch_history_prefix()
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "gitreins", "--", prefix],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("git ls-tree failed on gitreins branch (non-fatal): %s", e)
            return []
        if result.returncode != 0:
            return []  # branch missing or not a git repo

        # Paths are <prefix>/<date>/<hash>/verdict.json — reverse lexical
        # order matches the local reader's newest-first (date desc, hash desc).
        paths = sorted(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().endswith("/verdict.json")
            ),
            reverse=True,
        )

        entries = []
        rel_prefix = prefix.rstrip("/") + "/"
        for path in paths:
            rel = path[len(rel_prefix) :] if path.startswith(rel_prefix) else path
            parts = rel.split("/")
            if len(parts) != 3 or parts[2] != "verdict.json":
                continue
            date_dir, hash_dir = parts[0], parts[1]
            try:
                show = subprocess.run(
                    ["git", "show", f"gitreins:{path}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.workdir,
                )
                if show.returncode != 0:
                    continue
                data = json.loads(show.stdout)
            except (
                subprocess.TimeoutExpired,
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as e:
                logger.warning(
                    "Failed to read verdict %s from gitreins branch (non-fatal): %s",
                    path,
                    e,
                )
                continue
            if task_id and data.get("task_id") != task_id:
                continue
            data["_date"] = date_dir
            data["_hash"] = hash_dir
            entries.append(data)
            if len(entries) >= n:
                break

        return entries

    def count_verdicts(self) -> int:
        """Return total number of stored verdict entries.

        Local filesystem entries take precedence; when the local history
        dir is missing or empty and storage mode is "git", counts the
        verdict.json files on the `gitreins` branch instead.
        """
        count = 0
        if os.path.isdir(self.history_dir):
            for date_dir in os.listdir(self.history_dir):
                date_path = os.path.join(self.history_dir, date_dir)
                if os.path.isdir(date_path):
                    count += len(
                        [
                            d
                            for d in os.listdir(date_path)
                            if os.path.isdir(os.path.join(date_path, d))
                        ]
                    )
        if count == 0 and self.storage_mode == "git":
            return self._count_branch_verdicts()
        return count

    def _count_branch_verdicts(self) -> int:
        """Count verdict.json files on the `gitreins` branch (no content reads)."""
        prefix = self._branch_history_prefix()
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "gitreins", "--", prefix],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("git ls-tree failed on gitreins branch (non-fatal): %s", e)
            return 0
        if result.returncode != 0:
            return 0
        return sum(
            1 for line in result.stdout.splitlines() if line.strip().endswith("/verdict.json")
        )

    # ── Internal ─────────────────────────────────────────────

    def _build_summary(self, task_id: str, verdict_data: dict) -> str:
        passed = verdict_data.get("passed", False)
        verdict = verdict_data.get("verdict", None)
        task_title = verdict_data.get("task_title", task_id)
        items = verdict_data.get("items", [])
        stages = verdict_data.get("stages", {})
        summary_text = verdict_data.get("summary", "")
        evaluated_at = verdict_data.get("evaluated_at", "")

        lines = []
        lines.append(f"# Verdict: {task_id}")
        lines.append("")
        lines.append(f"**Task:** {task_title}")
        lines.append(f"**Evaluated:** {evaluated_at}")
        lines.append(f"**Result:** {'✓ PASS' if passed else '✗ FAIL'}")
        if verdict:
            verdict_str = verdict.verdict if hasattr(verdict, "verdict") else str(verdict)
            lines.append(f"**Verdict:** {verdict_str}")
        lines.append("")

        # Items from evaluator
        if items:
            lines.append("## Criteria")
            lines.append("")
            for item in items:
                if isinstance(item, dict):
                    status = "✓" if item.get("status") == "PASS" else "✗"
                    lines.append(f"- {status} **{item.get('criterion', '?')}**")
                    detail = item.get("detail", "")
                    if detail:
                        lines.append(f"  - {detail}")
                else:
                    status = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                    criterion = getattr(item, "criterion", str(item))
                    detail = getattr(item, "detail", "")
                    lines.append(f"- {status} **{criterion}**")
                    if detail:
                        lines.append(f"  - {detail}")
            lines.append("")

        # Pipeline stages
        if stages:
            lines.append("## Pipeline Stages")
            lines.append("")
            for stage_id, stage in stages.items():
                stage_passed = stage.get("passed", False)
                icon = "✓" if stage_passed else "✗"
                lines.append(f"- {icon} **{stage_id}**")
                stage_summary = stage.get("summary", "")
                if stage_summary:
                    lines.append(f"  - {stage_summary}")
            lines.append("")

        # Free-form summary
        if summary_text:
            lines.append("## Summary")
            lines.append("")
            lines.append(summary_text)
            lines.append("")

        return "\n".join(lines)

    def _git_commit(self, entry_dir: str, task_id: str, passed: bool) -> str:
        """Commit verdict entry to gitreins orphan branch. Returns short hash or 'dry-run'."""
        git_dir = os.path.join(self.workdir, ".git")
        if not os.path.exists(git_dir):
            logger.warning("No .git directory — verdict files written but not committed")
            return "dry-run"

        try:
            rel_path = os.path.relpath(entry_dir, self.workdir)

            # Check if gitreins branch exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "gitreins"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            branch_exists = result.returncode == 0

            if not branch_exists:
                return self._create_orphan(rel_path, task_id, passed)
            else:
                return self._commit_to_existing(rel_path, task_id, passed)

        except subprocess.TimeoutExpired:
            logger.warning("Git command timed out — files written but not committed")
            return "dry-run"
        except Exception as e:
            logger.warning("Git operation failed (non-fatal): %s", e)
            return "dry-run"

    def _create_orphan(self, rel_path: str, task_id: str, passed: bool) -> str:
        """Create gitreins orphan branch with initial verdict commit."""
        # Remember current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.workdir,
        )
        current_branch = result.stdout.strip()

        # Check for uncommitted changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=self.workdir,
        )
        stashed = False
        if status.stdout.strip():
            subprocess.run(
                ["git", "stash", "push", "-m", "gitreins-persist"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            stashed = True

        try:
            subprocess.run(
                ["git", "checkout", "--orphan", "gitreins"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            subprocess.run(
                ["git", "rm", "-rf", "."],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            subprocess.run(
                ["git", "add", rel_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            subprocess.run(
                ["git", "commit", "-m", f"verdict: {task_id} — {'PASS' if passed else 'FAIL'}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )
            commit_hash = hash_result.stdout.strip()[:8]

            # Return to original branch
            restore = current_branch if current_branch not in ("HEAD", "") else "main"
            subprocess.run(
                ["git", "checkout", restore],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=self.workdir,
            )

            return commit_hash

        finally:
            if stashed:
                subprocess.run(
                    ["git", "stash", "pop"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.workdir,
                )

    def _commit_to_existing(self, rel_path: str, task_id: str, passed: bool) -> str:
        """Commit to existing gitreins branch via worktree to avoid switching."""
        worktree_dir = tempfile.mkdtemp(prefix="gitreins-wt-")
        try:
            subprocess.run(
                ["git", "worktree", "add", worktree_dir, "gitreins"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workdir,
            )

            # Copy verdict files into worktree
            src = os.path.join(self.workdir, rel_path)
            dst = os.path.join(worktree_dir, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(src, dst)

            subprocess.run(
                ["git", "add", rel_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=worktree_dir,
            )
            subprocess.run(
                ["git", "commit", "-m", f"verdict: {task_id} — {'PASS' if passed else 'FAIL'}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=worktree_dir,
            )
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=worktree_dir,
            )
            commit_hash = hash_result.stdout.strip()[:8]

            return commit_hash

        except Exception:
            logger.debug("Worktree commit failed, falling back to direct", exc_info=True)
            return "dry-run"
        finally:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_dir],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=self.workdir,
                )
            except Exception:
                pass

    def _prune_old(self) -> None:
        """Remove oldest verdict entries if over max_verdicts."""
        max_v = self.config.get("max_verdicts", 1000)
        if max_v <= 0:
            return  # no pruning

        current = self.count_verdicts()
        if current <= max_v:
            return

        # Collect all entries sorted oldest-first
        all_entries = self.list_verdicts(n=current + 1000)
        all_entries.reverse()  # oldest first

        to_remove = current - max_v
        removed = 0
        for entry in all_entries:
            if removed >= to_remove:
                break
            entry_dir = os.path.join(
                self.history_dir,
                entry.get("_date", ""),
                entry.get("_hash", ""),
            )
            if os.path.isdir(entry_dir):
                try:
                    shutil.rmtree(entry_dir)
                    removed += 1
                except OSError:
                    pass


# ── Report builder (shared between CLI and TUI) ────────────────


def build_report(workdir: str, n: int = 10) -> str:
    """Build a text report of recent verdicts."""
    persister = VerdictPersister(workdir)

    if not persister.enabled:
        return "History is disabled (history.enabled = false in config)."

    entries = persister.list_verdicts(n=n)
    if not entries:
        return "No verdict history found."

    total = len(entries)
    passed_count = sum(1 for e in entries if e.get("passed", False))
    fail_count = total - passed_count

    lines = []
    lines.append("═══ GitReins Verdict Report ═══")
    lines.append("")
    lines.append(f"Recent: {total} evaluations")
    lines.append(f"Pass:   {passed_count} ({_pct(passed_count, total)})")
    lines.append(f"Fail:   {fail_count} ({_pct(fail_count, total)})")
    lines.append("")

    for _i, entry in enumerate(entries):
        icon = "✓" if entry.get("passed") else "✗"
        task_id = entry.get("task_id", "?")
        date = entry.get("_date", "?")
        title = entry.get("task_title", task_id)
        verdict = entry.get("verdict", None)
        verdict_str = ""
        if verdict:
            verdict_str = verdict.verdict if hasattr(verdict, "verdict") else str(verdict)
            verdict_str = f" — {verdict_str}"

        items = entry.get("items", [])
        criteria_str = ""
        if items:
            item_statuses = []
            for item in items:
                if isinstance(item, dict):
                    s = "✓" if item.get("status") == "PASS" else "✗"
                else:
                    s = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                item_statuses.append(s)
            criteria_str = f" [{''.join(item_statuses)}]"

        lines.append(f"  {icon} {task_id:<24} {date}  {criteria_str}{verdict_str}")
        if title and title != task_id:
            lines.append(f"     {title}")

    lines.append("")
    lines.append(f"Storage: {persister.storage_mode} ({persister.history_dir})")
    lines.append(f"Total entries: {persister.count_verdicts()}")

    return "\n".join(lines)


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part * 100 // total}%"
