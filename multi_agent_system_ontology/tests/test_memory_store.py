"""Тесты maos.memory.store: PostgreSQL + pgvector на РЕАЛЬНОМ embedded
сервере (pgserver) — общий кластер на весь модуль, каждая функция
получает свою свежую базу (CREATE DATABASE) для изоляции.

Требует psycopg и pgserver. Если их нет или не удалось поднять сервер —
модуль пропускается с понятным сообщением (как agent_system/tests/
test_pg_ontology.py).
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен (pip install \"psycopg[binary]\")"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp_pgdata = tempfile.mkdtemp(prefix="maos_pgserver_")
        _srv = pgserver.get_server(_tmp_pgdata)
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
    if not HAVE_DEPS:
        print(f"test_memory_store: тесты пропущены — {SKIP_REASON}")
        return 0

    from maos.memory.store import Store, StoreError, _parse_vec

    section("Store: подключение и схема")
    try:
        Store("")
        check("пустой DSN кидает StoreError", False)
    except StoreError as exc:
        check("пустой DSN кидает StoreError", True)
        check("сообщение упоминает DB_DSN", "DB_DSN" in str(exc))

    st = Store(_fresh_dsn(), dim=16)
    check("схема создана без ошибок (pgvector доступен)", True)

    section("_parse_vec: строка pgvector -> list[float]")
    check("None -> None", _parse_vec(None) is None)
    check("список остаётся списком", _parse_vec([1.0, 2.0]) == [1.0, 2.0])
    check("строка '[1,2,3]' парсится", _parse_vec("[1,2,3]") == [1.0, 2.0, 3.0])
    check("пустой вектор '[]'", _parse_vec("[]") == [])

    section("Agent CRUD")
    aid = st.create_agent("coder", "Coder", description="Пишет код на Python",
                          keywords="код python", llm_ref="local::llama3",
                          system_prompt="Ты программист.")
    check("agent id > 0", aid > 0)
    a = st.get_agent("coder")
    check("get_agent находит агента", a is not None)
    check("поля агента верны", a["name"] == "Coder" and a["llm_ref"] == "local::llama3")
    check("enabled=True по умолчанию", a["enabled"] is True)

    st.create_agent("writer", "Writer", description="Пишет тексты")
    all_agents = st.list_agents()
    check("list_agents видит обоих", len(all_agents) == 2)
    check("list_agents отсортирован по slug",
         [x["slug"] for x in all_agents] == ["coder", "writer"])

    ok = st.update_agent("coder", description="Пишет код на Rust", enabled=False)
    check("update_agent вернул True", ok is True)
    updated = st.get_agent("coder")
    check("описание обновилось", updated["description"] == "Пишет код на Rust")
    check("enabled стал False", updated["enabled"] is False)
    check("list_agents(enabled_only=True) не видит отключённого",
         "coder" not in {x["slug"] for x in st.list_agents(enabled_only=True)})

    try:
        st.update_agent("coder", forbidden_field="x")
        check("нельзя менять произвольное поле агента", False)
    except StoreError:
        check("нельзя менять произвольное поле агента", True)

    check("get_agent для несуществующего -> None", st.get_agent("ghost") is None)
    check("delete_agent удаляет", st.delete_agent("writer") is True)
    check("delete_agent повторно -> False", st.delete_agent("writer") is False)

    section("agents_for_routing: эмбеддинг присутствует/None")
    st.update_agent("coder", enabled=True, description_embedding=[0.1] * 16)
    routing = st.agents_for_routing()
    check("ровно один включённый агент", len(routing) == 1)
    check("эмбеддинг сохранён и парсится обратно",
         routing[0]["embedding"] is not None and len(routing[0]["embedding"]) == 16)

    section("Conversation + Message")
    cid = st.create_conversation("тестовый диалог")
    check("conversation id > 0", cid > 0)
    conv = st.get_conversation(cid)
    check("get_conversation находит диалог", conv is not None and conv["title"] == "тестовый диалог")
    check("get_conversation для отсутствующего -> None", st.get_conversation(999999) is None)

    st.add_message(cid, "user", "Привет!")
    st.add_message(cid, "agent", "Здравствуйте!", agent_id=aid,
                   provider_model="local::llama3", tokens_used=12,
                   confidence_score=0.9)
    msgs = st.messages(cid)
    check("оба сообщения сохранены", len(msgs) == 2)
    check("порядок сохранён (user затем agent)",
         [m["role"] for m in msgs] == ["user", "agent"])
    check("provider_model записан у ответа агента",
         msgs[1]["provider_model"] == "local::llama3")
    check("message_count совпадает", st.message_count(cid) == 2)
    check("list_conversations видит диалог",
         cid in {c["id"] for c in st.list_conversations()})

    section("Memory quantum: mid-term векторная память")
    v_close = [1.0] + [0.0] * 15
    v_far = [0.0] * 15 + [1.0]
    q1 = st.add_memory_quantum(cid, "Как дела?", "Хорошо!", agent_id=aid,
                              provider_model="local::llama3", tokens_used=5,
                              confidence_score=1.0, embedding=v_close)
    st.add_memory_quantum(cid, "Расскажи анекдот", "Не сегодня",
                          embedding=v_far)
    check("квант создан", q1 > 0)
    check("quantum_count == 2", st.quantum_count() == 2)

    found = st.semantic_search_quanta(v_close, limit=5)
    check("семантический поиск находит близкий квант первым",
         found and found[0]["question"] == "Как дела?")
    check("сходство близкого кванта ~1.0", found[0]["score"] > 0.99)

    found_filtered = st.semantic_search_quanta(v_close, limit=5, min_score=0.5)
    check("min_score отсекает далёкий квант",
         all(f["score"] >= 0.5 for f in found_filtered))

    conv2 = st.create_conversation("другой диалог")
    st.add_memory_quantum(conv2, "Вопрос в другом диалоге", "Ответ", embedding=v_close)
    scoped = st.semantic_search_quanta(v_close, limit=10, conversation_id=cid)
    check("conversation_id фильтрует чужие кванты",
         all(f["conversation_id"] == cid for f in scoped))

    all_q = st.all_quanta()
    check("all_quanta видит все три кванта", len(all_q) == 3)
    check("delete_quantum уменьшает счётчик",
         st.delete_quantum(q1) and st.quantum_count() == 2)

    section("Онтология: сущности и связи")
    eid1 = st.upsert_entity("project", "MAOS", description="multi agent system",
                            embedding=[0.2] * 16)
    eid2 = st.upsert_entity("agent", "coder", description="кодер")
    check("upsert_entity возвращает id", eid1 > 0 and eid2 > 0)
    same_id = st.upsert_entity("project", "MAOS", props={"status": "active"})
    check("повторный upsert той же сущности не создаёт дубль", same_id == eid1)
    ent = st.get_entity("project", "MAOS")
    check("props смержены", ent["props"].get("status") == "active")

    linked = st.link(("agent", "coder"), "works_on", ("project", "MAOS"))
    check("связь создана", linked is True)
    linked_again = st.link(("agent", "coder"), "works_on", ("project", "MAOS"))
    check("повторная идентичная связь не создаётся (UNIQUE)", linked_again is False)

    neigh = st.neighbours("agent", "coder")
    check("у coder есть исходящая связь works_on",
         any(n["pred"] == "works_on" and n["dir"] == "out" for n in neigh))
    neigh2 = st.neighbours("project", "MAOS")
    check("у MAOS есть входящая связь works_on",
         any(n["pred"] == "works_on" and n["dir"] == "in" for n in neigh2))

    e, r = st.graph_stats()
    check("graph_stats считает сущности", e >= 2)
    check("graph_stats считает связи", r >= 1)

    graph = st.graph_data()
    check("graph_data содержит узлы", len(graph["nodes"]) >= 2)
    check("graph_data содержит рёбра", len(graph["edges"]) >= 1)

    sem = st.semantic_search_entities([0.2] * 16, kind="project")
    check("семантический поиск сущностей находит MAOS",
         sem and sem[0]["name"] == "MAOS")

    section("merge_entities: слияние дублей графа")
    st.upsert_entity("project", "MAOS-dup", description="дубликат",
                     embedding=[0.2] * 16)
    st.link(("agent", "coder"), "helps", ("project", "MAOS-dup"))
    merged = st.merge_entities("project", "MAOS", "MAOS-dup")
    check("merge_entities вернул True", merged is True)
    check("дублирующая сущность удалена", st.get_entity("project", "MAOS-dup") is None)
    neigh3 = st.neighbours("agent", "coder")
    check("связь helps перенесена на оставшуюся сущность",
         any(n["pred"] == "helps" and n["name"] == "MAOS" for n in neigh3))
    check("merge_entities с несуществующей сущностью -> False",
         st.merge_entities("project", "MAOS", "ghost") is False)

    section("Chain: детерминированная ручная цепочка")
    chain_id = st.start_chain("тестовая цель", ["coder", "writer"], conversation_id=cid)
    check("chain id > 0", chain_id > 0)
    steps = st.chain_steps(chain_id)
    check("оба шага заведены сразу как pending",
         len(steps) == 2 and all(s["status"] == "pending" for s in steps))
    check("порядок шагов сохранён", [s["agent_slug"] for s in steps] == ["coder", "writer"])

    st.set_chain_step(steps[0]["id"], "running", task="Сделай X")
    running = st.chain_steps(chain_id)[0]
    check("статус шага обновился на running", running["status"] == "running")
    check("task записан", running["task"] == "Сделай X")

    st.set_chain_step(steps[0]["id"], "done", answer="Готово X",
                      provider_model="local::llama3")
    done_step = st.chain_steps(chain_id)[0]
    check("статус done, answer записан", done_step["status"] == "done"
         and done_step["answer"] == "Готово X")

    st.set_chain_step(steps[1]["id"], "failed", error="что-то сломалось")
    failed_step = st.chain_steps(chain_id)[1]
    check("статус failed, error записан", failed_step["status"] == "failed"
         and "сломалось" in failed_step["error"])

    st.finish_chain(chain_id, "failed")
    chain = st.get_chain(chain_id)
    check("finish_chain обновил статус", chain["status"] == "failed")
    check("finish_chain выставил finished", chain["finished"] is not None)
    check("get_chain для несуществующего -> None", st.get_chain(999999) is None)
    check("list_chains видит цепочку", chain_id in {c["id"] for c in st.list_chains()})

    section("memory_stats: агрегированная статистика")
    stats = st.memory_stats()
    check("agents в статистике", stats["agents"] >= 1)
    check("conversations в статистике", stats["conversations"] >= 2)
    check("messages в статистике", stats["messages"] == 2)
    check("message_tokens учтён", stats["message_tokens"] == 12)
    check("memory_quanta в статистике", stats["memory_quanta"] == 2)
    check("onto_entities/relations присутствуют",
         stats["onto_entities"] >= 2 and stats["onto_relations"] >= 1)
    check("tokens_by_model содержит local::llama3",
         any(x["provider_model"] == "local::llama3" for x in stats["tokens_by_model"]))

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
