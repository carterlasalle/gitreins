"""
Unit tests for engine/task_manager.py — YAML-backed task CRUD and lifecycle.
axiom:trace work_item=GR-001 spec=specs/05-Task-Manager.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import os
import pytest
from datetime import datetime

from engine.task_manager import Task, TaskManager, intent_to_prompt


# ── Phase 1-1: Task CRUD Operations and Edge Cases ──────────────────────────


class TestTaskDataclass:
    """Test the Task dataclass basic construction."""

    def test_task_construction_with_defaults(self):
        """Task dataclass has correct defaults for optional fields."""
        task = Task(id="myid", title="My Task")
        assert task.id == "myid"
        assert task.title == "My Task"
        assert task.criteria == []
        assert task.status == "pending"
        assert task.created_at == ""
        assert task.completed_at is None

    def test_task_construction_with_all_fields(self):
        """Task dataclass accepts all fields explicitly."""
        task = Task(
            id="myid",
            title="My Task",
            criteria=["c1", "c2"],
            status="in_progress",
            created_at="2024-01-01T00:00:00+00:00",
            completed_at="2024-01-02T00:00:00+00:00",
        )
        assert task.criteria == ["c1", "c2"]
        assert task.status == "in_progress"
        assert task.created_at == "2024-01-01T00:00:00+00:00"
        assert task.completed_at == "2024-01-02T00:00:00+00:00"


class TestTaskManagerCreate:
    """Test TaskManager.create() — step-1-1-1-2."""

    def test_create_task_populates_all_fields(self, task_manager, sample_task_dict):
        """create() returns a Task with id, title, criteria, status='pending', ISO created_at."""
        task = task_manager.create(
            sample_task_dict["id"],
            sample_task_dict["title"],
            sample_task_dict["criteria"],
        )
        assert task.id == "test-task-1"
        assert task.title == "Implement login endpoint"
        assert task.criteria == [
            "Accepts email+password",
            "Returns JWT on success",
            "Returns 401 on failure",
        ]
        assert task.status == "pending"
        # created_at must be ISO format with timezone
        assert "T" in task.created_at
        assert "+" in task.created_at or "Z" in task.created_at
        assert task.completed_at is None

    def test_create_task_with_empty_criteria(self, task_manager):
        """create() with empty criteria list stores criteria=[]."""
        task = task_manager.create("empty-criteria", "No Criteria Task", [])
        assert task.criteria == []
        assert task.status == "pending"

    def test_create_task_persists_to_yaml(self, task_manager, sample_task_dict):
        """create() writes task to .gitreins/tasks.yaml."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        yaml_path = os.path.join(task_manager.workdir, ".gitreins", "tasks.yaml")
        assert os.path.exists(yaml_path), "tasks.yaml should exist after create"
        content = open(yaml_path).read()
        assert "test-task-1" in content
        assert "Implement login endpoint" in content

    def test_create_task_duplicate_overwrites(self, task_manager):
        """Duplicate ID overwrites previous task (last-write-wins per spec)."""
        task_manager.create("dup-id", "First Task", ["c1"])
        task_manager.create("dup-id", "Second Task", ["c2", "c3"])
        task = task_manager.get("dup-id")
        assert task.title == "Second Task"
        assert task.criteria == ["c2", "c3"]

    def test_create_then_get_from_new_manager(self, tmp_workdir):
        """Task survives persist + reload (new TaskManager instance)."""
        from engine.task_manager import TaskManager

        tm1 = TaskManager(tmp_workdir)
        tm1.create("persisted", "Persisted Task", ["c1", "c2"])
        tm2 = TaskManager(tmp_workdir)
        task = tm2.get("persisted")
        assert task is not None
        assert task.title == "Persisted Task"
        assert task.criteria == ["c1", "c2"]


