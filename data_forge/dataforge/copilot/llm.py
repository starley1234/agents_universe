"""AI Copilot: минимальный клиент OpenAI-совместимого чат-протокола с
tool-calling (ТЗ §3.6, K6).

Не провайдер-агностичная абстракция с реестром (как в MAOS) — для
ОДНОГО протокола (OpenAI-совместимый, покрывает и облачные, и локальные
LM Studio/Ollama/vLLM серверы) отдельный реестр был бы преждевременной
сложностью; если понадобится другой протокол — код добавляется по
аналогии с `maos/llm/openai_like.py` в этом же репозитории.

Без `cfg.llm_base_url` Copilot отказывается работать с явной ошибкой
(`CopilotError`), а НЕ имитирует ответ — модуль полностью отключаем без
влияния на ядро платформы (остальные REST-эндпоинты DataForge работают
независимо от того, настроен ли LLM).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Ошибка обращения к LLM: сеть, авторизация, неверный формат ответа."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class OpenAILikeClient:
    """Клиент для .../chat/completions с опциональным tool-calling."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                timeout: int = 60, temperature: float = 0.2) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    @staticmethod
    def _tools_payload(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": t} for t in tools]

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> LLMReply:
        body: dict[str, Any] = {
            "model": self.model, "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = self._tools_payload(tools)
            body["tool_choice"] = "auto"
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
            raise LLMError(f"HTTP {exc.code} от {self.base_url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Не удалось связаться с {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Ответ {self.base_url} не является JSON") from exc
        return self._parse(data)

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMReply:
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
                args = {"__raw__": raw_args}
            calls.append(ToolCall(id=tc.get("id", f"call_{len(calls)}"),
                                  name=fn.get("name", ""), arguments=args))
        text = (msg.get("content") or "").strip()
        return LLMReply(text=text, tool_calls=calls)
