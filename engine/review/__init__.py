"""
engine.review — R2.6 review front-end (DESIGN_v2.md §7, §16).

The scout runs before the agent loop: a cheap model classifies the changed
files into a **retrieval plan** (not a review), and a deterministic
EvidencePlanner executes that plan against a CodeIntelProvider, storing each
result as evidence for the review agents.

    ScoutAgent.plan(changed_files, diff) ──▶ ScoutPlan
    EvidencePlanner.execute(plan)         ──▶ list[Evidence]
"""

from engine.review.context_builder import EvidencePlanner
from engine.review.scout import ChangedSymbol, RetrievalRequest, ScoutAgent, ScoutPlan

__all__ = [
    "ScoutAgent",
    "ScoutPlan",
    "ChangedSymbol",
    "RetrievalRequest",
    "EvidencePlanner",
]