class TestTaskManagerLifecycle:
    """Test Task start/complete transitions — step-1-1-1-3."""

    def test_start_changes_status_to_in_progress(self, task_manager, sample_task_dict):
        """start() changes status from 'pending' to 'in_progress'."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task = task_manager.start("test-task-1")
        assert task.status == "in_progress"

    def test_start_on_nonexistent_raises_keyerror(self, task_manager):
        """start() on nonexistent task raises KeyError."""
        with pytest.raises(KeyError, match="Task not found"):
            task_manager.start("nonexistent")

    def test_complete_sets_status_and_completed_at(self, task_manager, sample_task_dict):
        """complete() sets status='complete' and completed_at to ISO timestamp."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task_manager.start("test-task-1")
        task = task_manager.complete("test-task-1")
        assert task.status == "complete"
        assert task.completed_at is not None
        assert "T" in task.completed_at

    def test_complete_on_nonexistent_raises_keyerror(self, task_manager):
        """complete() on nonexistent task raises KeyError."""
        with pytest.raises(KeyError, match="Task not found"):
            task_manager.complete("nonexistent")

    def test_start_then_complete_persisted(self, task_manager, sample_task_dict):
        """Status transitions are persisted to YAML."""
        from engine.task_manager import TaskManager

        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task_manager.start("test-task-1")
        task_manager.complete("test-task-1")

        tm2 = TaskManager(task_manager.workdir)
        task = tm2.get("test-task-1")
        assert task.status == "complete"
        assert task.completed_at is not None


class TestTaskManagerList:
    """Test list_tasks() and get() — step-1-1-1-4."""

    def test_list_tasks_with_status_filter(self, task_manager):
        """list_tasks('pending') returns only pending tasks."""
        task_manager.create("t1", "Task 1", [])
        task_manager.create("t2", "Task 2", [])
        task_manager.create("t3", "Task 3", [])
        task_manager.start("t2")
        task_manager.start("t3")
        task_manager.complete("t3")

        pending = task_manager.list_tasks("pending")
        assert len(pending) == 1
        assert pending[0].id == "t1"

        # t3 is "complete", t2 is "in_progress"
        in_prog = task_manager.list_tasks("in_progress")
        assert len(in_prog) == 1
        assert in_prog[0].id == "t2"

    def test_list_tasks_none_returns_all(self, task_manager):
        """list_tasks(None) returns all tasks regardless of status."""
        task_manager.create("a", "A", [])
        task_manager.create("b", "B", [])
        all_tasks = task_manager.list_tasks(None)
        assert len(all_tasks) == 2

    def test_all_tasks_returns_all(self, task_manager):
        """all_tasks() returns complete list."""
        task_manager.create("x", "X", [])
        task_manager.create("y", "Y", [])
        assert len(task_manager.all_tasks()) == 2

    def test_get_existing_returns_task(self, task_manager, sample_task_dict):
        """get() returns the Task object for an existing ID."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task = task_manager.get("test-task-1")
        assert task is not None
        assert task.title == "Implement login endpoint"

    def test_get_nonexistent_returns_none(self, task_manager):
        """get() returns None for a nonexistent ID."""
        assert task_manager.get("nonexistent") is None


class TestTaskManagerDelete:
    """Test delete and to_dict — step-1-1-1-5."""

    def test_delete_existing_removes_from_index(self, task_manager, sample_task_dict):
        """delete() removes task from internal dict and get() returns None."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task_manager.delete("test-task-1")
        assert task_manager.get("test-task-1") is None

    def test_delete_nonexistent_raises_keyerror(self, task_manager):
        """delete() on nonexistent task raises KeyError."""
        with pytest.raises(KeyError, match="Task not found"):
            task_manager.delete("nonexistent")

    def test_delete_persisted(self, task_manager, sample_task_dict):
        """Delete is persisted so a new TaskManager doesn't see the task."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task_manager.delete("test-task-1")
        from engine.task_manager import TaskManager

        tm2 = TaskManager(task_manager.workdir)
        assert tm2.get("test-task-1") is None

    def test_to_dict_all_keys_present(self, task_manager, sample_task_dict):
        """to_dict() returns all 6 keys: id, title, criteria, status, created_at, completed_at."""
        task = task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        d = task_manager.to_dict(task)
        assert set(d.keys()) == {"id", "title", "criteria", "status", "created_at", "completed_at"}
        assert d["id"] == "test-task-1"
        assert d["completed_at"] is None

    def test_to_dict_after_complete_includes_completed_at(self, task_manager, sample_task_dict):
        """to_dict() includes completed_at after task is completed."""
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        task_manager.start("test-task-1")
        task = task_manager.complete("test-task-1")
        d = task_manager.to_dict(task)
        assert d["completed_at"] is not None


class TestTaskManagerEdgeCases:
    """Additional edge case coverage."""

    def test_constructor_with_default_workdir(self, task_manager):
        """TaskManager() with default '.' initializes without error."""
        assert task_manager._tasks is not None

    def test_load_corrupt_yaml(self, tmp_workdir):
        """TaskManager._load handles corrupt YAML gracefully."""
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        # Write invalid YAML
        with open(os.path.join(config_dir, "tasks.yaml"), "w") as f:
            f.write("{invalid: yaml: :broken")
        tm = TaskManager(tmp_workdir)
        # Should not crash; _load catches exceptions
        assert len(tm.all_tasks()) == 0

    def test_save_creates_config_dir_if_missing(self, task_manager, sample_task_dict):
        """_save() creates .gitreins/ directory if it doesn't exist."""
        # Delete the config dir
        import shutil

        shutil.rmtree(os.path.join(task_manager.workdir, ".gitreins"), ignore_errors=True)
        task_manager.create(
            sample_task_dict["id"], sample_task_dict["title"], sample_task_dict["criteria"]
        )
        assert os.path.exists(os.path.join(task_manager.workdir, ".gitreins", "tasks.yaml"))


