"""
R2.10 publisher — publish CommentBatch results to a pull request
(DESIGN_v2.md §14/§16 ``engine/github/publisher.py``).

Mirrors the writer's "never raises" philosophy (writer.py: the fallback never
raises): posting a review comment must never crash the review DAG. API
failures are logged and reported in the returned :class:`PublishResult`
``failed`` list — the caller can surface them without losing the review.

Posting strategy:

- Comment with a ``line`` → PR review comment: ``POST
  /repos/{o}/{r}/pulls/{n}/comments`` with ``body``/``path``/``line``
  (``commit_id`` omitted → GitHub pins the head commit).
- Comment without a ``line`` (file/line-less finding) → issue conversation
  comment: ``POST /repos/{o}/{r}/issues/{n}/comments``.

Primary transport is ``gh api`` (same CLI as checkout.py); when gh is missing
or unauthenticated and a token is available (``token=`` or
``GITHUB_TOKEN``/``GH_TOKEN`` env), the call falls back to the GitHub REST
API via ``requests`` (already a project dependency).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from engine.github.checkout import ChangeSourceError, _gh_api
from engine.review.writer import Comment, CommentBatch

logger = logging.getLogger("gitreins.github.publisher")

__all__ = ["PublishResult", "publish_comments", "_requests_api", "_post_one"]


@dataclass
class PublishResult:
    """Outcome of publishing one CommentBatch — failures never raise."""

    posted: list[Comment] = field(default_factory=list)
    failed: list[Comment] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fallback_used: bool = False

    @property
    def ok(self) -> bool:
        """True when every comment was posted (or there were none)."""
        return not self.failed


def _post_one(
    owner: str,
    repo: str,
    pr_number: int,
    comment: Comment,
    *,
    gh_binary: str,
    token: str | None,
    timeout: float,
) -> bool:
    """Post one comment; raise on failure (caller decides how to degrade).

    Returns True when the requests REST fallback was used (gh unavailable),
    False when the comment went out via gh.
    """
    if comment.line is None:
        path = f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
        fields = {"body": comment.body}
    else:
        path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        fields = {"body": comment.body, "path": comment.file, "line": comment.line}
    try:
        _gh_api("POST", path, fields, gh_binary=gh_binary, timeout=timeout)
        return False
    except ChangeSourceError as gh_err:
        # gh missing/unauthenticated → REST fallback when a token is available.
        token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise
        logger.info("gh api unavailable (%s); falling back to requests REST", gh_err)
        _requests_api("POST", path, fields, token=token, timeout=min(timeout, 30.0))
        return True


def _requests_api(
    method: str,
    path: str,
    fields: dict,
    *,
    token: str,
    timeout: float = 30.0,
) -> dict:
    """GitHub REST call via requests (fallback transport when gh is absent).

    Uses the same JSON body as ``gh api --input -``. Raises on HTTP errors —
    the caller wraps failures into the result's ``failed`` list.
    """
    import requests

    resp = requests.request(
        method,
        f"https://api.github.com{path}",
        json={k: v for k, v in fields.items() if v is not None},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def publish_comments(
    owner: str,
    repo: str,
    pr_number: int,
    batch: CommentBatch,
    *,
    gh_binary: str = "gh",
    token: str | None = None,
    timeout: float = 60.0,
) -> PublishResult:
    """Publish every comment in ``batch`` to the PR — never raises.

    Each comment is posted independently: one API failure marks only that
    comment as failed; the rest still go out. An empty batch (clean review)
    posts nothing and returns an ok result.
    """
    result = PublishResult()
    if batch is None or not batch.comments:
        return result
    for comment in batch.comments:
        try:
            used_fallback = _post_one(
                owner,
                repo,
                pr_number,
                comment,
                gh_binary=gh_binary,
                token=token,
                timeout=timeout,
            )
            result.posted.append(comment)
            result.fallback_used = result.fallback_used or used_fallback
        except Exception as exc:  # noqa: BLE001 — publishing never raises
            logger.warning(
                "failed to publish comment %s:%s: %s", comment.file, comment.line, exc
            )
            result.failed.append(comment)
            result.errors.append(str(exc))
    return result
