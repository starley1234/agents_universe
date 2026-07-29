"""Тесты dataforge.pipeline.ingest: Connector -> Bronze + lineage,
Bronze -> Silver/карантин через Quality Engine + lineage.

Реальный embedded PostgreSQL, реальный FileConnector на временных
файлах, реальный SqlConnector(sqlite) для инкрементальной выгрузки.
"""
from __future__ import annotations

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
        _tmp = tempfile.mkdtemp(prefix="forge_pipeline_pgserver_")
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
    if not HAVE_DEPS:
        print(f"test_pipeline: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.connectors.files import FileConnector
    from dataforge.connectors.sql import SqlConnector
    from dataforge.connectors.base import Cursor
    from dataforge.db.store import Store
    from dataforge.pipeline.ingest import (
        PipelineError,
        ingest_changes,
        ingest_full,
        promote_quality,
    )

    st = Store(_fresh_dsn())

    section("ingest_full: FileConnector -> Bronze + lineage")
    tmpdir = Path(tempfile.mkdtemp(prefix="forge_pipeline_files_"))
    csv_path = tmpdir / "customers.csv"
    csv_path.write_text("name,inn\nООО Ромашка,1234567890\nЗАО Лютик,0000000000\n",
                        encoding="utf-8")
    file_conn = FileConnector(str(csv_path))
    sid = st.upsert_source("crm_files", "file", {"path": str(csv_path)})
    result = ingest_full(st, sid, "crm_files", file_conn, file_conn.dataset_name)
    check("статус ok", result["status"] == "ok")
    check("записей выгружено 2", result["records_ingested"] == 2)
    did = result["dataset_id"]
    check("Bronze содержит 2 записи", st.count_bronze(did) == 2)
    check("row_count датасета обновился", st.get_dataset(did)["row_count"] == 2)
    lineage_in = st.lineage_edges_into(f"bronze:dataset:{did}")
    check("lineage-ребро source->bronze добавлено", len(lineage_in) == 1)
    check("lineage transform_ref = ingest_full", lineage_in[0]["transform_ref"] == "ingest_full")
    check("audit-запись создана", len(st.audit_trail_for("dataset", did)) == 1)

    section("ingest_full: повторный вызов ДОБАВЛЯЕТ записи (append-only Bronze)")
    result2 = ingest_full(st, sid, "crm_files", file_conn, file_conn.dataset_name)
    check("повторная выгрузка создаёт НОВЫЙ ingest_run",
         result2["run_id"] != result["run_id"])
    check("Bronze накопил записи от обоих запусков (2+2=4)", st.count_bronze(did) == 4)

    section("promote_quality: Bronze -> Silver + lineage")
    st.create_quality_rule(did, "not_null", field_name="name", severity="error")
    qresult = promote_quality(st, did)
    check("все 4 записи прошли (name всегда заполнено)", qresult["promoted_count"] == 4)
    silver_records = st.list_silver(did)
    check("Silver содержит 4 записи", len(silver_records) == 4)
    for rec in silver_records:
        edges = st.lineage_edges_into(f"silver:record:{rec['id']}")
        if not edges:
            check(f"lineage-ребро bronze->silver для записи {rec['id']}", False)
            break
    else:
        check("у каждой Silver-записи есть входящее lineage-ребро", True)

    section("promote_quality: повторный вызов НЕ дублирует старые lineage-рёбра")
    edges_before = len(st.lineage_edges_into(f"silver:record:{silver_records[0]['id']}"))
    promote_quality(st, did)  # без изменений в Bronze — новый прогон качества
    edges_after = len(st.lineage_edges_into(f"silver:record:{silver_records[0]['id']}"))
    check("число рёбер для СТАРОЙ Silver-записи не изменилось", edges_before == edges_after)

    section("ingest_full: ошибка коннектора оборачивается в PipelineError")

    class BrokenConnector:
        def read_full(self, dataset):
            raise RuntimeError("источник недоступен (симуляция)")

    sid_broken = st.upsert_source("broken_source", "file", {})
    try:
        ingest_full(st, sid_broken, "broken_source", BrokenConnector(), "whatever")
        check("ошибка коннектора оборачивается в PipelineError", False)
    except PipelineError as exc:
        check("ошибка коннектора оборачивается в PipelineError", True)
        check("исходная ошибка видна в сообщении", "источник недоступен" in str(exc))
    runs = st.list_ingest_runs(sid_broken)
    check("ingest_run зафиксирован со статусом error",
         any(r["status"] == "error" for r in runs))

    section("ingest_changes: SqlConnector(sqlite) инкрементальная выгрузка")
    sqlite_path = tempfile.mktemp(suffix=".db")
    conn_raw = sqlite3.connect(sqlite_path)
    conn_raw.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount REAL, updated_at INTEGER)")
    conn_raw.execute("INSERT INTO orders VALUES (1, 100.0, 10)")
    conn_raw.execute("INSERT INTO orders VALUES (2, 200.0, 20)")
    conn_raw.commit()
    conn_raw.close()
    sql_conn = SqlConnector(f"sqlite:///{sqlite_path}", "orders", cursor_field="updated_at")
    sid_sql = st.upsert_source("orders_db", "sql", {"dsn_env": "X", "table": "orders"})

    r1 = ingest_changes(st, sid_sql, "orders_db", sql_conn, "orders", cursor_value="")
    check("первый инкремент забрал обе записи", r1["records_ingested"] == 2)
    check("курсор продвинулся до последнего updated_at", r1["next_cursor"] == "20")

    conn_raw2 = sqlite3.connect(sqlite_path)
    conn_raw2.execute("INSERT INTO orders VALUES (3, 300.0, 30)")
    conn_raw2.commit()
    conn_raw2.close()
    r2 = ingest_changes(st, sid_sql, "orders_db", sql_conn, "orders",
                        cursor_value=r1["next_cursor"])
    check("второй инкремент забрал только новую запись", r2["records_ingested"] == 1)
    did_sql = r2["dataset_id"]
    check("Bronze датасета orders содержит все 3 записи суммарно (append-only)",
         st.count_bronze(did_sql) == 3)

    del Cursor  # импортирован для симметрии с другими тестами, не используется напрямую

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
