"""R2.5 code-intelligence providers — small structured answers, not files.

DESIGN_v2.md §5 / §16: ``engine/codeintel/`` provides the provider
interface (``base.py``), the always-available lexical fallback
(``ripgrep.py``), structural pattern search (``astgrep.py``), LSP semantics
(``lsp.py``), the persistent-graph integration point (``graph.py``), and
the serena MCP wrapper (``serena.py``).

``get_provider(kind, workdir)`` is the registry factory: it checks binary
availability (``shutil.which``) and gracefully falls back to
:class:`RipgrepProvider` — the always-available default — when a requested
backend is not installed. Unknown kinds raise :class:`ValueError`.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.codeintel.base import CodeIntelProvider, CodeIntelUnavailableError
from engine.codeintel.astgrep import AstGrepProvider
from engine.codeintel.ripgrep import RipgrepProvider
from engine.codeintel.lsp import LspProvider
from engine.codeintel.graph import GraphProvider
from engine.codeintel.serena import SerenaProvider, SerenaUnavailableError

logger = logging.getLogger("gitreins.codeintel")

__all__ = [
    "CodeIntelProvider",
    "CodeIntelUnavailableError",
    "AstGrepProvider",
    "RipgrepProvider",
    "LspProvider",
    "GraphProvider",
    "SerenaProvider",
    "SerenaUnavailableError",
    "get_provider",
]

#: Provider kinds the factory understands.
PROVIDER_KINDS = ("astgrep", "ripgrep", "lsp", "graph", "serena")


def get_provider(
    kind: str = "ripgrep", workdir: str | None = None, **kwargs: Any
) -> CodeIntelProvider:
    """Factory: return the best provider for ``kind``, falling back gracefully.

    Availability is checked per kind (binary on PATH). When the requested
    backend is unavailable the factory returns :class:`RipgrepProvider` —
    the always-available default — with a logged note, so agents never get
    a broken provider. ``graph`` is always available (interface-only).

    Raises:
        ValueError — unknown provider kind.
    """
    kind = (kind or "ripgrep").lower()
    if kind not in PROVIDER_KINDS:
        raise ValueError(
            f"Unknown codeintel provider kind: {kind!r} "
            f"(expected one of {', '.join(PROVIDER_KINDS)})"
        )

    if kind == "ripgrep":
        return RipgrepProvider(workdir=workdir, **kwargs)
    if kind == "graph":
        return GraphProvider(workdir=workdir)

    if kind == "astgrep":
        if AstGrepProvider.available():
            return AstGrepProvider(workdir=workdir, **kwargs)
        logger.warning("ast-grep not found on PATH — falling back to RipgrepProvider")
        return RipgrepProvider(workdir=workdir)
    if kind == "lsp":
        tool = kwargs.get("tool")
        if LspProvider.available(tool):
            return LspProvider(workdir=workdir, **kwargs)
        logger.warning(
            "no LSP tool%s found on PATH — falling back to RipgrepProvider",
            f" '{tool}'" if tool else "",
        )
        return RipgrepProvider(workdir=workdir)
    if kind == "serena":
        if SerenaProvider.available():
            return SerenaProvider(workdir=workdir, **kwargs)
        logger.warning("serena not found on PATH — falling back to RipgrepProvider")
        return RipgrepProvider(workdir=workdir)

    raise ValueError(f"Unhandled provider kind: {kind!r}")  # pragma: no cover
