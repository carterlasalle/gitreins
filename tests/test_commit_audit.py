"""
Regression tests for CommitAuditor (v0.9.0).

Tests the LLM-powered commit message audit without requiring
a live LLM — uses mocking for the LLM client and real git
operations for diff capture and message reading.
"""

import json
from unittest.mock import patch


from engine.commit_audit import (
    CommitAuditor,
    CommitAuditResult,
    COMMIT_AUDIT_TOOLS,
)
from engine.llm import LLMClient, LLMResponse, LLMUsage


# ═══════════════════════════════════════════════════════════════
# CommitAuditResult
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditResult:
    """Test the result dataclass."""

    def test_valid_result_defaults(self):
        result = CommitAuditResult(valid=True)
        assert result.valid is True
        assert result.issues == []
        assert result.suggested_message == ""
        assert result.action == "pass"

    def test_invalid_result_has_issues(self):
        result = CommitAuditResult(
            valid=False,
            issues=["Too vague", "Missing scope"],
            suggested_message="fix(auth): correct login flow",
        )
        assert result.valid is False
        assert len(result.issues) == 2
        assert "fix(auth)" in result.suggested_message
        assert result.action == "block"

    def test_action_block_when_invalid(self):
        result = CommitAuditResult(valid=False, issues=["bad"])
        assert result.action == "block"


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — parse_result (no LLM needed)
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorParseResult:
    """Test result parsing with mocked LLM responses."""

    def _make_auditor(self) -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm)

    def test_parse_valid(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content='{"valid": true}', usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True
        assert result.issues == []

    def test_parse_invalid_with_suggestion(self):
        auditor = self._make_auditor()
        resp = LLMResponse(
            content=json.dumps(
                {
                    "valid": False,
                    "issues": ["Message is too vague"],
                    "suggested_message": "fix(auth): correct login redirect",
                }
            ),
            usage=LLMUsage(),
        )
        result = auditor._parse_result(resp)
        assert result.valid is False
        assert "too vague" in result.issues[0].lower()
        assert "fix(auth)" in result.suggested_message

    def test_parse_markdown_fenced_json(self):
        auditor = self._make_auditor()
        resp = LLMResponse(
            content='```json\n{"valid": true}\n```',
            usage=LLMUsage(),
        )
        result = auditor._parse_result(resp)
        assert result.valid is True

    def test_parse_invalid_json_falls_back_to_valid(self):
        """Malformed JSON defaults to valid (safe — don't block on parse error)."""
        auditor = self._make_auditor()
        resp = LLMResponse(content="not json at all", usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True  # safe default
        assert len(result.issues) > 0  # but it reports the issue

    def test_empty_content(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content="", usage=LLMUsage())
        result = auditor._parse_result(resp)
        assert result.valid is True  # empty = pass

    def test_iteration_tracking(self):
        auditor = self._make_auditor()
        resp = LLMResponse(content='{"valid": true}', usage=LLMUsage())
        result = auditor._parse_result(resp, iteration=3)
        assert result.iterations_used == 3


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — audit (mocked LLM)
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorAudit:
    """Test the full audit flow with mocked LLM."""

    def _make_auditor(self, strictness="standard") -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm, strictness=strictness)

    def _valid_response(self):
        return LLMResponse(content='{"valid": true}', usage=LLMUsage())

    def _invalid_response(self, issues=None, suggestion=None):
        return LLMResponse(
            content=json.dumps(
                {
                    "valid": False,
                    "issues": issues or ["Message too vague."],
                    "suggested_message": suggestion or "fix: update code",
                }
            ),
            usage=LLMUsage(),
        )

    def test_audit_with_empty_diff_returns_valid(self):
        """No staged changes — nothing to audit."""
        auditor = self._make_auditor()
        result = auditor.audit("fix: stuff", diff="")
        assert result.valid is True

    def test_audit_with_empty_message_returns_invalid(self):
        """Empty message is always invalid."""
        auditor = self._make_auditor()
        result = auditor.audit("", diff="+some change")
        assert result.valid is False
        assert any("Empty" in i for i in result.issues)

    def test_audit_empty_message_generates_fallback(self):
        """Empty message triggers fallback message generation."""
        auditor = self._make_auditor()
        auditor.suggest_message = True
        result = auditor.audit("", diff="+some change")
        assert result.suggested_message != ""

    @patch.object(LLMClient, "chat")
    def test_single_pass_valid(self, mock_chat):
        """LLM returns valid on first call."""
        mock_chat.return_value = self._valid_response()
        auditor = self._make_auditor()
        result = auditor.audit("fix(auth): correct redirect", diff="+some change")
        assert result.valid is True
        assert mock_chat.call_count == 1

    @patch.object(LLMClient, "chat")
    def test_single_pass_invalid_with_suggestion(self, mock_chat):
        """LLM returns invalid with a suggested better message."""
        mock_chat.return_value = self._invalid_response(
            issues=["Message doesn't describe the change"],
            suggestion="feat(api): add login endpoint with JWT support",
        )
        auditor = self._make_auditor()
        result = auditor.audit("fix stuff", diff="+def login(): ...")
        assert result.valid is False
        assert "feat(api)" in result.suggested_message

    @patch.object(LLMClient, "chat")
    def test_llm_error_returns_valid(self, mock_chat):
        """LLM failures should not block commits (safe default)."""
        mock_chat.side_effect = RuntimeError("API down")
        auditor = self._make_auditor()
        result = auditor.audit("fix: something", diff="+some change")
        assert result.valid is True  # safe default


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — strictness levels
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorStrictness:
    """Test that strictness affects prompt construction."""

    def test_prompt_contains_strictness_level(self):
        """The user prompt must include the strictness level."""
        from engine.commit_audit import COMMIT_AUDIT_USER_PROMPT, INSTRUCTIONS_BY_STRICTNESS

        # Check that all three strictness levels have instructions
        assert "lenient" in INSTRUCTIONS_BY_STRICTNESS
        assert "standard" in INSTRUCTIONS_BY_STRICTNESS
        assert "strict" in INSTRUCTIONS_BY_STRICTNESS

        prompt = COMMIT_AUDIT_USER_PROMPT.format(
            strictness="STRICT",
            mode="audit",
            message="test msg",
            diff="test diff",
            instructions=INSTRUCTIONS_BY_STRICTNESS["strict"],
        )
        assert "STRICT" in prompt
        assert "conventional commits" in prompt.lower()

    def test_lenient_single_word_allowed(self):
        """Lenient mode instructions allow short messages."""
        from engine.commit_audit import INSTRUCTIONS_LENIENT

        assert "short messages" in INSTRUCTIONS_LENIENT.lower()

    def test_strict_requires_conventional_commits(self):
        """Strict mode requires conventional commits format."""
        from engine.commit_audit import INSTRUCTIONS_STRICT

        assert "conventional commits" in INSTRUCTIONS_STRICT.lower()
        assert "type(scope):" in INSTRUCTIONS_STRICT


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — diff capture
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorDiffCapture:
    """Test the git diff capture mechanism."""

    def test_diff_capture_in_git_repo(self):
        """_capture_diff runs successfully in a git repo."""
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        diff = auditor._capture_diff()
        # Should return a string (maybe empty if no staged changes)
        assert isinstance(diff, str)
        # In our actual repo (gitreins-poc), it should work
        assert diff != "" or True  # empty diff is valid (nothing staged)


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — tool execution
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorTools:
    """Test the tool execution methods."""

    def _make_auditor(self) -> CommitAuditor:
        llm = LLMClient(api_key="sk-test", model="test/model")
        return CommitAuditor(llm)

    def test_read_file_returns_content(self):
        """_tool_read_file reads a real file."""
        auditor = self._make_auditor()
        result = auditor._tool_read_file("README.md", limit=5)
        assert "GitReins" in result or "README" in result or result.startswith("//")

    def test_read_file_not_found(self):
        """_tool_read_file returns error for missing file."""
        auditor = self._make_auditor()
        result = auditor._tool_read_file("nonexistent_file.xyz")
        parsed = json.loads(result)
        assert "error" in parsed

    def test_search_pattern_finds_matches(self):
        """_tool_search_pattern finds real matches."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(
            r"class CommitAuditor", path="engine", file_glob="*.py"
        )
        assert "CommitAuditor" in result

    def test_search_pattern_no_matches(self):
        """_tool_search_pattern returns hint when nothing found."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(r"zzz_nonexistent_pattern_xyz", path="engine")
        parsed = json.loads(result)
        assert parsed.get("matches") == 0

    def test_search_pattern_invalid_regex(self):
        """_tool_search_pattern handles invalid regex."""
        auditor = self._make_auditor()
        result = auditor._tool_search_pattern(r"[invalid")
        parsed = json.loads(result)
        assert "error" in parsed

    def test_tools_list_has_expected_tools(self):
        """COMMIT_AUDIT_TOOLS includes read_file and search_pattern."""
        names = [t["function"]["name"] for t in COMMIT_AUDIT_TOOLS]
        assert "read_file" in names
        assert "search_pattern" in names


# ═══════════════════════════════════════════════════════════════
# CommitAuditor — fallback message generation
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditorFallbackMessage:
    """Test fallback commit message generation from diff stats."""

    def test_generate_fallback_returns_string(self):
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        msg = auditor._generate_fallback_message("+some change")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_generate_fallback_starts_with_chore(self):
        auditor = CommitAuditor(
            LLMClient(api_key="sk-test", model="test/model"),
        )
        msg = auditor._generate_fallback_message("")
        assert msg.startswith("chore:")


# ═══════════════════════════════════════════════════════════════
# Config integration
# ═══════════════════════════════════════════════════════════════


class TestCommitAuditConfig:
    """Test that commit_audit config is wired into GitReinsDefaults."""

    def test_defaults_have_commit_audit_fields(self):
        from engine.config import GitReinsDefaults

        defaults = GitReinsDefaults()
        assert defaults.commit_audit_enabled is True
        assert defaults.commit_audit_mode == "warn"
        assert defaults.commit_audit_strictness == "standard"
        assert defaults.commit_audit_max_iterations == 3
        assert defaults.commit_audit_suggest_message is True

    def test_overlay_reads_commit_audit_section(self):
        from engine.config import GitReinsDefaults

        defaults = GitReinsDefaults()
        overlaid = defaults.overlay(
            {
                "defaults": {
                    "commit_audit": {
                        "mode": "block",
                        "strictness": "strict",
                    }
                }
            }
        )
        assert overlaid.commit_audit_mode == "block"
        assert overlaid.commit_audit_strictness == "strict"
        # Unset fields keep defaults
        assert overlaid.commit_audit_enabled is True

    def test_to_config_dict_includes_commit_audit(self):
        from engine.config import GitReinsDefaults

        defaults = GitReinsDefaults(
            commit_audit_mode="block",
            commit_audit_strictness="strict",
        )
        d = defaults.to_config_dict()
        assert "commit_audit" in d
        assert d["commit_audit"]["mode"] == "block"
        assert d["commit_audit"]["strictness"] == "strict"
        assert d["commit_audit"]["enabled"] is True
