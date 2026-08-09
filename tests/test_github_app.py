"""
R2.10 tests for engine/github — publisher, checks, and the run_pr_review
surface (DESIGN_v2.md §14/§16).

Covers: publish_comments payloads (PR review comment vs issue comment for
line-less findings), publisher never-raises on API failure, requests REST
fallback when gh is unavailable + a token exists, empty batch, set_status_check
state mapping / ValueError on bad state / False on API failure, and the
run_pr_review smoke: PullRequestChangeSource → ReviewOrchestrator (stubbed
run) → rank/dedupe → CommentWriter (StubLLM) → publish + status check with
the right payloads, including the deterministic status mapping (high → failure,
clean → success, DAG error → error) and the ChangeSourceError path.

Hermetic: gh is a monkeypatched FakeGH, the orchestrator's DAG is stubbed,
the LLM is a deterministic StubLLM — zero network, zero real GitHub.
"""

import json

import pytest

from engine.github.app import run_pr_review
from engine.github.checks import VALID_STATES, set_status_check
from engine.github.checkout import ChangeSourceError
from engine.github.publisher import publish_comments
from engine.llm import LLMClient, LLMResponse
from engine.review import (
    Comment,
    CommentBatch,
    CommentWriter,
    ReviewOrchestrator,
    ReviewResult,
    ReviewerResult,
)
from engine.review.reviewers import ReviewFinding

# Two comments in ONE batch (pattern from tests/test_review_writer.py).
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

PR_META = {
    "baseRefOid": "b" * 40,
    "headRefOid": "h" * 40,
    "headRefName": "feature/x",
    "files": [{"path": "auth/session.py", "status": "MODIFIED"}],
}

PR_DIFF = "diff --git a/auth/session.py b/auth/session.py\n@@ -180,3 +180,3 @@\n"


class FakeGH:
    """A _run_gh stand-in: serves pr view/diff and records every api call."""

    def __init__(self):
        self.calls = []
        self.fail: Exception | None = None

    def __call__(self, args, *, gh_binary="gh", timeout=60.0, input=None):
        self.calls.append((list(args), input))
        if self.fail is not None:
            raise self.fail
        if args[:2] == ["pr", "view"]:
            return json.dumps(PR_META)
        if args[:2] == ["pr", "diff"]:
            return PR_DIFF
        if args[0] == "api":
            return "{}"
        raise AssertionError(f"unexpected gh call: {args}")


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


def stub_orchestrator(findings, per_agent=None):
    """A ReviewOrchestrator whose DAG run() is stubbed to canned output."""
    orch = ReviewOrchestrator(router=None, workdir=".")
    orch.run = lambda files, ctx="", **kw: ReviewResult(  # type: ignore[method-assign]
        findings=list(findings), per_agent=per_agent or {}
    )
    return orch


def make_finding(**overrides):
    defaults = dict(
        claim="rotate_token can dereference user=None for deleted accounts",
        severity="high",
        file="auth/session.py",
        line=182,
        evidence=["E1"],
        category="null-deref",
    )
    defaults.update(overrides)
    return ReviewFinding(**defaults)


def api_calls(fake):
    """All recorded gh api POSTs as (path, json body dict)."""
    out = []
    for args, input_ in fake.calls:
        if args[0] == "api":
            out.append((args[3], json.loads(input_) if input_ else {}))
    return out


# ── publisher ────────────────────────────────────────────────────────────


