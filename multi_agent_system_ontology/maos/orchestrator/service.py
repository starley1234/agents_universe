"""Оркестратор чата: маршрутизация к агенту, вызов LLM, запись памяти.

Единая точка входа для POST /v1/chat и для дашборда. Каждый вызов:
  1. Выбирает агента (semantic router) — если не передан явно.
  2. Собирает short+mid-term контекст (maos/orchestrator/context.py).
  3. Зовёт гибридный LLM (локальная/облачная модель + fallback).
  4. Пишет сообщение пользователя и ответ агента в message (историю).
  5. Пишет "квант памяти" question-answer в memory_quantum с меткой
     provider::model — источник для будущих semantic-поисков и для
     статистики "какой агент/модель сколько стоил" (ТЗ п.4, п.8).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..memory.store import Store
from .router import RouteDecision, route

if TYPE_CHECKING:
    from ..agents.runtime import AgentRuntime, TurnResult


@dataclass
class ChatResult:
    conversation_id: int
    agent_slug: str
    route: RouteDecision
    turn: "TurnResult"


class Orchestrator:
    def __init__(self, cfg: Config, store: Store, embedder: BaseEmbedder,
                runtime: "AgentRuntime | None" = None) -> None:
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        if runtime is None:
            from ..agents.runtime import AgentRuntime as _AgentRuntime
            runtime = _AgentRuntime(cfg)
        self.runtime = runtime

    def _history_as_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        rows = self.store.messages(conversation_id)
        out = []
        for r in rows:
            role = "assistant" if r["role"] == "agent" else r["role"]
            out.append({"role": role, "content": r["content"]})
        return out

    def chat(self, user_message: str, conversation_id: int | None = None,
             agent_slug: str | None = None) -> ChatResult:
        if conversation_id is None:
            conversation_id = self.store.create_conversation(
                title=user_message[:60])

        if agent_slug:
            agent_row = self.store.get_agent(agent_slug)
            if not agent_row:
                raise ValueError(f"Агент {agent_slug!r} не найден")
            decision = RouteDecision(agent_slug, "explicit",
                                     reason="агент указан явно")
        else:
            agents = self.store.agents_for_routing()
            decision = route(user_message, agents, self.embedder,
                             min_score=0.0)
            agent_row = self.store.get_agent(decision.agent_slug)
            if not agent_row:
                raise ValueError(
                    f"Роутер выбрал агента {decision.agent_slug!r}, "
                    "но он не найден в базе")

        history = self._history_as_messages(conversation_id)
        turn = self.runtime.respond(
            agent_row, user_message, history, store=self.store,
            embedder=self.embedder, conversation_id=conversation_id)

        self.store.add_message(conversation_id, "user", user_message)
        # авторитетность источника: fallback на локальную модель после
        # сбоя облачной помечается меньшей уверенностью — это "дешёвый"
        # ответ, отличить его от штатного облачного стоит для дальнейшего
        # анализа качества (ТЗ п.4: confidence_score в метаданных кванта).
        confidence = 0.6 if turn.used_fallback else 1.0
        self.store.add_message(
            conversation_id, "agent", turn.text, agent_id=agent_row["id"],
            provider_model=turn.provider_model, tokens_used=turn.tokens_used,
            confidence_score=confidence)

        try:
            qvec = self.embedder.embed_one(user_message)
        except EmbeddingError:
            qvec = None
        self.store.add_memory_quantum(
            conversation_id, user_message, turn.text, agent_id=agent_row["id"],
            provider_model=turn.provider_model, tokens_used=turn.tokens_used,
            confidence_score=confidence, embedding=qvec)

        return ChatResult(conversation_id, agent_row["slug"], decision, turn)
