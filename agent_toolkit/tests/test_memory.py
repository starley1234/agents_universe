"""Тесты долговременной памяти агента, векторного индекса HNSW (memory.*) и RAG-поиска (rag.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.memory import build_memory_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты памяти, векторного HNSW индекса и RAG (memory.*, rag.*)")
        tools = {t.name: t for t in build_memory_tools(ws)}
        check("зарегистрировано 5 инструментов памяти", len(tools) == 5)

        res_save = tools["memory.save_fact"].execute(
            key="api_token", value="secret-token-value", tags_json='["auth", "api"]'
        )
        check("memory.save_fact сохраняет факт", "сохранён в памяти" in res_save)

        res_search = tools["memory.search_facts"].execute(query="api")
        check("memory.search_facts находит сохранённый факт", "api_token" in res_search and "secret-token-value" in res_search)

        # Векторный индекс HNSW
        res_hnsw_add = tools["memory.vector_store_hnsw"].execute(
            doc_id="DOC-1", text="Авиационный стандарт АП-25 по безопасности полётов", metadata_json='{"cat": "aviation"}'
        )
        check("memory.vector_store_hnsw индексирует документ", "DOC-1" in res_hnsw_add and "проиндексирован" in res_hnsw_add)

        res_hnsw_search = tools["memory.vector_search_hnsw"].execute(query="безопасность полётов", top_k=3)
        check("memory.vector_search_hnsw находит документ по сходству", "DOC-1" in res_hnsw_search and "сходство:" in res_hnsw_search)

        # Создадим файл документации для RAG-поиска
        p_doc = ws.resolve("docs/rules.md")
        p_doc.parent.mkdir(parents=True, exist_ok=True)
        p_doc.write_text(
            "# Авиационные правила\nПункт 3.4 требует обязательного подписания протокола соответствия.\n",
            encoding="utf-8",
        )

        res_rag = tools["rag.query_kb"].execute(query="протокол соответствия", limit=2)
        check("rag.query_kb находит фрагмент в документации", "Пункт 3.4" in res_rag)

    return summary("Тесты памяти и RAG")


def test_memory_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
