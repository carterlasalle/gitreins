"""Shared environment sanitization for every subprocess GitReins spawns.

GitReins spawns many subprocesses — Tier-1 guards (pytest, ruff, gitleaks,
nested guards), evaluator/verifier ``run_command`` tools, and Tier-1 pipeline
script steps. Every spawn site must strip the same hostile variables, or the
block list drifts and the leak class returns (it has now hit twice:

- ENVFIX 9c9e8c6: judge budget caps (GITREINS_MAX_*) leaked into the tier1
  pytest subprocess and broke EvalCap/config-priority tests (R2-16).
- INFRA-LLM-ENV-001: LLM credential vars leaked the same way and broke
  tests/test_llm.py env-priority tests (``api_key == ''`` asserted, real
  key found).

Variables stripped from every spawned subprocess env:

- ``GIT_*`` — leaked by the pre-commit hook (GIT_INDEX_FILE and friends);
  poisons nested git commands (DF-008).
- ``GITREINS_MAX_*`` — judge budget controls (iterations/time/tokens).
- ``GITREINS_LLM_*`` — LLM endpoint/key/model/provider config.
- The provider fallback API keys read by :class:`engine.llm.LLMClient` at
  construction (engine/llm.py ``_api_key`` fallback chain): NEURALWATT,
  OPENAI, ANTHROPIC, DEEPSEEK, KIMI, GROQ, OPENROUTER.

CRITICAL: only SUBPROCESS spawn sites call :func:`sanitized_env`. The judge's
own process must keep these keys — the in-process LLMClient reads env at
construction and needs them to call the LLM. Never apply this to the judge's
own environment.
"""

import os

#: Key prefixes that must never reach judge/guard subprocesses.
_BLOCKED_PREFIXES = ("GIT_", "GITREINS_MAX_", "GITREINS_LLM_")

#: Exact keys that must never reach judge/guard subprocesses. These mirror the
#: LLMClient fallback chain in engine/llm.py (every *_API_KEY it reads at
#: construction) — any one of them leaking into a test subprocess defeats the
#: env-priority tests in tests/test_llm.py.
_BLOCKED_KEYS = frozenset(
    {
        "NEURALWATT_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "KIMI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    }
)


def is_blocked_env_key(key: str) -> bool:
    """Return True if ``key`` must not reach judge/guard subprocesses."""
    return key.startswith(_BLOCKED_PREFIXES) or key in _BLOCKED_KEYS


def sanitized_env() -> dict[str, str]:
    """Return the current environment minus every blocked variable.

    Use ONLY at subprocess spawn sites (``env=sanitized_env()``). The
    in-process evaluator keeps the keys — do NOT apply this to the judge's
    own environment.
    """
    return {k: v for k, v in os.environ.items() if not is_blocked_env_key(k)}
