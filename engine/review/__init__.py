"""
engine.review — R2.7 review ensemble front-end (DESIGN_v2.md §7-9, §16).

Lane B (defect discovery) runs as a DAG:

    ScoutAgent.plan(changed_files, diff) ──▶ ScoutPlan
    EvidencePlanner.execute(plan)         ──▶ list[Evidence] (→ EvidenceStore)
    ReviewOrchestrator.run_from_plan(...) ──▶ parallel reviewers (runtime /
                                               contracts / security_edges) →
                                               merged ReviewFinding list

The orchestrator also has a self-contained ``run(changed_files, diff)`` entry
that drives the whole DAG (diff evidence → scout → optional retrieval →
reviewers). The adversarial verifier (DESIGN_v2.md §9 verify_findings step,
one :class:`VerifierAgent` per candidate finding) is R2.8; the deterministic
rank + dedup stages (ranker.py / dedup.py) and the batched comment writer
(writer.py, :class:`CommentWriter`) are R2.9; pipeline wiring is R2.16.
"""

from engine.review.context_builder import EvidencePlanner
from engine.review.dedup import dedupe_findings
from engine.review.orchestrator import (
    DEFAULT_REVIEWER_ROLES,
    ReviewOrchestrator,
    ReviewerResult,
    ReviewResult,
)
from engine.review.ranker import finding_to_dict, rank_findings
from engine.review.reviewers import (
    ROLE_TO_REVIEWER,
    ContractReviewer,
    ReviewAgent,
    ReviewFinding,
    ReviewFindings,
    RuntimeReviewer,
    SecurityReviewer,
    serialize_evidence,
)
from engine.review.scout import ChangedSymbol, RetrievalRequest, ScoutAgent, ScoutPlan
from engine.review.verifier import (
    BLOCK,
    VerifierAgent,
    VerifierCandidate,
    VerifierFinding,
    VerifierFindings,
)
from engine.review.writer import Comment, CommentBatch, CommentWriter

__all__ = [
    "ScoutAgent",
    "ScoutPlan",
    "ChangedSymbol",
    "RetrievalRequest",
    "EvidencePlanner",
    "ReviewAgent",
    "RuntimeReviewer",
    "ContractReviewer",
    "SecurityReviewer",
    "ReviewFinding",
    "ReviewFindings",
    "ROLE_TO_REVIEWER",
    "DEFAULT_REVIEWER_ROLES",
    "serialize_evidence",
    "ReviewOrchestrator",
    "ReviewerResult",
    "ReviewResult",
    "VerifierAgent",
    "VerifierFinding",
    "VerifierFindings",
    "VerifierCandidate",
    "BLOCK",
    "rank_findings",
    "finding_to_dict",
    "dedupe_findings",
    "CommentWriter",
    "Comment",
    "CommentBatch",
]
