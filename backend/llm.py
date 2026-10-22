"""Provider and role selection. Model IDs reviewed against October 2026 catalogs."""

import logging
import os
from typing import Literal

from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.google import ChatGoogle
from browser_use.llm.openai.chat import ChatOpenAI

logger = logging.getLogger(__name__)
Role = Literal["agent", "exploration", "fast"]

DEFAULT_MODELS = {
    "gemini": {"agent": "gemini-4-argon", "exploration": "gemini-4-argon", "fast": "gemini-3.8-flash"},
    "claude": {"agent": "claude-sonnet-5-5", "exploration": "claude-sonnet-5-5", "fast": "claude-sonnet-5-5"},
    "openai": {"agent": "gpt-6.1-sol", "exploration": "gpt-6.1-sol", "fast": "gpt-6-luna"},
}
PREFIXES = {"gemini": "GEMINI", "claude": "ANTHROPIC", "openai": "OPENAI"}
KEYS = {"gemini": "GOOGLE_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def get_llm(role: Role = "agent") -> ChatGoogle | ChatAnthropic | ChatOpenAI:
    if role not in ("agent", "exploration", "fast"):
        raise ValueError(f"Unknown model role: {role}")
    provider = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    if provider == "anthropic":
        provider = "claude"
    if provider == "codex":
        if os.getenv("AGENT_DRIVER", "browser-use") != "pi":
            raise ValueError("LLM_PROVIDER=codex requires AGENT_DRIVER=pi")
        from .pi_llm import PiSubscriptionLLM
        return PiSubscriptionLLM(role)
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown LLM_PROVIDER={provider!r}; use gemini, claude, or openai")
    key = (os.getenv(KEYS[provider]) or "").strip()
    if not key:
        raise ValueError(f"{KEYS[provider]} env var is required when LLM_PROVIDER={provider}")
    prefix = PREFIXES[provider]
    suffix = "MODEL" if role == "agent" else f"{role.upper()}_MODEL"
    model = (os.getenv(f"{prefix}_{suffix}") or "").strip()
    # An explicit main model also applies to exploration unless overridden.
    if not model and role == "exploration":
        model = (os.getenv(f"{prefix}_MODEL") or "").strip()
    model = model or DEFAULT_MODELS[provider][role]
    max_tokens = 16384 if role == "fast" else 32768
    logger.info("Using %s %s model: %s", provider, role, model)
    common = {"model": model, "api_key": key, "max_retries": 2}
    if provider == "gemini":
        return ChatGoogle(**common, max_output_tokens=max_tokens,
                          http_options={"timeout": 120000})
    if provider == "claude":
        # Adaptive thinking avoids forcing a tool choice on current reasoning models.
        adaptive = any(part in model for part in ("-5", "-4-6", "-4-7", "-4-8"))
        return ChatAnthropic(**common, max_tokens=max_tokens, temperature=None, timeout=120,
                             thinking={"type": "adaptive"} if adaptive else None)
    # Current reasoning models reject legacy sampling/frequency penalties.
    reasoning = model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
    return ChatOpenAI(**common, temperature=None, frequency_penalty=None, timeout=120,
                      max_completion_tokens=max_tokens, reasoning_models=[model] if reasoning else [],
                      reasoning_effort="low" if role == "fast" else "medium")
