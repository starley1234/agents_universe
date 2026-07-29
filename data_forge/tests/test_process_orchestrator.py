"""Тесты dataforge.pipeline.orchestrator: сквозной процесс с обратной
записью (ТЗ K3) — карантин -> задача -> корректировка (guardrail:
повторная проверка правил качества) -> write-back в источник ->
неизменяемый audit trail, rollback до/после write-back.

Реальный embedded PostgreSQL, реальный SQLite как источник для
write-back (через SqlConnector — тот же реальный DB-API код, что
используется во всех остальных тестах коннекторов этого проекта).
"""
from __future__ import annotations

import os
import re
import sqlite3
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
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="forge_orch_pgserver_")
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


def _make_sqlite_source(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE customers(id TEXT PRIMARY KEY, name TEXT, inn TEXT)")
    conn.execute("INSERT INTO customers VALUES ('c1', 'ООО Ромашка', '')")
    conn.execute("INSERT INTO customers VALUES ('c2', 'ЗАО Лютик', '')")
    conn.commit()
    conn.close()


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_process_orchestrator: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.connectors.sql import SqlConnector
    from dataforge.db.store import Store
    from dataforge.pipeline import orchestrator as orch
    from dataforge.quality.engine import run_quality_checks

    st = Store(_fresh_dsn())

    sqlite_path = tempfile.mktemp(suffix=".db")
    _make_sqlite_source(sqlite_path)
    sid = st.upsert_source("erp_customers", "sql", {"dsn_env": "X", "table": "customers"})
    did = st.upsert_dataset(sid, "customers", layer="bronze")
    bid1 = st.insert_bronze(did, {"id": "c1", "name": "ООО Ромашка", "inn": ""})
    bid2 = st.insert_bronze(did, {"id": "c2", "name": "ЗАО Лютик", "inn": ""})
    st.create_quality_rule(did, "not_null", field_name="inn", severity="error")
    run_quality_checks(st, did)
    quarantine = st.list_quarantine(did)
    qid1 = next(q["id"] for q in quarantine if q["bronze_record_id"] == bid1)
    qid2 = next(q["id"] for q in quarantine if q["bronze_record_id"] == bid2)

    section("start_quarantine_correction: запуск и идемпотентность")
    process = orch.start_quarantine_correction(st, qid1, assignee="human:ivanov",
                                               actor="human:steward")
    check("процесс создан со статусом awaiting_task", process["status"] == "awaiting_task")
    check("контекст содержит dataset_id и bronze_record_id",
         process["context"]["dataset_id"] == did
         and process["context"]["bronze_record_id"] == bid1)
    tasks = st.list_tasks(process_instance_id=process["id"])
    check("создана ровно одна задача", len(tasks) == 1)
    check("задача назначена указанному исполнителю", tasks[0]["assignee"] == "human:ivanov")

    process_again = orch.start_quarantine_correction(st, qid1, assignee="human:other")
    check("повторный запуск на том же карантине возвращает ТОТ ЖЕ процесс "
         "(идемпотентность на уровне предмета)", process_again["id"] == process["id"])
    check("список задач не увеличился от повторного запуска",
         len(st.list_tasks(process_instance_id=process["id"])) == 1)

    section("start_quarantine_correction: ошибки")
    try:
        orch.start_quarantine_correction(st, 999999)
        check("несуществующая запись карантина -> ProcessError", False)
    except orch.ProcessError:
        check("несуществующая запись карантина -> ProcessError", True)

    resolved_qid = st.insert_quarantine(did, bid1, ["уже решено вручную"])
    st.resolve_quarantine(resolved_qid, "ручное решение")
    try:
        orch.start_quarantine_correction(st, resolved_qid)
        check("уже разрешённый карантин -> ProcessError", False)
    except orch.ProcessError:
        check("уже разрешённый карантин -> ProcessError", True)

    section("submit_correction: guardrail — невалидное исправление отклоняется")
    pid = process["id"]
    bad = orch.submit_correction(st, pid, {"id": "c1", "name": "ООО Ромашка", "inn": ""},
                                 actor="human:ivanov")
    check("невалидное исправление отклонено", bad["accepted"] is False)
    check("возвращён список нарушений", len(bad["errors"]) == 1 and "inn" in bad["errors"][0])
    check("процесс остался в awaiting_task (не продвинулся молча)",
         bad["process"]["status"] == "awaiting_task")
    check("Bronze НЕ изменился после отклонённого исправления",
         st.get_bronze(bid1)["payload"]["inn"] == "")
    check("задача НЕ завершена после отклонённого исправления",
         st.list_tasks(process_instance_id=pid, status="open")[0]["status"] == "open")

    section("submit_correction: валидное исправление")
    good = orch.submit_correction(st, pid, {"id": "c1", "name": "ООО Ромашка",
                                            "inn": "1234567890"}, actor="human:ivanov")
    check("валидное исправление принято", good["accepted"] is True)
    check("процесс перешёл в corrected", good["process"]["status"] == "corrected")
    check("Bronze обновился", st.get_bronze(bid1)["payload"]["inn"] == "1234567890")
    check("карантин разрешён", st.get_quarantine(qid1)["resolved"] is True)
    check("задача завершена", st.list_tasks(process_instance_id=pid)[0]["status"] == "done")

    section("submit_correction: неверный статус процесса")
    try:
        orch.submit_correction(st, pid, {"id": "c1", "inn": "1"}, actor="human:x")
        check("повторная подача исправления НЕ в awaiting_task -> ProcessError", False)
    except orch.ProcessError:
        check("повторная подача исправления НЕ в awaiting_task -> ProcessError", True)
    try:
        orch.submit_correction(st, 999999, {}, actor="human:x")
        check("несуществующий процесс -> ProcessError", False)
    except orch.ProcessError:
        check("несуществующий процесс -> ProcessError", True)

    section("write_back_correction: запись в реальный источник (SQLite)")
    connector = SqlConnector(f"sqlite:///{sqlite_path}", "customers", id_field="id")
    wb = orch.write_back_correction(st, pid, connector, sid, "customers", "c1",
                                    actor="human:ivanov")
    check("write-back успешен", wb["ok"] is True)
    check("процесс завершён (completed)", wb["process"]["status"] == "completed")
    conn = sqlite3.connect(sqlite_path)
    row = conn.execute("SELECT inn FROM customers WHERE id='c1'").fetchone()
    conn.close()
    check("значение РЕАЛЬНО записалось в источник", row[0] == "1234567890")

    section("write_back_correction: идемпотентность повторного вызова")
    wb2 = orch.write_back_correction(st, pid, connector, sid, "customers", "c1",
                                     actor="human:ivanov")
    check("повторный write-back определён как дубликат", wb2["skipped_duplicate"] is True)
    check("повторный write-back НЕ отправляет запись в источник дважды",
         wb2["ok"] is True)

    section("write_back_correction: до статуса corrected -> ProcessError")
    process2 = orch.start_quarantine_correction(st, qid2, actor="system")
    try:
        orch.write_back_correction(st, process2["id"], connector, sid, "customers", "c2",
                                   actor="human:x")
        check("write-back до corrected -> ProcessError", False)
    except orch.ProcessError:
        check("write-back до corrected -> ProcessError", True)

    section("rollback_process: до write-back — отменяет процесс и открытые задачи")
    bid3 = st.insert_bronze(did, {"id": "c3", "name": "ИП Сидоров", "inn": ""})
    run_quality_checks(st, did)
    qid3 = next(q["id"] for q in st.list_quarantine(did) if q["bronze_record_id"] == bid3)
    process3 = orch.start_quarantine_correction(st, qid3, actor="system")
    check("у нового процесса задача открыта",
         st.list_tasks(process_instance_id=process3["id"])[0]["status"] == "open")
    rolled = orch.rollback_process(st, process3["id"], actor="human:x", reason="передумали")
    check("процесс отменён", rolled["status"] == "cancelled")
    check("открытая задача отменена (не осталась open)",
         all(t["status"] == "cancelled" for t in st.list_tasks(process_instance_id=process3["id"])))

    orch.submit_correction(st, process2["id"], {"id": "c2", "name": "ЗАО Лютик",
                                                "inn": "0000000000"}, actor="human:x")
    rolled2 = orch.rollback_process(st, process2["id"], actor="human:x", reason="уже исправлено вручную")
    check("процесс, который уже был скорректирован (задача завершена ДО отката), тоже отменяется",
         rolled2["status"] == "cancelled")
    check("завершённая ДО отката задача остаётся 'done' (rollback не трогает уже done)",
         st.list_tasks(process_instance_id=process2["id"])[0]["status"] == "done")

    section("rollback_process: после успешного write-back — ProcessError")
    try:
        orch.rollback_process(st, pid, actor="human:x", reason="передумали")
        check("откат после успешного write-back -> ProcessError", False)
    except orch.ProcessError as exc:
        check("откат после успешного write-back -> ProcessError", True)
        check("сообщение объясняет невозможность отката", "невозможен" in str(exc))

    try:
        orch.rollback_process(st, process2["id"], actor="human:x")
        check("повторный откат уже отменённого процесса -> ProcessError", False)
    except orch.ProcessError:
        check("повторный откат уже отменённого процесса -> ProcessError", True)

    section("Audit trail: полная история процесса")
    trail = st.audit_trail_for("process_instance", pid)
    actions = [a["action"] for a in trail]
    check("start_process зафиксирован", "start_process" in actions)
    check("correction_rejected зафиксирован (неудачная попытка тоже)",
         "correction_rejected" in actions)
    check("correction_accepted зафиксирован", "correction_accepted" in actions)
    check("write_back_ok зафиксирован", "write_back_ok" in actions)

    st.close()
    os.unlink(sqlite_path)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
