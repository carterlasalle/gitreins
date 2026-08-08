"""R2.4 Evidence model — the first-class evidence type (DESIGN_v2.md §6).

Every claim from a review agent references evidence by store id (``E17``) or by
a stable tool ref (``static:semgrep:17``) — never "assertions from nowhere".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

# The 12 evidence kinds (DESIGN_v2.md §6). The dataclass uses the Literal
# exactly as specified; EvidenceKind is the ergonomic enum wrapper.
EvidenceKindLiteral = Literal[
    "diff",
    "symbol",
    "reference",
    "call_edge",
    "static_analysis",
    "lsp",
    "test",
    "command",
    "history",
    "requirement",
    "repo_rule",
    "ci",
]

EVIDENCE_KINDS: tuple[str, ...] = (
    "diff",
    "symbol",
    "reference",
    "call_edge",
    "static_analysis",
    "lsp",
    "test",
    "command",
    "history",
    "requirement",
    "repo_rule",
    "ci",
)


class EvidenceKind(str, Enum):
    """Enum of the 12 evidence kinds (DESIGN_v2.md §6)."""

    DIFF = "diff"
    SYMBOL = "symbol"
    REFERENCE = "reference"
    CALL_EDGE = "call_edge"
    STATIC_ANALYSIS = "static_analysis"
    LSP = "lsp"
    TEST = "test"
    COMMAND = "command"
    HISTORY = "history"
    REQUIREMENT = "requirement"
    REPO_RULE = "repo_rule"
    CI = "ci"


# Ref prefix <-> kind mapping for tool-style refs like ``static:semgrep:17``.
KIND_TO_PREFIX: dict[str, str] = {
    "diff": "diff",
    "symbol": "symbol",
    "reference": "source",
    "call_edge": "graph",
    "static_analysis": "static",
    "lsp": "lsp",
    "test": "test",
    "command": "command",
    "history": "history",
    "requirement": "requirement",
    "repo_rule": "rule",
    "ci": "ci",
}

PREFIX_TO_KIND: dict[str, str] = {v: k for k, v in KIND_TO_PREFIX.items()}


def format_evidence_id(ordinal: int) -> str:
    """Build a monotonic store id, e.g. ``format_evidence_id(17) == "E17"``."""
    return f"E{ordinal}"


def make_ref(kind: str, source: str, key: str) -> str:
    """Build a stable tool-style ref, e.g. ``make_ref("static_analysis", "semgrep", "17")``.

    Returns ``"static:semgrep:17"`` — the format review agents use in claims.
    """
    prefix = KIND_TO_PREFIX.get(kind, kind)
    return f"{prefix}:{source}:{key}"


@dataclass
class Evidence:
    """A single piece of evidence in the review DAG (DESIGN_v2.md §6).

    ``id`` is either a store-assigned monotonic id (``E17``) or a stable
    tool-style ref (``static:semgrep:17``). An empty ``id`` means the store
    assigns one on ``append``.
    """

    id: str
    kind: EvidenceKindLiteral
    source: str
    file: str | None
    line_start: int | None
    line_end: int | None
    payload: dict

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (round-trips via ``from_dict``)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        """Rebuild an Evidence from the dict produced by ``to_dict``."""
        missing = [
            k
            for k in ("id", "kind", "source", "file", "line_start", "line_end", "payload")
            if k not in data
        ]
        if missing:
            raise KeyError(f"Evidence.from_dict missing keys: {', '.join(missing)}")
        kind = data["kind"]
        if isinstance(kind, EvidenceKind):
            kind = kind.value
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind: {kind!r}")
        return cls(
            id=data["id"],
            kind=kind,
            source=data["source"],
            file=data["file"],
            line_start=data["line_start"],
            line_end=data["line_end"],
            payload=dict(data["payload"]),
        )


__all__ = [
    "Evidence",
    "EvidenceKind",
    "EvidenceKindLiteral",
    "EVIDENCE_KINDS",
    "KIND_TO_PREFIX",
    "PREFIX_TO_KIND",
    "format_evidence_id",
    "make_ref",
]
