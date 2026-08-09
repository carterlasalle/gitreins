"""
engine.github — GitHub/PR mode: the ChangeSource abstraction (DESIGN_v2.md
§14, §16).

The reviewer no longer cares whether the change is a local working tree, a
commit range, or a GitHub PR — the same ReviewOrchestrator engine runs
everywhere:

    CODING AGENT → local pre-commit review (WorkingTreeChangeSource)
        → git push → PR review (PullRequestChangeSource)
        → SAME REVIEW ENGINE → publisher + status checks

Public surface: the three sources + Diff/ChangeSource protocol
(checkout.py), publish_comments (publisher.py), set_status_check (checks.py),
and run_pr_review (app.py) — the thin PR glue over the orchestrator.
"""

from engine.github.app import PrReviewResult, run_pr_review
from engine.github.checks import VALID_STATES, set_status_check
from engine.github.checkout import (
    ChangeSource,
    ChangeSourceError,
    CommitRangeChangeSource,
    Diff,
    PullRequestChangeSource,
    WorkingTreeChangeSource,
)
from engine.github.publisher import PublishResult, publish_comments

__all__ = [
    "ChangeSource",
    "ChangeSourceError",
    "Diff",
    "WorkingTreeChangeSource",
    "CommitRangeChangeSource",
    "PullRequestChangeSource",
    "PublishResult",
    "publish_comments",
    "VALID_STATES",
    "set_status_check",
    "PrReviewResult",
    "run_pr_review",
]
