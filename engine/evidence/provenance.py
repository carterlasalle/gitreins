"""R2.4 Provenance — rich review history (DESIGN_v2.md §13).

Every finding/claim carries full provenance: which role+model generated it,
which model verified it (and the verdict), and the evidence refs it cites
(``static:semgrep:17``, ``graph:caller:Session.refresh``,
``source:auth/session.py:181``, ``test:verification-F19``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.evidence.models import PREFIX_TO_KIND

if TYPE_CHECKING:
    from engine.evidence.store import EvidenceStore


@dataclass
class Provenance:
    """Provenance of a claim/finding (DESIGN_v2.md §13 example).

    ``generated_by`` is ``{"role": ..., "model": ...}``; ``verified_by`` is
    ``{"model": ..., "verdict": ...}`` (e.g. ``{"verdict": "confirmed"}``).
    """

    id: str
    evidence_refs: list[str]
    generated_by: dict  # {"role": ..., "model": ...}
    verified_by: dict  # {"model": ..., "verdict": ...}
    claim: str = ""

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (round-trips via ``from_dict``)."""
        return {
            "id": self.id,
            "claim": self.claim,
            "evidence_refs": list(self.evidence_refs),
            "generated_by": dict(self.generated_by),
            "verified_by": dict(self.verified_by),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Provenance":
        """Rebuild a Provenance from the dict produced by ``to_dict``."""
        missing = [
            k for k in ("id", "evidence_refs", "generated_by", "verified_by") if k not in data
        ]
        if missing:
            raise KeyError(f"Provenance.from_dict missing keys: {', '.join(missing)}")
        return cls(
            id=data["id"],
            evidence_refs=list(data["evidence_refs"]),
            generated_by=dict(data["generated_by"]),
            verified_by=dict(data["verified_by"]),
            claim=data.get("claim", ""),
        )


def record(
    store: "EvidenceStore",
    *,
    evidence_refs: list[str],
    generated_by: dict,
    verified_by: dict,
    claim: str = "",
    id: str = "",
) -> Provenance:
    """Build a provenance record and append it to the store.

    An empty ``id`` gets a store-assigned ``P<n>`` id. Returns the appended
    record (mutated in place with its final id).
    """
    prov = Provenance(
        id=id,
        claim=claim,
        evidence_refs=list(evidence_refs),
        generated_by=dict(generated_by),
        verified_by=dict(verified_by),
    )
    return store.add_provenance(prov)


def resolve_ref(ref: str) -> dict:
    """Parse a claim ref string into store lookup keys.

    Supported formats (DESIGN_v2.md §13):
      ``static:semgrep:17``          -> kind=static_analysis, source=semgrep,
                                        ordinal=17
      ``graph:caller:Session.refresh`` -> kind=call_edge, source=caller,
                                        key=Session.refresh
      ``source:auth/session.py:181`` -> kind=reference, file=auth/session.py,
                                        line=181
      ``test:verification-F19``      -> kind=test, id=verification-F19
      ``E17``                        -> id=E17

    Bare ids (no ``:``) and unknown prefixes resolve to a literal id lookup.
    Raises ``ValueError`` on structurally malformed refs.
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"invalid evidence ref: {ref!r}")
    if ":" not in ref:
        return {"id": ref}

    parts = ref.split(":")
    prefix = parts[0]
    kind = PREFIX_TO_KIND.get(prefix)
    if kind is None:
        # Unknown prefix — treat the whole string as a literal id.
        return {"id": ref}
    rest = parts[1:]

    if kind == "static_analysis":
        # static:<source>:<ordinal>
        if len(rest) != 2:
            raise ValueError(f"malformed static ref {ref!r}: expected static:<source>:<ordinal>")
        try:
            ordinal = int(rest[1])
        except ValueError:
            raise ValueError(
                f"malformed static ref {ref!r}: ordinal must be an integer, got {rest[1]!r}"
            ) from None
        return {"kind": kind, "source": rest[0], "ordinal": ordinal}

    if kind == "call_edge":
        # graph:<source>:<key>
        if len(rest) != 2:
            raise ValueError(f"malformed graph ref {ref!r}: expected graph:<source>:<key>")
        return {"kind": kind, "source": rest[0], "key": rest[1]}

    if kind == "reference":
        # source:<file>[:<line>]
        if len(rest) == 1:
            return {"kind": kind, "file": rest[0], "line": None}
        if len(rest) == 2:
            try:
                line = int(rest[1])
            except ValueError:
                raise ValueError(
                    f"malformed source ref {ref!r}: line must be an integer, got {rest[1]!r}"
                ) from None
            return {"kind": kind, "file": rest[0], "line": line}
        raise ValueError(f"malformed source ref {ref!r}: expected source:<file>[:<line>]")

    # Generic kinds: test:<id> or <prefix>:<source>:<key|ordinal>
    if len(rest) == 1:
        return {"kind": kind, "id": rest[0]}
    if len(rest) == 2:
        if rest[1].isdigit():
            return {"kind": kind, "source": rest[0], "ordinal": int(rest[1])}
        return {"kind": kind, "source": rest[0], "key": rest[1]}
    raise ValueError(f"malformed ref {ref!r}: too many ':' segments")


__all__ = ["Provenance", "record", "resolve_ref"]
