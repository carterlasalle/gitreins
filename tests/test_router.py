"""
Unit tests for engine/router.py — per-role model routing (R2.1).

Covers: per-role resolution, env-default fallback (missing role / missing
review.models), config-driven roles, per-role caching, and Pipeline backward
compatibility with llm= injection.
"""

from unittest.mock import patch

from engine.llm import LLMClient, LLMResponse
from engine.pipeline import Pipeline
from engine.router import ModelRouter


# Two fully-configured roles from DESIGN_v2.md §3.
ROLE_CFG = {
    "scout": {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3.7-flash",
    },
    "verifier": {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3.7-flash",
    },
    "writer": {
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3.7-flash",
        "reasoning": "enabled",
    },
}


def _config_with_models(models: dict | None = None) -> dict:
    """Build a config dict with an optional review.models block."""
    if models is None:
        return {}
    return {"review": {"models": models}}


class TestPerRoleResolution:
    """(a) per-role resolution returns an LLMClient with that role's model/base_url."""

    def test_role_resolves_to_client_with_role_model(self):
        """scout role → LLMClient with scout's model/base_url, reasoning disabled."""
        router = ModelRouter(_config_with_models(ROLE_CFG))
        client = router.for_role("scout")

        assert isinstance(client, LLMClient)
        assert client.model == "qwen/qwen3.7-flash"
        assert client.provider == "openai"
        assert client._chat_url == "https://openrouter.ai/api/v1/chat/completions"
        assert client.llm_reasoning == "disabled"

    def test_role_reasoning_override(self):
        """writer role declares reasoning: enabled → client picks it up."""
        router = ModelRouter(_config_with_models(ROLE_CFG))
        client = router.for_role("writer")
        assert client.llm_reasoning == "enabled"

    def test_distinct_roles_get_distinct_models(self):
        """Different roles resolve to clients with their own configured models."""
        cfg = {
            "review": {
                "models": {
                    "scout": {
                        "provider": "openai",
                        "base_url": "https://openrouter.ai/api/v1",
                        "model": "qwen/qwen3.7-flash",
                    },
                    "runtime_reviewer": {
                        "provider": "openai",
                        "base_url": "https://openrouter.ai/api/v1",
                        "model": "deepseek/deepseek-v4-flash-0731",
                    },
                }
            }
        }
        router = ModelRouter(cfg)
        assert router.for_role("scout").model == "qwen/qwen3.7-flash"
        assert router.for_role("runtime_reviewer").model == "deepseek/deepseek-v4-flash-0731"


class TestFallback:
    """(b)/(c) missing role or missing review.models falls back to env defaults."""

    def test_missing_role_falls_back_to_env_defaults(self, monkeypatch):
        """Unknown role → client from GITREINS_LLM_* env vars, no raise."""
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://fallback.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "fallback-key")

        router = ModelRouter(_config_with_models(ROLE_CFG))
        client = router.for_role("security_reviewer")  # not configured

        assert isinstance(client, LLMClient)
        assert client.model == "fallback-model"
        assert client._chat_url == "https://fallback.test/v1/chat/completions"
        assert client.api_key == "fallback-key"

    def test_missing_review_models_block_falls_back(self, monkeypatch):
        """Config without review.models → env-default client, no raise."""
        monkeypatch.setenv("GITREINS_LLM_BASE_URL", "https://fallback.test/v1")
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        monkeypatch.setenv("GITREINS_LLM_API_KEY", "fallback-key")

        router = ModelRouter({})  # no review block at all
        client = router.for_role("scout")

        assert isinstance(client, LLMClient)
        assert client.model == "fallback-model"
        assert client._chat_url == "https://fallback.test/v1/chat/completions"

    def test_missing_role_is_cached_like_configured_roles(self, monkeypatch):
        """Fallback clients are cached per role too (same instance)."""
        monkeypatch.setenv("GITREINS_LLM_MODEL", "fallback-model")
        router = ModelRouter(_config_with_models(ROLE_CFG))
        assert router.for_role("verifier") is router.for_role("verifier")


class TestCaching:
    """(e) same role → same client instance."""

    def test_same_role_returns_same_instance(self):
        router = ModelRouter(_config_with_models(ROLE_CFG))
        assert router.for_role("scout") is router.for_role("scout")

    def test_different_roles_return_different_instances(self):
        router = ModelRouter(_config_with_models(ROLE_CFG))
        assert router.for_role("scout") is not router.for_role("verifier")


class TestPipelineIntegration:
    """(f) Pipeline still works with llm= injection; router wires role stages."""

    def _pipeline_config(self, role: str | None = None) -> dict:
        """Minimal two-stage pipeline; tier2 is an ai_eval stage."""
        tier2 = {
            "id": "tier2",
            "type": "ai_eval",
            "on": ["pre-eval"],
            "condition": "true",
            "max_iterations": 20,
        }
        if role:
            tier2["role"] = role
        return {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "steps": [
                            {
                                "id": "secrets",
                                "type": "script",
                                "run": "echo ok",
                                "on_fail": "continue",
                            },
                        ],
                    },
                    tier2,
                ]
            }
        }

    def _complete_verdict(self):
        """LLMResponse yielding a COMPLETE verdict (usage=None → no token accounting)."""
        return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"done"}')

    def test_pipeline_with_injected_llm_still_works(self, tmp_workdir, llm_client):
        """Backward compat: Pipeline(config, workdir, llm=...) runs ai_eval."""
        p = Pipeline(self._pipeline_config(), tmp_workdir, llm=llm_client)
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        with patch.object(llm_client, "chat", return_value=self._complete_verdict()) as mock_chat:
            result = p.run(task, trigger="pre-eval")
        assert result["stages"]["tier2"]["passed"] is True
        mock_chat.assert_called()

    def test_pipeline_with_router_resolves_role_client(self, tmp_workdir):
        """Router present + stage with role → stage runs on that role's client."""
        cfg = self._pipeline_config(role="scout")
        cfg["review"] = {
            "models": {
                "scout": {
                    "provider": "openai",
                    "base_url": "https://test.local/v1",
                    "model": "scout-model",
                }
            }
        }
        router = ModelRouter(cfg)
        p = Pipeline(cfg, tmp_workdir, router=router)
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}

        routed = router.for_role("scout")
        with patch.object(routed, "chat", return_value=self._complete_verdict()) as mock_chat:
            result = p.run(task, trigger="pre-eval")

        assert result["stages"]["tier2"]["passed"] is True
        mock_chat.assert_called()
        assert routed.model == "scout-model"

    def test_pipeline_with_router_and_no_role_keeps_injected_llm(self, tmp_workdir, llm_client):
        """Router present but stage without role → injected llm still used."""
        cfg = self._pipeline_config()  # no role on tier2
        router = ModelRouter(cfg)  # no review block either
        p = Pipeline(cfg, tmp_workdir, llm=llm_client, router=router)
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        with patch.object(llm_client, "chat", return_value=self._complete_verdict()) as mock_chat:
            result = p.run(task, trigger="pre-eval")
        assert result["stages"]["tier2"]["passed"] is True
        mock_chat.assert_called()
