"""
R2.13 ReviewLearning — developer feedback → durable review rules (§13).

Developers accept or reject review findings; ReviewLearning converts that
feedback into rules scoped by ``{repo, paths}`` and persists them to a JSONL
file (default ``.gitreins/review_learning.jsonl``). ``retrieve(repo,
file_paths)`` returns only the rules relevant to the code under review
(repo match AND path-glob match), so a rejection learned on ``asr/**``
never leaks into a review of unrelated code.

Rules accumulate: repeating the same feedback (same repo + finding id +
source type) increments ``support_count`` instead of duplicating the rule.
Persistence is best-effort: rules are loaded lazily on first access,
mutated under a lock, and a failed write is logged — the in-memory state
survives and nothing ever raises on I/O failure.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger("gitreins.review.learning")

#: Default persistence path (relative to cwd / repo root).
DEFAULT_LEARNING_PATH = os.path.join(".gitreins", "review_learning.jsonl")

#: Rule source types (DESIGN_v2.md §13 shows developer_rejection).
SOURCE_ACCEPTANCE = "developer_acceptance"
SOURCE_REJECTION = "developer_rejection"

#: Default confidence for a freshly recorded developer response.
DEFAULT_CONFIDENCE = 0.8


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReviewRule:
    """One learned review rule (DESIGN_v2.md §13 ReviewLearning shape).

    ``scope`` is ``{"repo": ..., "paths": [glob, ...]}``; ``source`` is
    ``{"type": "developer_rejection" | "developer_acceptance", "finding":
    <id>}``. ``paths`` are fnmatch-style globs — a rule only applies to
    files matching one of them within ``repo``.
    """

    scope: dict
    rule: str
    source: dict
    confidence: float = DEFAULT_CONFIDENCE
    support_count: int = 1
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (round-trips via from_dict)."""
        return {
            "scope": dict(self.scope),
            "rule": self.rule,
            "source": dict(self.source),
            "confidence": self.confidence,
            "support_count": self.support_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewRule":
        """Rebuild a ReviewRule from the dict produced by to_dict."""
        return cls(
            scope=dict(data.get("scope") or {}),
            rule=str(data.get("rule", "")),
            source=dict(data.get("source") or {}),
            confidence=float(data.get("confidence", DEFAULT_CONFIDENCE)),
            support_count=int(data.get("support_count", 1)),
            created_at=str(data.get("created_at", "")),
        )


class ReviewLearning:
    """Durable store of developer-feedback review rules (§13).

    Args:
        path: JSONL persistence file; defaults to
            ``.gitreins/review_learning.jsonl``.
    """

    def __init__(self, path: str | None = None):
        self.path = path if path is not None else DEFAULT_LEARNING_PATH
        self._lock = threading.RLock()
        self._rules: list[ReviewRule] | None = None  # None until first load

    # ── Writes ──────────────────────────────────────────────────

    def record_developer_response(
        self,
        repo: str,
        file_paths: str | Iterable[str] | None,
        finding_id: str,
        accepted: bool,
        *,
        rule_text: str | None = None,
        confidence: float | None = None,
    ) -> ReviewRule:
        """Record accept/reject feedback on a finding as a rule.

        A rejection (``accepted=False``) records the developer saying the
        finding is wrong — the rule tells future reviews not to report that
        class of issue for these paths. An acceptance (``accepted=True``)
        records the developer confirming the finding matters. Repeating the
        same feedback (same repo + finding id + source type) increments the
        existing rule's ``support_count`` instead of duplicating it.

        Returns the recorded rule. Never raises on persistence failure.
        """
        source_type = SOURCE_ACCEPTANCE if accepted else SOURCE_REJECTION
        with self._lock:
            rules = self._load()
            existing = self._find_rule(repo, finding_id, source_type)
            if existing is not None:
                existing.support_count += 1
                if confidence is not None:
                    existing.confidence = confidence
                self._persist()
                return existing
            rule = ReviewRule(
                scope={"repo": str(repo), "paths": _as_path_list(file_paths)},
                rule=(rule_text if rule_text else _default_rule_text(source_type, finding_id)),
                source={"type": source_type, "finding": str(finding_id)},
                confidence=(confidence if confidence is not None else DEFAULT_CONFIDENCE),
                support_count=1,
            )
            rules.append(rule)
            self._persist()
            return rule

    def add_rule(
        self,
        scope: dict,
        rule: str,
        source: dict,
        confidence: float = DEFAULT_CONFIDENCE,
        support_count: int = 1,
    ) -> ReviewRule:
        """Add a rule directly with the full §13 shape; persists and returns it."""
        with self._lock:
            rules = self._load()
            created = ReviewRule(
                scope=dict(scope),
                rule=rule,
                source=dict(source),
                confidence=confidence,
                support_count=int(support_count),
            )
            rules.append(created)
            self._persist()
            return created

    # ── Reads ───────────────────────────────────────────────────

    def retrieve(self, repo: str, file_paths: str | Iterable[str] | None) -> list[ReviewRule]:
        """Rules relevant to ``repo`` + ``file_paths`` (repo + path-glob scope).

        A rule applies when its ``scope.repo`` matches ``repo`` AND any of
        its path globs matches any of ``file_paths``. A rule with no paths
        is repo-wide and applies to every file in the repo.
        """
        with self._lock:
            rules = self._load()
            files = _as_path_list(file_paths)
        return [r for r in rules if _matches_scope(r, repo, files)]

    # ── Internals ───────────────────────────────────────────────

    def _find_rule(self, repo: str, finding_id: str, source_type: str) -> ReviewRule | None:
        """The rule already recording this exact feedback, if any."""
        for rule in self._rules or []:
            if (
                rule.scope.get("repo") == repo
                and rule.source.get("type") == source_type
                and rule.source.get("finding") == finding_id
            ):
                return rule
        return None

    def _load(self) -> list[ReviewRule]:
        """Lazy-load rules from the JSONL file once; never raises."""
        if self._rules is not None:
            return self._rules
        rules: list[ReviewRule] = []
        try:
            if os.path.isfile(self.path):
                with open(self.path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rules.append(ReviewRule.from_dict(json.loads(line)))
                        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                            logger.warning("Skipping malformed review-learning line: %s", exc)
        except OSError as exc:
            logger.warning("Failed to load review learning from %s: %s", self.path, exc)
        self._rules = rules
        return rules

    def _persist(self) -> None:
        """Rewrite the JSONL file atomically; never raises (state kept in memory)."""
        if self._rules is None:
            return
        try:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    for rule in self._rules:
                        f.write(json.dumps(rule.to_dict(), sort_keys=True) + "\n")
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.warning("Failed to persist review learning to %s: %s", self.path, exc)


def _as_path_list(file_paths: str | Iterable[str] | None) -> list[str]:
    """Normalize file_paths (single str or iterable) to a list of strings."""
    if file_paths is None:
        return []
    if isinstance(file_paths, str):
        return [file_paths]
    return [str(p) for p in file_paths]


def _matches_scope(rule: ReviewRule, repo: str, files: list[str]) -> bool:
    """True when ``rule`` applies to ``repo`` and any of ``files``."""
    if str(rule.scope.get("repo", "")) != str(repo):
        return False
    globs = [str(p) for p in (rule.scope.get("paths") or [])]
    if not globs:
        return True  # repo-wide rule
    for file_path in files:
        for pattern in globs:
            if fnmatch.fnmatch(_norm_path(file_path), _norm_path(pattern)):
                return True
    return False


def _norm_path(path: str) -> str:
    """Strip a leading ./ so relative paths and globs compare cleanly."""
    return path[2:] if path.startswith("./") else path


def _default_rule_text(source_type: str, finding_id: str) -> str:
    """Fallback rule text when the feedback carries no explicit lesson."""
    if source_type == SOURCE_ACCEPTANCE:
        return (
            f"Developer confirmed finding {finding_id} — this class of "
            "issue matters for these paths."
        )
    return (
        f"Developer rejected finding {finding_id} — do not report this "
        "class of issue for these paths."
    )


__all__ = [
    "ReviewLearning",
    "ReviewRule",
    "DEFAULT_LEARNING_PATH",
    "SOURCE_ACCEPTANCE",
    "SOURCE_REJECTION",
]
