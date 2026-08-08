"""
engine.evidence — R2.4 Evidence store (DESIGN_v2.md §6, §13).

First-class evidence for the review DAG: Lane A criteria evaluation and Lane B
defect review both cite claims as 'E17 / E31 / E44' (or tool refs like
'static:semgrep:17') instead of "assertions from nowhere". Review agents
reference evidence via ``EvidenceStore.evidence_for``; findings carry
``Provenance`` (generated_by / verified_by / evidence_refs).
"""

from engine.evidence.models import (
    EVIDENCE_KINDS,
    KIND_TO_PREFIX,
    PREFIX_TO_KIND,
    Evidence,
    EvidenceKind,
    EvidenceKindLiteral,
    format_evidence_id,
    make_ref,
)
from engine.evidence.producers import (
    from_command,
    from_static_diag,
    static_findings_to_evidence,
)
from engine.evidence.provenance import Provenance, record, resolve_ref
from engine.evidence.store import EvidenceStore

__all__ = [
    "Evidence",
    "EvidenceKind",
    "EvidenceKindLiteral",
    "EVIDENCE_KINDS",
    "KIND_TO_PREFIX",
    "PREFIX_TO_KIND",
    "EvidenceStore",
    "Provenance",
    "record",
    "resolve_ref",
    "format_evidence_id",
    "make_ref",
    "from_static_diag",
    "static_findings_to_evidence",
    "from_command",
]