class TestTaskManagerExtendedEdgeCases:
    """Additional edge cases beyond initial coverage."""

    def test_special_chars_in_title(self, task_manager):
        """create() accepts special characters in title."""
        special = 'Task with $pecial !@#$%^&*() chars "quoted" and <tags>'
        task = task_manager.create("special-title", special, ["c1"])
        assert task.title == special
        assert task.status == "pending"

    def test_created_at_is_close_to_now(self, task_manager, sample_task_dict):
        """created_at timestamp is within 5 seconds of task creation."""
        from datetime import timezone

        before = datetime.now(timezone.utc)
        task = task_manager.create(
            sample_task_dict["id"],
            sample_task_dict["title"],
            sample_task_dict["criteria"],
        )
        after = datetime.now(timezone.utc)
        created = datetime.fromisoformat(task.created_at)
        assert before <= created <= after

    def test_empty_title_accepted(self, task_manager):
        """create() with empty title string is accepted."""
        task = task_manager.create("empty-title", "", ["c1"])
        assert task.title == ""
        assert task.id == "empty-title"

    def test_load_empty_yaml_graceful(self, tmp_workdir):
        """TaskManager._load handles empty YAML dict gracefully."""
        import yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "tasks.yaml"), "w") as f:
            yaml.dump({}, f)
        tm = TaskManager(tmp_workdir)
        assert len(tm.all_tasks()) == 0

    def test_load_yaml_missing_task_id(self, tmp_workdir):
        """TaskManager._load gracefully skips task entries missing 'id'."""
        import yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "tasks.yaml"), "w") as f:
            yaml.dump({"tasks": [{"title": "No ID task", "status": "pending"}]}, f)
        tm = TaskManager(tmp_workdir)
        assert len(tm.all_tasks()) == 0

    def test_list_tasks_status_no_match(self, task_manager):
        """list_tasks('complete') returns empty list when none are complete."""
        task_manager.create("t1", "Task 1", [])
        result = task_manager.list_tasks("complete")
        assert result == []

    def test_all_tasks_returns_copies(self, task_manager):
        """all_tasks() returns a new list each time (mutation safe)."""
        task_manager.create("t1", "Task 1", [])
        lst1 = task_manager.all_tasks()
        lst2 = task_manager.all_tasks()
        assert lst1 is not lst2  # different list objects
        assert len(lst1) == len(lst2) == 1


# ── R2.12: Intent-context consumption (DESIGN_v2.md §10) ────────────────────


