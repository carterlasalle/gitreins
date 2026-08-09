"""R2.5 RipgrepProvider — cheap lexical recall via the ``rg`` binary.

DESIGN_v2.md §5: RipgrepProvider = "cheap lexical recall". Wraps ``rg
--json`` and turns match events into small structured dicts
(``file``/``line``/``snippet``). This is the always-available default
provider: it needs no index and works on any working tree.

Semantic queries (definitions, call edges, history) degrade to ``[]`` —
lexical search cannot resolve them. ``references``/``symbols`` are
approximated as plain text matches of the query, clearly labeled as lexical.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from engine.codeintel.base import CodeIntelUnavailableError

logger = logging.getLogger("gitreins.codeintel.ripgrep")

_BINARIES = ("rg", "ripgrep")


def parse_rg_json(raw: str, pattern: str) -> list[dict]:
    """Parse ``rg --json`` NDJSON into structured match dicts.

    Keeps only ``match`` events; each becomes
    ``{"file", "line", "snippet", "pattern", "kind"}``. Malformed lines are
    skipped; non-JSON input yields ``[]`` (never raises).
    """
    matches: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path = (data.get("path") or {}).get("text", "")
        snippet = (data.get("lines") or {}).get("text", "").rstrip("\n")
        matches.append(
            {
                "file": path,
                "line": int(data.get("line_number", 0)),
                "snippet": snippet,
                "pattern": pattern,
                "kind": "match",
            }
        )
    return matches


class RipgrepProvider:
    """Lexical recall over a working tree via ripgrep (always available)."""

    def __init__(self, workdir: str | None = None, binary: str | None = None):
        self.workdir = workdir or os.getcwd()
        self.binary = binary or self._find_binary()
        if not self.binary:
            raise CodeIntelUnavailableError(
                "rg binary not found on PATH (searched: "
                + ", ".join(_BINARIES)
                + "). Install ripgrep to use RipgrepProvider."
            )

    @staticmethod
    def _find_binary() -> str | None:
        for name in _BINARIES:
            resolved = shutil.which(name)
            if resolved:
                return resolved
        return None

    @classmethod
    def available(cls) -> bool:
        return cls._find_binary() is not None

    # ── Core lexical search ───────────────────────────────────

    def text_search(
        self,
        pattern: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Run ``rg --json -n --no-heading <pattern> [file_path]``.

        ``file_path`` defaults to the provider workdir. Returns small
        structured dicts (file/line/snippet), capped at ``limit``. Fails
        soft (``[]`` + log) on timeout, spawn errors, or bad output.
        """
        target: str = file_path if file_path is not None else "."
        binary: str = self.binary  # type: ignore[assignment]  # guarded in __init__
        try:
            result = subprocess.run(
                [binary, "--json", "-n", "--no-heading", pattern, target],
                capture_output=True,
                text=True,
                timeout=30.0,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("rg search failed (%s): %s", type(exc).__name__, exc)
            return []
        return parse_rg_json(result.stdout, pattern)[:limit]

    # ── Protocol surface ──────────────────────────────────────

    def definition(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Lexical recall cannot resolve definitions — use the LSP provider.
        return []

    def references(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        matches = self.text_search(symbol or query, file_path=file_path, limit=limit)
        for m in matches:
            m["kind"] = "reference"
        return matches

    def callers(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # No call graph in lexical recall — degrade gracefully.
        return []

    def callees(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def implementations(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def symbols(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        matches = self.text_search(symbol or query, file_path=file_path, limit=limit)
        for m in matches:
            m["kind"] = "symbol"
        return matches

    def contract(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Lexical approximation: every occurrence is a potential consumer.
        matches = self.text_search(symbol or query, file_path=file_path, limit=limit)
        for m in matches:
            m["kind"] = "impact"
        return matches

    def history(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def prs(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def cross_repo(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def get_cross_repo_impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Lexical recall is single-tree; cross-repo blast radius needs a
        # multi-repo graph. Degrade gracefully.
        return []
