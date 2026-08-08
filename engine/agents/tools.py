"""
Tool registry + dedup + bounded reads for the generic agent runtime (R2.2).

Extracted from the monolithic AgenticEvaluator (engine/evaluator.py):

- ``Tool`` — a callable exposed to the LLM with an OpenAI-style JSON schema
- ``ToolResult`` — the outcome of executing one tool call
- ``ToolRegistry`` — register/lookup + dedup (identical name+args within a
  window, stopping cheap models going in circles)
- ``read_file_bounded`` — path-safe file reads with byte/line caps
- ``make_read_file_tool`` — a bounded read_file Tool bound to a base dir
- ``sandbox_tools`` — scratch-space read/write tools over a plain dict
"""

import json
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

# Non-code/data paths excluded from judge code context (JUDGE-CONTEXT-001).
# Same list as engine/evaluator.py — kept in sync for bounded reads.
DATA_FILE_PATTERNS: tuple[str, ...] = (
    ".coding-hermes/",
    ".jsonl",
    ".json",
    ".parquet",
    ".db",
    ".sqlite",
    ".bak",
    ".log",
    "bin/",
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".git/",
)


@dataclass
class Tool:
    """A callable tool exposed to the LLM.

    ``parameters`` is a JSON-schema object (OpenAI function format), e.g.
    ``{"type": "object", "properties": {"path": {"type": "string"}},
    "required": ["path"]}``.

    ``time_critical`` tools are gated by the runner's wall-clock pre-check:
    with <10s of budget remaining the runner returns a TIME_CRITICAL error
    instead of executing them (so the model can deliver its final answer).
    Cheap state-saving tools (e.g. sandbox_write) should set it to False.
    """

    name: str
    description: str
    parameters: dict | None = None
    fn: Callable[..., Any] | None = None
    time_critical: bool = True

    def to_llm_schema(self) -> dict:
        """Render the tool in OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class ToolResult:
    """Outcome of executing a single tool call."""

    name: str
    output: Any = None
    was_duplicate: bool = False
    error: str | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def _dedup_key(name: str, args: dict) -> str:
    """Canonical key for one tool invocation (stable across arg order)."""
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


class ToolRegistry:
    """Register tools and track duplicate invocations.

    Dedup semantics: a call is a duplicate when the same name + same
    arguments were seen within ``dedup_window`` recent calls (None =
    whole-run dedup, matching the evaluator's per-run sets).
    """

    def __init__(self, dedup_window: int | None = 64):
        self._tools: dict[str, Tool] = {}
        self._dedup_window = dedup_window
        self._recent: deque[str] | None = deque(maxlen=dedup_window) if dedup_window else None
        self._seen: set[str] = set()

    # ── Registration ─────────────────────────────────────────

    def register(self, tool: Tool) -> Tool:
        """Register a tool. Raises on duplicate names."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def lookup(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """LLM-facing schemas for every registered tool."""
        return [t.to_llm_schema() for t in self._tools.values()]

    # ── Dedup ────────────────────────────────────────────────

    def check(self, name: str, args: dict) -> bool:
        """Record an invocation; return True when it duplicates a recent one.

        The call is recorded regardless (callers may still execute it — the
        duplicate flag lets the runner attach a _dedup_warning to the result
        so the LLM sees its own loop and moves on).
        """
        key = _dedup_key(name, args)
        if self._recent is not None:
            if key in self._recent:
                return True
            self._recent.append(key)
        else:
            if key in self._seen:
                return True
            self._seen.add(key)
        return False

    def reset(self) -> None:
        """Clear dedup state (called per run)."""
        self._seen.clear()
        if self._recent is not None:
            self._recent.clear()


def is_data_file_path(path: str) -> bool:
    """True when a repo-relative path is a non-code/data file.

    Directory prefixes (trailing "/") match at any path position; other
    patterns match the filename suffix. (Same semantics as
    engine/evaluator._is_data_file_path.)
    """
    p = path.replace("\\", "/").lower()
    for pat in DATA_FILE_PATTERNS:
        if pat.endswith("/"):
            if p.startswith(pat) or f"/{pat}" in p:
                return True
        elif p.endswith(pat):
            return True
    return False


def read_file_bounded(
    path: str,
    *,
    base_dir: str | None = None,
    offset: int = 0,
    limit: int = 0,
    byte_offset: int = 0,
    byte_limit: int = 0,
    mode: str = "lines",
    max_bytes: int = 131_072,
    first_read_max_chars: int = 12_000,
    first_read_max_lines: int = 400,
) -> dict:
    """Read a file with byte/line caps and path-traversal protection.

    Mirrors AgenticEvaluator._tool_read_file semantics as a standalone
    wrapper:

    - ``base_dir``: realpath of ``path`` must resolve inside it; without a
      base_dir the path is used as-is (caller is responsible for scoping).
    - ``mode='lines'``: offset/limit line ranges; a full read of a file
      larger than ``first_read_max_chars`` returns the first
      ``first_read_max_lines`` lines with a note.
    - ``mode='bytes'``: byte_offset/byte_limit ranges.
    - ``max_bytes``: hard cap on the returned content in both modes.

    Returns a dict with ``content``/metadata, or ``{"error": ...}``.
    """
    if base_dir is not None:
        full_path = os.path.join(base_dir, path)
        real = os.path.realpath(full_path)
        base_real = os.path.realpath(base_dir)
        if not real.startswith(base_real):
            return {"error": f"Path outside working tree: {path}"}
    else:
        real = os.path.realpath(path)
        if not os.path.exists(real):
            return {"error": f"File not found: {path}"}

    if not os.path.exists(real):
        return {"error": f"File not found: {path}"}
    if os.path.isdir(real):
        return {"error": f"Path is a directory: {path}"}

    try:
        total_bytes = os.path.getsize(real)

        if mode == "bytes":
            with open(real, "rb") as f:
                if byte_offset > 0:
                    if byte_offset >= total_bytes:
                        return {
                            "error": f"Byte offset {byte_offset} exceeds file size "
                            f"({total_bytes} bytes)",
                            "path": path,
                            "total_bytes": total_bytes,
                        }
                    f.seek(byte_offset)
                raw = f.read(byte_limit) if byte_limit > 0 else f.read()
            content = raw.decode("utf-8", errors="replace")
            shown_bytes = len(raw)
            has_more = (byte_offset + shown_bytes) < total_bytes if byte_limit > 0 else False

            content_bytes = content.encode("utf-8")
            capped = len(content_bytes) > max_bytes
            if capped:
                content = content_bytes[:max_bytes].decode("utf-8", errors="replace")
                content += (
                    f"\n\n... [capped at {max_bytes} bytes, {total_bytes} total. "
                    "Use offset/limit or byte_offset/byte_limit to read specific ranges.]"
                )

            return {
                "path": path,
                "content": content,
                "total_bytes": total_bytes,
                "shown_bytes": shown_bytes,
                "byte_offset_start": byte_offset,
                "has_more": has_more,
                "capped": capped,
                "mode": "bytes",
            }

        # Line-based read (default)
        with open(real, "r", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        total_chars = sum(len(line) for line in lines)

        if offset > 0:
            start_idx = offset - 1
            if start_idx >= total_lines:
                return {
                    "error": f"Offset {offset} exceeds file length ({total_lines} lines)",
                    "path": path,
                    "total_lines": total_lines,
                }
            lines = lines[start_idx:]
        if limit > 0:
            lines = lines[:limit]

        shown_lines = len(lines)
        content = "".join(lines)
        has_more = (
            (offset > 0 and (offset - 1 + limit < total_lines))
            or (offset > 0 and not limit)
            or (not offset and not limit and total_chars > first_read_max_chars)
        )

        # Full read of a large file: first N lines only
        if not offset and not limit and total_chars > first_read_max_chars:
            content = "".join(lines[:first_read_max_lines])
            content += (
                f"\n\n... [showing first {first_read_max_lines} of {total_lines} lines, "
                f"{total_chars} chars. Use offset/limit to read specific ranges.]"
            )

        # Hard byte cap (single read must not eat the whole context window)
        content_bytes = content.encode("utf-8")
        capped = len(content_bytes) > max_bytes
        if capped:
            content = content_bytes[:max_bytes].decode("utf-8", errors="replace")
            content += (
                f"\n\n... [capped at {max_bytes} bytes, {total_bytes} total. "
                "Use offset/limit or byte_offset/byte_limit to read specific ranges.]"
            )

        return {
            "path": path,
            "content": content,
            "total_lines": total_lines,
            "total_chars": total_chars,
            "total_bytes": total_bytes,
            "shown_lines": shown_lines,
            "has_more": has_more,
            "capped": capped,
            "mode": "lines",
        }
    except Exception as e:  # noqa: BLE001 — tool boundary: report, don't crash
        return {"error": str(e)}


def make_read_file_tool(
    base_dir: str,
    *,
    max_bytes: int = 131_072,
    allowed_files: set[str] | None = None,
    name: str = "read_file",
) -> Tool:
    """Build a bounded read_file Tool bound to ``base_dir``.

    ``allowed_files`` restricts reads to a set of repo-relative paths
    (None = full scope). Mirrors the evaluator's file-scope enforcement.
    """

    def _read_file(
        path: str,
        offset: int = 0,
        limit: int = 0,
        byte_offset: int = 0,
        byte_limit: int = 0,
        mode: str = "lines",
    ) -> dict:
        if allowed_files is not None:
            clean = path.lstrip("./").rstrip("/")
            if clean not in allowed_files:
                return {
                    "error": (
                        f"File not in scope: {path}. Agent is scoped to changed files "
                        "only. Set file_scope: full to allow all files."
                    )
                }
        return read_file_bounded(
            path,
            base_dir=base_dir,
            offset=offset,
            limit=limit,
            byte_offset=byte_offset,
            byte_limit=byte_limit,
            mode=mode,
            max_bytes=max_bytes,
        )

    return Tool(
        name=name,
        description=(
            "Read a file from the working tree. Supports line-based (offset/limit) "
            "and byte-based (byte_offset/byte_limit) partial reads. Set mode='bytes' "
            "for byte-level access. First call without offset/limit returns metadata "
            "plus first 400 lines for large files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to repo root."},
                "offset": {
                    "type": "integer",
                    "description": (
                        "Line number to start from (1-indexed). Omit to read from beginning."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. Omit for full file.",
                },
                "byte_offset": {
                    "type": "integer",
                    "description": (
                        "Byte position to start from (0-indexed). Requires mode='bytes'."
                    ),
                },
                "byte_limit": {
                    "type": "integer",
                    "description": "Max bytes to return. Requires mode='bytes'.",
                },
                "mode": {
                    "type": "string",
                    "description": "'lines' (default) or 'bytes' for byte-level reads.",
                },
            },
            "required": ["path"],
        },
        fn=_read_file,
    )


def sandbox_tools(sandbox: dict[str, str], *, max_read_chars: int = 4000) -> list[Tool]:
    """Build sandbox_read/sandbox_write tools over ``sandbox`` (scratch state).

    These are deliberately NOT time_critical: saving progress must be
    allowed even when the wall-clock budget is nearly exhausted.
    """

    def _write(key: str, content: str) -> dict:
        sandbox[key] = content
        return {"key": key, "written": len(content)}

    def _read(key: str) -> dict:
        if key in sandbox:
            content = sandbox[key]
            if len(content) > max_read_chars:
                content = content[:max_read_chars] + "... [truncated]"
            return {"key": key, "content": content}
        return {"error": f"Key not found: {key}"}

    return [
        Tool(
            name="sandbox_write",
            description=(
                "Write to the agent's scratch space. Use to track progress, files "
                "already read, commands already run — survives context compaction."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to store under."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["key", "content"],
            },
            fn=_write,
            time_critical=False,
        ),
        Tool(
            name="sandbox_read",
            description="Read from the agent's scratch space to check what was already done.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key to read."}},
                "required": ["key"],
            },
            fn=_read,
            time_critical=False,
        ),
    ]
