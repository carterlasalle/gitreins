"""
MCP Server — stdio transport exposing task.* and commit() tools.

Primary AI coding agents (Pi, Claude, Hermes, Codex) connect via stdio
and use these tools to manage tasks and commit code through the harness.

Implements proper JSON-RPC 2.0 framing over line-delimited stdio.
Messages can span multiple lines — we buffer until we have complete JSON.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time

from engine.task_manager import TaskManager
from engine.judge import Judge, judge_result_to_dict
from engine.llm import LLMClient
from engine.guard_manager import GuardManager
from engine.propagate import PolicyPropagator
from engine.job_store import (
    cap_from_dict,
    cap_to_dict,
    load_job,
    make_job,
    pid_alive,
    save_job,
)

# MCP_NOISE_FIX: suppress debug spam from mcp package
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)

logger = logging.getLogger("gitreins.mcp")


class GitReinsMCPServer:
    """MCP server that primary agents connect to via stdio."""

    def __init__(self, workdir: str = "."):
        self.workdir = os.path.abspath(workdir)
        self.tasks = TaskManager(workdir)
        self.llm = LLMClient()
        self.judge = Judge(self.llm, workdir)
        self._initialized = False

        # Runtime-configurable state (hot-reloaded via configure tool)
        self._configured = False

        # Background evaluation jobs (DF-003): judge.evaluate / task.complete
        # dispatch the full pipeline here so tool calls return well under the
        # ~300s client-side cap. Job record shape:
        #   {"id", "status": running|complete|error, "task_id", "workdir",
        #    "result": dict|None, "error": str|None, "started_at": float}
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        # Serializes the actual evaluate_task call — evaluation jobs run ONE
        # at a time per server instance (parallel judges contend on
        # ports/tmp and on the shared .gitreins/history git storage).
        self._eval_lock = threading.Lock()

        self._tools = {
            "configure": self._configure,
            "task.create": self._task_create,
            "task.start": self._task_start,
            "task.complete": self._task_complete,
            "task.list": self._task_list,
            "task.get": self._task_get,
            "task.delete": self._task_delete,
            "commit": self._commit,
            "guard.run": self._guard_run,
            "judge.evaluate": self._judge_evaluate,
            "judge.status": self._judge_status,
            "propagate": self._propagate,
        }

    def _tool_schemas(self) -> list[dict]:
        return [
            {
                "name": "configure",
                "description": "Hot-reload the MCP server's LLM configuration at runtime. Sets environment variables and recreates the LLM client so subsequent tool calls (judge.evaluate, task.complete) use the new config. Works with any MCP client — no config file editing or server restart needed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "env": {
                            "type": "object",
                            "description": 'Dict of environment variables to set (e.g. {"DEEPSEEK_API_KEY": "sk-xxx", "OPENROUTER_API_KEY": "sk-xxx"}). These are pushed into os.environ so the LLM client picks them up on next init.',
                            "additionalProperties": {"type": "string"},
                        },
                        "model": {
                            "type": "string",
                            "description": "Override the default model (e.g. 'deepseek-v4-flash'). Sets GITREINS_LLM_MODEL.",
                        },
                        "base_url": {
                            "type": "string",
                            "description": "Override the API base URL (e.g. 'https://api.deepseek.com/v1'). Sets GITREINS_LLM_BASE_URL.",
                        },
                        "provider": {
                            "type": "string",
                            "enum": ["openai", "anthropic"],
                            "description": "Override provider detection. Sets GITREINS_LLM_PROVIDER.",
                        },
                    },
                },
            },
            {
                "name": "task.create",
                "description": "Create a new task with criteria that must be met before commit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique task ID (e.g., 'login-endpoint')",
                        },
                        "title": {"type": "string", "description": "Human-readable title"},
                        "criteria": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of completion criteria — each must be verified",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo. Tasks are stored in <workdir>/.gitreins/tasks.yaml. Defaults to the MCP server's workdir.",
                        },
                    },
                    "required": ["id", "title", "criteria"],
                },
            },
            {
                "name": "task.start",
                "description": "Mark a task as in-progress.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to start"},
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir.",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.complete",
                "description": "Mark a task as complete. Triggers evaluation if LLM is configured.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to complete"},
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir.",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.list",
                "description": "List all tasks, optionally filtered by status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "complete"],
                            "description": "Filter by status",
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo. Defaults to the MCP server's workdir.",
                        },
                    },
                },
            },
            {
                "name": "task.get",
                "description": "Get a task by ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID"},
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir.",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "task.delete",
                "description": "Delete a task by ID.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to delete"},
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir.",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "commit",
                "description": "Create a git commit. Runs guards first. Rejects if guards fail.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Commit message"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "guard.run",
                "description": "Run Tier 1 static guards (secrets, lint, tests). Optional dead_code for dead-code detection. Optional workdir for cross-repo use.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo to guard. Defaults to the MCP server's workdir.",
                        },
                        "dead_code": {
                            "type": "boolean",
                            "description": "Enable dead-code detection (Python AST-based). Overrides config.",
                            "default": False,
                        },
                    },
                },
            },
            {
                "name": "judge.evaluate",
                "description": "Run full evaluation pipeline (Tier 1 + Tier 2) on a task. By default the evaluation runs in a background job and the call returns immediately with a job_id — poll judge.status for the result. Pass wait=true for the legacy synchronous behavior (block until done, return the full result). Caps can be set individually or via legacy eval_cap string.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID to evaluate"},
                        "workdir": {
                            "type": "string",
                            "description": "Absolute path to the repo containing the task. Defaults to the MCP server's workdir.",
                        },
                        "wait": {
                            "type": "boolean",
                            "description": "If true, block until the evaluation finishes and return the full result dict (legacy synchronous behavior). If false (default), dispatch a background job and return immediately — poll judge.status with the returned job_id.",
                            "default": False,
                        },
                        "max_iterations": {
                            "type": "number",
                            "description": "Max LLM reasoning turns (-1 = unlimited). Tool calls cost 0.1 by default.",
                        },
                        "max_time": {
                            "type": "string",
                            "description": "Wall-clock cap: '30s', '5m', '2h'.",
                        },
                        "max_input_tokens": {
                            "type": "string",
                            "description": "Input token budget: '200k', '0.1M'.",
                        },
                        "max_output_tokens": {
                            "type": "string",
                            "description": "Output token budget: '50k', '0.05M'.",
                        },
                        "tool_call_weight": {
                            "type": "number",
                            "description": "Fraction of an iteration each tool call costs (default 0.1).",
                        },
                        "eval_cap": {
                            "type": "string",
                            "description": "Legacy combined cap string: '100/30m/200k/50k'. Individual params take priority if both are set.",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "judge.status",
                "description": "Poll the status of a background evaluation job started by judge.evaluate (async) or task.complete (with LLM configured). Returns 'running', 'complete' (with the full result dict: task_id/passed/workdir/tier1_passed/verdict/items/summary), or 'error'. Jobs are disk-backed: they survive MCP server restarts and CLI 'gitreins judge --async' dispatches are visible here; a 'running' job whose process died is resumed automatically on poll.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job ID returned by judge.evaluate or task.complete",
                        },
                    },
                    "required": ["job_id"],
                },
            },
            {
                "name": "propagate",
                "description": "Propagate guard configuration to sibling repos.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Source repo path. Defaults to server workdir.",
                        },
                        "targets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of target repo paths to propagate config to",
                        },
                    },
                    "required": ["targets"],
                },
            },
        ]

    # ── Tool handlers ────────────────────────────────────────────

    def _configure(
        self,
        env: dict[str, str] | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """Hot-reload the MCP server's LLM configuration at runtime.

        Sets environment variables and recreates the LLM client and Judge
        so all subsequent tool calls (judge.evaluate, task.complete) use
        the new config. Works with any MCP client — Hermes, OpenCode,
        Claude Code, etc.

        Args:
            env: Dict of environment variables to set (e.g.
                 {"DEEPSEEK_API_KEY": "sk-xxx", "OPENROUTER_API_KEY": "sk-xxx"}).
                 These are pushed into os.environ so LLMClient picks them up.
            model: Override the default model (e.g. "deepseek-v4-flash").
            base_url: Override the API base URL (e.g. "https://api.deepseek.com/v1").
            provider: Override provider detection ("openai" or "anthropic").

        Returns current config state after applying changes.
        """
        old_config = self._config_snapshot()

        # Apply env vars
        if env:
            for key, value in env.items():
                os.environ[key] = value
                logger.info("configure: set env %s", key)

        # Push model/base_url/provider as GITREINS_LLM_* env vars
        if model:
            os.environ["GITREINS_LLM_MODEL"] = model
            logger.info("configure: model=%s", model)
        if base_url:
            os.environ["GITREINS_LLM_BASE_URL"] = base_url
            logger.info("configure: base_url=%s", base_url)
        if provider:
            os.environ["GITREINS_LLM_PROVIDER"] = provider
            logger.info("configure: provider=%s", provider)

        # Recreate LLM client and Judge with new env
        self.llm = LLMClient()
        self.judge = Judge(self.llm, self.workdir)
        self._configured = True

        new_config = self._config_snapshot()
        return {
            "configured": True,
            "previous": old_config,
            "current": new_config,
            "note": "LLM client and Judge recreated — all subsequent tool calls use new config.",
        }

    def _config_snapshot(self) -> dict:
        """Return current LLM config state for reporting."""
        return {
            "model": self.llm.model,
            "provider": self.llm.provider,
            "api_key_configured": bool(self.llm.api_key),
            "api_key_prefix": (self.llm.api_key[:10] + "...") if self.llm.api_key else None,
            "base_url": self.llm._chat_url,
            "env_keys": sorted(k for k in os.environ if "API_KEY" in k or "LLM" in k),
        }

    def _task_manager_for(self, workdir: str | None = None) -> TaskManager:
        """Return TaskManager for the given workdir, or the server default."""
        if workdir:
            wd = os.path.abspath(workdir)
            if wd != self.workdir:
                return TaskManager(wd)
        return self.tasks

    def _task_create(
        self, id: str, title: str, criteria: list[str], workdir: str | None = None
    ) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.create(id, title, criteria)
        logger.info("Task created: %s (workdir=%s)", id, tm.workdir)
        return tm.to_dict(task)

    def _task_start(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.start(id)
        logger.info("Task started: %s (workdir=%s)", id, tm.workdir)
        return tm.to_dict(task)

    def _task_complete(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.complete(id)
        logger.info("Task completed: %s (workdir=%s)", id, tm.workdir)

        # Trigger evaluation if LLM is configured — dispatched as a background
        # job so the tool call returns immediately (MCP clients cap tool-call
        # duration at ~300s; a full evaluation takes ~14 min). Evaluation
        # errors land in the job record, not in this response.
        api_key = os.getenv("GITREINS_LLM_API_KEY", "")
        if api_key:
            job_id = self._submit_eval_job(id, tm.workdir, task)
            return {
                "task": tm.to_dict(task),
                "job_id": job_id,
                "status": "running",
                "note": "evaluation running in background — poll judge.status",
            }

        return {"task": tm.to_dict(task), "note": "LLM not configured — skipping evaluation"}

    def _task_list(self, status: str | None = None, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        tasks = tm.list_tasks(status)
        return {"tasks": [tm.to_dict(t) for t in tasks]}

    def _task_get(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        task = tm.get(id)
        if not task:
            return {"error": f"Task not found: {id}"}
        return tm.to_dict(task)

    def _task_delete(self, id: str, workdir: str | None = None) -> dict:
        tm = self._task_manager_for(workdir)
        try:
            tm.delete(id)
            logger.info("Task deleted: %s (workdir=%s)", id, tm.workdir)
            return {"deleted": id}
        except KeyError:
            return {"error": f"Task not found: {id}"}

    def _commit(self, message: str) -> dict:
        """Commit staged changes; blocked while any task is in_progress."""
        # Check all in-progress tasks first
        in_progress = self.tasks.list_tasks("in_progress")
        if in_progress:
            ids = ", ".join(t.id for t in in_progress)
            return {
                "error": (
                    f"Tasks still in progress: {ids} — commits are blocked while "
                    "a task is in_progress because task.complete runs the quality "
                    "judge against the committed state. Complete them via "
                    "task.complete, or delete them via task.delete, then retry "
                    "commit."
                ),
                "tasks": [t.id for t in in_progress],
            }

        # Run guards after task check
        tier1 = self.judge.guard_manager.run_all()
        if not tier1.passed:
            return {
                "error": "Tier 1 guards failed — commit blocked",
                "details": tier1.summary,
            }

        try:
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workdir,
            )
            return {
                "committed": result.returncode == 0,
                "output": result.stdout + result.stderr,
            }
        except Exception as e:
            return {"error": str(e)}

    def _guard_run(self, workdir: str = None, dead_code: bool = False) -> dict:
        """Run Tier 1 static guards. Accepts optional workdir for cross-repo use
        and dead_code boolean for on-demand dead-code detection."""
        import yaml

        wd = os.path.abspath(workdir) if workdir else self.workdir
        # Load config from .gitreins/config.yaml (same pattern as CLI)
        config: dict[str, object] = {}
        config_path = os.path.join(wd, ".gitreins", "config.yaml")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r") as f:
                    config = yaml.safe_load(f) or {}
            except Exception:
                pass
        gm = GuardManager(wd, config=config)
        result = gm.run_all(force_dead_code=dead_code)
        return {
            "passed": result.passed,
            "workdir": wd,
            "results": [
                {"name": r.name, "passed": r.passed, "output": r.output[:500]}
                for r in result.results
            ],
        }

    def _judge_evaluate(
        self,
        id: str,
        workdir: str | None = None,
        max_iterations: float | None = None,
        max_time: str | None = None,
        max_input_tokens: str | None = None,
        max_output_tokens: str | None = None,
        tool_call_weight: float | None = None,
        eval_cap: str | None = None,
        wait: bool = False,
    ) -> dict:
        """Run the full evaluation pipeline on a task.

        By default (``wait=False``) the evaluation is dispatched to a
        background job and this returns immediately with
        ``{"job_id": ..., "status": "running", ...}`` — poll
        ``judge.status`` for the result. With ``wait=True`` the call
        blocks until the evaluation finishes and returns the full result
        dict (legacy synchronous behavior, unchanged shape). Accepts
        individual cap params or the legacy eval_cap string.
        """
        from engine.eval_cap import EvalCap

        # Skip LLM evaluation if no API key configured (avoid hanging in tests)
        if not self.llm.api_key:
            return {"error": "LLM not configured — set GITREINS_LLM_API_KEY"}

        # Build EvalCap from params
        cap = EvalCap()
        if eval_cap:
            from engine.eval_cap import parse_eval_cap

            cap = parse_eval_cap(eval_cap)
        if max_iterations is not None:
            cap.max_iterations = -1.0 if max_iterations <= 0 else float(max_iterations)
        if max_time is not None:
            from engine.eval_cap import _parse_time

            t = _parse_time(max_time)
            if t is not None:
                cap.max_seconds = float(t)
        if max_input_tokens is not None:
            from engine.eval_cap import _parse_tokens

            tok = _parse_tokens(max_input_tokens)
            if tok is not None:
                cap.max_input_tokens = tok
        if max_output_tokens is not None:
            from engine.eval_cap import _parse_tokens

            tok = _parse_tokens(max_output_tokens)
            if tok is not None:
                cap.max_output_tokens = tok
        if tool_call_weight is not None:
            cap.tool_call_weight = float(tool_call_weight)

        # Resolve the task up front (fast) so not-found errors are returned
        # immediately instead of surfacing from a background job.
        wd = os.path.abspath(workdir) if workdir else self.workdir
        if wd != self.workdir:
            tm = TaskManager(wd)
            task = tm.get(id)
            if not task:
                return {"error": f"Task not found: {id} in {wd}"}
        else:
            task = self.tasks.get(id)
            if not task:
                return {"error": f"Task not found: {id}"}

        # Mirror the legacy judge construction: the shared self.judge
        # (eval_cap=None → config-driven caps) is only equivalent when
        # running on the server workdir with no explicit cap params.
        # Jobs always build a FRESH Judge from the captured eval_cap —
        # the shared self.judge is never touched from a worker thread.
        has_cap_params = any(
            [
                max_iterations is not None,
                max_time,
                max_input_tokens,
                max_output_tokens,
                tool_call_weight,
                eval_cap,
            ]
        )
        job_cap = cap if (wd != self.workdir or has_cap_params) else None

        if wait:
            j = (
                Judge(self.llm, wd, eval_cap=job_cap)
                if wd != self.workdir or has_cap_params
                else self.judge
            )
            result = j.evaluate_task(task)
            return self._judge_result_dict(id, wd, result)

        # Async path: dispatch a background job and return immediately.
        job_id = self._submit_eval_job(id, wd, task, eval_cap=job_cap)
        return {
            "job_id": job_id,
            "status": "running",
            "task_id": id,
            "workdir": wd,
        }

    def _judge_result_dict(self, task_id: str, wd: str, result) -> dict:
        """Build the standard judge.evaluate result dict from a JudgeResult."""
        return judge_result_to_dict(task_id, wd, result)

    def _submit_eval_job(self, task_id: str, wd: str, task, eval_cap=None) -> str:
        """Register and start a background evaluation job.

        Evaluation jobs run ONE at a time per server instance: a lock
        serializes the actual ``evaluate_task`` call so concurrent judges
        can't contend on ports/tmp (LSP integration tests run real
        servers) or on the shared ``.gitreins/history`` git storage. A
        fresh ``Judge`` is built inside the job from the captured params.

        Jobs are persisted to the shared disk job store (DF-006) so they
        survive server restarts; an orphaned ``running`` job is resumed
        automatically on the next ``judge.status`` poll.

        Returns the new job id; the caller returns immediately while the
        job runs. Poll ``judge.status`` with the job id for the result.
        """
        job = make_job(task_id, wd, caps=cap_to_dict(eval_cap))
        job["pid"] = os.getpid()
        job_id = job["id"]
        with self._jobs_lock:
            self._jobs[job_id] = job
        save_job(job)

        self._start_job_thread(job, wd, task, eval_cap)
        return job_id

    def _start_job_thread(self, job: dict, wd: str, task, eval_cap=None) -> None:
        """Spawn the worker thread for a job record (fresh or resumed)."""

        def _finish(update: dict) -> None:
            # Disk-before-memory ordering: persist the terminal state FIRST so
            # a fresh server reading the job store sees complete/error even
            # while this instance's memory record still says running. A
            # memory-complete read must never precede the disk write — the
            # disk record is the only state a restarted server can see, and
            # the test test_completed_job_survives_server_restart asserts the
            # fresh server sees "complete" on its FIRST disk read.
            done = dict(job)
            done.update(update)
            try:
                save_job(done)
            except OSError as e:
                logger.error("Failed to persist job %s terminal state: %s", job["id"], e)
            with self._jobs_lock:
                job.update(update)

        def _run_job() -> None:
            try:
                j = Judge(self.llm, wd, eval_cap=eval_cap)
                with self._eval_lock:
                    result = j.evaluate_task(task)
                d = self._judge_result_dict(job["task_id"], wd, result)
                _finish(
                    {
                        "result": d,
                        "status": "complete",
                        "finished_at": time.time(),
                    }
                )
            except Exception as e:
                logger.exception("Background evaluation job %s failed", job["id"])
                _finish(
                    {
                        "status": "error",
                        "error": str(e),
                        "finished_at": time.time(),
                    }
                )

        threading.Thread(target=_run_job, name=f"judge-job-{job['id']}", daemon=True).start()

    def _load_or_resume_disk_job(self, job_id: str) -> dict | None:
        """Load a job from the shared disk store, resuming it if orphaned.

        Jobs found in ``running`` state whose owning process is gone
        (server restarted, CLI worker died) are re-dispatched as a fresh
        worker thread in this server instance — the job is remembered and
        keeps running. Polls during the resume return ``running`` until
        the fresh evaluation finishes.
        """
        job = load_job(job_id)
        if job is None:
            return None
        if job.get("status") == "running" and not pid_alive(job.get("pid")):
            job = self._resume_disk_job(job)
        elif job.get("status") == "running" and job.get("pid") == os.getpid():
            # Record written by this instance but evicted from memory
            # (should not happen — keep memory consistent regardless).
            with self._jobs_lock:
                self._jobs[job_id] = job
        with self._jobs_lock:
            if job.get("status") == "running":
                self._jobs[job_id] = job
        return job

    def _resume_disk_job(self, job: dict) -> dict:
        """Re-dispatch an orphaned running job in this server instance."""
        wd = job.get("workdir") or self.workdir
        task = TaskManager(wd).get(job.get("task_id", ""))
        if task is None:
            job["status"] = "error"
            job["error"] = (
                f"task {job.get('task_id')} no longer exists in {wd} — job could not be resumed"
            )
            job["finished_at"] = time.time()
            save_job(job)
            return job
        logger.info(
            "Resuming orphaned job %s (task=%s workdir=%s)",
            job["id"],
            job["task_id"],
            wd,
        )
        job["pid"] = os.getpid()
        job["resumed_at"] = time.time()
        save_job(job)
        self._start_job_thread(job, wd, task, eval_cap=cap_from_dict(job.get("caps")))
        return job

    def _judge_status(self, job_id: str) -> dict:
        """Return the status of a background evaluation job.

        Memory first, then the shared disk store — so jobs survive MCP
        server restarts and jobs started by ``gitreins judge --async``
        (CLI) are visible here too. A ``running`` job whose process died
        is resumed automatically.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            job = self._load_or_resume_disk_job(job_id)
            if job is None:
                return {"error": f"Job not found: {job_id}"}
        d = {
            "job_id": job["id"],
            "status": job["status"],
            "task_id": job["task_id"],
            "workdir": job["workdir"],
            "result": job.get("result"),
            "error": job.get("error"),
        }
        if job["status"] == "running":
            d["pid"] = job.get("pid")
            d["started_at"] = job.get("started_at")
        return d

    def _propagate(self, source: str | None = None, targets: list[str] | None = None) -> dict:
        """Propagate guard-policy configuration to sibling repos.

        Args:
            source: Source repo path. Defaults to server workdir.
            targets: List of target repo paths to propagate policy config to.

        Returns:
            Dict with ``source`` path and ``results`` list.
        """
        if not targets:
            return {"error": "targets list is required"}
        src = os.path.abspath(source) if source else self.workdir
        propagator = PolicyPropagator(src)
        return propagator.propagate_policy(targets)

    def handle_request(self, request: dict) -> dict | None:
        """Handle a single MCP JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        req_id = request.get("id")

        # Validate jsonrpc field per JSON-RPC 2.0 spec
        if request.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: jsonrpc field must be '2.0'",
                },
            }

        try:
            if method == "initialize":
                self._initialized = True
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "gitreins", "version": "0.1.0"},
                    },
                }
            elif method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": self._tool_schemas()},
                }
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {}) or {}
                handler: object = self._tools.get(tool_name)
                if not callable(handler):
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    }
                result = handler(**tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
                }
            elif method == "notifications/initialized":
                return None  # No response for notifications
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                }
        except Exception as e:
            logger.exception("Error handling request: %s", method)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(e)},
            }

    def run_stdio(self) -> None:
        """Run the MCP server over line-delimited JSON stdio.

        Reads JSON messages — each message is terminated by a newline.
        Multi-line JSON is handled by buffering until a complete JSON
        object can be parsed (balanced braces).
        """
        buffer = ""
        for line in sys.stdin:
            buffer += line

            # Try to parse complete JSON objects from the buffer
            while buffer.strip():
                try:
                    # Try to parse the entire buffer as JSON
                    request = json.loads(buffer)
                    buffer = ""
                    response = self.handle_request(request)
                    if response is not None:
                        self._write_response(response)
                    break
                except json.JSONDecodeError:
                    # Not complete JSON yet — try to find a complete object
                    # Count braces to find the first complete JSON object
                    depth = 0
                    in_string = False
                    escape = False
                    first_brace = -1
                    split_at = -1
                    for i, ch in enumerate(buffer):
                        if escape:
                            escape = False
                            continue
                        if ch == "\\":
                            escape = True
                            continue
                        if ch == '"' and not escape:
                            in_string = not in_string
                            continue
                        if in_string:
                            continue
                        if ch in "{[":
                            if depth == 0:
                                first_brace = i
                            depth += 1
                        elif ch in "}]":
                            depth -= 1
                            if depth == 0 and first_brace >= 0:
                                split_at = i + 1
                                break

                    if split_at > 0 and first_brace >= 0:
                        json_str = buffer[first_brace:split_at]
                        buffer = buffer[:first_brace] + buffer[split_at:]
                        try:
                            request = json.loads(json_str)
                            response = self.handle_request(request)
                            if response is not None:
                                self._write_response(response)
                        except json.JSONDecodeError:
                            logger.warning("Failed to parse extracted JSON: %.100s", json_str)
                            buffer = json_str + buffer  # Put it back
                            break
                    else:
                        # Need more data
                        break

    def _write_response(self, response: dict) -> None:
        """Write a JSON-RPC response to stdout."""
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


# Standalone entry point
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    # Accept optional workdir from command line (defaults to CWD)
    workdir = sys.argv[1] if len(sys.argv) > 1 else "."
    if workdir == "stdio":
        workdir = "."  # Hermes MCP passes "stdio" — ignore it
    server = GitReinsMCPServer(workdir)
    logger.info("GitReins MCP server starting — workdir=%s", server.workdir)
    server.run_stdio()
