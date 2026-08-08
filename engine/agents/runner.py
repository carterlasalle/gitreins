"""
AgentRunner — generic bounded agent loop (R2.2).

The reusable machinery extracted from the monolithic AgenticEvaluator
(engine/evaluator.py):

- iteration caps, wall-clock caps, token accounting, tool-call weighting
- context compaction (proactive threshold + context-error recovery)
- tool dedup (stops cheap models going in circles)
- bounded file reads (via engine.agents.tools)
- sandbox scratch state (read/write)
- LLM tool calling, resolved per model_role via ModelRouter.for_role()

Concrete agents (CriteriaEvaluator, ScoutAgent, ReviewAgent, ...) pass
their own system prompt, tools, and output schema — the runner supplies the
bounded loop.
"""

import json
import logging
import os
import time
from typing import Any, Callable, Protocol, TypeVar

from engine.agents.budget import Budget
from engine.agents.schemas import SchemaError, parse_response
from engine.agents.tools import Tool, ToolRegistry, ToolResult, sandbox_tools
from engine.llm import LLMClient, ToolCall
from engine.router import ModelRouter

logger = logging.getLogger("gitreins.agents.runner")

T = TypeVar("T")


class RoleRouter(Protocol):
    """Anything with ``for_role(role) -> LLMClient`` (ModelRouter satisfies it)."""

    def for_role(self, role: str) -> LLMClient: ...


class AgentRunError(Exception):
    """Fatal failure of an agent run (LLM transport, schema, budget)."""


class BudgetExceededError(AgentRunError):
    """The run exceeded its budget (iterations, wall-clock, or tokens)."""


def _serialize_tool_output(output: Any) -> str:
    """JSON-serialize a tool result for the message log (never crashes)."""
    try:
        return json.dumps(output)
    except (TypeError, ValueError):
        try:
            return json.dumps(
                {"error": "tool result not JSON-serializable", "raw": str(output)[:2000]}
            )
        except Exception:  # noqa: BLE001 — last resort
            return json.dumps({"error": "tool result not JSON-serializable"})


