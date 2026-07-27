"""Единый интерфейс к языковой модели.

Зачем слой абстракции: провайдеры отличаются форматом сообщений и
описанием инструментов, но агенту нужно одно и то же — отправить
историю и получить либо текст, либо вызовы инструментов. Всё различие
прячется в драйверах, ядро о провайдерах не знает.

Здесь же две сквозные возможности, общие для всех провайдеров:
  * УЧЁТ ТОКЕНОВ — сколько израсходовано и на какую сумму;
  * ПОВТОР ПРИ СБОЕ — сеть моргнула, а восьмичасовой прогон продолжился.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCall:
    """Запрос модели на вызов инструмента."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    """Расход токенов за один вызов."""

    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.prompt + other.prompt,
                     self.completion + other.completion)


@dataclass
class LLMReply:
    """Ответ модели: текст и/или список вызовов инструментов."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: текст взят из reasoning_content, а content был пуст.
    #: Для reasoning-моделей это признак НЕЗАВЕРШЁННОГО хода: модель
    #: рассуждала, но ни ответа, ни вызова инструмента не выдала.
    from_reasoning: bool = False
    # сырой ответ провайдера — нужен для отладки
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Ошибка обращения к модели (сеть, авторизация, неверный ответ)."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        # Различать обязательно: сеть и 5xx имеет смысл повторить,
        # а неверный ключ или битый запрос — нет, это просто трата времени.
        self.retryable = retryable


# Цены за миллион токенов, доллары. Для локальных моделей — нули.
# Список неполный намеренно: неизвестная модель считается бесплатной,
# и в отчёте это видно как «цена неизвестна», а не как выдуманная сумма.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3-mini": (1.10, 4.40),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-opus-4": (15.00, 75.00),
    "deepseek-chat": (0.27, 1.10),
    "qwen": (0.0, 0.0),
    "devstral": (0.0, 0.0),
    "llama": (0.0, 0.0),
    "mistral": (0.0, 0.0),
}


def price_of(model: str) -> tuple[float, float] | None:
    """Цена (вход, выход) за миллион токенов или None, если неизвестна.

    Ключи проверяются от САМОГО ДЛИННОГО к короткому. Иначе 'gpt-4o'
    перехватывает 'gpt-4o-mini' и счёт завышается в 16 раз — эта ошибка
    была поймана тестом.
    """
    m = (model or "").lower()
    for key in sorted(PRICES, key=len, reverse=True):
        if key in m:
            return PRICES[key]
    return None


class BaseLLM:
    """Драйвер провайдера.

    Реализации переопределяют _chat_once(). Публичный chat() добавляет
    повтор при сбоях и учёт расхода — одинаково для всех провайдеров.
    """

    name = "base"
    #: локальные драйверы ставят False — платить не за что
    billable = True

    def __init__(self, model: str, retries: int = 3, retry_base: float = 2.0,
                 **kwargs: Any) -> None:
        self.model = model
        self.options = kwargs
        self.retries = max(0, retries)
        self.retry_base = retry_base
        self.usage = Usage()          # накопительно за всё время жизни
        self.calls = 0
        self.retried = 0
        self.on_retry: Callable[[int, str, float], None] | None = None

    # --- реализуют драйверы ---
    def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMReply:
        raise NotImplementedError

    # --- общее для всех ---
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMReply:
        """Вызов с повтором при временных сбоях.

        Без этого одна моргнувшая сеть обрывает восьмичасовой прогон —
        самый обидный способ потерять работу. Пауза растёт (2, 4, 8 с)
        и слегка рандомизируется, чтобы несколько агентов не долбили
        сервер синхронно.
        """
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

    # --- деньги ---
    def cost(self) -> float | None:
        """Стоимость израсходованного или None, если цена неизвестна."""
        if not self.billable:
            return 0.0
        p = price_of(self.model)
        if p is None:
            return None
        return (self.usage.prompt * p[0] + self.usage.completion * p[1]) / 1e6

    def spend_report(self) -> str:
        u = self.usage
        base = (f"токенов: {u.total:,} (вход {u.prompt:,}, "
                f"выход {u.completion:,}), вызовов: {self.calls}")
        if self.retried:
            base += f", повторов после сбоя: {self.retried}"
        c = self.cost()
        if c is None:
            return base + "  · цена модели неизвестна"
        if c == 0:
            return base + "  · локальная модель, оплаты нет"
        return base + f"  · примерно ${c:.4f}"

    @staticmethod
    def _text_parts(msg: dict[str, Any]) -> tuple[str, bool]:
        """Текст и признак «взят из reasoning». См. _text_from."""
        text = (msg.get("content") or "").strip()
        if text:
            return text, False
        for key in ("reasoning_content", "reasoning", "thinking"):
            alt = msg.get(key)
            if isinstance(alt, str) and alt.strip():
                return alt.strip(), True
        return "", False

    @staticmethod
    def _text_from(msg: dict[str, Any]) -> str:
        """Текст ответа с учётом reasoning-моделей.

        Qwen3.5, DeepSeek-R1, o1 и им подобные кладут рассуждение в
        reasoning_content, а content оставляют ПУСТЫМ. Если читать только
        content, агент получает пустой ответ без вызовов инструментов,
        считает задачу выполненной и молча останавливается на втором шаге.
        Именно так и происходило.
        """
        text = (msg.get("content") or "").strip()
        if text:
            return text
        for key in ("reasoning_content", "reasoning", "thinking"):
            alt = msg.get(key)
            if isinstance(alt, str) and alt.strip():
                return alt.strip()
        return ""

    @staticmethod
    def _usage_from(data: dict[str, Any]) -> Usage:
        """Достать расход из ответа. Провайдеры зовут поля по-разному."""
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

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"<{type(self).__name__} model={self.model!r}>"
