"""LiteLLM/OpenAI-compatible inference proxy (optional)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class InferenceUnavailable(RuntimeError):
    pass


class LiteLLMProxy:
    """Единый async-like интерфейс для custom_remote/OpenRouter/LiteLLM.

    Если пакет `litellm` установлен, вызывается его unified completion API;
    иначе используется минимальный OpenAI-compatible HTTP fallback.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def _profile(self) -> tuple[str, str, str]:
        active = str(self.settings.llm_active_provider).lower()
        if active == "openrouter":
            return self.settings.openrouter_api_url, self.settings.openrouter_api_key, self.settings.openrouter_model
        if active == "litellm" and self.settings.litellm_api_url:
            return self.settings.litellm_api_url, self.settings.custom_remote_key, self.settings.custom_remote_model
        return self.settings.custom_remote_url, self.settings.custom_remote_key, self.settings.custom_remote_model

    async def complete(self, messages: list[dict[str, str]], *, model: str | None = None, temperature: float = 0.2, **kwargs: Any) -> dict[str, Any]:
        url, key, configured_model = self._profile()
        selected_model = model or configured_model
        if not url or not selected_model:
            raise InferenceUnavailable("LLM provider не настроен: задайте CUSTOM_REMOTE_URL/MODEL или OpenRouter profile")
        try:
            import litellm  # type: ignore
        except ImportError:
            litellm = None
        if litellm is not None:
            response = await litellm.acompletion(model=selected_model, messages=messages, api_key=key or None, api_base=url, temperature=temperature, **kwargs)
            if hasattr(response, "model_dump"):
                return response.model_dump()
            return dict(response)
        import asyncio
        return await asyncio.to_thread(self._http_completion, url, key, selected_model, messages, temperature, kwargs)

    @staticmethod
    def _http_completion(url: str, key: str, model: str, messages: list[dict[str, str]], temperature: float, extra: dict[str, Any]) -> dict[str, Any]:
        endpoint = url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = json.dumps({"model": model, "messages": messages, "temperature": temperature, **extra}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise InferenceUnavailable(f"LLM endpoint недоступен: {exc}") from exc


__all__ = ["LiteLLMProxy", "InferenceUnavailable"]
