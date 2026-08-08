"""R2.5 AstGrepProvider — structural pattern search via the ast-grep CLI.

DESIGN_v2.md §5: AstGrepProvider = structural pattern search. Wraps the
``ast-grep run`` subcommand (the ``sg`` binary), parses its JSON output into
small structured matches (``file``/``line_start``/``line_end``/``pattern``/
``snippet``), and never returns full files.

The binary is resolved at construction (``sg`` or ``ast-grep`` on PATH) and
raises :class:`CodeIntelUnavailableError` when absent — import time is always
safe. Semantic queries that structural search cannot answer (definitions,
call graphs, history, ...) degrade gracefully to ``[]``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

from engine.codeintel.base import CodeIntelUnavailableError

logger = logging.getLogger("gitreins.codeintel.astgrep")

# Binary names searched in order. Prefer `ast-grep` over `sg`: on some systems
# `/usr/bin/sg` is GNU screen (the multiplexer), NOT ast-grep — resolving `sg`
# to it made every search return 0 matches with "non-JSON output". `ast-grep`
# is the canonical name and never collides. (Fixed 2026-08-08)
_BINARIES = ("ast-grep", "sg")


@dataclass
class AstGrepMatch:
    """One structured ast-grep match (line numbers 1-based)."""

    file: str
    line_start: int
    line_end: int
    pattern: str
    snippet: str


def parse_astgrep_matches(raw: str, pattern: str) -> list[dict]:
    """Parse ``ast-grep run --json`` stdout into structured match dicts.

    ast-grep's JSON output is a list of match objects with ``file``,
    ``range.start/end.line`` (0-based), and ``text``. Line numbers are
    converted to 1-based. Non-JSON output yields ``[]`` (never raises).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ast-grep returned non-JSON output — no matches")
        return []
    if not isinstance(data, list):
        return []

    matches: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        matches.append(
            {
                "file": item.get("file", ""),
                "line_start": int(start.get("line", 0)) + 1,
                "line_end": int(end.get("line", 0)) + 1,
                "pattern": pattern,
                "snippet": item.get("text", ""),
                "kind": "match",
            }
        )
    return matches


class AstGrepProvider:
    """Structural pattern search over a working tree via the ast-grep CLI."""

    def __init__(self, workdir: str | None = None, binary: str | None = None):
        self.workdir = workdir or os.getcwd()
        self.binary = binary or self._find_binary()
        if not self.binary:
            raise CodeIntelUnavailableError(
                "ast-grep binary not found on PATH (searched: "
                + ", ".join(_BINARIES)
                + "). Install ast-grep to use AstGrepProvider."
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

    # ── Core structural search ────────────────────────────────

    def search(
        self,
        pattern: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Run ``ast-grep run --json -p <pattern> [file_path]``.

        ``file_path`` defaults to the provider workdir. Returns small
        structured dicts (file/line_start/line_end/pattern/snippet), capped
        at ``limit``. Fails soft (``[]`` + log) on timeout, spawn errors, or
        bad output.
        """
        target: str = file_path if file_path is not None else "."
        binary: str = self.binary  # type: ignore[assignment]  # guarded in __init__
        try:
            result = subprocess.run(
                [binary, "run", "--json", "-p", pattern, target],
                capture_output=True,
                text=True,
                timeout=30.0,
                cwd=self.workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("ast-grep run failed (%s): %s", type(exc).__name__, exc)
            return []
        return parse_astgrep_matches(result.stdout, pattern)[:limit]

    # ── Protocol surface ──────────────────────────────────────

    def definition(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Structural search cannot resolve definitions — callers should use
        # the LSP or graph provider for precise semantics.
        return []

    def references(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Lexical approximation: every structural match of the symbol name.
        pattern = symbol or query
        return self.search(pattern, file_path=file_path, limit=limit)

    def callers(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Call sites of a function are approximated as call-expression
        # patterns (e.g. ``foo($$$)``). This is structural, not semantic —
        # precise call edges belong to the graph provider.
        name = symbol or query
        pattern = f"{name}($$$)"
        return self.search(pattern, file_path=file_path, limit=limit)

    def callees(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Requires walking a function body — out of scope for pattern search.
        return []

    def implementations(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Type-hierarchy queries need a language server — not structural.
        return []

    def symbols(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        pattern = symbol or query
        matches = self.search(pattern, file_path=file_path, limit=limit)
        for m in matches:
            m["kind"] = "symbol"
        return matches

    def contract(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Approximate blast radius: all references to the symbol. Real
        # dependency-graph impact belongs to the graph provider.
        pattern = symbol or query
        return self.search(pattern, file_path=file_path, limit=limit)

    def history(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def prs(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []

    def cross_repo(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return []
