"""Единый интерфейс к языковой модели для MAOS.

Независимая реализация (не импортирует agent_system). Формат ссылки на
модель везде в системе — СТРОКА `provider::model_name`, например
`local::llama3` или `openrouter::anthropic/claude-3` (см. parse_model_ref
в maos/llm/registry.py). Здесь — только протокол драйвера и то, что
общее для всех: учёт токенов и повтор при временном сбое сети.

Tool-calling: агент MAOS МОЖЕТ вызывать инструменты (файлы, веб-поиск,
RAG и т.п.) — см. maos/agents/runtime.py и maos/agents/toolbox.py. Это
ОПЦИОНАЛЬНАЯ способность конкретной личности (поле agent.tools в БД);
агент без инструментов работает как раньше — чистый синтезатор ответа
без цикла "модель -> инструмент -> модель".
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Usage:
    """Расход токенов за один вызов."""

    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.prompt + other.prompt, self.completion + other.completion)


@dataclass
class ToolCall:
    """Запрос модели на вызов инструмента."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    """Ответ модели: текст и/или список запросов на вызов инструментов."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: текст взят из reasoning_content, а content был пуст — см. core.py
    #: agent_system, тот же класс проблемы с reasoning-моделями.
    from_reasoning: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Ошибка обращения к модели (сеть, авторизация, неверный ответ).

    retryable=True — сеть/5xx/429, имеет смысл повторить или переключиться
    на fallback-провайдера. retryable=False — неверный ключ/конфиг, ни
    повтор, ни fallback не помогут (но fallback на ДРУГОГО провайдера
    всё равно может сработать — это решает HybridLLM, а не сам драйвер).
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class BaseLLM:
    """Драйвер провайдера. Реализации переопределяют _chat_once()."""

    name = "base"
    #: локальные драйверы ставят False — платить не за что
    billable = True
    #: заявленное контекстное окно модели в токенах. Нужно оркестратору
    #: (maos/orchestrator/context.py) для решения "нужна ли экстренная
    #: суммаризация перед отправкой запроса". 0 — неизвестно, оркестратор
    #: тогда использует консервативную оценку по умолчанию.
    context_window: int = 0

    def __init__(self, model: str, retries: int = 3, retry_base: float = 2.0,
                 context_window: int = 0, **kwargs: Any) -> None:
        self.model = model
        self.options = kwargs
        self.retries = max(0, retries)
        self.retry_base = retry_base
        if context_window:
            self.context_window = context_window
        self.usage = Usage()
        self.calls = 0
        self.retried = 0
        self.on_retry: Callable[[int, str, float], None] | None = None

    def _chat_once(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> LLMReply:
        raise NotImplementedError

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> LLMReply:
        """Вызов с повтором при временных сбоях (растущая пауза 2/4/8с)."""
        last: LLMError | None = None
        for attempt in range(self.retries + 1):
            try:
                reply = self._chat_once(messages, tools)
                self.calls += 1
                self.usage = self.usage + reply.usage
                return reply
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
                delay = self.retry_base * (2 ** attempt)
                delay += random.uniform(0, delay * 0.25)
                self.retried += 1
                if self.on_retry:
                    try:
                        self.on_retry(attempt + 1, str(exc), delay)
                    except Exception:
                        pass
                time.sleep(delay)
        raise last or LLMError("не удалось обратиться к модели")

    def cost(self) -> float | None:
        if not self.billable:
            return 0.0
        return None  # цены не встроены в MAOS — учитывается provider::model в БД

    @staticmethod
    def _text_parts(msg: dict[str, Any]) -> tuple[str, bool]:
        """Текст и признак «взят из reasoning». reasoning-модели (DeepSeek-R1,
        Qwen thinking и т.п.) иногда кладут рассуждение в reasoning_content,
        оставляя content пустым."""
        text = (msg.get("content") or "").strip()
        if text:
            return text, False
        for key in ("reasoning_content", "reasoning", "thinking"):
            alt = msg.get(key)
            if isinstance(alt, str) and alt.strip():
                return alt.strip(), True
        return "", False

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"<{type(self).__name__} model={self.model!r}>"
