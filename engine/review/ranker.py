"""
R2.9 Ranker — deterministic, LLM-free usefulness ranking (DESIGN_v2.md §17
``rank`` step, type ``rank_findings``).

Verified findings (R2.8 output) are ranked by a deterministic key — no model
is ever asked *which finding is more useful*. The key combines, in order:

1. severity — ``critical > high > medium > low > info``
2. verifier confidence — higher first (0.0 when absent)
3. execution_path_confirmed — traced trigger→impact paths first
4. developer_relevance — ``high > medium > low``

with a stable final tie-break over ``(file, line, claim)`` so the output is
reproducible across runs and machines.

Pure module: no LLM calls, no I/O. Input findings may be dataclasses
(:class:`engine.review.reviewers.ReviewFinding`,
:class:`engine.review.verifier.VerifierFinding`) or plain dicts; the
``finding_*`` helpers are the single normalization home shared by dedup.py
and writer.py.
"""

from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, cast

__all__ = [
    "SEVERITY_RANK",
    "DEVELOPER_RELEVANCE_RANK",
    "finding_to_dict",
    "finding_severity",
    "finding_confidence",
    "finding_execution_path_confirmed",
    "finding_developer_relevance",
    "finding_evidence",
    "rank_findings",
]

#: Severity ordering — lower rank = more severe = more useful (critical first).
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: Developer-relevance ordering — high first.
DEVELOPER_RELEVANCE_RANK = {"high": 0, "medium": 1, "low": 2}


# ── Finding normalization (shared by ranker / dedup / writer) ────────────


def finding_to_dict(finding: Any) -> dict:
    """Normalize a finding (dataclass or dict) to a plain dict.

    Dataclasses with a ``to_dict`` method (ReviewFinding, VerifierFinding)
    use it; other dataclasses convert via ``dataclasses.asdict``; dicts are
    copied. Raises TypeError for anything else.
    """
    if isinstance(finding, dict):
        return dict(finding)
    to_dict = getattr(finding, "to_dict", None)
    if callable(to_dict):
        return dict(cast(dict, to_dict()))
    if is_dataclass(finding):
        return asdict(finding)  # type: ignore[arg-type]
    raise TypeError(f"unsupported finding type: {type(finding).__name__}")


def finding_severity(f: dict) -> str:
    """Normalized severity: ``severity`` (ReviewFinding) or ``impact`` (VerifierFinding)."""
    return str(f.get("severity") or f.get("impact") or "")


def finding_confidence(f: dict) -> float:
    """Verifier confidence (``verifier_confidence`` / ``confidence``), 0.0 when absent."""
    raw = f.get("verifier_confidence", f.get("confidence", 0.0))
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def finding_execution_path_confirmed(f: dict) -> bool:
    """Whether the verifier traced the trigger→impact execution path."""
    return bool(f.get("execution_path_confirmed", False))


def finding_developer_relevance(f: dict) -> str:
    """Developer-relevance level (high/medium/low), defaulting to medium."""
    return str(f.get("developer_relevance", "medium") or "medium")


def finding_evidence(f: dict) -> list[str]:
    """Evidence refs backing the claim (``evidence`` or ``evidence_refs``)."""
    refs = f.get("evidence") or f.get("evidence_refs") or []
    return [str(r) for r in refs]


# ── Ranking ──────────────────────────────────────────────────────────────


def _sort_key(f: dict) -> tuple:
    """Full deterministic key: usefulness factors desc, then (file, line, claim).

    Severity and developer-relevance ranks are ascending (lower = more
    useful); confidence and execution-path are descending (higher = more
    useful). Unknown severities/relevances sort last (rank = len of the
    mapping), so garbage model output can never outrank a real ``info``
    finding.
    """
    sev_rank = SEVERITY_RANK.get(finding_severity(f), len(SEVERITY_RANK))
    rel_rank = DEVELOPER_RELEVANCE_RANK.get(
        finding_developer_relevance(f), len(DEVELOPER_RELEVANCE_RANK)
    )
    line = f.get("line")
    return (
        sev_rank,
        -finding_confidence(f),
        -int(finding_execution_path_confirmed(f)),
        rel_rank,
        str(f.get("file", "")),
        line if isinstance(line, int) else -1,
        str(f.get("claim", "")),
    )


def rank_findings(findings: Iterable[Any]) -> list:
    """Rank findings most-useful-first; returns the same objects, sorted.

    Deterministic: the key is (severity desc, confidence desc,
    execution_path_confirmed desc, developer_relevance desc, then
    file/line/claim ascending) — calling twice with the same input yields
    the identical order. Findings identical in every factor keep their input
    order (Python's stable sort).
    """
    items = list(findings)
    return sorted(items, key=lambda f: _sort_key(finding_to_dict(f)))
