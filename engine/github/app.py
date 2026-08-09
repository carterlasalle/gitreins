"""
R2.10 run_pr_review — the PR review surface (DESIGN_v2.md §14/§16
``engine/github/app.py``).

Thin glue — ALL DAG logic lives in engine/review/orchestrator.py. The PR
surface does nothing the local surface doesn't:

    PullRequestChangeSource ──▶ ReviewOrchestrator.run (SAME engine as local)
        ──▶ rank + dedupe (deterministic, LLM-free)
        ──▶ CommentWriter batch
        ──▶ publish_comments + set_status_check summary

``workdir`` is the checkout the orchestrator's agents read from (callers pass
the PR checkout dir); ``router``/``config``/``provider``/``agents`` are the
same knobs ReviewOrchestrator accepts. The deterministic status mapping:

- review DAG errors      → ``error``
- critical/high comments → ``failure``
- everything else        → ``success``

ChangeSource failures (gh missing, PR not found) raise ChangeSourceError —
there is nothing to review; publishing failures never raise (publisher's
contract) and are reported in ``result.publish.failed``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from engine.github.checks import set_status_check
from engine.github.checkout import ChangeSource, PullRequestChangeSource
from engine.github.publisher import PublishResult, publish_comments
from engine.review.dedup import dedupe_findings
from engine.review.orchestrator import ReviewOrchestrator
from engine.review.ranker import rank_findings
from engine.review.writer import CommentBatch, CommentWriter

logger = logging.getLogger("gitreins.github.app")

__all__ = ["PrReviewResult", "run_pr_review", "BLOCKING_SEVERITIES", "DEFAULT_STATUS_CONTEXT"]

#: Status context name for the review summary check.
DEFAULT_STATUS_CONTEXT = "gitreins/review"

#: Comment severities that block the status check (→ ``failure``).
BLOCKING_SEVERITIES = ("critical", "high")


@dataclass
class PrReviewResult:
    """Outcome of one PR review run: findings → batch → publish + status."""

    owner: str
    repo: str
    pr_number: int
    head_sha: str = ""
    findings: list = field(default_factory=list)
    batch: CommentBatch = field(default_factory=CommentBatch)
    publish: PublishResult = field(default_factory=PublishResult)
    status: dict | None = None

    def to_dict(self) -> dict:
        """JSON-compatible summary for callers (CLI, MCP, CI)."""
        return {
            "owner": self.owner,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "findings_count": len(self.findings),
            "comments_posted": len(self.publish.posted),
            "comments_failed": len(self.publish.failed),
            "publish_errors": list(self.publish.errors),
            "summary": self.batch.summary,
            "status": self.status,
        }


def _status_for(batch: CommentBatch, review_ok: bool) -> tuple[str, str]:
    """Map review outcome → (status state, description) — deterministic."""
    if not review_ok:
        return "error", "review DAG had errors — see review comments"
    blocking = any(c.severity in BLOCKING_SEVERITIES for c in batch.comments)
    if blocking:
        return "failure", "review found critical or high severity issues"
    return "success", "review found no blocking issues"


def run_pr_review(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    source: ChangeSource | None = None,
    orchestrator: Any | None = None,
    writer: Any | None = None,
    router: Any | None = None,
    workdir: str = ".",
    config: dict | None = None,
    provider: Any | None = None,
    agents: dict | None = None,
    gh_binary: str = "gh",
    status_context: str = DEFAULT_STATUS_CONTEXT,
    token: str | None = None,
) -> PrReviewResult:
    """Run the full review DAG against a pull request and publish results.

    Args:
        owner/repo/pr_number: The pull request to review.
        source: Optional ChangeSource override (tests inject fakes here);
            defaults to PullRequestChangeSource(owner, repo, pr_number).
        orchestrator: Optional ReviewOrchestrator override (tests inject
            stubs); defaults to a fresh ReviewOrchestrator.
        writer: Optional CommentWriter override; defaults to a fresh
            CommentWriter. Takes the ranked+deduped findings.
        router/config/provider/agents: ReviewOrchestrator knobs — the same
            ones its constructor accepts.
        workdir: Repo checkout the orchestrator's agents read from.
        gh_binary: gh CLI path used for PR fetch, publishing, and checks.
        status_context: Name of the summary status check.
        token: GitHub token for the publisher's requests fallback (defaults
            to GITHUB_TOKEN/GH_TOKEN env when gh is unavailable).

    Returns:
        A :class:`PrReviewResult` with the merged findings, the writer batch,
        the publish outcome, and the status-check result.

    Raises:
        engine.github.checkout.ChangeSourceError: The PR could not be read
            (gh missing/unauthenticated/not found) — nothing to review.
    """
    src = source if source is not None else PullRequestChangeSource(
        owner, repo, pr_number, gh_binary=gh_binary
    )
    diff = src.diff()
    files = src.changed_files()

    orch = (
        orchestrator
        if orchestrator is not None
        else ReviewOrchestrator(
            router=router, workdir=workdir, config=config, provider=provider, agents=agents
        )
    )
    result = orch.run(files, diff.text)

    ranked = rank_findings(result.findings)
    deduped = dedupe_findings(ranked)

    writer_ = (
        writer
        if writer is not None
        else CommentWriter(router=router, workdir=workdir, config=config)
    )
    batch = writer_.run(deduped, changed_files=files, diff_context=diff.text)

    publish = publish_comments(
        owner, repo, pr_number, batch, gh_binary=gh_binary, token=token
    )

    state, description = _status_for(batch, result.all_ok)
    head_sha = src.head_sha()
    status: dict | None = None
    if head_sha:
        posted = set_status_check(
            owner,
            repo,
            head_sha,
            state,
            description=description,
            context=status_context,
            gh_binary=gh_binary,
        )
        status = {
            "state": state,
            "description": description,
            "context": status_context,
            "posted": posted,
        }
    else:
        logger.warning(
            "no head sha for %s/%s#%s — skipping status check", owner, repo, pr_number
        )

    return PrReviewResult(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        findings=list(result.findings),
        batch=batch,
        publish=publish,
        status=status,
    )
