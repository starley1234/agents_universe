"""Рантайм одной агентской личности: системный промпт + LLM-вызов.

Агент MAOS — НЕ инструментальный агент, как в agent_system (там цикл
"модель -> инструменты -> модель"). Здесь агент — это профиль синтеза
ответа: идентичность (имя, промпт, голос) плюс своя (или дефолтная)
LLM-модель. Вызов инструментов/файловой системы не предусмотрен ТЗ —
система про оркестрацию личностей и память, а не про исполнение кода.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..llm.embeddings import BaseEmbedder
from ..memory.store import Store
from ..orchestrator.context import build_messages
from ..orchestrator.hybrid import HybridLLM


@dataclass
class TurnResult:
    text: str
    provider_model: str
    used_fallback: bool
    tokens_used: int


DEFAULT_SYSTEM_PROMPT = (
    "Ты полезный ассистент. Отвечай по существу, кратко и по делу. "
    "Если не знаешь ответа — так и скажи, не выдумывай."
)


class AgentRuntime:
    """Исполняет один ход диалога от лица конкретного агента."""

    def __init__(self, cfg: Config, hybrid: HybridLLM | None = None) -> None:
        self.cfg = cfg
        self.hybrid = hybrid or HybridLLM(cfg)

    def respond(self, agent_row: dict[str, Any], user_message: str,
               history: list[dict[str, Any]], store: Store | None = None,
               embedder: BaseEmbedder | None = None,
               conversation_id: int | None = None) -> TurnResult:
        system_prompt = agent_row.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        llm_ref = agent_row.get("llm_ref") or ""
        from ..llm.registry import provider_context_window, parse_model_ref
        try:
            provider, _ = parse_model_ref(
                llm_ref or self.hybrid.choose_ref(user_message))
            window = provider_context_window(provider)
        except Exception:
            window = self.cfg.small_context_window

        messages = build_messages(
            system_prompt, history, user_message, self.cfg, window,
            store=store, embedder=embedder, conversation_id=conversation_id)

        result = self.hybrid.chat(messages, user_message, agent_llm_ref=llm_ref)
        return TurnResult(
            text=result.reply.text,
            provider_model=result.provider_model,
            used_fallback=result.used_fallback,
            tokens_used=result.reply.usage.total,
        )
