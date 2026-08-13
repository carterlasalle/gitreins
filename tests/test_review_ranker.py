"""
R2.9 tests for engine/review/ranker — deterministic usefulness ranking.

Covers: severity ordering across all five levels, the confidence /
execution-path / developer-relevance tie-breaks, the stable (file, line,
claim) final tie-break, determinism (same input twice → identical order),
dict and dataclass inputs (ReviewFinding / VerifierFinding),
unknown-severity handling, and the empty case.

Hermetic: pure functions — no LLM, no I/O.
"""

from typing import Any

from engine.review import rank_findings
from engine.review.ranker import SEVERITY_RANK, finding_to_dict
from engine.review.reviewers import ReviewFinding
from engine.review.verifier import VerifierFinding


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


def make_verdict(**overrides: Any):
    """A Lane-C VerifierFinding (impact + §12 verifier factors)."""
    defaults: dict[str, Any] = dict(
        finding_id="F19",
        impact="high",
        patch_causality="confirmed",
        reproducible=True,
        execution_path_confirmed=True,
        verifier_confidence=0.95,
        developer_relevance="high",
        verdict="CONFIRMED",
        notes="",
    )
    defaults.update(overrides)
    return VerifierFinding(**defaults)


def make_dict(**overrides: Any):
    """A raw dict finding with every rank factor available."""
    defaults: dict[str, Any] = dict(
        claim="claim text",
        severity="high",
        file="a.py",
        line=1,
        evidence=["E1"],
        verifier_confidence=0.5,
        execution_path_confirmed=False,
        developer_relevance="medium",
    )
    defaults.update(overrides)
    return defaults


# ── (a) Severity ordering ────────────────────────────────────────────────


class TestSeverityOrdering:
    def test_all_five_severities_ordered(self):
        findings = [
            make_finding(claim="info one", severity="info"),
            make_finding(claim="critical one", severity="critical"),
            make_finding(claim="low one", severity="low"),
            make_finding(claim="medium one", severity="medium"),
            make_finding(claim="high one", severity="high"),
        ]
        ranked = rank_findings(findings)
        assert [f.severity for f in ranked] == [
            "critical",
            "high",
            "medium",
            "low",
            "info",
        ]

    def test_severity_dominates_alphabetical_claim_order(self):
        """critical beats high even when its claim sorts later alphabetically."""
        ranked = rank_findings(
            [
                make_finding(claim="zzz", severity="high"),
                make_finding(claim="aaa", severity="critical"),
            ]
        )
        assert [f.severity for f in ranked] == ["critical", "high"]

    def test_unknown_severity_sorts_last(self):
        ranked = rank_findings(
            [
                make_finding(claim="mystery", severity="bogus"),
                make_finding(claim="known", severity="info"),
            ]
        )
        assert [f.severity for f in ranked] == ["info", "bogus"]

    def test_severity_rank_mapping(self):
        assert SEVERITY_RANK == {
            "critical": 0,
            "high": 1,
            "medium": 2,
            "low": 3,
            "info": 4,
        }


# ── (b) Tie-breaks ───────────────────────────────────────────────────────


class TestTieBreaks:
    def test_confidence_descending(self):
        ranked = rank_findings(
            [
                make_dict(claim="c", verifier_confidence=0.4),
                make_dict(claim="c", verifier_confidence=0.9),
            ]
        )
        assert [f["verifier_confidence"] for f in ranked] == [0.9, 0.4]

    def test_execution_path_confirmed_first(self):
        ranked = rank_findings(
            [
                make_dict(claim="c", execution_path_confirmed=False),
                make_dict(claim="c", execution_path_confirmed=True),
            ]
        )
        assert [f["execution_path_confirmed"] for f in ranked] == [True, False]

    def test_developer_relevance_order(self):
        ranked = rank_findings(
            [
                make_dict(claim="c", developer_relevance="low"),
                make_dict(claim="c", developer_relevance="high"),
                make_dict(claim="c", developer_relevance="medium"),
            ]
        )
        assert [f["developer_relevance"] for f in ranked] == [
            "high",
            "medium",
            "low",
        ]

    def test_verifier_finding_uses_impact_and_confidence(self):
        """VerifierFinding ranks on impact (severity) before confidence."""
        ranked = rank_findings(
            [
                make_verdict(
                    finding_id="F1",
                    impact="medium",
                    verifier_confidence=0.99,
                    execution_path_confirmed=True,
                ),
                make_verdict(
                    finding_id="F2",
                    impact="high",
                    verifier_confidence=0.5,
                    execution_path_confirmed=False,
                ),
            ]
        )
        assert [v.finding_id for v in ranked] == ["F2", "F1"]

    def test_stable_tie_break_file_then_line_then_claim(self):
        ranked = rank_findings(
            [
                make_dict(claim="zzz", file="b.py", line=1),
                make_dict(claim="aaa", file="a.py", line=9),
                make_dict(claim="mmm", file="a.py", line=2),
            ]
        )
        assert [f["file"] for f in ranked] == ["a.py", "a.py", "b.py"]
        assert [f["line"] for f in ranked] == [2, 9, 1]
        assert [f["claim"] for f in ranked] == ["mmm", "aaa", "zzz"]


# ── (c) Determinism ──────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_twice_identical_order(self):
        findings = [
            make_dict(claim="a", severity="low", file="f1.py", line=3),
            make_dict(claim="b", severity="critical", file="f2.py", line=1),
            make_dict(claim="c", severity="medium", file="f1.py", line=1),
            make_dict(claim="d", severity="high", file="f3.py", line=2),
            make_dict(claim="e", severity="info", file="f2.py", line=9),
            make_dict(claim="f", severity="critical", file="f1.py", line=1),
        ]
        assert rank_findings(findings) == rank_findings(findings)
        assert [f["claim"] for f in rank_findings(findings)] == [
            f["claim"] for f in rank_findings(findings)
        ]

    def test_returns_same_objects(self):
        findings = [make_finding(claim="x"), make_finding(claim="y")]
        ranked = rank_findings(findings)
        assert {id(f) for f in ranked} == {id(f) for f in findings}


# ── (d) Input flexibility ────────────────────────────────────────────────


class TestInputs:
    def test_empty(self):
        assert rank_findings([]) == []

    def test_mixed_dataclass_and_dict(self):
        ranked = rank_findings(
            [
                make_finding(claim="low one", severity="low"),
                make_dict(claim="crit one", severity="critical"),
            ]
        )
        assert [finding_to_dict(f)["claim"] for f in ranked] == [
            "crit one",
            "low one",
        ]

    def test_finding_to_dict_normalizes_all_types(self):
        assert finding_to_dict(make_finding())["severity"] == "medium"
        assert finding_to_dict(make_verdict())["impact"] == "high"
        assert finding_to_dict({"a": 1}) == {"a": 1}
