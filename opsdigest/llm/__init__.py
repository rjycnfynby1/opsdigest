"""Провайдеры LLM. Выбор — llm.provider в config.yaml или флаг --provider.

Типы профилей:
  openai     — любой OpenAI-совместимый сервер: LM Studio/Bionic, Ollama, vLLM, корпоративный шлюз
  anthropic  — Claude API (api.anthropic.com)
  http_json  — произвольный HTTP JSON API (например, корпоративный агент) по шаблону запроса
  command    — внешняя программа: промпт в stdin, ответ из stdout (например, `claude -p` из Claude Code)
  maverick   — корпоративный агент Maverick (Agent Core API: /api/v1/query, X-API-Key)
"""
from __future__ import annotations

from .base import LLMClient, LLMError
from .providers import AnthropicClient, CommandClient, HttpJsonClient, MaverickClient, OpenAIClient

TYPES = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "http_json": HttpJsonClient,
    "command": CommandClient,
    "maverick": MaverickClient,
}


def make_client(name: str, profile: dict) -> LLMClient:
    t = (profile.get("type") or "openai").lower()
    if t not in TYPES:
        raise LLMError(f"Неизвестный тип LLM-профиля '{t}' (профиль {name}). Доступны: {', '.join(TYPES)}")
    return TYPES[t](name, profile)


__all__ = ["LLMClient", "LLMError", "make_client"]
