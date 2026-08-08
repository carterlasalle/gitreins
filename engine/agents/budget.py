"""
Budget — cost/context caps for the generic bounded agent runtime (R2.2).

Mirrors the ``evaluator`` block of ``.gitreins/config.yaml`` and the
accounting semantics of ``engine.eval_cap.EvalCap`` (the proven machinery
from the monolithic AgenticEvaluator): fractional tool-call weighting,
lenient iteration pre-checks (one final step past the cap is allowed),
and hard wall-clock / token limits.

Caps are individually optional: -1 (or <= 0) disables a cap.

    Config:

      evaluator:
        max_iterations: 200          # -1 = unlimited, supports fractional
        max_time: "45m"              # wall-clock; 30s, 5m, 2h
        max_input_tokens: "10M"      # total input (regular + cache)
        max_output_tokens: "1M"
        tool_call_weight: 0.1        # fraction of an iteration per tool call
        compaction_threshold: 0.9    # context fraction that triggers compaction
        code_context_budget: 0.7     # fraction of input budget for pre-loaded code
"""

import logging
import time
from dataclasses import dataclass

from engine.eval_cap import _fmt_seconds, _fmt_tokens

logger = logging.getLogger("gitreins.agents.budget")


@dataclass
class Budget:
    """Caps that limit how much an agent run can consume.

    All caps default to -1 (unlimited). Tool calls are discounted:
    each tool call costs ``tool_call_weight`` iterations (default 0.1);
    an LLM reasoning turn costs 1.0.
    """

    max_iterations: float = -1.0  # -1 = unlimited, supports fractional
    max_time: float = -1.0  # -1 = unlimited (seconds)
    max_input_tokens: int = -1  # -1 = unlimited
    max_output_tokens: int = -1  # -1 = unlimited
    tool_call_weight: float = 0.1  # fraction of an iteration per tool call
    compaction_threshold: float = 0.9  # context fraction that triggers compaction
    code_context_budget: float = 0.7  # fraction of input budget for code context

    # Runtime tracking
    iteration_credit: float = 0.0
    start_time: float = 0.0
    cumulative_input_tokens: int = 0  # total input (regular + cache)
    cumulative_output_tokens: int = 0  # output tokens
    cumulative_cache_read: int = 0  # tokens served from cache
    cumulative_cache_write: int = 0  # tokens written to cache
    source: str = ""

    # ── Constructors ─────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict | None = None) -> "Budget":
        """Build a Budget from a ``.gitreins/config.yaml``-shaped dict.

        Delegates the evaluator-block parsing to
        ``engine.eval_cap.eval_cap_from_config`` (single source of truth:
        defaults from GitReinsDefaults, individual keys, legacy ``cap``
        string, and GITREINS_MAX_* env overrides), then overlays the
        compaction / code-context ratios from the evaluator block.
        """
        from engine.eval_cap import eval_cap_from_config

        cfg = config or {}
        cap = eval_cap_from_config(cfg)
        ev = cfg.get("evaluator", {}) or {}
        return cls(
            max_iterations=cap.max_iterations,
            max_time=cap.max_seconds,
            max_input_tokens=cap.max_input_tokens,
            max_output_tokens=cap.max_output_tokens,
            tool_call_weight=cap.tool_call_weight,
            compaction_threshold=float(ev.get("compaction_threshold", 0.9)),
            code_context_budget=float(ev.get("code_context_budget", 0.7)),
            source=cap.source,
        )

    @property
    def is_unlimited(self) -> bool:
        return (
            self.max_iterations == -1.0
            and self.max_time == -1.0
            and self.max_input_tokens == -1
            and self.max_output_tokens == -1
        )

    @property
    def max_iterations_int(self) -> int:
        """Integer iteration ceiling (10_000 safety maximum for unlimited)."""
        if self.max_iterations <= 0:
            return 10_000
        return int(self.max_iterations)

    # ── Accounting ───────────────────────────────────────────

    def start(self) -> None:
        """Begin the wall-clock timer."""
        self.start_time = time.time()

    def reset_context_tracking(self) -> None:
        """Reset token counters for a fresh context window.

        Called after compaction — the conversation is rebuilt from scratch
        so accumulated token counts from the old window are irrelevant.
        Iteration and time tracking are NOT reset (they span the whole run).
        """
        self.cumulative_input_tokens = 0
        self.cumulative_output_tokens = 0
        self.cumulative_cache_read = 0
        self.cumulative_cache_write = 0

    def track(
        self,
        iterations: float = 1.0,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> str | None:
        """Record an LLM or tool step; return a cap-exceeded message or None.

        Iterations are checked BEFORE the step is added (lenient: one final
        step past the iteration cap is allowed, matching EvalCap). Time and
        token caps are hard limits checked after accounting.
        """
        if self.max_iterations > 0 and self.iteration_credit >= self.max_iterations:
            return (
                f"Iteration cap ({_fmt_num(self.max_iterations)}) reached "
                f"({_fmt_num(self.iteration_credit)} used). "
                "Increase max_iterations or split the task."
            )
        self.iteration_credit += iterations
        self.cumulative_input_tokens += prompt_tokens + cache_read_tokens + cache_write_tokens
        self.cumulative_output_tokens += completion_tokens
        self.cumulative_cache_read += cache_read_tokens
        self.cumulative_cache_write += cache_write_tokens
        return self.exceeded()

    def exceeded(self) -> str | None:
        """Return a message for the first exceeded hard cap, else None.

        Hard caps: wall-clock time, input tokens, output tokens.
        """
        if self.max_time > 0 and self.start_time > 0:
            elapsed = time.time() - self.start_time
            if elapsed >= self.max_time:
                return (
                    f"Time cap ({_fmt_seconds(self.max_time)}) exceeded "
                    f"({_fmt_seconds(elapsed)} elapsed). "
                    "Increase max_time or simplify the task."
                )
        if self.max_input_tokens > 0 and self.cumulative_input_tokens >= self.max_input_tokens:
            return (
                f"Input token budget ({_fmt_tokens(self.max_input_tokens)}) exceeded "
                f"({_fmt_tokens(self.cumulative_input_tokens)} used). "
                "Increase max_input_tokens or reduce message context."
            )
        if self.max_output_tokens > 0 and self.cumulative_output_tokens >= self.max_output_tokens:
            return (
                f"Output token budget ({_fmt_tokens(self.max_output_tokens)}) exceeded "
                f"({_fmt_tokens(self.cumulative_output_tokens)} used). "
                "Increase max_output_tokens or simplify the task."
            )
        return None

    def remaining_seconds(self) -> float:
        """Return remaining wall-clock budget in seconds.

        States:
          - max_time <= 0          → -1 (unlimited, time not tracked)
          - start_time == 0        → max_time (timer not started yet)
          - mid-run                → max_time - (now - start_time)
          - over budget            → negative float
        """
        if self.max_time <= 0:
            return -1.0
        if self.start_time == 0:
            return float(self.max_time)
        return self.max_time - (time.time() - self.start_time)

    def summary(self) -> str:
        """Human-readable summary of caps and current usage."""
        parts = []
        if self.max_iterations > 0:
            parts.append(
                f"iterations: {_fmt_num(self.iteration_credit)}/{_fmt_num(self.max_iterations)}"
            )
        elif self.max_iterations == -1:
            parts.append("iterations: unlimited")
        if self.max_time > 0:
            elapsed = int(time.time() - self.start_time) if self.start_time > 0 else 0
            parts.append(f"time: {_fmt_seconds(elapsed)}/{_fmt_seconds(self.max_time)}")
        if self.max_input_tokens > 0 or self.max_output_tokens > 0:
            in_str = f"in: {_fmt_tokens(self.cumulative_input_tokens)}"
            if self.max_input_tokens > 0:
                in_str += f"/{_fmt_tokens(self.max_input_tokens)}"
            if self.cumulative_cache_read > 0 or self.cumulative_cache_write > 0:
                cache_parts = []
                if self.cumulative_cache_read > 0:
                    cache_parts.append(f"cache-hit {_fmt_tokens(self.cumulative_cache_read)}")
                if self.cumulative_cache_write > 0:
                    cache_parts.append(f"cache-write {_fmt_tokens(self.cumulative_cache_write)}")
                in_str += f" ({', '.join(cache_parts)})"
            parts.append(in_str)
            parts.append(
                f"out: {_fmt_tokens(self.cumulative_output_tokens)}"
                + (f"/{_fmt_tokens(self.max_output_tokens)}" if self.max_output_tokens > 0 else "")
            )
        if not parts:
            return "no caps (unlimited)"
        return ", ".join(parts)


def _fmt_num(n: float) -> str:
    """Format a float, dropping trailing .0 for whole numbers."""
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"
