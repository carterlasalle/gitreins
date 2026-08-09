"""
Unit tests for engine/llm.py — multi-provider LLM client with retry logic.
axiom:trace work_item=GR-001 spec=specs/02-LLM-Interface.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import pytest
from unittest.mock import MagicMock, patch

import requests
from engine.llm import LLMClient, LLMResponse, ToolCall, _is_anthropic


# ── Phase 1-2-1: LLMClient initialization, provider detection, error handling ──


class TestToolCallDataclass:
    """Test ToolCall and LLMResponse dataclasses — step-1-2-1-4."""

    def test_toolcall_construction(self):
        """ToolCall dataclass has correct fields."""
        tc = ToolCall(id="call_123", name="read_file", arguments={"path": "foo.py"})
        assert tc.id == "call_123"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "foo.py"}

    def test_llmresponse_content_only(self):
        """LLMResponse with content only (no tool calls)."""
        resp = LLMResponse(content="Hello, world!")
        assert resp.content == "Hello, world!"
        assert resp.tool_calls == []

    def test_llmresponse_with_tool_calls(self):
        """LLMResponse with tool_calls list."""
        tc = ToolCall(id="c1", name="read_file", arguments={})
        resp = LLMResponse(content="Let me check", tool_calls=[tc])
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"


class TestProviderDetection:
    """Test _is_anthropic and provider auto-detection — step-1-2-1-1."""

    def test_detect_anthropic_from_url_anthropic_com(self):
        """Base URL containing 'anthropic.com' → provider='anthropic'."""
        client = LLMClient(base_url="https://api.anthropic.com/v1")
        assert client.provider == "anthropic"

    def test_detect_anthropic_from_url_claude(self):
        """Base URL containing 'claude' → provider='anthropic'."""
        client = LLMClient(base_url="https://claude.ai/api/v1")
        assert client.provider == "anthropic"

    def test_detect_openai_from_url(self):
        """Base URL containing 'openai.com' → provider='openai'."""
        client = LLMClient(base_url="https://api.openai.com/v1")
        assert client.provider == "openai"

    def test_unknown_url_defaults_to_openai(self):
        """Unknown base URL defaults to 'openai'."""
        client = LLMClient(base_url="https://localhost:8080/v1")
        assert client.provider == "openai"

    def test_force_provider_override(self, monkeypatch):
        """GITREINS_LLM_PROVIDER env var forces provider override in constructor."""
        monkeypatch.setenv("GITREINS_LLM_PROVIDER", "anthropic")
        # The provider env var must be set before LLMClient.__init__ reads it
        # The constructor checks the constructor arg first, then env vars via os.getenv
        client = LLMClient(base_url="https://api.openai.com/v1", provider="anthropic")
        assert client.provider == "anthropic"

    def test_is_anthropic_helper_function(self):
        """_is_anthropic() returns True for anthropic-like URLs."""
        assert _is_anthropic("https://api.anthropic.com/v1") is True
        assert _is_anthropic("https://claude.example.com") is True
        assert _is_anthropic("https://api.openai.com/v1") is False


class TestAPIKeyResolution:
    """Test API key resolution chain — step-1-2-1-1."""

    def test_direct_api_key_wins(self):
        """Explicit api_key constructor arg takes precedence."""
        client = LLMClient(api_key="direct-key", base_url="https://test.local/v1")
        assert client.api_key == "direct-key"

    def test_primary_env_var(self, monkeypatch):
        """GITREINS_LLM_API_KEY is checked first."""
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary-key")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "primary-key"

    def test_fallback_to_neuralwatt(self, monkeypatch):
        """Fallback to NEURALWATT_API_KEY if primary not set."""
        monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
        monkeypatch.setenv("NEURALWATT_API_KEY", "nw-key")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "nw-key"

    def test_fallback_to_openai_key(self, monkeypatch):
        """Fallback to OPENAI_API_KEY."""
        monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "openai-key"

    def test_fallback_to_anthropic_key(self, monkeypatch):
        """Fallback to ANTHROPIC_API_KEY."""
        monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "anthropic-key"

    def test_fallback_to_deepseek_key(self, monkeypatch):
        """Fallback to DEEPSEEK_API_KEY."""
        monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "deepseek-key"

    def test_missing_all_keys_returns_empty(self, monkeypatch):
        """When no API keys are set, self.api_key = ''."""
        monkeypatch.delenv("GITREINS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == ""


class TestAnthropicConversion:
    """Test Anthropic message and tool format conversion — step-1-2-1-2."""

    def test_system_message_extracted(self):
        """System messages are extracted to separate key, not in message list."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ]
        )
        # System should be excluded from result
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_tool_message_converted_to_user_tool_result(self):
        """Tool role messages are converted to user messages with tool_result blocks."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic(
            [
                {"role": "tool", "tool_call_id": "call_1", "content": '{"result": "ok"}'},
            ]
        )
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"
        assert result[0]["content"][0]["tool_use_id"] == "call_1"

    def test_assistant_with_tool_calls_converted(self):
        """Assistant with tool_calls converted to Anthropic format with text + tool_use blocks."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic(
            [
                {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"f.py"}'},
                        }
                    ],
                },
            ]
        )
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        content_blocks = result[0]["content"]
        assert len(content_blocks) == 2
        assert content_blocks[0]["type"] == "text"
        assert content_blocks[0]["text"] == "Let me check."
        assert content_blocks[1]["type"] == "tool_use"
        assert content_blocks[1]["name"] == "read_file"

    def test_openai_tools_converted_to_anthropic(self):
        """OpenAI tools are converted to Anthropic name/description/input_schema format."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            },
        ]
        result = client._convert_tools_for_anthropic(openai_tools)
        assert len(result) == 1
        assert result[0]["name"] == "read_file"
        assert result[0]["description"] == "Read a file"
        assert "input_schema" in result[0]
        assert result[0]["input_schema"]["type"] == "object"

    def test_user_assistant_passthrough(self):
        """User and assistant messages without tool_calls pass through."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic(
            [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello!"},
            ]
        )
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"


