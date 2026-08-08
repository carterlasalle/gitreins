"""
R2.9 tests for engine/review/dedup — deterministic evidence-overlap dedup.

Covers: shared-evidence grouping, transitive grouping (A~B, B~C → one
group), keep-most-useful representative (severity, then confidence, then
specificity), file+line proximity grouping and its boundary, non-overlapping
findings all kept in input order, determinism (same input twice → identical
output), dict/dataclass inputs, and the empty case.

Hermetic: pure functions — no LLM, no I/O.
"""

from typing import Any

from engine.review import dedupe_findings
from engine.review.dedup import DEFAULT_PROXIMITY_LINES
from engine.review.reviewers import ReviewFinding


def make_finding(**overrides: Any):
    """A Lane-B ReviewFinding (severity, no verifier factors)."""
    defaults: dict[str, Any] = dict(
        claim="A defect claim.",
        severity="medium",
        file="auth/session.py",
        line=182,
        evidence=["E12"],
        category="null-deref",
        suggestion="",
    )
    defaults.update(overrides)
    return ReviewFinding(**defaults)


def make_dict(**overrides: Any):
    """A raw dict finding with every usefulness factor available."""
    defaults = dict(
        claim="claim text",
        severity="high",
        file="a.py",
        line=10,
        evidence=["E1"],
        verifier_confidence=0.5,
        execution_path_confirmed=False,
        developer_relevance="medium",
    )
    defaults.update(overrides)
    return defaults


# ── (a) Evidence-overlap grouping ────────────────────────────────────────


