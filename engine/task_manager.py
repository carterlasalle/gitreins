"""
Task Manager — YAML-backed task lifecycle.

Tasks are stored in .gitreins/tasks.yaml inside the repo.
Format:

tasks:
  - id: "login-endpoint"
    title: "Implement POST /login endpoint"
    criteria:
      - "Accepts email+password as JSON body"
      - "Returns JWT token on success"
      - "Returns 401 on invalid credentials"
      - "Has tests for happy path and error cases"
    status: pending  # pending | in_progress | complete
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml


class DependencyError(Exception):
    """Raised when a task cannot complete because its dependencies are not met."""

    pass


@dataclass
class Task:
    id: str
    title: str
    criteria: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | complete
    created_at: str = ""
    completed_at: str | None = None
    depends_on: list[str] = field(default_factory=list)  # task IDs that must complete first


def intent_to_prompt(tasks: list[dict]) -> str:
    """Render task-intent dicts (from ``TaskManager.intent_context``) as a
    compact 'Task intent (developer criteria)' prompt block (DESIGN_v2.md §10).

    Each task renders as ``- [id] (status) title`` followed by its criteria as
    indented bullets. An empty input yields an empty string so callers can omit
    the section entirely.
    """
    if not tasks:
        return ""
    lines = ["Task intent (developer criteria):"]
    for t in tasks:
        lines.append(f"- [{t.get('id', '')}] ({t.get('status', '')}) {t.get('title', '')}")
        for c in t.get("criteria") or []:
            lines.append(f"    - {c}")
    return "\n".join(lines)


class TaskManager:
    """Manage tasks stored in .gitreins/tasks.yaml."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self._config_dir = os.path.join(self.workdir, ".gitreins")
        self._tasks_file = os.path.join(self._config_dir, "tasks.yaml")
        self._tasks: dict[str, Task] = {}
        self._load()

    # ── Intent-context consumption (DESIGN_v2.md §10) ────────────────

    def intent_context(self, statuses: list[str] | None = None) -> list[dict]:
        """Read-only intent-context projection of EXISTING tasks (§10).

        Returns a list of ``{id, title, criteria, status}`` dicts for the
        tasks already present in ``.gitreins/tasks.yaml``, optionally
        filtered by ``statuses``. This is a pure consumer API: it never
        creates tasks and never writes to ``tasks.yaml`` — intent is context
        the review consumes, not something v2.0 auto-creates.
        """
        tasks = self.all_tasks()
        if statuses:
            status_set = set(statuses)
            tasks = [t for t in tasks if t.status in status_set]
        return [
            {
                "id": t.id,
                "title": t.title,
                "criteria": list(t.criteria),
                "status": t.status,
            }
            for t in tasks
        ]

    def intent_prompt(self, statuses: list[str] | None = None) -> str:
        """Render existing tasks as a compact 'Task intent' prompt block.

        Read-only convenience: ``intent_to_prompt(self.intent_context(statuses))``.
        Returns an empty string when no tasks match, so callers can omit the
        section entirely.
        """
        return intent_to_prompt(self.intent_context(statuses))

    def _load(self) -> None:
        """Load tasks from YAML file."""
        if not os.path.exists(self._tasks_file):
            return
        try:
            with open(self._tasks_file, "r") as f:
                data = yaml.safe_load(f) or {}
            for item in data.get("tasks", []):
                task = Task(
                    id=item["id"],
                    title=item.get("title", ""),
                    criteria=item.get("criteria", []),
                    status=item.get("status", "pending"),
                    created_at=item.get("created_at", ""),
                    completed_at=item.get("completed_at"),
                    depends_on=item.get("depends_on", []),
                )
                self._tasks[task.id] = task
        except Exception as e:
            print(f"Warning: failed to load tasks: {e}")

    def _save(self) -> None:
        """Save tasks to YAML file."""
        os.makedirs(self._config_dir, exist_ok=True)
        tasks_list = []
        for task in self._tasks.values():
            entry: dict[str, Any] = {
                "id": task.id,
                "title": task.title,
                "criteria": task.criteria,
                "status": task.status,
                "created_at": task.created_at,
            }
            if task.completed_at:
                entry["completed_at"] = task.completed_at
            if task.depends_on:
                entry["depends_on"] = task.depends_on
            tasks_list.append(entry)
        with open(self._tasks_file, "w") as f:
            yaml.dump({"tasks": tasks_list}, f, default_flow_style=False, sort_keys=False)

    def create(
        self, id: str, title: str, criteria: list[str], depends_on: list[str] | None = None
    ) -> Task:
        """Create a new task. Optional depends_on lists task IDs that must complete first."""
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            id=id,
            title=title,
            criteria=criteria,
            status="pending",
            created_at=now,
            depends_on=depends_on or [],
        )
        self._tasks[id] = task
        self._save()
        return task

    def start(self, id: str) -> Task:
        """Mark a task as in progress."""
        task = self._tasks.get(id)
        if not task:
            raise KeyError(f"Task not found: {id}")
        task.status = "in_progress"
        self._save()
        return task

    def complete(self, id: str, force: bool = False) -> Task:
        """Mark a task as complete."""
        task = self._tasks.get(id)
        if not task:
            raise KeyError(f"Task not found: {id}")

        # Check dependencies (skip if forced)
        if not force:
            blocked = self.check_dependencies(id)
            if blocked:
                raise DependencyError(
                    f"Task '{id}' depends on incomplete tasks: {', '.join(blocked)}. "
                    f"Complete those first or use --force to skip."
                )

        task.status = "complete"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return task

    def check_dependencies(self, id: str) -> list[str]:
        """Return list of dependency task IDs that are not yet complete."""
        task = self._tasks.get(id)
        if not task:
            return []
        blocked = []
        for dep_id in task.depends_on:
            dep = self._tasks.get(dep_id)
            if not dep or dep.status != "complete":
                blocked.append(dep_id)
        return blocked

    def get(self, id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(id)

    def list_tasks(self, status: str | None = None) -> list["Task"]:
        """List tasks, optionally filtered by status."""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def all_tasks(self) -> list["Task"]:
        """Return all tasks."""
        return list(self._tasks.values())

    def delete(self, id: str) -> None:
        """Delete a task by ID."""
        if id not in self._tasks:
            raise KeyError(f"Task not found: {id}")
        del self._tasks[id]
        self._save()

    def to_dict(self, task: Task) -> dict:
        """Convert a Task to a plain dict (for MCP/serialization)."""
        result = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
            "status": task.status,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }
        if task.depends_on:
            result["depends_on"] = task.depends_on
        return result