class AgentRunner:
    """Run an LLM agent loop with tools under a hard Budget.

    ``run()`` resolves the LLM client per ``model_role`` through
    ``ModelRouter.for_role(role)``, then iterates: LLM reasons → executes
    tool calls (deduped, bounded, budgeted) → repeats until the LLM emits a
    final answer matching ``output_schema`` or the budget is exhausted.

    A fresh sandbox (scratch dict) is created per run, and
    sandbox_read/sandbox_write tools are injected unless the caller
    provides their own with those names. Dedup state resets per run.
    """

    def __init__(
        self,
        router: RoleRouter | None = None,
        *,
        workdir: str = ".",
        config: dict | None = None,
        command_timeout: int = 30,
        max_tokens_per_call: int = 16384,
        max_compactions: int = 3,
        skip_duplicates: bool = False,
        on_compact: Callable[[list[dict], int], list[dict]] | None = None,
    ):
        """``router`` resolves LLM clients per role; when None, one is built
        from ``config`` (or ``workdir/.gitreins/config.yaml`` when config is
        also None). ``on_compact`` receives (messages, compaction_count) and
        returns the fresh compacted message list; the default keeps the
        system prompt and asks the model to continue from sandbox state."""
        self.workdir = os.path.abspath(workdir)
        self.command_timeout = command_timeout
        self.max_tokens_per_call = max_tokens_per_call
        self.max_compactions = max_compactions
        self.skip_duplicates = skip_duplicates
        self._on_compact = on_compact

        if router is not None:
            self._router = router
        elif config is not None:
            self._router = ModelRouter(config)
        else:
            self._router = ModelRouter(self._load_config())

        self._sandbox: dict[str, str] = {}

    # ── Config helpers ───────────────────────────────────────

    def _load_config(self) -> dict:
        """Load .gitreins/config.yaml from the workdir if present."""
        import yaml

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.isfile(config_path):
            try:
                with open(config_path) as f:
                    return yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001 — best-effort config read
                pass
        return {}

    @property
    def sandbox(self) -> dict[str, str]:
        """Scratch state for the current run (reset at each run() call)."""
        return self._sandbox

    # ── The generic bounded loop ─────────────────────────────

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[Tool],
        output_schema: type[T],
        model_role: str,
        budget: Budget,
    ) -> T:
        """Run the bounded agent loop and return an output_schema-typed result.

        Raises:
          BudgetExceededError — iteration/time/token budget exhausted.
          AgentRunError — LLM transport failure or empty final response.
        """
        # Fresh per-run state
        self._sandbox.clear()
        registry = ToolRegistry(dedup_window=None)  # whole-run dedup (evaluator semantics)
        for tool in tools:
            registry.register(tool)
        for tool in sandbox_tools(self._sandbox):
            if tool.name not in registry:
                registry.register(tool)

        client = self._router.for_role(model_role)
        budget.start()

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        iteration = 0
        iter_limit = budget.max_iterations_int
        compaction_count = 0
        last_prompt_tok = 0
        max_compactions = self.max_compactions
        parse_error: SchemaError | None = None

        while iteration < iter_limit:
            # Hard caps (time/tokens) checked before each LLM call
            cap_error = budget.exceeded()
            if cap_error:
                raise BudgetExceededError(cap_error)

            # Proactive compaction: context near the input-token threshold
            if (
                last_prompt_tok > 0
                and compaction_count < max_compactions
                and budget.max_input_tokens > 0
            ):
                threshold = int(budget.max_input_tokens * budget.compaction_threshold)
                if last_prompt_tok > threshold:
                    logger.warning(
                        "Context near limit (%d/%d tokens) — compacting (compaction #%d)",
                        last_prompt_tok,
                        budget.max_input_tokens,
                        compaction_count + 1,
                    )
                    messages = self._compact(messages, system_prompt, compaction_count)
                    compaction_count += 1
                    iteration = 0  # fresh conversation resets the iteration counter
                    last_prompt_tok = 0
                    budget.reset_context_tracking()
                    continue

            try:
                response = client.chat(
                    messages,
                    tools=registry.schemas(),
                    max_tokens=self.max_tokens_per_call,
                )
            except Exception as e:  # noqa: BLE001 — classify below
                if self._is_context_error(e) and compaction_count < max_compactions:
                    logger.warning(
                        "Context error on iteration %d (compacting #%d): %s",
                        iteration,
                        compaction_count + 1,
                        str(e)[:200],
                    )
                    messages = self._compact(messages, system_prompt, compaction_count)
                    compaction_count += 1
                    iteration = 0
                    last_prompt_tok = 0
                    budget.reset_context_tracking()
                    continue
                logger.error("LLM call failed on iteration %d: %s", iteration, e)
                raise AgentRunError(f"LLM call failed: {e}") from e

            # Token accounting (LLM reasoning turn costs 1.0 iterations)
            usage = response.usage
            prompt_tok = usage.prompt_tokens if usage else 0
            completion_tok = usage.completion_tokens if usage else 0
            cache_read = usage.cache_read_tokens if usage else 0
            cache_write = usage.cache_write_tokens if usage else 0
            last_prompt_tok = max(last_prompt_tok, prompt_tok)

            cap_error = budget.track(
                iterations=1.0,
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            if cap_error:
                raise BudgetExceededError(cap_error)

            # No tool calls → the model is delivering its final answer
            if not response.tool_calls:
                if response.content:
                    try:
                        return parse_response(response.content, output_schema)
                    except SchemaError as e:
                        parse_error = e
                        logger.warning("Output schema parse failed: %s", e)
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"Your previous response did not match the required "
                                    f"output format. Error: {e}. Respond with ONLY the "
                                    "JSON object in the required format — no markdown "
                                    "fences, no extra text."
                                ),
                            }
                        )
                        iteration += 1
                        continue
                raise AgentRunError("LLM returned an empty response with no tool calls.")

            # Assistant message carrying the tool calls
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
            )

            # Execute each tool call (deduped, time-gated, budgeted)
            for tc in response.tool_calls:
                result = self._execute_tool_call(tc, registry, budget)
                cap_error = budget.track(iterations=budget.tool_call_weight)
                if cap_error:
                    raise BudgetExceededError(cap_error)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _serialize_tool_output(result.output),
                    }
                )

            logger.debug(
                "Agent iteration %d: %d tool calls, %d messages",
                iteration + 1,
                len(response.tool_calls),
                len(messages),
            )
            iteration += 1

        # Hit the iteration cap — the model never produced a valid final answer
        msg = f"Cap exceeded: {budget.summary()}. Increase caps or split the task."
        if parse_error is not None:
            msg += f" Last schema error: {parse_error}"
        raise BudgetExceededError(msg)

    # ── Loop internals ───────────────────────────────────────

    def _execute_tool_call(
        self, tc: ToolCall, registry: ToolRegistry, budget: Budget
    ) -> ToolResult:
        """Execute one tool call with dedup + wall-clock gating."""
        tool = registry.lookup(tc.name)
        if tool is None:
            return ToolResult(
                name=tc.name,
                error=f"Unknown tool: {tc.name}",
                output={"error": f"Unknown tool: {tc.name}"},
            )

        # Wall-clock pre-check for time_critical tools (BEFORE dedup state,
        # so skipped calls don't pollute the dedup window)
        if tool.time_critical:
            try:
                remaining = budget.remaining_seconds()
            except Exception:  # noqa: BLE001
                remaining = -1.0
            if remaining < 0 and remaining != -1.0:
                return ToolResult(
                    name=tc.name,
                    output={
                        "error": (
                            "TIME_EXCEEDED: Time budget exhausted. "
                            "Deliver your final answer immediately."
                        )
                    },
                    error="TIME_EXCEEDED",
                )
            if 0 < remaining < 10:
                return ToolResult(
                    name=tc.name,
                    output={
                        "error": (
                            f"TIME_CRITICAL: Only {int(remaining)} seconds remaining. "
                            "Deliver your final answer NOW with what you have."
                        )
                    },
                    error="TIME_CRITICAL",
                )

        was_dup = registry.check(tc.name, tc.arguments)

        if was_dup and self.skip_duplicates:
            return ToolResult(
                name=tc.name,
                was_duplicate=True,
                output={
                    "error": (
                        f"Duplicate call skipped: you already used {tc.name} with these "
                        "arguments. See previous result above. Move on."
                    )
                },
            )

        start = time.monotonic()
        try:
            output = (
                tool.fn(**tc.arguments)
                if tool.fn is not None
                else {"error": f"Tool {tc.name} has no implementation"}
            )
            if was_dup:
                if isinstance(output, dict):
                    output["_dedup_warning"] = (
                        f"You already used {tc.name} with these arguments. "
                        "See previous result above. Move on to unchecked work."
                    )
                else:
                    output = {
                        "_dedup_warning": (
                            f"You already used {tc.name} with these arguments. "
                            "See previous result above. Move on to unchecked work."
                        ),
                        "result": output,
                    }
            return ToolResult(
                name=tc.name,
                output=output,
                was_duplicate=was_dup,
                duration=time.monotonic() - start,
            )
        except Exception as e:  # noqa: BLE001 — tool boundary: report, don't crash
            logger.exception("Tool %s failed", tc.name)
            return ToolResult(
                name=tc.name,
                output={"error": str(e)},
                error=str(e),
                was_duplicate=was_dup,
                duration=time.monotonic() - start,
            )

    def _compact(
        self, messages: list[dict], system_prompt: str, compaction_count: int
    ) -> list[dict]:
        """Compact the conversation; returns a fresh message list."""
        if self._on_compact is not None:
            return self._on_compact(messages, compaction_count)
        system_msg = (
            messages[0]
            if messages and messages[0].get("role") == "system"
            else {"role": "system", "content": system_prompt}
        )
        logger.info(
            "Compacting agent context (compaction #%d, %d messages → clean slate)",
            compaction_count + 1,
            len(messages),
        )
        return [
            system_msg,
            {
                "role": "user",
                "content": (
                    "CONTEXT COMPACTED: the previous conversation was dropped to free "
                    "context space. Continue the task from your sandbox state "
                    "(sandbox_read) — do NOT redo work you already recorded there. "
                    "Deliver the final structured output when done."
                ),
            },
        ]

    @staticmethod
    def _is_context_error(exc: Exception) -> bool:
        """Classify LLM errors: context-window exhaustion (→ compact)."""
        is_context = False
        try:
            import requests

            if isinstance(exc, requests.HTTPError):
                if hasattr(exc, "response") and exc.response is not None:
                    status = exc.response.status_code
                    if 400 <= status < 500 and status != 429:
                        is_context = True
        except Exception:  # noqa: BLE001
            pass
        if not is_context:
            msg = str(exc).lower()
            is_context = any(
                kw in msg
                for kw in ("context", "token", "maximum", "exceeded", "window", "truncat", "length")
            )
        return is_context
