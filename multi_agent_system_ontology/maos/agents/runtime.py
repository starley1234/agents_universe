"""Рантайм одной агентской личности: системный промпт + LLM-вызов, с
ОПЦИОНАЛЬНЫМ инструментальным циклом.

Агент MAOS по умолчанию — чистый синтезатор ответа (идентичность + LLM,
без вызова инструментов), как и было задумано в исходном ТЗ ("система
про оркестрацию личностей и память, а не про исполнение кода"). Но
конкретной личности МОЖНО назначить набор навыков (agent.tools в БД:
"files,web,rag,office") — тогда ход диалога выполняется циклом «модель
-> инструмент -> модель» (maos/agents/loop.py), как в agent_system.
Агент без agent.tools работает ровно как раньше — разницы в поведении
для существующих личностей нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..llm.embeddings import BaseEmbedder
from ..memory.store import Store
from ..orchestrator.context import build_messages
from ..orchestrator.hybrid import HybridLLM
from ..tools.base import ToolRegistry
from ..tools.toolbox import ToolboxError, build_toolbox
from .loop import run_tool_loop


@dataclass
class TurnResult:
    text: str
    provider_model: str
    used_fallback: bool
    tokens_used: int
    tool_calls: int = 0
    stopped_by: str = "done"       # "done" | "max_steps" | "error"


DEFAULT_SYSTEM_PROMPT = (
    "Ты полезный ассистент. Отвечай по существу, кратко и по делу. "
    "Если не знаешь ответа — так и скажи, не выдумывай."
)

TOOLS_SYSTEM_SUFFIX = (
    "\n\nУ тебя есть инструменты — используй их, а не выдумывай факты: "
    "читай файлы, ищи в вебе, работай с документами по необходимости. "
    "Проверяй результат инструментом, а не полагайся на догадку."
)


class AgentRuntime:
    """Исполняет один ход диалога от лица конкретного агента."""

    def __init__(self, cfg: Config, hybrid: HybridLLM | None = None) -> None:
        self.cfg = cfg
        self.hybrid = hybrid or HybridLLM(cfg)

    def respond(self, agent_row: dict[str, Any], user_message: str,
               history: list[dict[str, Any]], store: Store | None = None,
               embedder: BaseEmbedder | None = None,
               conversation_id: int | None = None,
               on_event=None) -> TurnResult:
        system_prompt = agent_row.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        llm_ref = agent_row.get("llm_ref") or ""
        from ..llm.registry import provider_context_window, parse_model_ref
        try:
            provider, _ = parse_model_ref(
                llm_ref or self.hybrid.choose_ref(user_message))
            window = provider_context_window(provider)
        except Exception:
            window = self.cfg.small_context_window

        tools_field = (agent_row.get("tools") or "").strip()
        registry: ToolRegistry | None = None
        if tools_field:
            try:
                built = build_toolbox(self.cfg, agent_row, store=store,
                                      embedder=embedder)
            except ToolboxError as exc:
                return TurnResult(f"Ошибка конфигурации инструментов агента: {exc}",
                                  "", False, 0, 0, "error")
            if built:
                registry = ToolRegistry()
                registry.extend(built)
                system_prompt = system_prompt + TOOLS_SYSTEM_SUFFIX

        messages = build_messages(
            system_prompt, history, user_message, self.cfg, window,
            store=store, embedder=embedder, conversation_id=conversation_id)

        if registry is not None:
            loop_result = run_tool_loop(
                self.hybrid, self.cfg, messages, user_message, llm_ref,
                registry, on_event=on_event)
            return TurnResult(
                text=loop_result.text,
                provider_model=loop_result.provider_model,
                used_fallback=loop_result.used_fallback,
                tokens_used=loop_result.tokens_used,
                tool_calls=loop_result.tool_calls,
                stopped_by=loop_result.stopped_by,
            )

        result = self.hybrid.chat(messages, user_message, agent_llm_ref=llm_ref)
        return TurnResult(
            text=result.reply.text,
            provider_model=result.provider_model,
            used_fallback=result.used_fallback,
            tokens_used=result.reply.usage.total,
        )
