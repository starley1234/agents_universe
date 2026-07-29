"""Драйвер для OpenAI-совместимого API.

Покрывает сразу большинство вариантов: сам OpenAI, OpenRouter, LM Studio,
llama.cpp server, vLLM и Ollama в режиме /v1. Отличаются они только
base_url и наличием ключа.

Namespace-пакет не используем: работаем на голой стандартной библиотеке,
чтобы систему можно было запустить без pip install.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseLLM, LLMError, LLMReply, ToolCall


class OpenAILike(BaseLLM):
    name = "openai"
    supports_vision = True


    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: int = 120,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    # --- преобразование формата инструментов ---------------------------
    @staticmethod
    def _tools_payload(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": t} for t in tools]

    def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = self._tools_payload(tools)
            body["tool_choice"] = "auto"

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
        except urllib.error.HTTPError as exc:  # понятная диагностика вместо трейсбека
            detail = exc.read().decode("utf-8", "replace")[:400]
            # 429 и 5xx — временные, их имеет смысл повторить;
            # 401/400 повторять бессмысленно, это ошибка настройки.
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

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                # Модель иногда портит JSON. Не падаем: отдаём как есть,
                # исполнитель вернёт понятную ошибку валидации.
                args = {"__raw__": raw_args}
            calls.append(
                ToolCall(id=tc.get("id", f"call_{len(calls)}"),
                         name=fn.get("name", ""), arguments=args)
            )
        text, from_reasoning = cls._text_parts(msg)
        return LLMReply(text=text, tool_calls=calls,
                        from_reasoning=from_reasoning,
                        usage=cls._usage_from(data), raw=data)
