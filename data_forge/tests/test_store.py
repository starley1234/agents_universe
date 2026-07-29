"""Тесты dataforge.db.store: PostgreSQL-хранилище DataForge — источники,
датасеты, слои Bronze/Silver/карантин, правила качества и их прогоны,
golden records и связи, кандидаты на матчинг, survivorship, lineage,
неизменяемый аудит.

Проверяется на РЕАЛЬНОМ embedded PostgreSQL (pgserver) — общий кластер
на весь модуль, каждая тестовая функция получает СВОЮ базу (CREATE
DATABASE) для изоляции.

Требует psycopg и pgserver. Если их нет или не удалось поднять сервер —
модуль пропускается с понятным сообщением.
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
        _tmp = tempfile.mkdtemp(prefix="forge_store_pgserver_")
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
        print(f"test_store: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.db.store import Store, StoreError

    section("Store: подключение и схема")
    try:
        Store("")
        check("пустой DSN кидает StoreError", False)
    except StoreError as exc:
        check("пустой DSN кидает StoreError", True)
        check("сообщение упоминает DB_DSN", "DB_DSN" in str(exc))

    st = Store(_fresh_dsn())
    check("схема создана без ошибок", True)

    section("Source: CRUD, upsert по имени")
    sid = st.upsert_source("crm_export", "file", {"path": "crm.csv"})
    check("id > 0", sid > 0)
    src = st.get_source(sid)
    check("config сохранён как dict", src["config"] == {"path": "crm.csv"})
    same_sid = st.upsert_source("crm_export", "sql", {"table": "customers"})
    check("повторный upsert по тому же имени не создаёт дубль", same_sid == sid)
    check("kind и config обновились", st.get_source(sid)["kind"] == "sql")
    check("get_source_by_name находит", st.get_source_by_name("crm_export") is not None)
    check("get_source для отсутствующего -> None", st.get_source(999999) is None)
    check("list_sources видит запись", len(st.list_sources()) == 1)

    section("Dataset: upsert по (source_id, name, layer)")
    did = st.upsert_dataset(sid, "customers.csv", layer="bronze")
    check("id > 0", did > 0)
    same_did = st.upsert_dataset(sid, "customers.csv", layer="bronze")
    check("повторный upsert не создаёт дубль", same_did == did)
    did_silver = st.upsert_dataset(sid, "customers.csv", layer="silver")
    check("тот же датасет в другом слое — отдельная запись", did_silver != did)
    check("get_dataset для отсутствующего -> None", st.get_dataset(999999) is None)
    check("list_datasets(source_id=) фильтрует", len(st.list_datasets(sid)) == 2)
    st.set_dataset_row_count(did, 42)
    check("row_count обновился", st.get_dataset(did)["row_count"] == 42)

    section("Ingest run: lifecycle")
    run_id = st.start_ingest_run(sid, "customers.csv")
    check("статус running по умолчанию",
         st.get_ingest_run(run_id)["status"] == "running")
    st.finish_ingest_run(run_id, "ok", records_ingested=3)
    check("статус и число записей обновились",
         st.get_ingest_run(run_id)["status"] == "ok"
         and st.get_ingest_run(run_id)["records_ingested"] == 3)
    check("list_ingest_runs(source_id=) находит", 
         any(r["id"] == run_id for r in st.list_ingest_runs(sid)))

    section("Bronze record: вставка, батч, подсчёт")
    bid1 = st.insert_bronze(did, {"name": "ООО Ромашка", "inn": "1234567890"},
                            source_record_id="ext-1", ingest_run_id=run_id)
    check("id > 0", bid1 > 0)
    b = st.get_bronze(bid1)
    check("payload сохранён как dict", b["payload"] == {"name": "ООО Ромашка", "inn": "1234567890"})
    check("get_bronze для отсутствующего -> None", st.get_bronze(999999) is None)

    batch_count = st.insert_bronze_batch(
        did, [{"name": "ЗАО Лютик", "id": "e2"}, {"name": "ИП Иванов", "id": "e3"}],
        run_id, id_field="id")
    check("insert_bronze_batch вернул число записей", batch_count == 2)
    check("count_bronze учитывает все записи", st.count_bronze(did) == 3)
    check("list_bronze возвращает все записи", len(st.list_bronze(did)) == 3)

    section("Silver record: продвижение из Bronze")
    silver_id = st.insert_silver(did, bid1, {"name": "ООО Ромашка", "inn": "1234567890"})
    check("id > 0", silver_id > 0)
    s = st.get_silver(silver_id)
    check("payload сохранён", s["payload"]["name"] == "ООО Ромашка")
    check("list_silver находит запись", any(x["id"] == silver_id for x in st.list_silver(did)))

    section("Quarantine record: карантин и разрешение")
    q_id = st.insert_quarantine(did, bid1, ["not_null: name пусто"])
    check("id > 0", q_id > 0)
    check("список карантина видит запись, resolved=False",
         any(q["id"] == q_id and q["resolved"] is False
            for q in st.list_quarantine(did)))
    resolved_ok = st.resolve_quarantine(q_id, "проверено вручную, ложное срабатывание")
    check("resolve_quarantine вернул True", resolved_ok)
    check("после разрешения resolved=True",
         any(q["id"] == q_id and q["resolved"] is True
            for q in st.list_quarantine(did, resolved=True)))
    check("фильтр resolved=False больше не находит", 
         not any(q["id"] == q_id for q in st.list_quarantine(did, resolved=False)))
    check("повторное разрешение отсутствующей записи -> False",
         st.resolve_quarantine(999999, "x") is False)

    section("Data profile: upsert по (dataset_id, field_name)")
    pid = st.upsert_profile(did, "name", total_count=3, null_count=0,
                            distinct_count=3, min_value="ЗАО Лютик",
                            max_value="ООО Ромашка", sample_values=["ООО Ромашка"])
    check("id > 0", pid > 0)
    same_pid = st.upsert_profile(did, "name", total_count=4, null_count=1,
                                 distinct_count=3)
    check("повторный upsert по тому же полю не дублирует", same_pid == pid)
    profiles = st.list_profiles(did)
    check("total_count обновился", any(p["field_name"] == "name" and p["total_count"] == 4
                                       for p in profiles))

    section("Quality rule: CRUD, активность")
    rid = st.create_quality_rule(did, "not_null", field_name="name", severity="error")
    check("id > 0", rid > 0)
    check("severity сохранена", st.get_quality_rule(rid)["severity"] == "error")
    check("активна по умолчанию", st.get_quality_rule(rid)["active"] is True)
    check("get_quality_rule для отсутствующего -> None", st.get_quality_rule(999999) is None)
    rid2 = st.create_quality_rule(did, "unique", field_name="inn", severity="warning")
    check("list_quality_rules видит обе активные", len(st.list_quality_rules(did)) == 2)
    ok = st.set_rule_active(rid2, False)
    check("set_rule_active вернул True", ok)
    check("active_only=True больше не видит выключенное правило",
         len(st.list_quality_rules(did, active_only=True)) == 1)
    check("active_only=False видит оба", len(st.list_quality_rules(did, active_only=False)) == 2)

    section("Quality run + result: агрегация")
    qrun_id = st.start_quality_run(did)
    check("dataset_id сохранён", st.get_quality_run(qrun_id)["dataset_id"] == did)
    st.insert_quality_result(qrun_id, rid, bid1, True, "")
    st.insert_quality_result(qrun_id, rid, bid1, False, "не прошло")
    st.finish_quality_run(qrun_id, rules_checked=2, records_checked=3,
                          violations_count=1, quarantined_count=1, promoted_count=2)
    finished = st.get_quality_run(qrun_id)
    check("статистика прогона сохранена",
         finished["violations_count"] == 1 and finished["promoted_count"] == 2)
    results = st.quality_results_for_run(qrun_id)
    check("оба результата видны", len(results) == 2)
    passed_only = st.quality_results_for_run(qrun_id, passed=True)
    check("фильтр passed=True работает", len(passed_only) == 1)
    rec_results = st.quality_results_for_record(bid1)
    check("quality_results_for_record находит результаты по записи", len(rec_results) == 2)
    check("list_quality_runs(dataset_id=) находит", 
         any(r["id"] == qrun_id for r in st.list_quality_runs(did)))

    section("Gold entity: создание, обновление, список")
    gold_id = st.create_gold_entity("counterparty", {"name": "ООО Ромашка"})
    check("id > 0", gold_id > 0)
    check("attributes сохранены", st.get_gold_entity(gold_id)["attributes"]["name"] == "ООО Ромашка")
    st.update_gold_attributes(gold_id, {"name": "ООО Ромашка", "inn": "1234567890"})
    check("attributes обновились", st.get_gold_entity(gold_id)["attributes"]["inn"] == "1234567890")
    check("get_gold_entity для отсутствующего -> None", st.get_gold_entity(999999) is None)
    check("list_gold_entities(entity_type=) фильтрует",
         len(st.list_gold_entities("counterparty")) == 1
         and len(st.list_gold_entities("part")) == 0)

    section("Source record link: привязка Silver -> Gold")
    link_id = st.link_source_record(gold_id, did, silver_id, match_score=0.95)
    check("id > 0", link_id > 0)
    same_link = st.link_source_record(gold_id, did, silver_id, match_score=0.5)
    check("повторная привязка той же пары не дублирует", same_link == link_id)
    links = st.links_for_gold(gold_id)
    check("links_for_gold находит связь", len(links) == 1)
    check("gold_for_silver находит золотую запись", st.gold_for_silver(silver_id) == gold_id)
    check("gold_for_silver для непривязанной записи -> None",
         st.gold_for_silver(999999) is None)

    section("Match candidate: lifecycle решений")
    silver_id2 = st.insert_silver(did, bid1, {"name": "ооо ромашка", "inn": "1234567890"})
    cand_id = st.create_match_candidate("counterparty", silver_id, silver_id2, 0.87)
    check("id > 0", cand_id > 0)
    check("статус по умолчанию pending", st.get_match_candidate(cand_id)["decision"] == "pending")
    check("get_match_candidate для отсутствующего -> None",
         st.get_match_candidate(999999) is None)
    check("list_match_candidates(decision=pending) находит",
         any(c["id"] == cand_id for c in st.list_match_candidates("pending")))
    ok2 = st.set_match_decision(cand_id, "confirmed_match", "human:x", gold_id)
    check("решение зафиксировано", ok2)
    check("gold_entity_id сохранён", st.get_match_candidate(cand_id)["gold_entity_id"] == gold_id)
    check("decided timestamp выставлен", st.get_match_candidate(cand_id)["decided"] is not None)

    section("Survivorship rule: приоритет источников")
    sr_id = st.set_survivorship_rule("counterparty", "name", ["1c", "crm"])
    check("id > 0", sr_id > 0)
    same_sr = st.set_survivorship_rule("counterparty", "name", ["crm", "1c"])
    check("повторный set по тому же (entity_type, field) не дублирует", same_sr == sr_id)
    rules = st.survivorship_rules_for("counterparty")
    check("приоритет обновился", rules["name"] == ["crm", "1c"])
    check("survivorship_rules_for для чужого типа -> пусто",
         st.survivorship_rules_for("part") == {})

    section("Lineage: рёбра и обратный обход (K4)")
    st.add_lineage_edge("source:crm:customers.csv", f"bronze:record:{bid1}",
                        transform_ref="ingest_full")
    st.add_lineage_edge(f"bronze:record:{bid1}", f"silver:record:{silver_id}",
                        transform_ref="quality_engine")
    st.add_lineage_edge(f"silver:record:{silver_id}", f"gold:entity:{gold_id}",
                        transform_ref="mdm.matching")
    into = st.lineage_edges_into(f"gold:entity:{gold_id}")
    check("lineage_edges_into находит входящее ребро", len(into) == 1)
    frm = st.lineage_edges_from(f"bronze:record:{bid1}")
    check("lineage_edges_from находит исходящее ребро", len(frm) == 1)
    trail = st.trace_lineage(f"gold:entity:{gold_id}")
    check("trace_lineage восстанавливает полную цепочку", len(trail) == 3)
    check("цепочка идёт от истока к цели",
         trail[0]["from_asset"] == "source:crm:customers.csv"
         and trail[-1]["to_asset"] == f"gold:entity:{gold_id}")
    check("trace_lineage для актива без истории -> пусто",
         st.trace_lineage("bronze:dataset:999999") == [])

    section("Audit log: неизменяемость на уровне API (только INSERT)")
    audit_id = st.log_audit("human:test", "test_action", "gold_entity", gold_id,
                            {"detail": "проверка"})
    check("audit id > 0", audit_id > 0)
    trail_a = st.audit_trail_for("gold_entity", gold_id)
    check("audit_trail_for находит запись", any(a["id"] == audit_id for a in trail_a))
    check("В классе Store НЕТ метода update/delete для audit_log",
         not hasattr(st, "update_audit") and not hasattr(st, "delete_audit"))
    recent = st.recent_audit(limit=5)
    check("recent_audit возвращает записи в порядке убывания id",
         recent == sorted(recent, key=lambda a: -a["id"]))

    section("Ontology: object_type, object_instance, object_link, action_def")
    otid = st.upsert_object_type("Контрагент", gold_entity_type="counterparty",
                                 attributes_schema=[{"name": "inn", "type": "string"}])
    check("object_type id > 0", otid > 0)
    same_otid = st.upsert_object_type("Контрагент", gold_entity_type="counterparty")
    check("повторный upsert по тому же имени не дублирует", same_otid == otid)
    check("get_object_type_by_name находит", st.get_object_type_by_name("Контрагент") is not None)
    check("get_object_type_by_gold_entity_type находит по entity_type",
         st.get_object_type_by_gold_entity_type("counterparty")["id"] == otid)
    check("get_object_type для отсутствующего -> None", st.get_object_type(999999) is None)
    check("list_object_types видит запись", len(st.list_object_types()) >= 1)

    oiid = st.create_object_instance(otid, {"name": "ООО Ромашка"}, gold_entity_id=gold_id)
    check("object_instance id > 0", oiid > 0)
    check("get_object_instance_by_gold находит", st.get_object_instance_by_gold(gold_id)["id"] == oiid)
    st.update_object_instance_attributes(oiid, {"name": "ООО Ромашка (обновлено)"})
    check("атрибуты обновились", st.get_object_instance(oiid)["attributes"]["name"]
         == "ООО Ромашка (обновлено)")
    try:
        st.create_object_instance(otid, {"x": 1}, gold_entity_id=gold_id)
        check("повторная привязка того же gold_entity_id -> ошибка уникальности", False)
    except Exception:
        check("повторная привязка того же gold_entity_id -> ошибка уникальности", True)
        st.conn.rollback()

    otid2 = st.upsert_object_type("Деталь", gold_entity_type="part")
    oiid2 = st.create_object_instance(otid2, {"sku": "A1"})
    link_id = st.create_object_link("поставляет", oiid, oiid2)
    check("object_link id > 0", link_id > 0)
    same_link = st.create_object_link("поставляет", oiid, oiid2)
    check("повторное создание той же связи не дублирует", same_link == link_id)
    check("links_from находит связь", len(st.links_from(oiid)) == 1)
    check("links_to находит связь", len(st.links_to(oiid2)) == 1)
    check("links_from с фильтром по типу находит", len(st.links_from(oiid, "поставляет")) == 1)
    check("links_from с фильтром по чужому типу не находит", len(st.links_from(oiid, "чужой")) == 0)

    adid = st.create_action_def(otid, "correct_attribute", "ontology.actions.correct_attribute")
    check("action_def id > 0", adid > 0)
    same_adid = st.create_action_def(otid, "correct_attribute", "new.handler")
    check("повторное создание того же действия обновляет handler, не дублирует",
         same_adid == adid and st.get_action_def(adid)["handler"] == "new.handler")
    check("list_action_defs видит действие", len(st.list_action_defs(otid)) == 1)
    check("get_action_def_by_name находит", st.get_action_def_by_name(otid, "correct_attribute") is not None)
    check("get_action_def для отсутствующего -> None", st.get_action_def(999999) is None)

    section("Process instance: lifecycle и идемпотентность предмета")
    pid = st.create_process_instance("quarantine_correction", "quarantine_record", 42,
                                     context={"foo": "bar"}, created_by="system")
    check("process id > 0", pid > 0)
    check("статус по умолчанию open", st.get_process_instance(pid)["status"] == "open")
    check("get_process_instance для отсутствующего -> None",
         st.get_process_instance(999999) is None)
    ok_set = st.set_process_status(pid, "awaiting_task", context={"step": 1})
    check("set_process_status обновил статус и контекст", ok_set
         and st.get_process_instance(pid)["status"] == "awaiting_task"
         and st.get_process_instance(pid)["context"]["step"] == 1)
    found_open = st.find_open_process_for_subject("quarantine_record", 42)
    check("find_open_process_for_subject находит незавершённый процесс",
         found_open is not None and found_open["id"] == pid)
    st.set_process_status(pid, "completed")
    check("find_open_process_for_subject НЕ находит завершённый процесс",
         st.find_open_process_for_subject("quarantine_record", 42) is None)
    check("list_process_instances(process_type=) фильтрует",
         any(p["id"] == pid for p in st.list_process_instances("quarantine_correction")))
    check("list_process_instances(status=) фильтрует",
         all(p["status"] == "completed"
            for p in st.list_process_instances(status="completed")))

    section("Task: lifecycle")
    tid = st.create_task(pid, "Исправить запись", description="детали",
                         assignee="human:ivanov")
    check("task id > 0", tid > 0)
    check("статус по умолчанию open", st.get_task(tid)["status"] == "open")
    check("get_task для отсутствующего -> None", st.get_task(999999) is None)
    ok_complete = st.complete_task(tid, result={"x": 1})
    check("complete_task обновил статус и результат", ok_complete
         and st.get_task(tid)["status"] == "done"
         and st.get_task(tid)["result"] == {"x": 1}
         and st.get_task(tid)["completed"] is not None)
    tid2 = st.create_task(pid, "Вторая задача")
    ok_cancel = st.cancel_task(tid2)
    check("cancel_task обновил статус", ok_cancel and st.get_task(tid2)["status"] == "cancelled")
    check("list_tasks(process_instance_id=) видит обе задачи",
         len(st.list_tasks(process_instance_id=pid)) == 2)
    check("list_tasks(status=) фильтрует", len(st.list_tasks(status="done")) >= 1
         and all(t["status"] == "done" for t in st.list_tasks(status="done")))
    check("list_tasks(assignee=) фильтрует",
         all(t["assignee"] == "human:ivanov" for t in st.list_tasks(assignee="human:ivanov")))

    section("Write-back log: идемпотентность")
    wb_id, is_new = st.write_back_log_attempt(pid, sid, "customers", "c1", "idem-key-1")
    check("первая попытка — новая запись", is_new)
    wb_id2, is_new2 = st.write_back_log_attempt(pid, sid, "customers", "c1", "idem-key-1")
    check("повторная попытка с тем же ключом -> не новая", not is_new2)
    check("id тот же (та же запись)", wb_id == wb_id2)
    st.write_back_mark_result(wb_id, "ok")
    check("get_write_back_log видит обновлённый статус",
         st.get_write_back_log(wb_id)["status"] == "ok")
    check("get_write_back_log для отсутствующего -> None",
         st.get_write_back_log(999999) is None)
    check("attempts увеличился", st.get_write_back_log(wb_id)["attempts"] == 1)
    check("list_write_back_log(process_instance_id=) находит",
         any(w["id"] == wb_id for w in st.list_write_back_log(process_instance_id=pid)))
    check("list_write_back_log(status=) фильтрует",
         all(w["status"] == "ok" for w in st.list_write_back_log(status="ok")))

    section("AI interaction: неизменяемый журнал")
    ai_id = st.log_ai_interaction("human:ivanov", "покажи статистику", "ops",
                                  tools_called=[{"name": "get_dashboard_stats"}],
                                  result_text="вот ответ")
    check("ai_interaction id > 0", ai_id > 0)
    check("В классе Store НЕТ метода update/delete для ai_interaction",
         not hasattr(st, "update_ai_interaction") and not hasattr(st, "delete_ai_interaction"))
    interactions = st.list_ai_interactions()
    check("list_ai_interactions находит запись", any(a["id"] == ai_id for a in interactions))
    check("list_ai_interactions(actor=) фильтрует",
         all(a["actor"] == "human:ivanov" for a in st.list_ai_interactions("human:ivanov")))

    section("Bronze update: правка записи процессом коррекции")
    bid_for_update = st.insert_bronze(did, {"name": "исходное значение"})
    ok_update = st.update_bronze_payload(bid_for_update, {"name": "исправленное значение"})
    check("update_bronze_payload вернул True", ok_update)
    check("payload реально обновился",
         st.get_bronze(bid_for_update)["payload"]["name"] == "исправленное значение")
    check("update_bronze_payload для отсутствующей записи -> False",
         st.update_bronze_payload(999999, {}) is False)

    section("Quarantine: get_quarantine (единичный доступ)")
    check("get_quarantine находит существующую запись",
         st.get_quarantine(q_id) is not None and st.get_quarantine(q_id)["id"] == q_id)
    check("get_quarantine для отсутствующей -> None", st.get_quarantine(999999) is None)

    section("Dashboard stats: агрегированная статистика")
    stats = st.dashboard_stats()
    check("sources >= 1", stats["sources"] >= 1)
    check("datasets >= 2", stats["datasets"] >= 2)
    check("bronze_records >= 3", stats["bronze_records"] >= 3)
    check("silver_records >= 2", stats["silver_records"] >= 2)
    check("gold_entities >= 1", stats["gold_entities"] >= 1)
    check("audit_entries >= 1", stats["audit_entries"] >= 1)
    check("object_types >= 2", stats["object_types"] >= 2)
    check("object_instances >= 2", stats["object_instances"] >= 2)
    check("open_processes учитывается корректно (наш процесс завершён)",
         stats["open_processes"] == 0)
    check("open_tasks >= 0", stats["open_tasks"] >= 0)
    check("ai_interactions >= 1", stats["ai_interactions"] >= 1)

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
