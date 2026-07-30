"""Тесты maos.orchestrator.context: контроль контекста, суммаризация,
mid-term semantic retrieval (реальный embedded Postgres+pgvector).
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.config import Config                                    # noqa: E402
from maos.llm.embeddings import HashEmbedder                       # noqa: E402
from maos.orchestrator.context import (build_messages, estimate_tokens,
                                       estimate_messages_tokens,
                                       needs_summarization,
                                       retrieve_mid_term,
                                       summarize_history)            # noqa: E402

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="maos_ctx_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def main() -> int:
    section("estimate_tokens: грубая, но монотонная оценка")
    check("пустая строка -> 0", estimate_tokens("") == 0)
    check("непустая строка -> >0", estimate_tokens("a") > 0)
    check("длинный текст даёт больше токенов, чем короткий",
         estimate_tokens("a" * 400) > estimate_tokens("a" * 40))

    history = [{"role": "user", "content": "a" * 100},
              {"role": "assistant", "content": "b" * 100}]
    check("estimate_messages_tokens суммирует все сообщения",
         estimate_messages_tokens(history) ==
         estimate_tokens("a" * 100) + estimate_tokens("b" * 100))

    section("needs_summarization: маленькое окно модели -> всегда True")
    check("окно 2000 < small_context_window 4096 -> нужна суммаризация",
         needs_summarization([], context_window=2000, small_context_window=4096))
    check("пустая история при большом окне -> суммаризация не нужна",
         not needs_summarization([], context_window=100_000,
                                 small_context_window=4096))
    long_history = [{"role": "user", "content": "слово " * 10000}]
    check("очень длинная история при обычном окне -> нужна суммаризация",
         needs_summarization(long_history, context_window=8192,
                             small_context_window=4096))

    section("summarize_history: сохраняет последние N, сжимает остальное")
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"}
           for i in range(10)]
    summarized = summarize_history(hist, keep_last=3)
    check("последние 3 сообщения сохранены дословно",
         [m["content"] for m in summarized[-3:]] == ["msg7", "msg8", "msg9"])
    check("добавлена сводная заметка в начале",
         summarized[0]["role"] == "system" and "сводка" in summarized[0]["content"])
    check("история короче keep_last не трогается",
         summarize_history(hist[:2], keep_last=5) == hist[:2])

    captured = []

    def fake_summarizer(text: str) -> str:
        captured.append(text)
        return "КОРОТКАЯ СВОДКА"

    summarized2 = summarize_history(hist, keep_last=3, summarizer=fake_summarizer)
    check("кастомный summarizer реально вызван", len(captured) == 1)
    check("результат summarizer попал в заметку",
         "КОРОТКАЯ СВОДКА" in summarized2[0]["content"])
    check("summarizer получил ИМЕННО старую часть, не последние 3",
         "msg9" not in captured[0] and "msg0" in captured[0])

    if not HAVE_DEPS:
        print(f"\ntest_context: часть про mid-term пропущена — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    from maos.memory.store import Store

    section("retrieve_mid_term + build_messages: реальный Postgres+pgvector")
    st = Store(_fresh_dsn(), dim=64)
    emb = HashEmbedder(dim=64)
    cid = st.create_conversation("ctx test")
    q1, a1 = "Как настроить Postgres?", "Установите pgvector и создайте расширение"
    q2, a2 = "Расскажи рецепт борща", "Возьмите свёклу, капусту и мясо"
    st.add_memory_quantum(cid, q1, a1, provider_model="local::llama3",
                          embedding=emb.embed_one(f"{q1} {a1}"))
    st.add_memory_quantum(cid, q2, a2, provider_model="local::llama3",
                          embedding=emb.embed_one(f"{q2} {a2}"))

    cfg = Config(mid_term_top_k=5, mid_term_min_score=0.05)
    found = retrieve_mid_term(st, emb, "Как поставить расширение в Postgres?", cfg,
                              conversation_id=cid)
    check("mid-term поиск нашёл релевантный квант про Postgres",
         found and found[0]["question"] == q1)

    messages = build_messages(
        "Ты ассистент.", [], "Как поставить расширение в Postgres?", cfg,
        context_window=8192, store=st, embedder=emb, conversation_id=cid)
    check("system prompt первым сообщением", messages[0]["role"] == "system"
         and messages[0]["content"] == "Ты ассистент.")
    check("mid-term заметка присутствует и содержит найденный квант",
         any("pgvector" in m["content"] for m in messages if m["role"] == "system"))
    check("пользовательское сообщение последним",
         messages[-1] == {"role": "user",
                          "content": "Как поставить расширение в Postgres?"})

    empty_cfg = Config(mid_term_top_k=5, mid_term_min_score=0.999)
    messages2 = build_messages(
        "Ты ассистент.", [], "Что-то совсем не связанное", empty_cfg,
        context_window=8192, store=st, embedder=emb, conversation_id=cid)
    check("при высоком пороге mid-term заметки нет",
         len(messages2) == 2)  # system + user, без вставки

    section("retrieve_long_term_graph + format_long_term_note: Graph-RAG (long-term memory)")
    from maos.orchestrator.context import retrieve_long_term_graph, format_long_term_note
    st.upsert_entity("person", "starley", description="Создатель MAOS",
                     embedding=emb.embed_one("person:starley Создатель MAOS"))
    st.upsert_entity("project", "MAOS", description="мультиагентная онтология",
                     embedding=emb.embed_one("project:MAOS мультиагентная онтология"))
    st.link(("person", "starley"), "created", ("project", "MAOS"))

    lt_hits = retrieve_long_term_graph(st, emb, "Кто такой starley и что он сделал?", cfg)
    check("Graph-RAG нашёл сущность из long-term онтологии",
          any(h["name"] == "starley" for h in lt_hits))
    check("Graph-RAG подтянул связи сущности",
          any(len(h.get("neighbours", [])) > 0 for h in lt_hits if h["name"] == "starley"))

    lt_note = format_long_term_note(lt_hits)
    check("format_long_term_note сформировал системное сообщение",
          lt_note is not None and lt_note["role"] == "system")
    check("заметка содержит текст про сущность и её связь created",
          lt_note is not None and "starley" in lt_note["content"] and "created" in lt_note["content"])

    st.upsert_entity("concept", "pgvector", description="векторное расширение",
                     embedding=emb.embed_one("concept:pgvector векторное расширение"))
    st.link(("project", "MAOS"), "uses", ("concept", "pgvector"))

    cfg.long_term_max_hops = 2
    lt_hits_2hop = retrieve_long_term_graph(st, emb, "Кто такой starley?", cfg)
    check("Multi-Hop Graph-RAG нашёл сущность 1-го шага (project:MAOS)",
          any(h["name"] == "MAOS" and h.get("hop") == 1 for h in lt_hits_2hop))
    lt_note_2hop = format_long_term_note(lt_hits_2hop)
    check("format_long_term_note указывает метку (hop 1) для соседей",
          lt_note_2hop is not None and "(hop 1)" in lt_note_2hop["content"])

    lt_hits_path = retrieve_long_term_graph(st, emb, "Как связаны starley и pgvector?", cfg)
    lt_note_path = format_long_term_note(lt_hits_path)
    check("format_long_term_note выводит связующий путь между starley и pgvector",
          lt_note_path is not None and "★ Найден связующий путь:" in lt_note_path["content"] and "starley" in lt_note_path["content"] and "pgvector" in lt_note_path["content"])

    messages_with_graph = build_messages(
        "Ты ассистент.", [], "Расскажи про starley", cfg,
        context_window=8192, store=st, embedder=emb, conversation_id=cid)
    check("build_messages включает long-term онтологическую заметку",
          any("long-term memory" in m["content"] for m in messages_with_graph if m["role"] == "system"))

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
