"""
ModelRouter — per-role LLM client resolution (R2.1).

Replaces the single-client ``Pipeline._llm``: resolves an ``LLMClient`` per
role invocation using the ``review.models`` block from
``.gitreins/config.yaml`` (DESIGN_v2.md §3). Roles without a configured
model fall back to a client built from environment defaults
(``GITREINS_LLM_BASE_URL`` / ``GITREINS_LLM_API_KEY`` / ``GITREINS_LLM_MODEL``)
exactly like the pipeline's legacy lazy init, so existing configs keep
working and nothing raises.
"""

import logging

from engine.llm import LLMClient

logger = logging.getLogger("gitreins.router")


class ModelRouter:
    """Resolve an LLMClient per role from config.

    Config shape (top-level, from .gitreins/config.yaml)::

        review:
          models:
            scout:
              provider: openai
              base_url: https://openrouter.ai/api/v1
              model: qwen/qwen3.7-flash
              reasoning: disabled   # optional, defaults to "disabled"

    A missing role or a missing ``review.models`` block falls back to a
    client built from ``GITREINS_LLM_*`` env defaults (same behavior as
    ``Pipeline._run_ai_eval``'s legacy lazy init).
    """

    def __init__(self, config: dict):
        self.config = config or {}
        self._models = self.config.get("review", {}).get("models", {})
        self._cache: dict[str, LLMClient] = {}

    def for_role(self, role: str) -> LLMClient:
        """Return the LLMClient for ``role``, cached per role.

        Never raises for unknown roles: falls back to an env-default
        client when the role has no configured model.
        """
        cached = self._cache.get(role)
        if cached is not None:
            return cached

        cfg = self._models.get(role)
        if cfg is None:
            logger.debug(
                "No model configured for role %r — falling back to env-default client",
                role,
            )
            client = LLMClient()
        else:
            client = LLMClient(
                model=cfg.get("model"),
                base_url=cfg.get("base_url"),
                provider=cfg.get("provider"),
                llm_reasoning=cfg.get("reasoning", "disabled"),
            )
        self._cache[role] = client
        return client
