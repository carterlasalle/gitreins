"""R2.5 code-intelligence agent tools — small structured answers, not files.

Builds the 8 agent-facing tools (ast_search, text_search, get_symbol_definition,
find_references, get_callers, get_callees, get_implementations, get_change_impact)
on top of the :mod:`engine.codeintel` providers, wrapped in the
``engine.agents.tools.Tool`` shape so AgentRunner / ToolRegistry can call them.

Every tool returns a **small list of dicts** (file/line/symbol/kind), capped
at ``limit`` (default 10) — never raw file dumps. Provider failures degrade
to ``[]`` rather than raising into the agent loop.

NOTE: this module imports ``Tool`` from ``engine.agents.tools``; the reverse
direction (agents/tools.py → codeintel/tools.py) is a lazy import inside the
builder to avoid a module cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from engine.agents.tools import Tool
from engine.codeintel import get_provider
from engine.codeintel.base import CodeIntelProvider

logger = logging.getLogger("gitreins.codeintel.tools")

#: Default cap — keep answers small.
DEFAULT_LIMIT = 10

#: Max characters for snippet/text fields in tool results.
_MAX_SNIPPET_CHARS = 500


def _cap_result(result: Any, limit: int) -> list[dict]:
    """Coerce a provider result into a small list of JSON-safe dicts.

    Filters non-dict entries, truncates long snippet/text values, and caps
    the list at ``limit``. Never raises — bad results become ``[]``.
    """
    if not isinstance(result, list):
        return []
    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        clean: dict[str, Any] = {}
        for key, value in item.items():
            if isinstance(value, str) and len(value) > _MAX_SNIPPET_CHARS:
                clean[key] = value[:_MAX_SNIPPET_CHARS] + "..."
            else:
                clean[key] = value
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _tool(
    name: str,
    description: str,
    provider_method: Callable[..., list[dict]],
) -> Tool:
    """Wrap a provider method as an agent Tool with the shared schema."""

    def _fn(
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        try:
            result = provider_method(query, file_path=file_path, symbol=symbol, limit=limit)
        except Exception as exc:  # noqa: BLE001 — tool boundary: degrade, don't crash
            logger.warning("codeintel tool %s failed: %s", name, exc)
            return []
        return _cap_result(result, limit)

    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Symbol name or search pattern.",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Optional repo-relative file to scope the query to. "
                        "Omit to search the whole working tree."
                    ),
                },
                "symbol": {
                    "type": "string",
                    "description": (
                        "Optional fully-qualified symbol (e.g. "
                        "pkg.Module.method). Defaults to query."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {DEFAULT_LIMIT}).",
                },
            },
            "required": ["query"],
        },
        fn=_fn,
    )


def make_codeintel_tools(
    workdir: str | None = None, provider: CodeIntelProvider | None = None
) -> list[Tool]:
    """Build the 8 R2.5 code-intelligence tools.

    ``provider`` overrides the per-tool provider selection (tests / custom
    wiring). When omitted, each tool binds to its natural provider kind via
    :func:`engine.codeintel.get_provider` (with graceful ripgrep fallback):
    ast_search→astgrep, text_search→ripgrep, definition/references/callers/
    callees/implementations→lsp, change_impact→graph.
    """
    workdir = workdir or os.getcwd()
    if provider is not None:
        astgrep: Any = provider
        ripgrep: Any = provider
        lsp: Any = provider
        graph: Any = provider
    else:
        astgrep = get_provider("astgrep", workdir=workdir)
        ripgrep = get_provider("ripgrep", workdir=workdir)
        lsp = get_provider("lsp", workdir=workdir)
        graph = get_provider("graph", workdir=workdir)

    return [
        _tool(
            "ast_search",
            "Structural code search using an ast-grep pattern (e.g. "
            "'$A.method($$$)' or 'if ($COND) { $$$ }'). Returns small "
            "structured matches: file, line_start, line_end, snippet.",
            astgrep.search,
        ),
        _tool(
            "text_search",
            "Regex text search across the working tree (ripgrep). Returns "
            "small structured matches: file, line, snippet.",
            ripgrep.text_search,
        ),
        _tool(
            "get_symbol_definition",
            "Locate the definition of a symbol. Returns small structured "
            "locations: file, line_start, line_end, kind.",
            lsp.definition,
        ),
        _tool(
            "find_references",
            "Find references to a symbol. Returns small structured "
            "locations: file, line_start, line_end, kind.",
            lsp.references,
        ),
        _tool(
            "get_callers",
            "Find callers of a function/method. Returns small structured "
            "locations: file, line_start, line_end, kind.",
            lsp.callers,
        ),
        _tool(
            "get_callees",
            "Find callees (calls made inside) of a function/method. "
            "Returns small structured locations.",
            lsp.callees,
        ),
        _tool(
            "get_implementations",
            "Find implementations of an interface/abstract symbol. Returns "
            "small structured locations.",
            lsp.implementations,
        ),
        _tool(
            "get_change_impact",
            "Estimate the blast radius of a change to a symbol: which files "
            "and symbols depend on it. Returns small structured results.",
            graph.impact,
        ),
    ]
