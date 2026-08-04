from dataclasses import dataclass
import httpx
from app.config import settings


@dataclass
class LLMResult:
    content: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    model: str = "unknown"


class LLMProvider:
    def complete_sync(self, messages: list[dict], model: str | None = None) -> LLMResult:
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model

    def complete_sync(self, messages: list[dict], model: str | None = None) -> LLMResult:
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(timeout=120) as client:
            response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        usage = data.get("usage") or {}
        return LLMResult(
            content=data["choices"][0]["message"]["content"],
            tokens_used=int(usage.get("total_tokens") or usage.get("completion_tokens") or 0),
            model=payload["model"],
        )


class DeterministicProvider(LLMProvider):
    """Только для локальных unit-тестов. В production не используется автоматически."""

    def complete_sync(self, messages: list[dict], model: str | None = None) -> LLMResult:
        prompt = messages[-1]["content"] if messages else ""
        return LLMResult(
            content=(
                "# Тестовый ответ детерминированного провайдера\n\n"
                "Это fallback только для тестов. В production настройте CUSTOM_REMOTE_URL/OPENROUTER_API_KEY.\n\n"
                f"Фрагмент запроса: {prompt[:500]}"
            ),
            tokens_used=max(1, len(prompt.split())),
            model="deterministic-test-only",
        )


def get_llm_provider() -> LLMProvider:
    if settings.llm_active_provider == "openrouter":
        return OpenAICompatibleProvider(
            settings.openrouter_api_url,
            settings.openrouter_api_key,
            settings.openrouter_default_model,
        )
    if settings.llm_active_provider == "custom_remote":
        return OpenAICompatibleProvider(
            settings.custom_remote_url,
            settings.custom_remote_key,
            settings.custom_remote_default_model,
        )
    if settings.llm_active_provider == "deterministic":
        return DeterministicProvider()
    raise RuntimeError(
        f"Неизвестный LLM_ACTIVE_PROVIDER={settings.llm_active_provider!r}. "
        "Допустимо: custom_remote, openrouter, deterministic."
    )
