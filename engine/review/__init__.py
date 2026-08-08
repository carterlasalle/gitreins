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
reviewers). The adversarial verifier (verify_findings) is R2.8.
"""

from engine.review.context_builder import EvidencePlanner
from engine.review.orchestrator import (
    DEFAULT_REVIEWER_ROLES,
    ReviewOrchestrator,
    ReviewerResult,
    ReviewResult,
)
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
]
