"""
R2.10 checks — GitHub commit status checks (DESIGN_v2.md §14/§16
``engine/github/checks.py``).

``set_status_check`` posts a commit status via ``gh api POST
/repos/{o}/{r}/statuses/{sha}`` so the PR surface can report the review
outcome as a check: pending → success/failure/error.

Contract: an invalid ``state`` is a caller bug and raises ValueError; an API
failure (gh missing/unauthenticated/network) never raises — it logs and
returns False, matching the publisher's "never raises" philosophy.
"""

from __future__ import annotations

import logging

from engine.github.checkout import ChangeSourceError, _gh_api

logger = logging.getLogger("gitreins.github.checks")

__all__ = ["VALID_STATES", "set_status_check"]

#: GitHub commit status states (the only four the API accepts).
VALID_STATES = ("pending", "success", "failure", "error")

#: Default context for GitReins review status checks.
DEFAULT_CONTEXT = "gitreins/review"


def set_status_check(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str = "",
    context: str = DEFAULT_CONTEXT,
    *,
    gh_binary: str = "gh",
    timeout: float = 60.0,
) -> bool:
    """Set a commit status check; True on success, False on API failure.

    Args:
        owner/repo/sha: The repository and commit the status belongs to.
        state: One of ``pending``/``success``/``failure``/``error``
            (:data:`VALID_STATES`) — anything else raises ValueError.
        description: Short human-readable summary (e.g. the review verdict).
        context: Status-check name, e.g. ``gitreins/review``.

    Returns:
        True when the status was posted; False (with a logged warning) when
        the gh API call failed — never raises on API failure.
    """
    if state not in VALID_STATES:
        raise ValueError(
            f"invalid status state {state!r}; expected one of {', '.join(VALID_STATES)}"
        )
    fields = {"state": state}
    if description:
        fields["description"] = description
    if context:
        fields["context"] = context
    try:
        _gh_api(
            "POST",
            f"/repos/{owner}/{repo}/statuses/{sha}",
            fields,
            gh_binary=gh_binary,
            timeout=timeout,
        )
        return True
    except ChangeSourceError as exc:
        logger.warning(
            "status check %s/%s@%s (%s) failed: %s",
            owner,
            repo,
            sha,
            context,
            exc,
        )
        return False
