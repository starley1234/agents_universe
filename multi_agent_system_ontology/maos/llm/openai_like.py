"""Драйвер для OpenAI-совместимого чат-протокола.

Покрывает local (llama.cpp server / vLLM / LM Studio / Ollama в режиме
/v1), openai и openrouter — все различаются только base_url/ключом.
MAOS-агенты НЕ вызывают инструменты через LLM tool-calls (см. docstring
maos/llm/base.py) — оркестрация детерминированная, поэтому здесь нет
части протокола про tools/tool_choice, только простой chat.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseLLM, LLMError, LLMReply, Usage


class OpenAILike(BaseLLM):
    name = "openai_like"

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: int = 120,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    def _chat_once(self, messages: list[dict[str, Any]]) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            again = exc.code == 429 or 500 <= exc.code < 600
            raise LLMError(f"HTTP {exc.code} от {self.base_url}: {detail}",
                           retryable=again) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Не достучались до {self.base_url}: {exc}",
                           retryable=True) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ответ {self.base_url} не является JSON",
                           retryable=True) from exc
        return self._parse(data)

    @classmethod
    def _parse(cls, data: dict[str, Any]) -> LLMReply:
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданная структура ответа: {str(data)[:300]}") from exc
        text = (msg.get("content") or "").strip()
        if not text:
            # reasoning-модели (DeepSeek-R1, Qwen thinking и т.п.) иногда
            # кладут текст в reasoning_content, оставляя content пустым.
            for key in ("reasoning_content", "reasoning", "thinking"):
                alt = msg.get(key)
                if isinstance(alt, str) and alt.strip():
                    text = alt.strip()
                    break
        return LLMReply(text=text, usage=_usage_from(data), raw=data)


def _usage_from(data: dict[str, Any]) -> Usage:
    u = data.get("usage") or {}
    if not isinstance(u, dict):
        return Usage()
    prompt = (u.get("prompt_tokens") or u.get("input_tokens")
              or u.get("prompt_eval_count") or 0)
    completion = (u.get("completion_tokens") or u.get("output_tokens")
                  or u.get("eval_count") or 0)
    try:
        return Usage(int(prompt), int(completion))
    except (TypeError, ValueError):
        return Usage()
