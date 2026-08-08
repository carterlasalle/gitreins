"""
R2.9 Dedup — deterministic finding dedup by evidence overlap + usefulness
(board R2.9; DESIGN_v2.md §17 "dedupe + usefulness rank").

Three reviewers run in parallel (R2.7) and routinely surface the same defect
from different angles: the same evidence refs, or the same file region. Pure
:func:`dedupe_findings` groups findings whose evidence refs overlap (a shared
evidence id, e.g. both cite ``E17``) OR whose locations are the same file
within :data:`DEFAULT_PROXIMITY_LINES`, then keeps the single most useful
representative per group — usefulness scored deterministically by (severity,
verifier confidence, specificity: line present → more evidence refs → longer
claim), never by an LLM. Groups emit in first-appearance order, and the
representatives are the original objects, so the output is reproducible
across runs.

No LLM calls, no I/O; findings may be dataclasses or dicts.
"""

from typing import Any, Iterable

from engine.review.ranker import (
    SEVERITY_RANK,
    finding_confidence,
    finding_evidence,
    finding_severity,
    finding_to_dict,
)

__all__ = ["dedupe_findings", "DEFAULT_PROXIMITY_LINES"]

#: Line-distance window for grouping findings located in the same file.
DEFAULT_PROXIMITY_LINES = 10


def _location(f: dict) -> tuple[str, int | None]:
    """(file, line) of a finding; line may be None when not pinpointed."""
    return str(f.get("file", "") or ""), f.get("line")


def _overlaps(a: dict, b: dict) -> bool:
    """True when two findings cite shared evidence or sit near each other.

    Either criterion suffices: a shared evidence id (both claims rest on
    ``E17``) or the same file with lines within
    :data:`DEFAULT_PROXIMITY_LINES` (the reviewers looked at the same region).
    """
    if set(finding_evidence(a)) & set(finding_evidence(b)):
        return True
    fa, la = _location(a)
    fb, lb = _location(b)
    if fa and fa == fb and isinstance(la, int) and isinstance(lb, int):
        return abs(la - lb) <= DEFAULT_PROXIMITY_LINES
    return False


def _groups(findings: list[dict]) -> list[list[int]]:
    """Union-find grouping: indices whose findings transitively overlap.

    Transitivity matters: A overlaps B and B overlaps C puts A, B and C in
    one group even when A and C do not directly overlap.
    """
    parent = list(range(len(findings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            if _overlaps(findings[i], findings[j]):
                union(i, j)

    roots: dict[int, list[int]] = {}
    for i in range(len(findings)):
        roots.setdefault(find(i), []).append(i)
    return sorted(roots.values(), key=min)


def _usefulness(index: int, f: dict) -> tuple:
    """Deterministic 'keep this one' score — higher tuple = more useful.

    Order: severity (critical first), verifier confidence, then specificity
    (pinpointed line > file-wide, more evidence refs, longer claim), with the
    original index as the final tie-break so earlier findings win ties.
    """
    sev_rank = SEVERITY_RANK.get(finding_severity(f), len(SEVERITY_RANK))
    specificity = (
        int(isinstance(f.get("line"), int)),  # pinpointed > file-wide
        len(finding_evidence(f)),  # more backing evidence
        len(str(f.get("claim", ""))),  # longer claim = more specific
    )
    return (-sev_rank, finding_confidence(f), *specificity, -index)


def dedupe_findings(findings: Iterable[Any]) -> list:
    """Deduplicate findings by evidence overlap + usefulness.

    Groups findings whose evidence refs overlap (a shared evidence id) or
    whose locations are the same file within :data:`DEFAULT_PROXIMITY_LINES`,
    keeps the most useful representative per group, and returns the
    representatives in the first-appearance order of their groups.
    Deterministic: the same input always yields the same output, and the
    returned objects are the original inputs (identity preserved).
    """
    items = list(findings)
    normalized = [finding_to_dict(f) for f in items]
    kept: list[Any] = []
    for group in _groups(normalized):
        rep = max(group, key=lambda i: _usefulness(i, normalized[i]))
        kept.append(items[rep])
    return kept
