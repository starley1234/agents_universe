"""Тесты maos.maintenance.service: дистилляция, дедупликация квантов
памяти, синтез/очистка графа онтологии. Реальный embedded Postgres+pgvector.
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.config import Config                                     # noqa: E402
from maos.llm.embeddings import HashEmbedder                        # noqa: E402
from maos.maintenance.distill import distill_conversation           # noqa: E402
from maos.maintenance.service import MaintenanceService             # noqa: E402

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
        _tmp = tempfile.mkdtemp(prefix="maos_maint_pgserver_")
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
    section("distill_conversation: эвристика без LLM")
    msgs = [
        {"role": "user", "content": "Первый вопрос"},
        {"role": "agent", "content": "Первый ответ"},
        {"role": "user", "content": "Второй вопрос"},
        {"role": "agent", "content": "Финальный ответ"},
    ]
    q, a = distill_conversation(msgs)
    check("вопрос — это первое сообщение пользователя", q == "Первый вопрос")
    check("ответ — это последний ответ агента", a == "Финальный ответ")
    q2, a2 = distill_conversation([])
    check("пустой диалог даёт пустые строки", q2 == "" and a2 == "")
    q3, a3 = distill_conversation([{"role": "user", "content": "только вопрос"}])
    check("диалог без ответа агента даёт пустые строки", q3 == "" and a3 == "")

    captured = []

    def fake_summarizer(text: str) -> str:
        captured.append(text)
        return "СЖАТЫЙ ИТОГ"

    long_msgs = msgs * 2  # 8 сообщений > порог "> 4" внутри distill_conversation
    q4, a4 = distill_conversation(long_msgs, summarizer=fake_summarizer)
    check("summarizer вызван для длинного диалога", len(captured) == 1)
    check("результат summarizer использован как ответ", a4 == "СЖАТЫЙ ИТОГ")

    if not HAVE_DEPS:
        print(f"\ntest_maintenance: тесты MaintenanceService пропущены — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    from maos.memory.store import Store

    st = Store(_fresh_dsn(), dim=32)
    emb = HashEmbedder(dim=32)
    cfg = Config(maintenance_distill_after_messages=4,
                maintenance_dedup_similarity=0.999)
    svc = MaintenanceService(cfg, st, emb)

    section("distill(): длинный диалог -> квант памяти")
    cid = st.create_conversation("длинный диалог")
    for i in range(3):
        st.add_message(cid, "user", f"Вопрос {i}")
        st.add_message(cid, "agent", f"Ответ {i}")
    check("6 сообщений в диалоге", st.message_count(cid) == 6)
    check("квантов памяти ещё нет", st.quantum_count() == 0)

    distilled = svc.distill()
    check("distill() создал ровно один квант", distilled == 1)
    check("квант появился в базе", st.quantum_count() == 1)

    short_cid = st.create_conversation("короткий диалог")
    st.add_message(short_cid, "user", "Быстрый вопрос")
    st.add_message(short_cid, "agent", "Быстрый ответ")
    distilled2 = svc.distill()
    check("короткий диалог не дистиллируется (ниже порога)", distilled2 == 0)

    distilled3 = svc.distill()
    check("повторный вызов distill() идемпотентен (не плодит дубли)",
         distilled3 == 0 and st.quantum_count() == 1)

    section("dedup(): удаляет квант с высоким сходством, оставляя новый")
    v = [1.0] + [0.0] * 31
    q_old = st.add_memory_quantum(cid, "вопрос А", "ответ А", embedding=v)
    q_new = st.add_memory_quantum(cid, "вопрос А (перефраз)", "ответ А", embedding=v)
    before = st.quantum_count()
    removed = svc.dedup()
    check("dedup() нашёл и удалил дубль", removed >= 1)
    check("количество квантов уменьшилось", st.quantum_count() < before)
    check("удалён именно старый (меньший id)", st.all_quanta() and
         all(q["id"] != q_old for q in st.all_quanta()))
    check("новый квант остался", any(q["id"] == q_new for q in st.all_quanta()))

    section("dedup(): непохожие кванты не трогает")
    v_far = [0.0] * 31 + [1.0]
    before2 = st.quantum_count()
    st.add_memory_quantum(cid, "совсем другой вопрос", "другой ответ", embedding=v_far)
    removed2 = svc.dedup()
    check("непохожий квант не удалён", removed2 == 0)
    check("общее число квантов выросло на один", st.quantum_count() == before2 + 1)

    section("synthesize_graph(): слияние похожих сущностей одного kind")
    st.upsert_entity("person", "Иванов", description="инженер",
                     embedding=[1.0] + [0.0] * 31)
    st.upsert_entity("person", "иванов", description="инженер (дубль с опечаткой)",
                     embedding=[0.999] + [0.01] * 31)
    st.upsert_entity("person", "Петров", description="менеджер",
                     embedding=[0.0] * 31 + [1.0])
    e_before, _ = st.graph_stats()
    merged = svc.synthesize_graph(similarity_threshold=0.9)
    e_after, _ = st.graph_stats()
    check("найдена и слита минимум одна пара дублей", merged >= 1)
    check("количество сущностей уменьшилось", e_after < e_before)
    check("непохожая сущность Петров осталась",
         st.get_entity("person", "Петров") is not None)

    section("run_once(): полный цикл без падения на любой из фаз")
    report = svc.run_once()
    check("run_once не накопил ошибок", report.errors == [])
    check("run_once вернул структуру с числами",
         isinstance(report.distilled, int) and isinstance(report.deduped, int))

    section("run_once(): фаза-исключение не роняет остальные фазы")
    class BrokenEmbedder(HashEmbedder):
        def embed_one(self, text):
            raise RuntimeError("нарочно сломанный эмбеддер")

    svc_broken = MaintenanceService(cfg, st, BrokenEmbedder(dim=32))
    st.create_conversation("ещё диалог для дистилляции")
    report2 = svc_broken.run_once()
    check("ошибка эмбеддера при dedup/synthesize не убивает run_once",
         isinstance(report2, type(report)))

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
