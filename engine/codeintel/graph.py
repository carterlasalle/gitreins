"""R2.5 GraphProvider — interface-only provider for a persistent repo graph.

DESIGN_v2.md §5: "RepoGraphProvider (YOUR graph — persistent topology /
flows / architecture)". This module defines the **integration point**: a
provider that a hilo/gitnexus/codegraph-style backend can later implement.

It deliberately does NOT shell out to hilo or any external graph tool —
every method returns an empty list, documented as "no backend wired yet".
The factory always returns this provider for ``kind='graph'`` (it is always
"available" — availability means the interface exists, not that a backend
is installed).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("gitreins.codeintel.graph")

#: Marker for tools/agents: results are empty because no graph backend is
#: configured — not because the query failed.
NO_BACKEND = {"status": "no_backend", "detail": "repo graph backend not wired (R2.6+)"}


class GraphProvider:
    """Placeholder for a persistent topology/flow/architecture graph.

    definition/references/impact and friends return ``[]`` — see
    :data:`NO_BACKEND` for the reason. To integrate a real backend,
    subclass this and override the methods (the Protocol in
    ``engine.codeintel.base`` is the contract).
    """

    def __init__(self, workdir: str | None = None):
        self.workdir = workdir or os.getcwd()

    @classmethod
    def available(cls) -> bool:
        # The interface itself is always available; a backend may not be.
        return True

    def definition(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def references(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def callers(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def callees(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def implementations(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def symbols(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def contract(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def history(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def prs(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def cross_repo(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        return [NO_BACKEND]

    def get_cross_repo_impact(self, query, file_path=None, symbol=None, limit=10) -> list[dict]:
        # Graph-backend-only query — the interface exists, the backend is
        # not wired yet. Same documented no-backend marker as cross_repo.
        return [NO_BACKEND]