class TestPublisher:
    def test_posts_line_comments_as_pr_review_comments(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        batch = CommentBatch(
            comments=[
                Comment(
                    file="auth/session.py",
                    line=182,
                    severity="high",
                    title="t",
                    body="Guard user before deref.",
                )
            ]
        )
        result = publish_comments("acme", "widgets", 42, batch)
        assert result.ok and result.posted == batch.comments and not result.failed

        paths = [p for p, _ in api_calls(fake)]
        assert paths == ["/repos/acme/widgets/pulls/42/comments"]
        _, body = api_calls(fake)[0]
        assert body == {"body": "Guard user before deref.", "path": "auth/session.py", "line": 182}

    def test_line_less_comment_goes_to_issue_conversation(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        batch = CommentBatch(
            comments=[
                Comment(
                    file="", line=None, severity="info", title="t", body="General note."
                )
            ]
        )
        result = publish_comments("acme", "widgets", 42, batch)
        assert result.ok
        paths = [p for p, _ in api_calls(fake)]
        assert paths == ["/repos/acme/widgets/issues/42/comments"]
        _, body = api_calls(fake)[0]
        assert body == {"body": "General note."}

    def test_never_raises_on_api_failure(self, monkeypatch):
        fake = FakeGH()
        fake.fail = ChangeSourceError("gh api failed (rc=1): HTTP 403 rate limited")
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        batch = CommentBatch(
            comments=[
                Comment(file="a.py", line=1, severity="high", title="t", body="b")
            ]
        )
        result = publish_comments("acme", "widgets", 42, batch)  # no raise
        assert not result.ok
        assert result.failed == batch.comments
        assert len(result.errors) == 1
        assert "403" in result.errors[0]
        assert result.posted == []

    def test_empty_batch_posts_nothing_and_ok(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        result = publish_comments("acme", "widgets", 42, CommentBatch())
        assert result.ok and result.posted == [] and result.failed == []
        assert api_calls(fake) == []

    def test_requests_fallback_when_gh_down_and_token_set(self, monkeypatch):
        fake = FakeGH()
        fake.fail = ChangeSourceError("gh CLI not found ('gh') — install GitHub CLI")
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        seen = {}

        def fake_requests(method, path, fields, *, token, timeout=30.0):
            seen.update(method=method, path=path, fields=fields, token=token)
            return {}

        monkeypatch.setattr("engine.github.publisher._requests_api", fake_requests)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_123")

        batch = CommentBatch(
            comments=[Comment(file="a.py", line=3, severity="high", title="t", body="b")]
        )
        result = publish_comments("acme", "widgets", 42, batch)
        assert result.ok and result.posted == batch.comments
        assert result.fallback_used is True
        assert seen["method"] == "POST"
        assert seen["path"] == "/repos/acme/widgets/pulls/42/comments"
        assert seen["fields"] == {"body": "b", "path": "a.py", "line": 3}
        assert seen["token"] == "ghp_test_token_123"

    def test_no_fallback_without_token(self, monkeypatch):
        fake = FakeGH()
        fake.fail = ChangeSourceError("gh CLI not found ('gh')")
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        batch = CommentBatch(
            comments=[Comment(file="a.py", line=1, severity="high", title="t", body="b")]
        )
        result = publish_comments("acme", "widgets", 42, batch)
        assert not result.ok and len(result.failed) == 1
        assert result.fallback_used is False

    def test_one_failure_does_not_block_others(self, monkeypatch):
        calls = []

        def flaky(args, *, gh_binary="gh", timeout=60.0, input=None):
            calls.append((list(args), input))
            if args[0] == "api" and "pulls/42" in args[3]:
                raise ChangeSourceError("gh api failed (rc=1): HTTP 422")
            return "{}"

        monkeypatch.setattr("engine.github.checkout._run_gh", flaky)
        batch = CommentBatch(
            comments=[
                Comment(file="a.py", line=1, severity="high", title="t", body="b1"),
                Comment(file="", line=None, severity="info", title="t2", body="b2"),
            ]
        )
        result = publish_comments("acme", "widgets", 42, batch)
        assert len(result.posted) == 1  # issue comment went out
        assert len(result.failed) == 1  # PR review comment failed
        assert result.failed[0].file == "a.py"


# ── checks ───────────────────────────────────────────────────────────────


class TestStatusCheck:
    def test_posts_status_with_fields(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        ok = set_status_check(
            "acme", "widgets", "h" * 40, "success",
            description="review found no blocking issues", context="gitreins/review",
        )
        assert ok is True
        paths = [p for p, _ in api_calls(fake)]
        assert paths == [f"/repos/acme/widgets/statuses/{'h' * 40}"]
        _, body = api_calls(fake)[0]
        assert body == {
            "state": "success",
            "description": "review found no blocking issues",
            "context": "gitreins/review",
        }

    @pytest.mark.parametrize("state", VALID_STATES)
    def test_all_valid_states_post(self, monkeypatch, state):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        assert set_status_check("acme", "widgets", "h" * 40, state) is True
        _, body = api_calls(fake)[0]
        assert body["state"] == state

    def test_invalid_state_raises_value_error(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        with pytest.raises(ValueError) as ei:
            set_status_check("acme", "widgets", "h" * 40, "bogus")
        assert "bogus" in str(ei.value)
        assert api_calls(fake) == []  # nothing posted

    def test_api_failure_returns_false_no_raise(self, monkeypatch):
        fake = FakeGH()
        fake.fail = ChangeSourceError("gh api failed (rc=1): HTTP 401")
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        assert (
            set_status_check("acme", "widgets", "h" * 40, "error") is False
        )  # no raise


# ── run_pr_review smoke ──────────────────────────────────────────────────


class TestRunPrReview:
    def test_full_flow_publishes_comments_and_failure_status(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator([make_finding()])
        writer = CommentWriter(router=FakeRouter(StubLLM([content_response(BATCH_JSON)])), workdir=".")

        result = run_pr_review(
            "acme", "widgets", 42, orchestrator=orch, writer=writer, workdir="."
        )

        # Findings plumbed; batch from the writer (2 comments, one high).
        assert result.owner == "acme" and result.pr_number == 42
        assert len(result.findings) == 1
        assert len(result.batch.comments) == 2
        assert result.batch.summary == "Two issues found."
        assert result.head_sha == "h" * 40

        # Two review comments posted, one status check set to failure (high present).
        paths = [p for p, _ in api_calls(fake)]
        assert paths.count("/repos/acme/widgets/pulls/42/comments") == 2
        assert paths.count(f"/repos/acme/widgets/statuses/{'h' * 40}") == 1
        status_path, status_body = api_calls(fake)[-1]
        assert status_body["state"] == "failure"
        assert status_body["context"] == "gitreins/review"
        assert status_body["description"]

        assert result.publish.ok and len(result.publish.posted) == 2
        assert result.status is not None
        assert result.status == {
            "state": "failure",
            "context": "gitreins/review",
            "posted": True,
            "description": status_body["description"],
        }

    def test_clean_review_sets_success_and_posts_nothing(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator([])
        writer = CommentWriter(
            router=FakeRouter(StubLLM([content_response(EMPTY_BATCH_JSON)])), workdir="."
        )

        result = run_pr_review("acme", "widgets", 42, orchestrator=orch, writer=writer, workdir=".")

        assert result.batch.comments == []
        assert result.publish.ok and result.publish.posted == []
        paths = [p for p, _ in api_calls(fake)]
        assert paths == [f"/repos/acme/widgets/statuses/{'h' * 40}"]  # no comment posts
        assert result.status is not None
        assert result.status["state"] == "success"

    def test_dag_errors_map_to_error_status(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator(
            [],
            per_agent={"runtime_reviewer": ReviewerResult(role="runtime_reviewer", ok=False, error="boom")},
        )
        writer = CommentWriter(
            router=FakeRouter(StubLLM([content_response(EMPTY_BATCH_JSON)])), workdir="."
        )

        result = run_pr_review("acme", "widgets", 42, orchestrator=orch, writer=writer, workdir=".")
        assert result.status is not None
        assert result.status["state"] == "error"
        assert result.publish.ok  # nothing to post — clean batch

    def test_writer_receives_ranked_deduped_findings_and_diff_context(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator(
            [
                make_finding(claim="dup one", evidence=["E1"]),
                make_finding(claim="dup two", evidence=["E1"]),  # same evidence → deduped
            ]
        )
        stub = StubLLM([content_response(EMPTY_BATCH_JSON)])
        writer = CommentWriter(router=FakeRouter(stub), workdir=".")

        run_pr_review("acme", "widgets", 42, orchestrator=orch, writer=writer, workdir=".")

        user_prompt = stub.messages_seen[0][1]["content"]
        assert "auth/session.py" in user_prompt  # changed_files context
        assert "diff --git a/auth/session.py" in user_prompt  # diff_context
        # Dedup kept ONE representative (same evidence ref) — prompt lists it once.
        assert user_prompt.count("dup one") == 1
        assert "dup two" not in user_prompt

    def test_source_is_pull_request_change_source_by_default(self, monkeypatch):
        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator([])
        writer = CommentWriter(
            router=FakeRouter(StubLLM([content_response(EMPTY_BATCH_JSON)])), workdir="."
        )

        run_pr_review("acme", "widgets", 42, orchestrator=orch, writer=writer, workdir=".")
        # The default source fetched PR metadata + diff through gh.
        assert any(c[0][:2] == ["pr", "view"] for c in fake.calls)
        assert any(c[0][:2] == ["pr", "diff"] for c in fake.calls)

    def test_injected_source_overrides_default(self, monkeypatch):
        class FakeSource:
            def diff(self):
                from engine.github.checkout import Diff

                return Diff(text="fake diff", files=["a.py"])

            def changed_files(self):
                return ["a.py"]

            def base_sha(self):
                return "b" * 40

            def head_sha(self):
                return "h" * 40

        fake = FakeGH()
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        orch = stub_orchestrator([])
        writer = CommentWriter(
            router=FakeRouter(StubLLM([content_response(EMPTY_BATCH_JSON)])), workdir="."
        )

        result = run_pr_review(
            "acme", "widgets", 42,
            source=FakeSource(), orchestrator=orch, writer=writer, workdir=".",
        )
        assert result.status is not None
        assert result.status["state"] == "success"
        # No pr view/diff calls — the injected source replaced the gh fetch.
        assert not any(c[0][:2] == ["pr", "view"] for c in fake.calls)

    def test_gh_unavailable_raises_change_source_error(self, monkeypatch):
        fake = FakeGH()
        fake.fail = ChangeSourceError("gh CLI not found ('gh') — install GitHub CLI")
        monkeypatch.setattr("engine.github.checkout._run_gh", fake)
        with pytest.raises(ChangeSourceError):
            run_pr_review("acme", "widgets", 42, workdir=".")

    def test_to_dict_shape(self):
        from engine.github.app import PrReviewResult

        r = PrReviewResult(owner="o", repo="r", pr_number=1)
        d = r.to_dict()
        assert d["owner"] == "o" and d["pr_number"] == 1
        assert d["findings_count"] == 0 and d["status"] is None
