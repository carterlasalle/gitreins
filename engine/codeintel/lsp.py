"""R2.5 LspProvider — extend engine/lsp.py semantics into a provider.

DESIGN_v2.md §5: "The current ``lsp.py`` already launches servers +
consumes JSON-RPC diagnostics; extend it as an *interface*." This provider
reuses ``engine.lsp``'s ``find_lsp_tool`` / ``_lsp_initialize`` /
``_lsp_did_open`` / ``_lsp_shutdown`` machinery and adds request/response
queries (``textDocument/documentSymbol``, ``textDocument/definition``,
``textDocument/references``) where the LSP protocol supports them.

Capabilities LSP cannot answer (callers, callees, contracts, history, PRs,
cross-repo consumers) degrade gracefully to ``[]`` — documented per method.
When no server binary is installed, every query returns ``[]``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.parse
from pathlib import Path

from engine.lsp import (
    _LANGUAGE_MAP,
    _lsp_did_open,
    _lsp_encode_message,
    _lsp_initialize,
    _lsp_read_response,
    _lsp_shutdown,
    find_lsp_tool,
)

logger = logging.getLogger("gitreins.codeintel.lsp")

_DEFAULT_TOOL = "pylsp"

# LSP SymbolKind numeric → readable name (LSP 3.17).
_SYMBOL_KINDS = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}


def _symbol_kind_name(kind: int) -> str:
    return _SYMBOL_KINDS.get(kind, "unknown")


def parse_document_symbols(result: object | None) -> list[dict]:
    """Normalize a documentSymbol / workspace/symbol response.

    Handles both the hierarchical ``DocumentSymbol[]`` shape (with
    ``children``) and the flat ``SymbolInformation[]`` shape (with
    ``location``). Returns ``{file, line, kind, name}`` dicts.
    """
    out: list[dict] = []

    def _walk(node: dict) -> None:
        location = node.get("location") or {}
        rng = node.get("range") or location.get("range") or {}
        line = (rng.get("start") or {}).get("line", 0) + 1
        file_uri = location.get("uri") or node.get("uri") or ""
        out.append(
            {
                "file": urllib.parse.urlparse(file_uri).path if file_uri else "",
                "line": line,
                "kind": _symbol_kind_name(node.get("kind", 0)),
                "name": node.get("name", ""),
            }
        )
        for child in node.get("children") or []:
            if isinstance(child, dict):
                _walk(child)

    if not isinstance(result, list):
        return out
    for item in result:
        if isinstance(item, dict):
            _walk(item)
    return out


def parse_lsp_locations(result: object, kind: str = "location") -> list[dict]:
    """Normalize a textDocument/definition-or-references response.

    Accepts ``Location | Location[] | LocationLink[]``. Returns
    ``{file, line_start, line_end, kind}`` dicts.
    """
    items = result if isinstance(result, list) else [result]
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("targetUri") or item.get("uri") or ""
        rng = item.get("targetRange") or item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        out.append(
            {
                "file": urllib.parse.urlparse(uri).path if uri else "",
                "line_start": start.get("line", 0) + 1,
                "line_end": end.get("line", 0) + 1,
                "kind": kind,
            }
        )
    return out


def _lsp_request(
    proc: subprocess.Popen,
    method: str,
    params: dict,
    request_id: int = 100,
    timeout: float = 30.0,
) -> dict | None:
    """Send a JSON-RPC request and wait for the response with matching id."""
    import time as _time

    msg = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    assert proc.stdin is not None
    proc.stdin.write(_lsp_encode_message(msg))
    proc.stdin.flush()

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break
        response = _lsp_read_response(proc, timeout=remaining)
        if response is None:
            continue
        if response.get("id") == request_id:
            if "error" in response:
                logger.warning(
                    "LSP request %s failed: %s", method, response["error"]
                )
                return None
            return response.get("result")
    return None


def _uri_for(file_path: str) -> str:
    return Path(file_path).as_uri()


def _language_for(file_path: str) -> str:
    return _LANGUAGE_MAP.get(Path(file_path).suffix.lower(), "python")


class LspProvider:
    """Semantic queries (symbols/definition/references) over an LSP server.

    One server process is started per query (initialize → didOpen → request
    → shutdown), mirroring ``engine.lsp.run_lsp_check``'s lifecycle. All
    failures degrade to ``[]``.
    """

    def __init__(
        self,
        workdir: str | None = None,
        tool: str | None = None,
        binary: str | None = None,
    ):
        self.workdir = workdir or os.getcwd()
        self.tool = tool or _DEFAULT_TOOL
        self.binary = binary  # explicit override (tests); else resolved per query

    @property
    def _tool_path(self) -> str | None:
        if self.binary:
            return self.binary
        return find_lsp_tool(self.tool)

    @classmethod
    def available(cls, tool: str | None = None) -> bool:
        return find_lsp_tool(tool or _DEFAULT_TOOL) is not None

    def _with_server(self, file_path: str, method: str, params: dict) -> dict | None:
        """Run one request against a fresh server session, or None."""
        tool_path = self._tool_path
        if not tool_path:
            logger.warning(
                "LSP tool '%s' not found on PATH — returning empty results", self.tool
            )
            return None
        try:
            proc = subprocess.Popen(
                [tool_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.workdir,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 — spawn failures degrade to []
            logger.warning("Failed to start LSP tool '%s': %s", self.tool, exc)
            return None
        try:
            if not _lsp_initialize(proc, self.workdir, timeout=30.0):
                return None
            _lsp_did_open(proc, file_path, _language_for(file_path))
            return _lsp_request(proc, method, params, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — protocol errors degrade to []
            logger.warning("LSP request error on '%s': %s", self.tool, exc)
            return None
        finally:
            _lsp_shutdown(proc)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    import signal as _signal

                    lsp_pid = proc.pid
                    if (
                        not isinstance(lsp_pid, int)
                        or isinstance(lsp_pid, bool)
                        or lsp_pid <= 1
                    ):
                        raise ValueError(f"unsafe LSP pid: {lsp_pid!r}")
                    lsp_pgid = os.getpgid(lsp_pid)
                    our_pgid = os.getpgid(os.getpid())
                    if lsp_pgid == lsp_pid and lsp_pgid != our_pgid:
                        os.killpg(lsp_pgid, _signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    # ── Core LSP queries ──────────────────────────────────────

    def symbols(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        if not file_path:
            return []
        result = self._with_server(
            file_path,
            "textDocument/documentSymbol",
            {"textDocument": {"uri": _uri_for(file_path)}},
        )
        found = parse_document_symbols(result)
        needle = (symbol or query).lower()
        if needle:
            found = [s for s in found if needle in s.get("name", "").lower()]
        return found[:limit]

    def definition(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        if not file_path:
            return []
        pos = self._find_position(file_path, symbol or query)
        if pos is None:
            return []
        result = self._with_server(
            file_path,
            "textDocument/definition",
            {"textDocument": {"uri": _uri_for(file_path)}, "position": pos},
        )
        return parse_lsp_locations(result, kind="definition")[:limit]

    def references(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        if not file_path:
            return []
        pos = self._find_position(file_path, symbol or query)
        if pos is None:
            return []
        result = self._with_server(
            file_path,
            "textDocument/references",
            {
                "textDocument": {"uri": _uri_for(file_path)},
                "position": pos,
                "context": {"includeDeclaration": True},
            },
        )
        return parse_lsp_locations(result, kind="reference")[:limit]

    def _find_position(self, file_path: str, needle: str) -> dict | None:
        """Locate ``needle`` in ``file_path`` as a 0-based line/character."""
        try:
            with open(file_path, "r", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None
        for idx, line in enumerate(lines):
            col = line.find(needle)
            if col >= 0:
                return {"line": idx, "character": col}
        return None

    # ── Unsupported capabilities (graceful []) ────────────────

    def callers(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Requires a call graph — not part of the base LSP protocol.
        return []

    def callees(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def implementations(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # textDocument/implementation exists in LSP 3.17+; not all servers
        # support it. Degrade to [] when the server lacks the capability.
        if not file_path:
            return []
        pos = self._find_position(file_path, symbol or query)
        if pos is None:
            return []
        result = self._with_server(
            file_path,
            "textDocument/implementation",
            {"textDocument": {"uri": _uri_for(file_path)}, "position": pos},
        )
        return parse_lsp_locations(result, kind="implementation")[:limit]

    def contract(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Best-effort: references are a proxy for blast radius.
        return self.references(query, file_path, symbol, limit)

    def history(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def prs(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def cross_repo(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []
