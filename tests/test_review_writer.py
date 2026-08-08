"""
R2.9 tests for engine/review/writer — the batched CommentWriter.

Covers: Comment/CommentBatch schema shape (parse_response round-trip with
nested dataclasses, required-field enforcement, defaults, to_dict), batched
output (one LLM call for ALL comments — DESIGN_v2.md §15), MODEL_ROLE='writer'
router wiring, ranked+deduped findings + change-context serialization into
the user prompt, VerifierFinding (impact/confidence, no claim) input,
findings-wrapper input, config fallback (review.models.writer missing → env
defaults, never raises), budget-cap propagation, and the empty batch.

Hermetic: the LLM is a deterministic StubLLM (pattern from
tests/test_review_verifier.py) — no network, no real model.
"""

from typing import Any
from unittest.mock import patch

import pytest

from engine.agents import Budget, BudgetExceededError
from engine.agents.schemas import parse_response, schema_to_prompt
from engine.llm import LLMClient, LLMResponse
from engine.review import Comment, CommentBatch, CommentWriter
from engine.review.reviewers import ReviewFinding, ReviewFindings
from engine.review.verifier import VerifierFinding
from engine.review.writer import serialize_findings
from engine.router import ModelRouter

# Two comments in ONE batch (DESIGN_v2.md §15: batched writer output).
BATCH_JSON = (
    '{"comments": ['
    '{"file": "auth/session.py", "line": 182, "severity": "high", '
    '"title": "Null deref on deleted accounts", '
    '"body": "Guard user before deref.", "finding_ids": ["F19"]}, '
    '{"file": "auth/tokens.py", "line": 41, "severity": "medium", '
    '"title": "Token rotation race", '
    '"body": "Two refreshes can race.", "finding_ids": ["F21"]}], '
    '"summary": "Two issues found."}'
)

EMPTY_BATCH_JSON = '{"comments": [], "summary": "clean"}'


class StubLLM:
    """Deterministic fake LLMClient: plays back responses, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.messages_seen = []

    def chat(self, messages, tools=None, max_tokens=16384, temperature=0.1):
        self.calls += 1
        self.messages_seen.append(list(messages))
        resp = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return resp() if callable(resp) else resp


class FakeRouter:
    """Minimal ModelRouter stand-in returning one fixed client for any role."""

    def __init__(self, client):
        self.client = client
        self.roles = []

    def for_role(self, role: str) -> LLMClient:
        self.roles.append(role)
        return self.client  # type: ignore[return-value]  # StubLLM is duck-typed


def content_response(text):
    return LLMResponse(content=text)


def make_finding(**overrides: Any):
    """A ranked+deduped ReviewFinding (R2.7 shape)."""
    defaults: dict[str, Any] = dict(
        claim="rotate_token can dereference user=None for deleted accounts",
        severity="high",
        file="auth/session.py",
        line=182,
        evidence=["E1", "E2"],
        category="null-deref",
        suggestion="guard on user before deref",
    )
    defaults.update(overrides)
    return ReviewFinding(**defaults)


# ── (a) Schema shape ─────────────────────────────────────────────────────


class TestCommentSchema:
    def test_parse_response_builds_typed_batch(self):
        result = parse_response(BATCH_JSON, CommentBatch)
        assert isinstance(result, CommentBatch)
        assert result.summary == "Two issues found."
        assert len(result.comments) == 2
        c = result.comments[0]
        assert isinstance(c, Comment)
        assert c.file == "auth/session.py"
        assert c.line == 182
        assert c.severity == "high"
        assert c.title == "Null deref on deleted accounts"
        assert "Guard user" in c.body
        assert c.finding_ids == ["F19"]

    def test_parse_response_optional_fields_default(self):
        result = parse_response(
            '{"comments": [{"file": "a.py", "severity": "low", "title": "t", '
            '"body": "b"}], "summary": ""}',
            CommentBatch,
        )
        c = result.comments[0]
        assert c.line is None
        assert c.finding_ids == []

    def test_parse_response_missing_required_field_raises(self):
        with pytest.raises(Exception) as ei:
            parse_response(
                '{"comments": [{"line": 1, "severity": "low", "title": "t", '
                '"body": "b"}], "summary": ""}',
                CommentBatch,
            )
        assert "file" in str(ei.value)

    def test_parse_response_empty_comments_ok(self):
        result = parse_response(EMPTY_BATCH_JSON, CommentBatch)
        assert result.comments == []
        assert result.summary == "clean"

    def test_parse_response_markdown_fence(self):
        result = parse_response("```json\n" + BATCH_JSON + "\n```", CommentBatch)
        assert len(result.comments) == 2

    def test_schema_to_prompt_lists_fields(self):
        prompt = schema_to_prompt(CommentBatch)
        assert '"comments"' in prompt
        assert '"summary"' in prompt
        assert "list[Comment]" in prompt
        for name in ("file", "line", "severity", "title", "body", "finding_ids"):
            assert name in schema_to_prompt(Comment)

    def test_comment_to_dict_shape(self):
        c = Comment(
            file="a.py", line=3, severity="high", title="t", body="b",
            finding_ids=["F19"],
        )
        d = c.to_dict()
        assert d["file"] == "a.py"
        assert d["line"] == 3
        assert d["severity"] == "high"
        assert d["title"] == "t"
        assert d["body"] == "b"
        assert d["finding_ids"] == ["F19"]


# ── (b) Writer runs (batched, stubbed LLM) ───────────────────────────────


class TestWriterRun:
    def test_run_returns_typed_batch_single_call(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        result = writer.run([make_finding()])
        assert isinstance(result, CommentBatch)
        assert stub.calls == 1  # batched: ONE call for ALL comments
        assert len(result.comments) == 2
        assert result.comments[0].finding_ids == ["F19"]

    def test_model_role_resolved_to_writer(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        router = FakeRouter(stub)
        writer = CommentWriter(router=router, workdir=tmp_workdir)
        writer.run([make_finding()])
        assert router.roles == ["writer"]  # ModelRouter.for_role('writer')

    def test_model_role_constant(self):
        assert CommentWriter.MODEL_ROLE == "writer"

    def test_subclass_of_agent_runner(self, tmp_workdir):
        from engine.agents import AgentRunner

        assert issubclass(CommentWriter, AgentRunner)

    def test_batched_many_findings_one_call(self, tmp_workdir):
        """10 findings → still exactly one LLM call (batched, §15)."""
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        findings = [
            make_finding(claim=f"claim {i}", file=f"f{i}.py", line=i)
            for i in range(10)
        ]
        result = writer.run(findings)
        assert stub.calls == 1
        assert len(result.comments) == 2

    def test_user_prompt_contains_findings_and_context(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        writer.run(
            [make_finding()],
            changed_files=["auth/session.py"],
            diff_context="rotated refresh tokens",
        )
        user_prompt = stub.messages_seen[0][1]["content"]
        assert "auth/session.py:182" in user_prompt
        assert "rotate_token can dereference" in user_prompt
        assert "E1" in user_prompt and "E2" in user_prompt
        assert "high" in user_prompt
        assert "Changed files" in user_prompt
        assert "auth/session.py" in user_prompt
        assert "rotated refresh tokens" in user_prompt

    def test_system_prompt_is_batched_writer(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        writer.run([make_finding()])
        system_prompt = stub.messages_seen[0][0]["content"]
        assert "batch" in system_prompt.lower()
        assert "comments" in system_prompt.lower()
        assert "finding" in system_prompt.lower()
        assert "Severity levels" in system_prompt
        for level in ("critical", "high", "medium", "low", "info"):
            assert level in system_prompt

    def test_run_accepts_dict_findings(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        result = writer.run(
            [{"claim": "x", "severity": "high", "file": "a.py", "line": 1}]
        )
        assert isinstance(result, CommentBatch)

    def test_run_accepts_verifier_finding(self, tmp_workdir):
        """VerifierFinding (impact/confidence, no claim/file) serializes safely."""
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        result = writer.run(
            [
                VerifierFinding(
                    finding_id="F19", impact="high", patch_causality="confirmed",
                    verdict="CONFIRMED", execution_path_confirmed=True,
                    verifier_confidence=0.94,
                    notes="Traced caller guard; claim survived.",
                )
            ]
        )
        assert isinstance(result, CommentBatch)
        prompt = stub.messages_seen[0][1]["content"]
        assert "F19" in prompt
        assert "CONFIRMED" in prompt
        assert "confidence=0.94" in prompt

    def test_run_accepts_findings_wrapper(self, tmp_workdir):
        stub = StubLLM([content_response(BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        wrapper = ReviewFindings(findings=[make_finding()], summary="one finding")
        result = writer.run(wrapper)
        assert isinstance(result, CommentBatch)

    def test_budget_cap_propagates(self, tmp_workdir):
        """A model that never emits valid JSON stops at the iteration cap."""
        stub = StubLLM([content_response("this is not json")])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        with pytest.raises(BudgetExceededError) as ei:
            writer.run([make_finding()], budget=Budget(max_iterations=3))
        assert "Cap exceeded" in str(ei.value)

    def test_empty_findings_batch(self, tmp_workdir):
        """No findings → 'clean' note in the prompt, still one schema-shaped call."""
        stub = StubLLM([content_response(EMPTY_BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=tmp_workdir)
        result = writer.run([])
        assert result.comments == []
        assert stub.calls == 1
        assert "clean" in stub.messages_seen[0][1]["content"]


# ── (c) Config fallback ──────────────────────────────────────────────────


class TestConfigFallback:
    def test_missing_writer_config_falls_back_no_raise(self, tmp_workdir, monkeypatch):
        """Absent review.models.writer → env-default client; run still works."""
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://fallback.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "fallback-key")

        router = ModelRouter({})
        client = router.for_role("writer")  # never raises
        assert client.model == "fallback-model"

        writer = CommentWriter(router=router, workdir=tmp_workdir)
        with patch.object(client, "chat", return_value=LLMResponse(content=BATCH_JSON)):
            result = writer.run([make_finding()])
        assert len(result.comments) == 2


# ── (d) Finding → prompt serialization ───────────────────────────────────


class TestSerializeFindings:
    def test_synthetic_ids_when_no_finding_id(self):
        text = serialize_findings([make_finding(claim="first"), make_finding(claim="second")])
        assert "F1" in text
        assert "F2" in text

    def test_own_finding_id_used(self):
        text = serialize_findings(
            [
                VerifierFinding(
                    finding_id="F19", impact="high", patch_causality="confirmed",
                    verdict="CONFIRMED", execution_path_confirmed=True,
                    verifier_confidence=0.94,
                )
            ]
        )
        assert "F19" in text
        assert "CONFIRMED" in text
        assert "confidence=0.94" in text
        assert "execution_path_confirmed" in text

    def test_empty(self):
        assert serialize_findings([]) == ""
