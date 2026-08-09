"""R2.5 code-intelligence provider interface — the CodeIntelProvider Protocol.

DESIGN_v2.md §5: "Define a provider interface and implement in priority
order" — SerenaProvider (LSP — precise current symbol semantics),
RepoGraphProvider (persistent topology), AstGrepProvider (structural pattern
search), RipgrepProvider (cheap lexical recall).

Every method takes ``(query, file_path=None, symbol=None, limit=N)`` and
returns **small structured answers** — a list of dicts with
``file``/``line``/``symbol``/``kind`` fields — NEVER full files. Providers
degrade gracefully: a capability a backend cannot answer returns ``[]``
(with a documented reason), never raises.
"""

from __future__ import annotations

from typing import Protocol


class CodeIntelProvider(Protocol):
    """Structural / semantic code-intelligence queries against a repo.

    All methods return ``list[dict]``; each dict is a small structured
    answer (e.g. ``{"file": ..., "line": ..., "kind": ...}``). ``limit``
    caps the number of results. Unsupported capabilities return ``[]``.
    """

    def definition(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def references(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def callers(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def callees(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def implementations(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def symbols(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def contract(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def impact(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def history(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def prs(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def cross_repo(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...

    def get_cross_repo_impact(
        self,
        query: str,
        file_path: str | None = None,
        symbol: str | None = None,
        limit: int = 10,
    ) -> list[dict]: ...


class CodeIntelUnavailableError(RuntimeError):
    """Raised at *construction* when a provider's backend is not installed.

    Providers must never fail at import time — the module imports cleanly
    even when the backing binary (ast-grep, rg, serena, ...) is absent.
    """