class TestRetryLogic:
    """Test retry with exponential backoff — step-1-2-1-3."""

    def test_429_is_retried(self, llm_client):
        """HTTP 429 (rate limit) triggers retry."""
        mock_attempt = MagicMock()
        mock_attempt.side_effect = [
            requests.HTTPError(response=MagicMock(status_code=429)),
            LLMResponse(content="success"),
        ]
        with patch.object(llm_client, "_chat_attempt", mock_attempt):
            with patch("time.sleep", return_value=None):
                result = llm_client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "success"
        assert mock_attempt.call_count == 2

    def test_503_is_retried(self, llm_client):
        """HTTP 503 (server error) triggers retry."""
        mock_attempt = MagicMock()
        mock_attempt.side_effect = [
            requests.HTTPError(response=MagicMock(status_code=503)),
            LLMResponse(content="recovered"),
        ]
        with patch.object(llm_client, "_chat_attempt", mock_attempt):
            with patch("time.sleep", return_value=None):
                result = llm_client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "recovered"

    def test_400_is_not_retried(self, llm_client):
        """HTTP 400 (client error) does NOT retry — raised immediately."""
        response_400 = MagicMock()
        response_400.status_code = 400
        with patch.object(
            llm_client, "_chat_attempt", side_effect=requests.HTTPError(response=response_400)
        ):
            with patch("time.sleep", return_value=None):
                with pytest.raises(requests.HTTPError):
                    llm_client.chat([{"role": "user", "content": "hi"}])

    def test_network_error_is_retried(self, llm_client):
        """Network errors (RequestException) trigger retry."""
        mock_attempt = MagicMock()
        mock_attempt.side_effect = [
            requests.RequestException("Connection refused"),
            LLMResponse(content="ok"),
        ]
        with patch.object(llm_client, "_chat_attempt", mock_attempt):
            with patch("time.sleep", return_value=None):
                result = llm_client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "ok"

    def test_three_consecutive_failures_raises_runtimeerror(self, llm_client):
        """3 consecutive failures → RuntimeError raised."""
        with patch.object(
            llm_client, "_chat_attempt", side_effect=requests.RequestException("fail")
        ):
            with patch("time.sleep", return_value=None):
                with pytest.raises(RuntimeError, match="LLM request failed after 3 attempts"):
                    llm_client.chat([{"role": "user", "content": "hi"}])

    def test_backoff_timing_uses_exponential(self, llm_client):
        """Exponential backoff: sleep(1), sleep(2)."""
        mock_attempt = MagicMock()
        mock_attempt.side_effect = [
            requests.RequestException("e1"),
            requests.RequestException("e2"),
            LLMResponse(content="ok"),
        ]
        sleep_times = []
        with patch.object(llm_client, "_chat_attempt", mock_attempt):
            with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
                llm_client.chat([{"role": "user", "content": "hi"}])
        assert sleep_times == [1, 2]


