"""OpenAI-совместимый драйвер на стандартной библиотеке.

Один класс покрывает OpenAI, OpenRouter, LM Studio, llama.cpp server,
vLLM и Ollama в режиме /v1 — они различаются только base_url и ключом.
Для КБ это важно: контур часто закрытый, и единственная доступная
модель — локальная, поднятая рядом на своём железе.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseLLM, LLMError, Reply, Usage


class OpenAILike(BaseLLM):
    name = "openai_like"

    def __init__(self, model: str, *, base_url: str = "https://api.openai.com/v1",
                 api_key: str = "", timeout: int = 120,
                 temperature: float = 0.0, **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    def _chat_once(self, messages: list[dict[str, Any]]) -> Reply:
        body = {"model": self.model, "messages": messages,
                "temperature": self.temperature}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise LLMError(f"HTTP {exc.code} от {self.base_url}: {detail}",
                           retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Не достучались до {self.base_url}: {exc}",
                           retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ответ {self.base_url} не является JSON",
                           retryable=True) from exc
        return self._parse(data, self.model)

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> Reply:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"Неожиданная структура ответа: {str(data)[:300]}") from exc
        text = (msg.get("content") or "").strip()
        if not text:
            # Reasoning-модели иногда кладут текст в reasoning_content.
            for key in ("reasoning_content", "reasoning", "thinking"):
                alt = msg.get(key)
                if isinstance(alt, str) and alt.strip():
                    text = alt.strip()
                    break
        return Reply(text=text, usage=_usage(data),
                     model=str(data.get("model") or model), raw=data)


def _usage(data: dict[str, Any]) -> Usage:
    u = data.get("usage")
    if not isinstance(u, dict):
        return Usage()
    tin = (u.get("prompt_tokens") or u.get("input_tokens")
           or u.get("prompt_eval_count") or 0)
    tout = (u.get("completion_tokens") or u.get("output_tokens")
            or u.get("eval_count") or 0)
    try:
        return Usage(int(tin), int(tout))
    except (TypeError, ValueError):
        return Usage()
