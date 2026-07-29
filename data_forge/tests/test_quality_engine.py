"""Тесты dataforge.quality.engine: профилирование, декларативные правила
качества, продвижение в Silver/карантин.

Реальный embedded PostgreSQL (pgserver), одна общая база на модуль,
изоляция через отдельную схему не нужна — каждая секция теста строит
свой независимый датасет.
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
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="forge_quality_pgserver_")
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
        print(f"test_quality_engine: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.db.store import Store
    from dataforge.quality.engine import (
        QualityError,
        profile_dataset,
        run_quality_checks,
    )

    st = Store(_fresh_dsn())
    sid = st.upsert_source("crm", "file", {})

    section("profile_dataset: базовая статистика по полям")
    did = st.upsert_dataset(sid, "customers", layer="bronze")
    st.insert_bronze_batch(did, [
        {"name": "ООО Ромашка", "email": "a@x.ru", "age": "30"},
        {"name": "ЗАО Лютик", "email": "", "age": "40"},
        {"name": "", "email": "c@x.ru", "age": "не число"},
    ])
    profiles = profile_dataset(st, did)
    by_field = {p["field_name"]: p for p in profiles}
    check("все 3 поля профилированы", set(by_field) == {"name", "email", "age"})
    check("null_count для name учитывает пустую строку", by_field["name"]["null_count"] == 1)
    check("null_count для email учитывает пустую строку", by_field["email"]["null_count"] == 1)
    check("distinct_count для name = 2 (одна пустая не считается)",
         by_field["name"]["distinct_count"] == 2)
    check("сохранилось в БД (list_profiles видит те же данные)",
         len(st.list_profiles(did)) == 3)

    section("run_quality_checks: без правил всё продвигается как есть")
    did2 = st.upsert_dataset(sid, "no_rules_dataset", layer="bronze")
    st.insert_bronze_batch(did2, [{"x": 1}, {"x": None}])
    result0 = run_quality_checks(st, did2)
    check("rules_checked == 0", result0["rules_checked"] == 0)
    check("promoted_count == records_checked (ничего не блокируется)",
         result0["promoted_count"] == 2 and result0["quarantined_count"] == 0)
    check("обе записи в Silver", len(st.list_silver(did2)) == 2)

    section("run_quality_checks: not_null (error) отправляет в карантин")
    did3 = st.upsert_dataset(sid, "not_null_dataset", layer="bronze")
    st.insert_bronze_batch(did3, [
        {"name": "Годная запись"},
        {"name": ""},
        {"name": None},
    ])
    st.create_quality_rule(did3, "not_null", field_name="name", severity="error")
    result1 = run_quality_checks(st, did3)
    check("1 запись прошла, 2 в карантине", result1["promoted_count"] == 1
         and result1["quarantined_count"] == 2)
    check("violations_count == 2", result1["violations_count"] == 2)
    check("Silver содержит только годную запись", len(st.list_silver(did3)) == 1)
    check("карантин содержит 2 записи", len(st.list_quarantine(did3)) == 2)
    quarantine_reasons = st.list_quarantine(did3)[0]["reasons"]
    check("причина карантина содержит имя правила", any("not_null" in r for r in quarantine_reasons))

    section("run_quality_checks: warning не блокирует продвижение")
    did4 = st.upsert_dataset(sid, "warning_dataset", layer="bronze")
    st.insert_bronze_batch(did4, [{"inn": ""}, {"inn": "123"}])
    st.create_quality_rule(did4, "not_null", field_name="inn", severity="warning")
    result2 = run_quality_checks(st, did4)
    check("обе записи продвинуты, несмотря на нарушение", result2["promoted_count"] == 2)
    check("нарушение зафиксировано в violations_count", result2["violations_count"] == 1)
    check("карантин пуст", result2["quarantined_count"] == 0)

    section("run_quality_checks: unique — второе одинаковое значение нарушает")
    did5 = st.upsert_dataset(sid, "unique_dataset", layer="bronze")
    st.insert_bronze_batch(did5, [
        {"sku": "A1"}, {"sku": "A1"}, {"sku": "A2"},
    ])
    st.create_quality_rule(did5, "unique", field_name="sku", severity="error")
    result3 = run_quality_checks(st, did5)
    check("одна из двух дублирующихся записей ушла в карантин",
         result3["quarantined_count"] == 1 and result3["promoted_count"] == 2)

    section("run_quality_checks: regex")
    did6 = st.upsert_dataset(sid, "regex_dataset", layer="bronze")
    st.insert_bronze_batch(did6, [{"inn": "1234567890"}, {"inn": "abc"}])
    st.create_quality_rule(did6, "regex", field_name="inn",
                           params={"pattern": r"\d{10,12}"}, severity="error")
    result4 = run_quality_checks(st, did6)
    check("валидный ИНН прошёл, невалидный в карантине",
         result4["promoted_count"] == 1 and result4["quarantined_count"] == 1)

    section("run_quality_checks: range")
    did7 = st.upsert_dataset(sid, "range_dataset", layer="bronze")
    st.insert_bronze_batch(did7, [{"qty": "5"}, {"qty": "-1"}, {"qty": "abc"}])
    st.create_quality_rule(did7, "range", field_name="qty",
                           params={"min": 0, "max": 1000}, severity="error")
    result5 = run_quality_checks(st, did7)
    check("только положительное число прошло",
         result5["promoted_count"] == 1 and result5["quarantined_count"] == 2)

    section("run_quality_checks: allowed_values")
    did8 = st.upsert_dataset(sid, "allowed_dataset", layer="bronze")
    st.insert_bronze_batch(did8, [{"status": "active"}, {"status": "unknown_status"}])
    st.create_quality_rule(did8, "allowed_values", field_name="status",
                           params={"values": ["active", "inactive"]}, severity="error")
    result6 = run_quality_checks(st, did8)
    check("допустимое значение прошло, недопустимое в карантине",
         result6["promoted_count"] == 1 and result6["quarantined_count"] == 1)

    section("run_quality_checks: неизвестный тип правила -> QualityError")
    did9 = st.upsert_dataset(sid, "bad_rule_dataset", layer="bronze")
    st.insert_bronze_batch(did9, [{"x": 1}])
    st.create_quality_rule(did9, "not_a_real_rule_type", field_name="x")
    try:
        run_quality_checks(st, did9)
        check("QualityError выброшена для неизвестного типа правила", False)
    except QualityError as exc:
        check("QualityError выброшена для неизвестного типа правила", True)
        check("сообщение упоминает тип правила", "not_a_real_rule_type" in str(exc))

    section("run_quality_checks: отключённое правило не проверяется")
    did10 = st.upsert_dataset(sid, "inactive_rule_dataset", layer="bronze")
    st.insert_bronze_batch(did10, [{"name": ""}])
    rid10 = st.create_quality_rule(did10, "not_null", field_name="name", severity="error")
    st.set_rule_active(rid10, False)
    result7 = run_quality_checks(st, did10)
    check("отключённое правило не применяется — запись продвинута",
         result7["promoted_count"] == 1 and result7["rules_checked"] == 0)

    section("run_quality_checks: повторный запуск добавляет новую историю (append-only)")
    result_again = run_quality_checks(st, did3)
    check("повторный прогон создаёт новый run_id",
         result_again["run_id"] != result1["run_id"])
    check("Silver накопил записи от обоих прогонов (append-only)",
         len(st.list_silver(did3)) == 2)  # было 1, добавился ещё 1

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
