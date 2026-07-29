"""Гибридный LLM-роутинг: локальная модель по умолчанию, облако — по
потребности, с авто-переключением на локальную при сбое (ТЗ п.3).

Три источника решения "какую модель звать", по убыванию приоритета:
  1. У агента явно задан llm_ref (agent.llm_ref в БД) — используем его.
  2. Иначе — эвристика сложности задачи: длинный запрос считается
     "сложным" и уходит в облако (default_cloud_model), короткий — в
     локальную модель (default_local_model). Экономия токенов и денег:
     обслуживание памяти и простые реплики не должны платить цену
     топовой модели.
  3. Fallback: при ошибке ВЫБРАННОГО провайдера (сеть, лимиты, 5xx) и
     cfg.fallback_to_local=True — автоматически пробуем локальную модель,
     а не роняем запрос целиком. Успешный ответ всегда несёт метку
     provider::model, которая РЕАЛЬНО его сгенерировала — это и есть та
     самая "авторитетность" записи из ТЗ (Knowledge Labeling).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..llm.base import LLMError, LLMReply
from ..llm.registry import build_from_ref, parse_model_ref


@dataclass
class HybridReply:
    reply: LLMReply
    provider_model: str    # provider::model, РЕАЛЬНО ответившая модель
    used_fallback: bool


class HybridLLM:
    """Решает, какую модель звать, и умеет откатиться на локальную."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def choose_ref(self, task: str, agent_llm_ref: str = "") -> str:
        """Ссылка на модель для конкретной задачи, без фактического вызова."""
        if agent_llm_ref:
            return agent_llm_ref
        if len(task) >= self.cfg.complexity_char_threshold:
            return self.cfg.default_cloud_model
        return self.cfg.default_local_model

    def chat(self, messages: list[dict], task: str,
             agent_llm_ref: str = "",
             tools: list[dict] | None = None) -> HybridReply:
        primary_ref = self.choose_ref(task, agent_llm_ref)
        overrides = {"retries": self.cfg.llm_retries,
                    "retry_base": self.cfg.llm_retry_base}
        try:
            llm = build_from_ref(primary_ref, **overrides)
        except LLMError:
            # неизвестный провайдер в конфиге агента — сразу пробуем
            # дефолтную локальную, это ошибка конфигурации, а не сети
            return self._fallback(messages, primary_ref, None, tools)
        try:
            reply = llm.chat(messages, tools)
            return HybridReply(reply, primary_ref, used_fallback=False)
        except LLMError as exc:
            if not self.cfg.fallback_to_local:
                raise
            return self._fallback(messages, primary_ref, exc, tools)

    def _fallback(self, messages: list[dict], primary_ref: str,
                  cause: LLMError | None,
                  tools: list[dict] | None = None) -> HybridReply:
        local_ref = self.cfg.default_local_model
        provider, _ = parse_model_ref(primary_ref) if "::" in primary_ref else ("", "")
        if provider == "local" or local_ref == primary_ref:
            # уже пытались локальную — второго дна нет, поднимаем исходную ошибку
            raise cause or LLMError(
                f"Не удалось обратиться к модели {primary_ref!r}, "
                "а локальный fallback не отличается от неё же.")
        try:
            llm = build_from_ref(local_ref, retries=self.cfg.llm_retries,
                                 retry_base=self.cfg.llm_retry_base)
            reply = llm.chat(messages, tools)
            return HybridReply(reply, local_ref, used_fallback=True)
        except LLMError as exc2:
            raise LLMError(
                f"Основная модель {primary_ref!r} недоступна "
                f"({cause or 'ошибка конфигурации'}), fallback {local_ref!r} "
                f"тоже недоступен: {exc2}"
            ) from exc2

