"""Контроль контекста: экономия токенов через three-tier память (ТЗ п.4, п.5).

Перед каждым обращением к LLM оркестратор:
  1. Оценивает размер накопленной short-term истории. Оценка токенов —
     грубая (len(text) / 4, стандартное для английского и рабочее для
     большинства языков приближение), но детерминированная и не требует
     тяжёлого токенизатора конкретной модели — нам не нужна точность до
     токена, только решение "влезаем или нет".
  2. Если не влезаем (или окно модели меньше small_context_window из
     конфига) — запускает ЭКСТРЕННУЮ суммаризацию: старые сообщения
     сворачиваются в одну заметку через summarizer (обычно дешёвая
     локальная модель), последние short_term_keep_last сообщений
     остаются как есть.
  3. Подмешивает mid-term "кванты памяти": векторный поиск по вопросу
     пользователя среди memory_quantum, чтобы в контекст попадали только
     релевантные фрагменты прошлых диалогов, а не вся история целиком.
"""
from __future__ import annotations

from typing import Any, Callable

from ..config import Config
from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..memory.store import Store

#: грубая оценка символов на токен для большинства языков/моделей.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(m.get("content", ""))) for m in messages)


Summarizer = Callable[[str], str]


def _default_summarizer(text: str) -> str:
    """Суммаризация без LLM — на крайний случай, если summarizer не передан
    (например, тесты или полностью офлайн-режим). Не заменяет настоящую
    модель по качеству, но детерминирована и не роняет пайплайн."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 6:
        return " / ".join(lines)
    head = lines[:3]
    tail = lines[-3:]
    return " / ".join(head) + f" … [{len(lines) - 6} строк опущено] … " + " / ".join(tail)


def needs_summarization(history: list[dict[str, Any]], context_window: int,
                        small_context_window: int) -> bool:
    """Порог срабатывания — либо у модели маленькое окно (ТЗ: "если окно
    модели < 4k"), либо накопленная история сама по себе близка к любому
    разумному окну (консервативный запас 70%, чтобы остался простор под
    system prompt, mid-term кванты и сам ответ модели)."""
    if context_window and context_window < small_context_window:
        return True
    budget = context_window or small_context_window
    return estimate_messages_tokens(history) > budget * 0.7


def summarize_history(history: list[dict[str, Any]], keep_last: int,
                      summarizer: Summarizer | None = None) -> list[dict[str, Any]]:
    """Сжимает всё, кроме последних keep_last сообщений, в одну заметку."""
    if len(history) <= keep_last:
        return history
    old, recent = history[:-keep_last] if keep_last > 0 else history, \
        history[-keep_last:] if keep_last > 0 else []
    if not old:
        return history
    joined = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in old)
    fn = summarizer or _default_summarizer
    summary_text = fn(joined)
    note = {"role": "system",
           "content": f"[Сжатая сводка более ранней части диалога: {summary_text}]"}
    return [note, *recent]


def retrieve_mid_term(store: Store, embedder: BaseEmbedder, query: str,
                      cfg: Config, conversation_id: int | None = None,
                      ) -> list[dict[str, Any]]:
    """Кванты памяти, релевантные текущему запросу (semantic search)."""
    try:
        vec = embedder.embed_one(query)
    except EmbeddingError:
        return []
    quanta = store.semantic_search_quanta(
        vec, limit=cfg.mid_term_top_k, conversation_id=conversation_id,
        min_score=cfg.mid_term_min_score)
    return quanta


def format_mid_term_note(quanta: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not quanta:
        return None
    lines = ["[Релевантные фрагменты памяти из прошлых диалогов:]"]
    for q in quanta:
        lines.append(
            f"- Q: {q['question']}\n  A: {q['answer']} "
            f"(источник: {q.get('provider_model') or 'н/д'}, "
            f"сходство {q['score']:.2f})")
    return {"role": "system", "content": "\n".join(lines)}


def build_messages(system_prompt: str, history: list[dict[str, Any]],
                   user_message: str, cfg: Config, context_window: int,
                   store: Store | None = None, embedder: BaseEmbedder | None = None,
                   conversation_id: int | None = None,
                   summarizer: Summarizer | None = None) -> list[dict[str, Any]]:
    """Собирает полный набор сообщений для отправки в LLM.

    Порядок: system prompt -> (опционально) mid-term кванты -> (короткая
    или суммированная) short-term история -> новое сообщение пользователя.
    """
    short = history
    if needs_summarization(history, context_window, cfg.small_context_window):
        short = summarize_history(history, cfg.short_term_keep_last, summarizer)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if store is not None and embedder is not None:
        quanta = retrieve_mid_term(store, embedder, user_message, cfg,
                                   conversation_id=conversation_id)
        note = format_mid_term_note(quanta)
        if note:
            messages.append(note)
    messages.extend(short)
    messages.append({"role": "user", "content": user_message})
    return messages
