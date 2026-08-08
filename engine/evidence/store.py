"""R2.4 EvidenceStore — thread-safe in-memory store with optional JSON replay.

The store is the foundation for the review DAG: review agents append evidence
(diff/symbol/reference/call_edge/static_analysis/...) and reference claims by
id (``E17``) or by tool ref (``static:semgrep:17``), which ``evidence_for``
resolves back to Evidence objects. Provenance records (DESIGN_v2.md §13) are
held alongside.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Iterable

from engine.evidence.models import Evidence, EvidenceKind, format_evidence_id
from engine.evidence.provenance import Provenance, resolve_ref


class EvidenceStore:
    """Thread-safe evidence store (DESIGN_v2.md §6/§13).

    In-memory dict keyed by evidence id plus an append-order index, so tool
    refs like ``static:semgrep:17`` (ordinal within kind+source) resolve
    deterministically. Pass ``path`` to persist every mutation as JSON and
    reload it on construction (replay).
    """

    def __init__(self, path: str | None = None):
        self._lock = threading.RLock()
        self._items: dict[str, Evidence] = {}
        self._order: list[str] = []
        self._provenance: list[Provenance] = []
        self._counter = 0
        self._prov_counter = 0
        self._path = path
        if path:
            self._load(path)

    # ── writes ──────────────────────────────────────────────────────────

    def append(self, evidence: Evidence | None = None, **fields) -> Evidence:
        """Add evidence, auto-assigning an ``E<n>`` id when ``id`` is empty.

        Accepts either an ``Evidence`` object or field kwargs (``kind``,
        ``source``, ...). Returns the stored object (with its final id).
        Raises ``KeyError`` on duplicate ids.
        """
        if evidence is None:
            evidence = Evidence(id=fields.pop("id", ""), **fields)
        if not isinstance(evidence, Evidence):
            raise TypeError(f"append() expects Evidence, got {type(evidence).__name__}")
        with self._lock:
            if not evidence.id:
                evidence.id = self._next_id()
            if evidence.id in self._items:
                raise KeyError(f"duplicate evidence id: {evidence.id!r}")
            self._items[evidence.id] = evidence
            self._order.append(evidence.id)
            self._persist()
            return evidence

    def add_provenance(self, prov: Provenance) -> Provenance:
        """Store a provenance record, auto-assigning a ``P<n>`` id when empty."""
        with self._lock:
            if not prov.id:
                prov.id = self._next_prov_id()
            if any(p.id == prov.id for p in self._provenance):
                raise KeyError(f"duplicate provenance id: {prov.id!r}")
            self._provenance.append(prov)
            self._persist()
            return prov

    # ── reads ───────────────────────────────────────────────────────────

    def get(self, id: str) -> Evidence | None:
        """Return evidence by exact id (``E17``, ``static:semgrep:17``), or None."""
        with self._lock:
            return self._items.get(id)

    def all(self) -> list[Evidence]:
        """All evidence in append order."""
        with self._lock:
            return [self._items[i] for i in self._order]

    def query(
        self,
        kind: str | EvidenceKind | None = None,
        source: str | None = None,
        file: str | None = None,
        id: str | None = None,
    ) -> list[Evidence]:
        """Filter evidence by any combination of kind/source/file/id (AND)."""
        if isinstance(kind, EvidenceKind):
            kind = kind.value
        with self._lock:
            result = []
            for e in self._iter_items():
                if kind is not None and e.kind != kind:
                    continue
                if source is not None and e.source != source:
                    continue
                if file is not None and e.file != file:
                    continue
                if id is not None and e.id != id:
                    continue
                result.append(e)
            return result

    def provenance(self, id: str | None = None) -> list[Provenance]:
        """Provenance records, in record order.

        With no argument, returns all records. With an argument, returns
        records whose own id matches OR whose ``evidence_refs`` cite the
        given evidence id/ref.
        """
        with self._lock:
            if id is None:
                return list(self._provenance)
            return [p for p in self._provenance if p.id == id or id in p.evidence_refs]

    def evidence_for(
        self, claim_refs: str | Iterable[str], *, strict: bool = False
    ) -> list[Evidence]:
        """Resolve claim ref strings back to Evidence objects.

        Accepts a single ref or an iterable; preserves input order. Resolution
        order: exact id match first, then the parsed lookup keys from
        ``resolve_ref`` (ordinal within kind+source, payload key, file/line
        span). Unresolvable refs are skipped unless ``strict=True``, which
        raises ``KeyError`` naming the first missing ref.
        """
        if isinstance(claim_refs, str):
            refs = [claim_refs]
        else:
            refs = list(claim_refs)
        found: list[Evidence] = []
        with self._lock:
            for ref in refs:
                ev = self._resolve_one(ref)
                if ev is None:
                    if strict:
                        raise KeyError(f"no evidence for ref: {ref!r}")
                    continue
                found.append(ev)
            return found

    # ── internals ───────────────────────────────────────────────────────

    def _iter_items(self) -> Iterable[Evidence]:
        return (self._items[i] for i in self._order)

    def _resolve_one(self, ref: str) -> Evidence | None:
        # 1. Exact id match ('E17', 'static:semgrep:17', ...)
        if ref in self._items:
            return self._items[ref]
        # 2. Parse the ref into lookup keys.
        try:
            keys = resolve_ref(ref)
        except ValueError:
            return None
        if "id" in keys and keys["id"] in self._items:
            return self._items[keys["id"]]
        kind = keys.get("kind")
        if kind is None:
            return None
        candidates = [e for e in self._iter_items() if e.kind == kind]
        if "source" in keys:
            candidates = [e for e in candidates if e.source == keys["source"]]
        if "ordinal" in keys:
            idx = keys["ordinal"] - 1  # ordinals are 1-based
            if 0 <= idx < len(candidates):
                return candidates[idx]
            return None
        if "key" in keys:
            key = keys["key"]
            for e in candidates:
                if e.id == key:
                    return e
                if e.payload.get("key") == key:
                    return e
                if any(str(v) == key for v in e.payload.values()):
                    return e
            return None
        if "file" in keys:
            file = keys["file"]
            line = keys.get("line")
            for e in candidates:
                if e.file != file:
                    continue
                if line is None:
                    return e
                lo = e.line_start if e.line_start is not None else line
                hi = e.line_end if e.line_end is not None else line
                if lo <= line <= hi:
                    return e
            return None
        return None

    def _next_id(self) -> str:
        while True:
            self._counter += 1
            candidate = format_evidence_id(self._counter)
            if candidate not in self._items:
                return candidate

    def _next_prov_id(self) -> str:
        while True:
            self._prov_counter += 1
            candidate = f"P{self._prov_counter}"
            if not any(p.id == candidate for p in self._provenance):
                return candidate

    # ── JSON persistence (replay) ───────────────────────────────────────

    def _persist(self) -> None:
        if not self._path:
            return
        data = {
            "counter": self._counter,
            "prov_counter": self._prov_counter,
            "order": list(self._order),
            "evidence": [e.to_dict() for e in self._iter_items()],
            "provenance": [p.to_dict() for p in self._provenance],
        }
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path) as f:
            data = json.load(f)
        self._counter = data.get("counter", 0)
        self._prov_counter = data.get("prov_counter", 0)
        self._order = list(data.get("order", []))
        self._items = {}
        for d in data.get("evidence", []):
            ev = Evidence.from_dict(d)
            self._items[ev.id] = ev
        self._provenance = [
            Provenance.from_dict(d) for d in data.get("provenance", [])
        ]


__all__ = ["EvidenceStore"]
