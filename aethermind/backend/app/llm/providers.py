from dataclasses import dataclass
import httpx
from app.config import settings

@dataclass
class LLMResult:
    content: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    model: str = "deterministic"

class LLMProvider:
    async def complete(self, messages: list[dict], model: str | None = None) -> LLMResult:
        raise NotImplementedError

class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, default_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model

    async def complete(self, messages: list[dict], model: str | None = None) -> LLMResult:
        payload = {"model": model or self.default_model, "messages": messages, "temperature": 0.2}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage") or {}
        return LLMResult(
            content=data["choices"][0]["message"]["content"],
            tokens_used=int(usage.get("total_tokens") or 0),
            model=payload["model"],
        )

class DeterministicProvider(LLMProvider):
    async def complete(self, messages: list[dict], model: str | None = None) -> LLMResult:
        prompt = messages[-1]["content"] if messages else ""
        return LLMResult(content=f"Deterministic response for: {prompt[:500]}", tokens_used=len(prompt.split()), model="deterministic")

def get_llm_provider() -> LLMProvider:
    if settings.llm_active_provider == "openrouter":
        return OpenAICompatibleProvider(settings.openrouter_api_url, settings.openrouter_api_key, settings.openrouter_default_model)
    if settings.llm_active_provider == "custom_remote":
        return OpenAICompatibleProvider(settings.custom_remote_url, settings.custom_remote_key, settings.custom_remote_default_model)
    return DeterministicProvider()
