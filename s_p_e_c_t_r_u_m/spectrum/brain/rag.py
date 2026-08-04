"""RAG-пайплайн: Retrieval-Augmented Generation с трейсабилити."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from ..storage.vector import SearchHit, VectorStore
from .prompts import SystemPrompts

logger = logging.getLogger("spectrum.rag")


@dataclass
class RAGResponse:
    """Ответ RAG с источниками."""
    answer: str
    sources: list[SearchHit]
    question: str
    context_used: str = ""
    model: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "question": self.question,
            "model": self.model,
            "sources": [
                {
                    "text_preview": s.text[:200],
                    "source_path": s.source_path,
                    "page_number": s.page_number,
                    "score": round(s.score, 4),
                    "citation": s.citation(),
                }
                for s in self.sources
            ],
        }


class RAG:
    """RAG-пайплайн: вопрос → поиск → генерация с источниками.

    Классический цикл:
    1. Embedding запроса пользователя
    2. Поиск в векторном индексе (top-k)
    3. Формирование контекста с трейсабилити
    4. Генерация ответа через LLM
    """

    def __init__(
        self,
        vector_store: VectorStore,
        api_url: str = "",
        api_key: str = "",
        model: str = "",
        top_k: int = 5,
        similarity_threshold: float = 0.3,
    ):
        self._store = vector_store
        self._api_url = api_url
        self._api_key = api_key
        self._model = model
        self._top_k = top_k
        self._similarity_threshold = similarity_threshold
        self._embedder = None

    @classmethod
    def from_settings(cls, vector_store: VectorStore, top_k: int = 5) -> RAG:
        """Создаёт RAG из настроек."""
        from ..config import settings
        cfg = settings()
        p = cfg.llm_profile
        return cls(
            vector_store=vector_store,
            api_url=p.api_url,
            api_key=p.api_key,
            model=p.model,
            top_k=top_k,
        )

    def ask(self, question: str, **kwargs) -> RAGResponse:
        """Полный цикл RAG: вопрос → ответ с источниками."""
        # 1. Embedding запроса
        query_embedding = self._embed(question)

        # 2. Поиск в векторном индексе
        hits = self._store.search(query_embedding, top_k=self._top_k)
        hits = [h for h in hits if h.score >= self._similarity_threshold]

        if not hits:
            return RAGResponse(
                answer="К сожалению, я не нашёл релевантной информации в базе знаний по вашему вопросу.",
                sources=[],
                question=question,
            )

        # 3. Формируем контекст с трейсабилити
        context = self._build_context(hits)

        # 4. Генерируем ответ
        system_prompt = SystemPrompts.rag_system()
        user_prompt = SystemPrompts.rag_with_context(question, context)

        answer, usage = self._generate(system_prompt, user_prompt)

        return RAGResponse(
            answer=answer,
            sources=hits,
            question=question,
            context_used=context,
            model=self._model,
            token_usage=usage,
        )

    def _build_context(self, hits: list[SearchHit]) -> str:
        """Формирует контекст с чёткой привязкой к источникам."""
        parts = []
        for i, hit in enumerate(hits, 1):
            citation = hit.citation()
            parts.append(
                f"[Источник {i}: {citation} | Релевантность: {hit.score:.2f}]\n"
                f"{hit.text}\n"
            )
        return "\n---\n".join(parts)

    def _embed(self, text: str) -> list[float]:
        """Генерирует embedding для текста."""
        # Пытаемся использовать sentence-transformers
        try:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            return self._embedder.encode(text).tolist()
        except ImportError:
            pass

        # Фолбэк: детерминированный хеш-вектор
        return self._hash_embed(text, dim=384)

    def _generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        """Генерация ответа через LLM API."""
        if not self._api_url:
            return self._offline_generate(user), {}

        try:
            import requests

            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 4096,
                "temperature": 0.1,
            }

            resp = requests.post(
                f"{self._api_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            answer = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            return answer, {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }

        except Exception as e:
            logger.warning("LLM call failed: %s", e)
            return self._offline_generate(user), {}

    def _offline_generate(self, user_prompt: str) -> str:
        """Оффлайн-режим: возвращает извлечённый контекст без LLM."""
        # Извлекаем блоки контекста
        import re
        sources = re.findall(r"\[Источник \d+:.*?\]\n(.*?)(?=\n---|\Z)", user_prompt, re.S)
        if not sources:
            return "Нет данных для ответа. LLM не подключён."

        result = "⚠️ **Оффлайн-режим** (LLM не подключён). Вот найденная информация:\n\n"
        for i, src in enumerate(sources, 1):
            result += f"**Фрагмент {i}:**\n{src.strip()}\n\n"
        return result

    @staticmethod
    def _hash_embed(text: str, dim: int = 384) -> list[float]:
        """Детерминированный embedding из хеша (для тестов)."""
        from ..storage.vector import _hash_embed
        return _hash_embed(text, dim)
