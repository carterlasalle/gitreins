"""
R2.6 EvidencePlanner — deterministic retrieval executor (DESIGN_v2.md §7).

The scout is a cheap model, so the system — not the model — does the
retrieval. This planner takes a :class:`~engine.review.scout.ScoutPlan` and a
:class:`~engine.codeintel.base.CodeIntelProvider`, executes each
``retrieval_request`` against the matching provider method, caps the results
(e.g. ``callers of foo → 3 callers``), and appends each hit to the evidence
store as ``Evidence``.

Evidence kinds map onto the existing 12-kind taxonomy (DESIGN_v2.md §6):
callers/callees → ``call_edge``, references → ``reference``,
definition/implementations/symbols → ``symbol``, all with
``source="codeintel"`` so the whole packet is queryable via
``store.query(source="codeintel")``.

The planner is deterministic and never raises: unsupported request types and
provider errors degrade to skipped/no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from engine.codeintel.base import CodeIntelProvider
from engine.evidence.models import Evidence
from engine.evidence.store import EvidenceStore
from engine.review.scout import RetrievalRequest, ScoutPlan

logger = logging.getLogger("gitreins.review.context_builder")

__all__ = ["EvidencePlanner", "REQUEST_TO_METHOD", "REQUEST_TO_KIND"]

#: Retrieval request type → CodeIntelProvider method name (R2.5 protocol).
REQUEST_TO_METHOD: dict[str, str] = {
    "callers": "callers",
    "references": "references",
    "definition": "definition",
    "implementations": "implementations",
    "callees": "callees",
    "symbols": "symbols",
}

#: Retrieval request type → evidence kind (existing taxonomy, §6).
REQUEST_TO_KIND: dict[str, str] = {
    "callers": "call_edge",
    "callees": "call_edge",
    "references": "reference",
    "definition": "symbol",
    "implementations": "symbol",
    "symbols": "symbol",
}

#: Evidence source tag for everything the planner stores.
EVIDENCE_SOURCE = "codeintel"


def _to_int(value: Any) -> int | None:
    """Best-effort line-number coercion; None for anything non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class EvidencePlanner:
    """Execute a ScoutPlan's retrieval requests against a code-intel provider.

    Deterministic (no LLM). ``execute()`` runs every ``retrieval_request`` in
    order, caps each result at ``default_limit`` (overridable per call), and
    appends one ``Evidence`` per hit to the store. Returns the appended
    evidence list in store order.

    Unsupported request types are skipped with a warning; provider failures
    are caught and degrade to ``[]``. The planner never raises.
    """

    def __init__(
        self,
        provider: CodeIntelProvider,
        store: EvidenceStore | None = None,
        *,
        default_limit: int = 3,
    ) -> None:
        self.provider = provider
        self.store = store if store is not None else EvidenceStore()
        self.default_limit = default_limit

    def execute(
        self, plan: ScoutPlan, *, limit: int | None = None
    ) -> list[Evidence]:
        """Execute the plan's retrieval requests; returns appended Evidence."""
        cap = limit if limit is not None else self.default_limit
        appended: list[Evidence] = []
        for request in plan.retrieval_requests:
            for hit in self._fetch(request, cap):
                appended.append(self._append(request, hit))
        return appended

    # ── internals ─────────────────────────────────────────────

    def _fetch(self, request: RetrievalRequest, cap: int) -> list[dict]:
        """Run one retrieval request, degrading to [] instead of raising."""
        method_name = REQUEST_TO_METHOD.get(request.type)
        if method_name is None:
            logger.warning(
                "Unsupported retrieval type %r (query=%r) — skipping",
                request.type,
                request.query,
            )
            return []
        method = getattr(self.provider, method_name, None)
        if method is None:
            logger.warning(
                "Provider has no %r method — skipping (type=%r)",
                method_name,
                request.type,
            )
            return []
        try:
            results = method(request.query, limit=cap)
        except Exception as exc:  # noqa: BLE001 — provider boundary: degrade, don't crash
            logger.warning("Retrieval %r failed: %s", request.type, exc)
            return []
        if not isinstance(results, list):
            logger.warning("Retrieval %r returned non-list — ignoring", request.type)
            return []
        return [r for r in results[:cap] if isinstance(r, dict)]

    def _append(self, request: RetrievalRequest, hit: dict) -> Evidence:
        """Convert one provider hit into Evidence and store it."""
        file = hit.get("file") if isinstance(hit.get("file"), str) else None
        line_start = _to_int(hit.get("line_start") or hit.get("line"))
        line_end = _to_int(hit.get("line_end")) or line_start
        ev = Evidence(
            id="",
            kind=REQUEST_TO_KIND.get(request.type, "reference"),  # type: ignore[arg-type]
            source=EVIDENCE_SOURCE,
            file=file,
            line_start=line_start,
            line_end=line_end,
            payload={
                "request_type": request.type,
                "query": request.query,
                "result": dict(hit),
            },
        )
        return self.store.append(ev)
