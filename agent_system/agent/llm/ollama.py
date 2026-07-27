"""Драйвер Ollama (нативный /api/chat).

Ollama умеет и OpenAI-совместимый /v1 — тогда берите провайдер "openai"
с base_url=http://localhost:11434/v1. Нативный эндпоинт оставлен потому,
что он не требует ключа и отдаёт tool_calls без обёртки choices.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import BaseLLM, LLMError, LLMReply, ToolCall, Usage


class Ollama(BaseLLM):
    name = "ollama"
    billable = False   # локальная модель: платить не за что

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: int = 300,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature

    def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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
            raise LLMError(
                f"Не достучались до Ollama {self.base_url}: {exc}. "
                "Запущен ли демон (ollama serve)?", retryable=True
            ) from exc

        msg = data.get("message", {})
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"__raw__": args}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}",
                                  name=fn.get("name", ""), arguments=args))
        # Ollama отдаёт prompt_eval_count / eval_count в корне ответа
        text, from_reasoning = self._text_parts(msg)
        return LLMReply(text=text, tool_calls=calls,
                        from_reasoning=from_reasoning,
                        usage=self._usage_from({"usage": data}), raw=data)
