"""R2.5 SerenaProvider — MCP-client wrapper for oraios/serena.

DESIGN_v2.md §5: "SerenaProvider (LSP — precise current symbol semantics)".
Serena is an MCP server exposing symbol-level queries (find_declaration,
find_referencing_symbols, find_implementations, find_symbol, ...).

This module imports **cleanly** even when serena / the MCP client are not
installed — availability is checked at *construction*:

- ``SerenaProvider()`` raises :class:`SerenaUnavailableError` when the
  ``serena`` binary is not on PATH.
- The MCP stdio client (``mcp`` package) is imported lazily inside the
  query path, so module import never depends on it.

Capabilities serena's tool surface cannot answer (callees, contracts,
history, PRs, cross-repo) degrade gracefully to ``[]``.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from engine.codeintel.base import CodeIntelUnavailableError

logger = logging.getLogger("gitreins.codeintel.serena")

_SERENA_BINARY = "serena"

# serena MCP tool names (oraios/serena MCP server).
_TOOL_DEFINITION = "find_declaration"
_TOOL_REFERENCES = "find_referencing_symbols"
_TOOL_IMPLEMENTATIONS = "find_implementations"
_TOOL_SYMBOLS = "find_symbol"


class SerenaUnavailableError(CodeIntelUnavailableError):
    """Serena binary (or MCP client) is not available at construction."""


def _parse_mcp_text(result: Any) -> list[dict]:
    """Extract JSON-ish dicts from an MCP call_tool result.

    serena returns tool results whose ``content`` is a list of text blocks
    (JSON). Best-effort: parse each text block as JSON and collect any dicts
    / dict lists; fall back to a single text entry. Never raises.
    """
    out: list[dict] = []
    content = result.get("content", []) if isinstance(result, dict) else []
    for block in content:
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        if not text:
            continue
        import json

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            out.append({"text": text})
            continue
        if isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
    return out


class SerenaProvider:
    """Symbol-semantics queries via a serena MCP stdio session.

    One stdio session is opened per query (serena is stateless enough for
    this; the graph/LSP providers handle the persistent side). All failures
    degrade to ``[]``.
    """

    def __init__(self, workdir: str | None = None, binary: str | None = None):
        self.workdir = workdir or os.getcwd()
        self.binary = binary or shutil.which(_SERENA_BINARY)
        if not self.binary:
            raise SerenaUnavailableError(
                "serena binary not found on PATH (searched: "
                + _SERENA_BINARY
                + "). Install oraios/serena (MCP server) to use "
                "SerenaProvider, or use the astgrep/ripgrep fallback."
            )

    @classmethod
    def available(cls) -> bool:
        return shutil.which(_SERENA_BINARY) is not None

    # ── MCP plumbing (lazy) ───────────────────────────────────

    def _call_tool(self, name: str, arguments: dict) -> list[dict]:
        """Run one serena MCP tool via a fresh stdio session."""
        binary = self.binary
        if not binary:
            return []
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            import asyncio

            async def _run() -> list[dict]:
                params = StdioServerParameters(command=binary, args=[], env=None)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
                        return _parse_mcp_text(result)

            return asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 — MCP failures degrade to []
            logger.warning("serena MCP call %s failed: %s", name, exc)
            return []

    def _name_args(self, query: str, file_path: str | None, symbol: str | None) -> dict:
        args: dict[str, Any] = {"name_path": symbol or query}
        if file_path:
            args["file"] = file_path
        return args

    def _symbol_args(self, query: str, file_path: str | None, symbol: str | None) -> dict:
        # find_symbol takes a plain `name` query (substring/prefix match).
        args: dict[str, Any] = {"name": symbol or query}
        if file_path:
            args["file"] = file_path
        return args

    # ── Protocol surface ──────────────────────────────────────

    def definition(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return self._call_tool(_TOOL_DEFINITION, self._name_args(query, file_path, symbol))[:limit]

    def references(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return self._call_tool(_TOOL_REFERENCES, self._name_args(query, file_path, symbol))[:limit]

    def callers(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Serena finds referencing symbols — a reasonable callers proxy.
        return self.references(query, file_path, symbol, limit)

    def callees(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Not exposed by serena's tool surface — degrade gracefully.
        return []

    def implementations(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return self._call_tool(
            _TOOL_IMPLEMENTATIONS, self._name_args(query, file_path, symbol)
        )[:limit]

    def symbols(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return self._call_tool(_TOOL_SYMBOLS, self._symbol_args(query, file_path, symbol))[:limit]

    def contract(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return self.references(query, file_path, symbol, limit)

    def history(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def prs(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def cross_repo(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def get_cross_repo_impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Serena indexes a single project; cross-repo impact needs a
        # multi-repo graph. Degrade gracefully.
        return []
