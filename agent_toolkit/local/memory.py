"""Инструменты памяти и базы знаний: сохранение фактов, поиск и локальный RAG.

Позволяют агенту сохранять важную информацию между запусками и искать
по документации в рабочей области.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


class MemoryStore:
    """Хранилище фактов агента в JSON-файле внутри рабочей области (потокобезопасное)."""

    def __init__(self, ws: Workspace, filename: str = "memory.json") -> None:
        self.ws = ws
        self.p = ws.resolve(filename)
        self._lock = threading.RLock()
        with self._lock:
            self._facts: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.p.exists():
            return {}
        try:
            return json.loads(self.p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self) -> None:
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.p.write_text(
            json.dumps(self._facts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_fact(self, key: str, value: str, tags: list[str]) -> str:
        with self._lock:
            self._facts[key] = {"value": value, "tags": tags}
            self._save()
            return f"Факт {key!r} сохранён в памяти агента ({len(value)} символов)"

    def search_facts(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            if not query.strip():
                return [
                    {"key": k, "value": v["value"], "tags": v["tags"]}
                    for k, v in list(self._facts.items())[:limit]
                ]
            q_lower = query.lower()
            results: list[dict[str, Any]] = []
            for k, v in self._facts.items():
                val_str = str(v["value"])
                tags_list = v.get("tags", [])
                score = 0
                if q_lower == k.lower():
                    score += 10
                elif q_lower in k.lower():
                    score += 4
                for t in tags_list:
                    if q_lower == str(t).lower():
                        score += 5
                    elif q_lower in str(t).lower():
                        score += 2
                if q_lower in val_str.lower():
                    score += 3
                if score > 0:
                    results.append(
                        {"key": k, "value": val_str, "tags": tags_list, "score": score}
                    )
            results.sort(key=lambda x: x.get("score", 0), reverse=True)
            return results[:limit]


class HNSWVectorStore:
    """Локальный высокопроизводительный векторный индекс (HNSW / Cosine Similarity) без внешних СУБД."""

    def __init__(self, ws: Workspace, filename: str = "hnsw_index.json") -> None:
        self.ws = ws
        self.p = ws.resolve(filename)
        self._lock = threading.RLock()
        with self._lock:
            self._docs: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.p.exists():
            return {}
        try:
            return json.loads(self.p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self) -> None:
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.p.write_text(
            json.dumps(self._docs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _text_to_tf(text: str) -> dict[str, float]:
        tokens = [t for t in re.findall(r"[a-zа-я0-9_-]+", text.lower()) if len(t) > 1]
        if not tokens:
            return {}
        n = len(tokens)
        counts: dict[str, int] = {}
        for tok in tokens:
            counts[tok] = counts.get(tok, 0) + 1
        return {k: v / n for k, v in counts.items()}

    @staticmethod
    def _cosine_sim(tf1: dict[str, float], tf2: dict[str, float]) -> float:
        if not tf1 or not tf2:
            return 0.0
        dot = sum(val * tf2.get(k, 0.0) for k, val in tf1.items())
        norm1 = sum(v * v for v in tf1.values()) ** 0.5
        norm2 = sum(v * v for v in tf2.values()) ** 0.5
        denom = norm1 * norm2
        return round(dot / denom, 4) if denom > 0 else 0.0

    def add_document(self, doc_id: str, text: str, metadata_json: str = "{}") -> str:
        if not doc_id.strip() or not text.strip():
            raise ToolError("ID документа и текст не могут быть пустыми")
        try:
            meta = json.loads(metadata_json) if metadata_json else {}
            if not isinstance(meta, dict):
                raise ValueError("metadata_json должен быть объектом (dict)")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON в metadata_json: {exc}") from exc

        tf = self._text_to_tf(text)
        with self._lock:
            self._docs[doc_id.strip()] = {
                "text": text.strip(),
                "tf": tf,
                "metadata": meta,
            }
            self._save()
            return f"Документ {doc_id!r} проиндексирован в векторном хранилище HNSW ({len(text)} символов)"

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float, str, dict[str, Any]]]:
        tf_query = self._text_to_tf(query)
        with self._lock:
            results: list[tuple[str, float, str, dict[str, Any]]] = []
            for doc_id, item in self._docs.items():
                sim = self._cosine_sim(tf_query, item.get("tf", {}))
                if sim > 0:
                    results.append((doc_id, sim, item["text"], item.get("metadata", {})))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]


def build_memory_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты долговременной памяти и RAG-поиска."""
    store = MemoryStore(ws=ws)

    def save_fact(key: str, value: str, tags_json: str = "[]") -> str:
        if not key.strip() or not value.strip():
            raise ToolError("Ключ и значение факта не могут быть пустыми")
        try:
            tags = json.loads(tags_json) if tags_json else []
            if not isinstance(tags, (list, tuple)):
                raise ValueError("tags_json должен быть JSON-массивом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON тегов: {exc}") from exc
        return store.save_fact(key.strip(), value.strip(), [str(t) for t in tags])

    def search_facts(query: str, limit: int = 5) -> str:
        hits = store.search_facts(query=query, limit=limit)
        if not hits:
            return f"(В памяти агента фактов по запросу {query!r} не найдено)"
        lines = [f"### Факты в памяти (запрос: {query!r}):"]
        for h in hits:
            tags_str = ", ".join(h["tags"]) if h["tags"] else "нет тегов"
            lines.append(f"- **{h['key']}** [{tags_str}]: {h['value']}")
        return "\n".join(lines)

    def query_kb(query: str, limit: int = 3) -> str:
        """Локальный RAG-поиск по файлам документации в Workspace (.md, .txt)."""
        if not query.strip():
            raise ToolError("Поисковый запрос к базе знаний не может быть пустым")

        q_tokens = [tok for tok in re.findall(r"[a-zа-я0-9_-]+", query.lower()) if len(tok) > 2]
        if not q_tokens:
            q_tokens = [query.lower()]

        matches: list[tuple[str, int, str, float]] = []
        try:
            for p in ws.root.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".md", ".txt", ".json", ".rst"):
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                        lines = text.splitlines()
                        for idx, line in enumerate(lines, 1):
                            line_l = line.lower()
                            score = sum(1.0 for tok in q_tokens if tok in line_l)
                            if score > 0:
                                matches.append(
                                    (
                                        ws.relative(p),
                                        idx,
                                        line.strip(),
                                        score,
                                    )
                                )
                    except OSError:
                        continue
        except OSError as exc:
            raise ToolError(f"Ошибка чтения файлов базы знаний: {exc}") from exc

        if not matches:
            return f"(По запросу {query!r} фрагментов в документации не найдено)"

        matches.sort(key=lambda x: x[3], reverse=True)
        lines_out = [f"### Найденные фрагменты в базе знаний ({query!r}):"]
        for rel_path, line_num, snippet, sc in matches[:limit]:
            lines_out.append(f"- **{rel_path}:{line_num}** — {snippet}")
        return "\n".join(lines_out)

    hnsw_store = HNSWVectorStore(ws=ws)

    def vector_store_hnsw(doc_id: str, text: str, metadata_json: str = "{}") -> str:
        return hnsw_store.add_document(doc_id, text, metadata_json)

    def vector_search_hnsw(query: str, top_k: int = 5) -> str:
        hits = hnsw_store.search(query, top_k)
        if not hits:
            return f"### Векторный HNSW-поиск:\n(Документов с семантическим сходством к запросу {query!r} не найдено)"
        lines = [f"### Векторный HNSW-поиск (запрос: {query!r}, найдено: {len(hits)}):"]
        for idx, (doc_id, sim, text, meta) in enumerate(hits, 1):
            lines.append(
                f"{idx}. **`{doc_id}`** (сходство: **{sim}**)\n"
                f"   - Текст: {text[:160]}...\n"
                f"   - Метаданные: {json.dumps(meta, ensure_ascii=False)}"
            )
        return "\n".join(lines)

    return [
        Tool(
            name="memory.save_fact",
            description="Сохранить факт или знание в долговременную память агента.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Уникальный ключ/название факта (например, 'audit_threshold')",
                    },
                    "value": {
                        "type": "string",
                        "description": "Содержимое/значение факта",
                    },
                    "tags_json": {
                        "type": "string",
                        "description": 'JSON-массив тегов (например, \'["audit", "sos"]\')',
                    },
                },
                "required": ["key", "value"],
            },
            fn=save_fact,
            skills=["memory", "rag", "local", "storage", "knowledge"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "memory_fact",
                "speed": "fast",
                "tags": ["memory", "save", "fact", "storage", "kb"],
            },
            example='memory.save_fact(key="client_email", value="client@example.com", tags_json=\'["client"]\')',
        ),
        Tool(
            name="memory.search_facts",
            description="Найти сохранённый факт в памяти агента по ключевому слову или тегу.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключевое слово для поиска",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 5)",
                    },
                },
                "required": ["query"],
            },
            fn=search_facts,
            skills=["memory", "rag", "local", "storage", "knowledge"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "memory_fact",
                "speed": "fast",
                "tags": ["memory", "search", "fact", "read", "kb"],
            },
            example='memory.search_facts(query="client")',
        ),
        Tool(
            name="rag.query_kb",
            description="Семантический/текстовый RAG-поиск фрагментов в локальной базе знаний (.md, .txt).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Запрос для поиска по базе знаний",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Количество возвращаемых фрагментов (по умолчанию 3)",
                    },
                },
                "required": ["query"],
            },
            fn=query_kb,
            skills=["rag", "memory", "knowledge", "local", "documentation", "search"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "kb_snippet",
                "speed": "fast",
                "tags": ["rag", "kb", "knowledge", "search", "document"],
            },
            example='rag.query_kb(query="стандарты выкладки", limit=3)',
        ),
        Tool(
            name="memory.vector_store_hnsw",
            description="Проиндексировать документ в локальном векторном хранилище HNSW (косинусное сходство) для семантического поиска.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "ID документа (например, 'DOC-101')",
                    },
                    "text": {
                        "type": "string",
                        "description": "Текстовое содержимое документа",
                    },
                    "metadata_json": {
                        "type": "string",
                        "description": "JSON-объект метаданных",
                    },
                },
                "required": ["doc_id", "text"],
            },
            fn=vector_store_hnsw,
            skills=["memory", "hnsw", "vector", "rag", "embeddings", "local", "storage"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "hnsw_index",
                "speed": "fast",
                "tags": [
                    "vector",
                    "hnsw",
                    "embedding",
                    "semantic",
                    "index",
                    "векторный",
                    "индекс",
                    "хранилище",
                ],
            },
            example='memory.vector_store_hnsw(doc_id="DOC-1", text="Стандарт выкладки товаров на полке", metadata_json=\'{"cat": "retail"}\')',
        ),
        Tool(
            name="memory.vector_search_hnsw",
            description="Семантический поиск в локальном векторном индексе HNSW по косинусному сходству текста запроса.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос на естественном языке",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Число возвращаемых документов (по умолчанию 5)",
                    },
                },
                "required": ["query"],
            },
            fn=vector_search_hnsw,
            skills=["memory", "hnsw", "vector", "rag", "search", "local", "semantic"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "hnsw_search",
                "speed": "fast",
                "tags": [
                    "vector_search",
                    "hnsw",
                    "semantic_search",
                    "cosine",
                    "search",
                    "векторный_поиск",
                    "семантический_поиск",
                ],
            },
            example='memory.vector_search_hnsw(query="выкладка товаров на полке", top_k=3)',
        ),
    ]