class TestTaskIntentContext:
    """Read-only intent-context API — consumes existing tasks, never writes."""

    def _write_fixture(self, workdir, tasks):
        """Write a tasks.yaml fixture (same style as TaskManager._save)."""
        import yaml

        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "tasks.yaml"), "w") as f:
            yaml.dump({"tasks": tasks}, f, default_flow_style=False, sort_keys=False)

    def test_intent_context_returns_expected_dicts(self, tmp_workdir):
        """intent_context() projects existing tasks as {id,title,criteria,status}."""
        self._write_fixture(
            str(tmp_workdir),
            [
                {
                    "id": "login-endpoint",
                    "title": "Implement POST /login endpoint",
                    "criteria": [
                        "Accepts email+password as JSON body",
                        "Returns JWT token on success",
                    ],
                    "status": "in_progress",
                },
                {
                    "id": "logout",
                    "title": "Implement POST /logout",
                    "criteria": ["Invalidates session"],
                    "status": "pending",
                },
            ],
        )
        tm = TaskManager(str(tmp_workdir))
        assert tm.intent_context() == [
            {
                "id": "login-endpoint",
                "title": "Implement POST /login endpoint",
                "criteria": [
                    "Accepts email+password as JSON body",
                    "Returns JWT token on success",
                ],
                "status": "in_progress",
            },
            {
                "id": "logout",
                "title": "Implement POST /logout",
                "criteria": ["Invalidates session"],
                "status": "pending",
            },
        ]

    def test_intent_context_status_filter(self, tmp_workdir):
        """intent_context(statuses=[...]) keeps only matching statuses."""
        self._write_fixture(
            str(tmp_workdir),
            [
                {"id": "a", "title": "A", "criteria": [], "status": "pending"},
                {"id": "b", "title": "B", "criteria": [], "status": "in_progress"},
                {"id": "c", "title": "C", "criteria": [], "status": "complete"},
            ],
        )
        tm = TaskManager(str(tmp_workdir))
        assert [t["id"] for t in tm.intent_context(statuses=["in_progress"])] == ["b"]
        assert [t["id"] for t in tm.intent_context(statuses=["pending", "complete"])] == [
            "a",
            "c",
        ]
        assert tm.intent_context(statuses=["complete"])[0]["status"] == "complete"

    def test_intent_prompt_renders_id_title_criteria(self, tmp_workdir):
        """intent_prompt() renders a block containing id, title, criteria."""
        self._write_fixture(
            str(tmp_workdir),
            [
                {
                    "id": "login-endpoint",
                    "title": "Implement POST /login endpoint",
                    "criteria": [
                        "Accepts email+password as JSON body",
                        "Returns 401 on invalid credentials",
                    ],
                    "status": "in_progress",
                },
            ],
        )
        prompt = TaskManager(str(tmp_workdir)).intent_prompt()
        assert "Task intent (developer criteria):" in prompt
        assert "login-endpoint" in prompt
        assert "in_progress" in prompt
        assert "Implement POST /login endpoint" in prompt
        assert "Accepts email+password as JSON body" in prompt
        assert "Returns 401 on invalid credentials" in prompt

    def test_intent_context_read_only_no_file_write(self, tmp_workdir):
        """intent_context()/intent_prompt() never write to tasks.yaml."""
        self._write_fixture(
            str(tmp_workdir),
            [
                {
                    "id": "login-endpoint",
                    "title": "Login",
                    "criteria": ["Accepts email"],
                    "status": "pending",
                },
            ],
        )
        path = os.path.join(str(tmp_workdir), ".gitreins", "tasks.yaml")
        before_content = open(path).read()
        before_mtime = os.path.getmtime(path)

        tm = TaskManager(str(tmp_workdir))
        assert tm.intent_context()[0]["id"] == "login-endpoint"
        assert "Accepts email" in tm.intent_prompt()

        assert open(path).read() == before_content  # no content drift
        assert os.path.getmtime(path) == before_mtime  # no rewrite

    def test_intent_context_missing_tasks_file(self, tmp_path):
        """No tasks.yaml → empty intent, and no .gitreins dir is created."""
        workdir = str(tmp_path / "bare")
        os.makedirs(workdir, exist_ok=True)
        tm = TaskManager(workdir)
        assert tm.intent_context() == []
        assert tm.intent_prompt() == ""
        assert not os.path.exists(os.path.join(workdir, ".gitreins"))

    def test_intent_to_prompt_empty(self):
        """intent_to_prompt([]) → empty string (callers omit the section)."""
        assert intent_to_prompt([]) == ""

    def test_intent_to_prompt_multi_task(self):
        """Multiple tasks render as separate bullet entries with criteria."""
        prompt = intent_to_prompt(
            [
                {"id": "a", "title": "A", "criteria": ["c1"], "status": "pending"},
                {"id": "b", "title": "B", "criteria": ["c2", "c3"], "status": "complete"},
            ]
        )
        assert "- [a] (pending) A" in prompt
        assert "- [b] (complete) B" in prompt
        assert prompt.index("c1") < prompt.index("- [b]") < prompt.index("c2")