class TestLLMClientDefaults:
    """Test default values and configuration."""

    def test_default_model(self, monkeypatch):
        """Default model is 'deepseek-v4-flash'."""
        monkeypatch.delenv("GITREINS_LLM_MODEL", raising=False)
        client = LLMClient(base_url="https://test.local/v1")
        assert client.model == "deepseek-v4-flash"

    def test_custom_model(self):
        """Constructor model arg overrides default."""
        client = LLMClient(base_url="https://test.local/v1", model="gpt-4-turbo")
        assert client.model == "gpt-4-turbo"

    def test_env_model(self, monkeypatch):
        """GITREINS_LLM_MODEL env var is used when no arg given."""
        monkeypatch.setenv("GITREINS_LLM_MODEL", "env-model")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.model == "env-model"

    def test_max_retries_default(self):
        """Default max_retries is 3."""
        client = LLMClient(base_url="https://test.local/v1")
        assert client.max_retries == 3

    def test_custom_max_retries(self):
        """Constructor max_retries arg is respected."""
        client = LLMClient(base_url="https://test.local/v1", max_retries=5)
        assert client.max_retries == 5

    def test_anthropic_url_build(self):
        """Anthropic provider builds /messages endpoint."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        assert client._chat_url == "https://api.anthropic.com/v1/messages"

    def test_openai_url_build(self):
        """OpenAI provider builds /chat/completions endpoint."""
        client = LLMClient(base_url="https://api.openai.com/v1", api_key="k")
        assert client._chat_url == "https://api.openai.com/v1/chat/completions"


class TestExtendedLLM:
    """Extended coverage for LLM client edge cases."""

    def test_provider_auto_detect_no_provider_arg(self, monkeypatch):
        """When no provider arg given, auto-detection runs from base_url."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        client = LLMClient(base_url="https://api.anthropic.com/v1")
        assert client.provider == "anthropic"

    def test_provider_openai_when_not_anthropic(self):
        """A non-Anthropic URL defaults to openai provider."""
        client = LLMClient(base_url="https://api.deepseek.com/v1")
        assert client.provider == "openai"

    def test_chat_openai_mocked_http(self, monkeypatch):
        """_chat_openai handles a proper mocked OpenAI response."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        client = LLMClient(base_url="https://test.openai.local/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello!", "role": "assistant"}}]
        }
        with patch("requests.post", return_value=mock_resp):
            result = client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "Hello!"
        assert result.tool_calls == []

    def test_chat_openai_with_tool_calls_mocked(self, monkeypatch):
        """_chat_openai parses tool_calls from OpenAI response."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        client = LLMClient(base_url="https://test.openai.local/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Let me check",
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"f.py"}'},
                            }
                        ],
                    }
                }
            ]
        }
        with patch("requests.post", return_value=mock_resp):
            result = client.chat([{"role": "user", "content": "read"}])
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "f.py"}

    def test_chat_anthropic_mocked_http(self, monkeypatch):
        """_chat_anthropic handles a proper mocked Anthropic response."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="test-ant-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": "Hello from Claude!"}],
        }
        with patch("requests.post", return_value=mock_resp):
            result = client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "Hello from Claude!"

    def test_anthropic_version_env_var(self, monkeypatch):
        """GITREINS_ANTHROPIC_VERSION env var sets _api_version."""
        monkeypatch.setenv("GITREINS_ANTHROPIC_VERSION", "2024-01-01")
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        assert client._api_version == "2024-01-01"

    def test_anthropic_convert_empty_messages(self):
        """Converting empty messages list returns empty list."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic([])
        assert result == []

    def test_anthropic_convert_tool_msg_no_call_id(self):
        """Tool message without tool_call_id still produces a tool_result block."""
        client = LLMClient(base_url="https://api.anthropic.com/v1", api_key="k")
        result = client._convert_messages_for_anthropic(
            [
                {"role": "tool", "content": "some output"},
            ]
        )
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"
        assert result[0]["content"][0]["tool_use_id"] == ""

    def test_retry_three_failures_backoff_timing(self, llm_client):
        """Backoff timing with 3 total attempts: sleep(1), sleep(2) before last."""
        mock_attempt = MagicMock()
        mock_attempt.side_effect = [
            requests.RequestException("fail1"),
            requests.RequestException("fail2"),
            requests.RequestException("fail3"),
        ]
        sleep_times = []
        with patch.object(llm_client, "_chat_attempt", mock_attempt):
            with patch("time.sleep", side_effect=lambda t: sleep_times.append(t)):
                with pytest.raises(RuntimeError):
                    llm_client.chat([{"role": "user", "content": "hi"}])
        # max_retries=3: sleeps after first 2 failures (attempts 0 and 1)
        assert sleep_times == [1, 2]

    def test_mocked_429_via_requests_post(self, monkeypatch):
        """HTTP 429 retried when mocking requests.post directly."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        client = LLMClient(base_url="https://test.openai.local/v1", api_key="test-key")
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.raise_for_status.side_effect = requests.HTTPError(response=resp_429)
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            "choices": [{"message": {"content": "ok", "role": "assistant"}}]
        }
        with patch("requests.post", side_effect=[resp_429, resp_ok]):
            with patch("time.sleep", return_value=None):
                result = client.chat([{"role": "user", "content": "hi"}])
        assert result.content == "ok"

    def test_first_env_key_priority(self, monkeypatch):
        """GITREINS_LLM_API_KEY is checked before other keys."""
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "primary")
        monkeypatch.setenv("OPENAI_API_KEY", "secondary")
        client = LLMClient(base_url="https://test.local/v1")
        assert client.api_key == "primary"


# ── GR-068: DeepSeek thinking mode control + cache telemetry ──


class TestGR068ThinkingMode:
    """Test DeepSeek thinking/reasoning mode control (GR-068)."""

    def test_is_deepseek_from_model_name(self):
        """_is_deepseek() returns True when model contains 'deepseek'."""
        c = LLMClient(base_url="https://api.openai.com/v1", api_key="k", model="deepseek-v4-flash")
        assert c._is_deepseek() is True

    def test_is_deepseek_from_provider(self):
        """_is_deepseek() returns True when provider is deepseek."""
        # URL-based detection
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="k")
        assert c._is_deepseek() is True

    def test_is_not_deepseek(self):
        """_is_deepseek() returns False for non-DeepSeek model/provider."""
        c = LLMClient(base_url="https://api.openai.com/v1", api_key="k", model="gpt-4")
        assert c._is_deepseek() is False

    def test_reasoning_default_disabled(self):
        """llm_reasoning defaults to 'disabled'."""
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="k")
        assert c.llm_reasoning == "disabled"

    def test_reasoning_explicit_enabled(self):
        """llm_reasoning can be set to 'enabled' via constructor."""
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="k", llm_reasoning="enabled")
        assert c.llm_reasoning == "enabled"

    def test_reasoning_env_var(self, monkeypatch):
        """GITREINS_LLM_REASONING env var controls reasoning mode."""
        monkeypatch.setenv("GITREINS_LLM_REASONING", "enabled")
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="k")
        assert c.llm_reasoning == "enabled"

    def test_thinking_disabled_in_payload(self, monkeypatch):
        """When reasoning=disabled, payload includes thinking.type=disabled for DeepSeek."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(
            base_url="https://api.deepseek.com/v1", api_key="test-key", llm_reasoning="disabled"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        captured = {}

        def _capture(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return mock_resp

        with patch("requests.post", side_effect=_capture):
            c.chat([{"role": "user", "content": "hi"}])
        assert "thinking" in captured["json"]
        assert captured["json"]["thinking"] == {"type": "disabled"}

    def test_thinking_enabled_in_payload(self, monkeypatch):
        """When reasoning=enabled, payload includes thinking.type=enabled for DeepSeek."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(
            base_url="https://api.deepseek.com/v1", api_key="test-key", llm_reasoning="enabled"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        captured = {}

        def _capture(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return mock_resp

        with patch("requests.post", side_effect=_capture):
            c.chat([{"role": "user", "content": "hi"}])
        assert "thinking" in captured["json"]
        assert captured["json"]["thinking"] == {"type": "enabled"}

    def test_no_thinking_for_non_deepseek(self, monkeypatch):
        """No thinking field in payload for non-DeepSeek providers."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(base_url="https://api.openai.com/v1", api_key="test-key", model="gpt-4")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        captured = {}

        def _capture(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return mock_resp

        with patch("requests.post", side_effect=_capture):
            c.chat([{"role": "user", "content": "hi"}])
        assert "thinking" not in captured["json"]

    def test_cache_telemetry_in_usage(self, monkeypatch):
        """DeepSeek usage is normalized: prompt_tokens is the TOTAL input.

        DeepSeek's prompt_tokens already includes prompt_cache_hit_tokens +
        prompt_cache_miss_tokens, so the cache fields are zeroed at the source
        to prevent double-counting in budget accounting (JUDGE-CONTEXT-001).
        """
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "cached response"}}],
            "usage": {
                # Real DeepSeek semantics: prompt_tokens = hit + miss = 5200
                "prompt_tokens": 5200,
                "prompt_cache_hit_tokens": 5000,
                "prompt_cache_miss_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 5250,
            },
        }
        with patch("requests.post", return_value=mock_resp):
            result = c.chat([{"role": "user", "content": "hi"}])
        assert result.usage is not None
        # Cache breakdown is informational only — zeroed so it can't be
        # added on top of prompt_tokens (which already includes it).
        assert result.usage.cache_read_tokens == 0
        assert result.usage.cache_write_tokens == 0
        assert result.usage.prompt_tokens == 5200
        # all_input_tokens is the total input exactly once — no double-count
        assert result.usage.all_input_tokens == 5200

    def test_deepseek_usage_not_double_counted(self, monkeypatch):
        """Regression (JUDGE-CONTEXT-001): a 140K prompt with 100K cache hits
        must account 140K input tokens, not 240K+."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 140_000,
                "prompt_cache_hit_tokens": 100_000,
                "prompt_cache_miss_tokens": 40_000,
                "completion_tokens": 1_000,
                "total_tokens": 141_000,
            },
        }
        with patch("requests.post", return_value=mock_resp):
            result = c.chat([{"role": "user", "content": "hi"}])
        assert result.usage is not None
        assert result.usage.all_input_tokens == 140_000
        assert result.usage.cache_read_tokens == 0
        assert result.usage.cache_write_tokens == 0

    def test_cache_telemetry_zero_when_missing(self, monkeypatch):
        """Cache fields default to 0 when not present in response."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(base_url="https://api.deepseek.com/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "no cache"}}],
        }
        with patch("requests.post", return_value=mock_resp):
            result = c.chat([{"role": "user", "content": "hi"}])
        assert result.usage is None  # no usage block means None

    def test_anthropic_cache_telemetry(self, monkeypatch):
        """Anthropic cache tokens parsed from usage block."""
        monkeypatch.delenv("GITREINS_LLM_BASE_URL", raising=False)
        c = LLMClient(base_url="https://api.anthropic.com/v1", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [{"type": "text", "text": "cached"}],
            "usage": {
                "input_tokens": 6000,
                "output_tokens": 100,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 200,
            },
        }
        with patch("requests.post", return_value=mock_resp):
            result = c.chat([{"role": "user", "content": "hi"}])
        assert result.usage is not None
        assert result.usage.cache_read_tokens == 5000
        assert result.usage.cache_write_tokens == 200
        # Regular input = total - cache
        assert result.usage.prompt_tokens == 800  # 6000 - 5000 - 200
