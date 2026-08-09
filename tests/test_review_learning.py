"""
R2.13 tests for engine/review/learning — ReviewLearning (DESIGN_v2.md §13
developer feedback → durable review rules).

Covers: accept/reject feedback creating rules with the §13 shape, repo +
path-glob scoping (rules only return for relevant code, never for other
repos/paths), support_count incrementing on repeat feedback, the DESIGN
example rule round-trip via add_rule, JSONL persistence round-trip (write
then reload), malformed-line skipping, and graceful degradation (I/O
failures never raise).

Hermetic: pure filesystem + fnmatch — no LLM, no network, no git.
"""

import os

from engine.review.learning import (
    SOURCE_ACCEPTANCE,
    SOURCE_REJECTION,
    ReviewLearning,
    ReviewRule,
)


# ── (a) record_developer_response ────────────────────────────────────────


class TestRecordDeveloperResponse:
    def test_rejection_creates_rule(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        rule = learn.record_developer_response("mockingbird-dsp", ["asr/models.py"], "F821", False)
        assert isinstance(rule, ReviewRule)
        assert rule.scope == {"repo": "mockingbird-dsp", "paths": ["asr/models.py"]}
        assert rule.source == {"type": SOURCE_REJECTION, "finding": "F821"}
        assert rule.support_count == 1
        assert rule.confidence > 0
        assert rule.rule  # non-empty rule text

        # The rule is immediately retrievable for the relevant code.
        rules = learn.retrieve("mockingbird-dsp", ["asr/models.py"])
        assert len(rules) == 1
        assert rules[0] is rule

    def test_acceptance_creates_rule(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        rule = learn.record_developer_response("repo-a", ["src/x.py"], "F3", True)
        assert rule.source == {"type": SOURCE_ACCEPTANCE, "finding": "F3"}
        assert rule.scope == {"repo": "repo-a", "paths": ["src/x.py"]}

    def test_single_string_file_path_accepted(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("repo-a", "src/x.py", "F1", False)
        rules = learn.retrieve("repo-a", "src/x.py")
        assert len(rules) == 1

    def test_explicit_rule_text_and_confidence(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        rule = learn.record_developer_response(
            "mockingbird-dsp",
            ["asr/**"],
            "F821",
            False,
            rule_text="Unknown radio street names must never be normalized.",
            confidence=0.9,
        )
        assert rule.rule == "Unknown radio street names must never be normalized."
        assert rule.confidence == 0.9


# ── (b) repo + path-glob scoping ─────────────────────────────────────────


class TestScoping:
    def test_repo_scoping(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("mockingbird-dsp", ["asr/models.py"], "F1", False)

        assert len(learn.retrieve("mockingbird-dsp", ["asr/models.py"])) == 1
        # Same paths, different repo → no rules.
        assert learn.retrieve("other-repo", ["asr/models.py"]) == []
        assert learn.retrieve("mockingbird-dsp", []) == []

    def test_path_glob_scoping(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("mockingbird-dsp", ["asr/**"], "F1", False)

        assert len(learn.retrieve("mockingbird-dsp", ["asr/models.py"])) == 1
        assert len(learn.retrieve("mockingbird-dsp", ["asr/decoder/utils.py"])) == 1
        # A review touching several files only gets the rule when one matches.
        assert len(learn.retrieve("mockingbird-dsp", ["frontend/App.tsx"])) == 0
        assert len(learn.retrieve("mockingbird-dsp", ["frontend/App.tsx", "asr/models.py"])) == 1

    def test_repo_wide_rule_when_no_paths(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.add_rule(
            {"repo": "repo-a", "paths": []},
            "repo-wide rule",
            {"type": "manual", "finding": ""},
        )
        assert len(learn.retrieve("repo-a", ["any/file.py"])) == 1
        assert learn.retrieve("repo-b", ["any/file.py"]) == []

    def test_design_example_rule_retrieves_for_relevant_code(self, tmp_path):
        """The DESIGN_v2.md §13 example rule, verbatim."""
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.add_rule(
            scope={"repo": "mockingbird-dsp", "paths": ["asr/**"]},
            rule="Unknown radio street names must never be normalized to dictionary words.",
            source={"type": SOURCE_REJECTION, "finding": "F821"},
            confidence=0.94,
            support_count=3,
        )
        rules = learn.retrieve("mockingbird-dsp", ["asr/tokenizer.py"])
        assert len(rules) == 1
        rule = rules[0]
        assert rule.scope["repo"] == "mockingbird-dsp"
        assert rule.scope["paths"] == ["asr/**"]
        assert rule.confidence == 0.94
        assert rule.support_count == 3
        assert rule.source["finding"] == "F821"
        # Not relevant → not retrieved.
        assert learn.retrieve("mockingbird-dsp", ["frontend/ui.py"]) == []
        assert learn.retrieve("other-repo", ["asr/tokenizer.py"]) == []


# ── (c) support_count accumulation ───────────────────────────────────────


class TestSupportCount:
    def test_repeat_feedback_increments_support_count(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)

        rules = learn.retrieve("repo-a", ["src/x.py"])
        assert len(rules) == 1  # one rule, not three
        assert rules[0].support_count == 3

    def test_different_finding_creates_separate_rule(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)
        learn.record_developer_response("repo-a", ["src/x.py"], "F8", False)
        rules = learn.retrieve("repo-a", ["src/x.py"])
        assert len(rules) == 2
        assert {r.source["finding"] for r in rules} == {"F7", "F8"}

    def test_accept_then_reject_same_finding_are_distinct(self, tmp_path):
        learn = ReviewLearning(path=str(tmp_path / "learning.jsonl"))
        learn.record_developer_response("repo-a", ["src/x.py"], "F1", True)
        learn.record_developer_response("repo-a", ["src/x.py"], "F1", False)
        rules = learn.retrieve("repo-a", ["src/x.py"])
        assert len(rules) == 2
        assert {r.source["type"] for r in rules} == {SOURCE_ACCEPTANCE, SOURCE_REJECTION}


# ── (d) persistence ──────────────────────────────────────────────────────


class TestPersistence:
    def test_round_trip_write_then_reload(self, tmp_path):
        path = str(tmp_path / ".gitreins" / "review_learning.jsonl")
        learn = ReviewLearning(path=path)
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)
        learn.add_rule(
            {"repo": "repo-b", "paths": ["lib/**"]},
            "manual rule",
            {"type": "manual", "finding": ""},
            confidence=0.9,
            support_count=2,
        )

        # A fresh instance reloads the same rules from disk.
        fresh = ReviewLearning(path=path)
        a = fresh.retrieve("repo-a", ["src/x.py"])
        assert len(a) == 1
        assert a[0].source == {"type": SOURCE_REJECTION, "finding": "F7"}
        assert a[0].scope == {"repo": "repo-a", "paths": ["src/x.py"]}
        assert a[0].support_count == 1

        b = fresh.retrieve("repo-b", ["lib/y.py"])
        assert len(b) == 1
        assert b[0].rule == "manual rule"
        assert b[0].confidence == 0.9
        assert b[0].support_count == 2

    def test_reload_preserves_incremented_support_count(self, tmp_path):
        path = str(tmp_path / "learning.jsonl")
        learn = ReviewLearning(path=path)
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)
        learn.record_developer_response("repo-a", ["src/x.py"], "F7", False)

        fresh = ReviewLearning(path=path)
        rules = fresh.retrieve("repo-a", ["src/x.py"])
        assert len(rules) == 1
        assert rules[0].support_count == 2

    def test_lazy_load_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "learning.jsonl"
        path.write_text(
            '{"scope": {"repo": "r", "paths": []}, "rule": "ok", '
            '"source": {"type": "t", "finding": "F"}, "confidence": 0.5, '
            '"support_count": 1}\n'
            "this is not json\n"
        )
        learn = ReviewLearning(path=str(path))
        rules = learn.retrieve("r", ["a.py"])
        assert len(rules) == 1
        assert rules[0].rule == "ok"

    def test_io_failure_never_raises(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        learn = ReviewLearning(path=str(blocker / "learning.jsonl"))

        rule = learn.record_developer_response("repo-a", ["src/x.py"], "F1", False)
        assert rule is not None  # in-memory state survives the failed write
        assert rule.source["finding"] == "F1"
        assert len(learn.retrieve("repo-a", ["src/x.py"])) == 1
        # The file was never created, and nothing raised.
        assert not os.path.exists(str(blocker / "learning.jsonl"))