class TestEvidenceOverlap:
    def test_shared_evidence_dedupes(self):
        findings = [
            make_finding(claim="short", severity="info", evidence=["E12"]),
            make_finding(
                claim="the real detailed claim", severity="critical",
                evidence=["E12"],
            ),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 1
        assert kept[0].severity == "critical"

    def test_transitive_overlap_groups_all(self):
        """A shares E1 with B, B shares E2 with C → A, B, C are one group."""
        findings = [
            make_dict(claim="a", severity="low", evidence=["E1"], file="x.py", line=1),
            make_dict(claim="b", severity="medium", evidence=["E1", "E2"], file="x.py", line=2),
            make_dict(claim="c", severity="high", evidence=["E2"], file="x.py", line=3),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 1
        assert kept[0]["severity"] == "high"

    def test_partial_evidence_overlap_keeps_most_useful(self):
        findings = [
            make_dict(claim="dup", severity="low", evidence=["E1", "E2"]),
            make_dict(claim="better", severity="medium", evidence=["E2"]),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 1
        assert kept[0]["claim"] == "better"

    def test_no_overlap_keeps_all_in_input_order(self):
        findings = [
            make_dict(claim="a", evidence=["E1"], file="a.py", line=1),
            make_dict(claim="b", evidence=["E2"], file="b.py", line=100),
            make_dict(claim="c", evidence=["E3"], file="c.py", line=200),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 3
        assert [k["claim"] for k in kept] == ["a", "b", "c"]


# ── (b) Usefulness — which representative survives ───────────────────────


class TestUsefulness:
    def test_severity_wins_over_confidence(self):
        """critical beats high regardless of confidence."""
        findings = [
            make_dict(claim="x", severity="critical", verifier_confidence=0.1),
            make_dict(claim="x", severity="high", verifier_confidence=0.99, evidence=["E1", "E9"]),
        ]
        kept = dedupe_findings(findings)
        assert kept[0]["severity"] == "critical"

    def test_confidence_breaks_severity_tie(self):
        findings = [
            make_dict(claim="x", severity="high", verifier_confidence=0.3),
            make_dict(claim="x", severity="high", verifier_confidence=0.9),
        ]
        kept = dedupe_findings(findings)
        assert kept[0]["verifier_confidence"] == 0.9

    def test_specificity_line_present_beats_missing(self):
        findings = [
            make_dict(claim="x", severity="high", line=None),
            make_dict(claim="x", severity="high", line=42),
        ]
        kept = dedupe_findings(findings)
        assert kept[0]["line"] == 42

    def test_specificity_more_evidence_beats_fewer(self):
        findings = [
            make_dict(claim="x", severity="high", evidence=["E1"]),
            make_dict(claim="x", severity="high", evidence=["E1", "E2", "E3"]),
        ]
        kept = dedupe_findings(findings)
        assert kept[0]["evidence"] == ["E1", "E2", "E3"]

    def test_specificity_longer_claim_beats_shorter(self):
        findings = [
            make_dict(claim="short", severity="high"),
            make_dict(claim="a much longer and more specific claim", severity="high"),
        ]
        kept = dedupe_findings(findings)
        assert kept[0]["claim"] == "a much longer and more specific claim"


# ── (c) File+line proximity grouping ─────────────────────────────────────


class TestLineProximity:
    def test_nearby_lines_same_file_grouped(self):
        findings = [
            make_dict(claim="a", file="a.py", line=10, evidence=[]),
            make_dict(claim="b", file="a.py", line=15, evidence=[]),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 1

    def test_far_lines_same_file_kept_separate(self):
        findings = [
            make_dict(claim="a", file="a.py", line=10, evidence=[]),
            make_dict(claim="b", file="a.py", line=100, evidence=[]),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 2

    def test_proximity_boundary_is_inclusive(self):
        assert DEFAULT_PROXIMITY_LINES == 10
        # |15 - 5| = 10 → grouped
        findings = [
            make_dict(claim="a", file="a.py", line=5, evidence=[]),
            make_dict(claim="b", file="a.py", line=15, evidence=[]),
        ]
        assert len(dedupe_findings(findings)) == 1
        # |16 - 5| = 11 → kept separate
        findings = [
            make_dict(claim="a", file="a.py", line=5, evidence=[]),
            make_dict(claim="b", file="a.py", line=16, evidence=[]),
        ]
        assert len(dedupe_findings(findings)) == 2

    def test_same_file_without_lines_not_grouped_by_proximity(self):
        """No concrete lines → no proximity edge (evidence still can group)."""
        findings = [
            make_dict(claim="a", file="a.py", line=None, evidence=[]),
            make_dict(claim="b", file="a.py", line=None, evidence=[]),
        ]
        kept = dedupe_findings(findings)
        assert len(kept) == 2


# ── (d) Order preservation + determinism ─────────────────────────────────


class TestOrderAndDeterminism:
    def test_groups_emit_in_first_appearance_order(self):
        """Group starting at index 2 emits before group starting at index 5."""
        findings = [
            make_dict(claim="a", evidence=["E1"], file="a.py", line=1),
            make_dict(claim="b", evidence=["E2"], file="b.py", line=2),
            make_dict(claim="c1", severity="low", evidence=["E3"], file="c.py", line=3),
            make_dict(claim="c2", severity="high", evidence=["E3"], file="c.py", line=4),
            make_dict(claim="d", evidence=["E4"], file="d.py", line=5),
            make_dict(claim="e1", severity="low", evidence=["E5"], file="e.py", line=6),
            make_dict(claim="e2", severity="high", evidence=["E5"], file="e.py", line=7),
        ]
        kept = dedupe_findings(findings)
        assert [k["claim"] for k in kept] == ["a", "b", "c2", "d", "e2"]

    def test_deterministic_same_input_twice(self):
        findings = [
            make_dict(claim="a", evidence=["E1"], file="a.py", line=1),
            make_dict(claim="x1", severity="low", evidence=["E5"], file="x.py", line=10),
            make_dict(claim="x2", severity="high", evidence=["E5"], file="x.py", line=11),
            make_dict(claim="b", evidence=["E2"], file="b.py", line=2),
        ]
        assert dedupe_findings(findings) == dedupe_findings(findings)

    def test_empty_input(self):
        assert dedupe_findings([]) == []

    def test_returns_original_objects(self):
        findings = [
            make_finding(claim="x", severity="low", evidence=["E1"]),
            make_finding(claim="y", severity="high", evidence=["E1"]),
        ]
        kept = dedupe_findings(findings)
        assert kept == [findings[1]]
        assert kept[0] is findings[1]

    def test_accepts_dataclass_and_dict_mixed(self):
        kept = dedupe_findings([
            make_finding(claim="dup", severity="info", evidence=["E1"]),
            make_dict(claim="better", severity="critical", evidence=["E1"]),
        ])
        assert len(kept) == 1
        assert kept[0]["severity"] == "critical"
