"""Тесты dataforge.api.server (FastAPI): реальный HTTP-сервер (uvicorn в
отдельном потоке), реальный embedded PostgreSQL, реальный HTTP-клиент
(httpx) — без TestClient-моков, максимально приближено к тому, как
систему будет дёргать настоящий клиент.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
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

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="forge_api_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"

try:
    import httpx  # type: ignore
    import uvicorn  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = SKIP_REASON or "httpx/uvicorn не установлены"


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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ApiServer:
    """Реальный uvicorn-сервер FastAPI-приложения в отдельном потоке."""

    def __init__(self, dsn: str, token: str | None = None):
        from dataforge.api.server import app, configure
        from dataforge.config import Config

        self.cfg = Config(db_dsn=dsn)
        configure(self.cfg, token)
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self._wait_ready()

    def _wait_ready(self, attempts: int = 40) -> None:
        for _ in range(attempts):
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=1)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("сервер не поднялся за отведённое время")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_api: тесты пропущены — {SKIP_REASON}")
        return 0

    tmpdir = Path(tempfile.mkdtemp(prefix="forge_api_files_"))
    csv_path = tmpdir / "customers.csv"
    csv_path.write_text(
        "name,inn\nООО Ромашка,1234567890\nооо  ромашка,1234567890\n"
        "ЗАО Совсем Другое,0000000000\n", encoding="utf-8")

    section("Сервер поднимается и отвечает на /health без токена")
    srv = _ApiServer(_fresh_dsn())
    try:
        r = httpx.get(f"{srv.base_url}/health", timeout=5)
        check("health -> 200", r.status_code == 200 and r.json()["status"] == "ok")

        section("Sources: регистрация, discover, ingest")
        r = httpx.post(f"{srv.base_url}/v1/sources",
                       json={"name": "crm", "kind": "file",
                            "config": {"path": str(csv_path)}}, timeout=5)
        check("создание источника -> 200", r.status_code == 200)
        sid = r.json()["id"]

        r = httpx.get(f"{srv.base_url}/v1/sources", timeout=5)
        check("список источников видит созданный", any(s["id"] == sid for s in r.json()))

        r = httpx.post(f"{srv.base_url}/v1/sources/{sid}/discover", timeout=5)
        check("discover -> 200", r.status_code == 200)
        check("схема содержит поля name/inn",
             {"name", "inn"} <= {f["name"] for f in r.json()[0]["fields"]})

        r = httpx.post(f"{srv.base_url}/v1/sources/999999/discover", timeout=5)
        check("discover для несуществующего источника -> 404", r.status_code == 404)

        r = httpx.post(f"{srv.base_url}/v1/sources/{sid}/ingest/full",
                       json={"dataset": "customers.csv"}, timeout=5)
        check("ingest/full -> 200", r.status_code == 200)
        did = r.json()["dataset_id"]
        check("выгружено 3 записи", r.json()["records_ingested"] == 3)

        section("Datasets: список, детали, bronze")
        r = httpx.get(f"{srv.base_url}/v1/datasets", timeout=5)
        check("список датасетов видит созданный", any(d["id"] == did for d in r.json()))

        r = httpx.get(f"{srv.base_url}/v1/datasets/{did}", timeout=5)
        check("детали датасета -> 200 с profiles", r.status_code == 200
             and "profiles" in r.json())

        r = httpx.get(f"{srv.base_url}/v1/datasets/999999", timeout=5)
        check("несуществующий датасет -> 404", r.status_code == 404)

        r = httpx.get(f"{srv.base_url}/v1/datasets/{did}/bronze", timeout=5)
        check("bronze-записи видны через API", len(r.json()) == 3)

        section("Quality: профилирование, правило, прогон, карантин")
        r = httpx.post(f"{srv.base_url}/v1/datasets/{did}/profile", timeout=5)
        check("profile -> 200", r.status_code == 200)
        check("профиль содержит поле name", any(p["field_name"] == "name" for p in r.json()))

        r = httpx.post(f"{srv.base_url}/v1/datasets/{did}/quality-rules",
                       json={"rule_type": "not_null", "field_name": "name",
                            "severity": "error"}, timeout=5)
        check("создание правила -> 200", r.status_code == 200)

        r = httpx.get(f"{srv.base_url}/v1/datasets/{did}/quality-rules", timeout=5)
        check("список правил видит созданное", len(r.json()) == 1)

        r = httpx.post(f"{srv.base_url}/v1/datasets/{did}/quality-run", timeout=5)
        check("quality-run -> 200", r.status_code == 200)
        check("все 3 записи прошли (name всегда заполнено)",
             r.json()["promoted_count"] == 3)

        r = httpx.get(f"{srv.base_url}/v1/datasets/{did}/silver", timeout=5)
        check("silver-записи видны через API", len(r.json()) == 3)

        r = httpx.get(f"{srv.base_url}/v1/datasets/{did}/quarantine", timeout=5)
        check("карантин пуст (все записи валидны)", len(r.json()) == 0)

        section("MDM: матчинг, stewardship, слияние")
        r = httpx.post(f"{srv.base_url}/v1/mdm/match",
                       json={"entity_type": "counterparty", "dataset_id": did,
                            "fields": ["name", "inn"]}, timeout=5)
        check("match -> 200", r.status_code == 200)
        candidates = r.json()
        check("найден кандидат на дубль", len(candidates) >= 1)
        cand_id = candidates[0]["id"]

        r = httpx.get(f"{srv.base_url}/v1/mdm/candidates", timeout=5)
        check("список кандидатов видит созданного", any(c["id"] == cand_id for c in r.json()))

        r = httpx.post(f"{srv.base_url}/v1/mdm/candidates/{cand_id}/merge",
                       json={"decided_by": "human:api-test"}, timeout=5)
        check("merge -> 200", r.status_code == 200)
        gold_id = r.json()["id"]

        r = httpx.post(f"{srv.base_url}/v1/mdm/candidates/{cand_id}/merge",
                       json={"decided_by": "human:api-test"}, timeout=5)
        check("повторный merge решённого кандидата -> 400 (доменная ошибка, не 500)",
             r.status_code == 400)

        r = httpx.post(f"{srv.base_url}/v1/mdm/candidates/999999/reject",
                       json={"decided_by": "human:x"}, timeout=5)
        check("reject несуществующего кандидата -> 400", r.status_code == 400)

        section("Gold: детали, связи")
        r = httpx.get(f"{srv.base_url}/v1/gold/{gold_id}", timeout=5)
        check("детали golden record -> 200 с links", r.status_code == 200
             and len(r.json()["links"]) == 2)
        r = httpx.get(f"{srv.base_url}/v1/gold/999999", timeout=5)
        check("несуществующая golden record -> 404", r.status_code == 404)

        section("Ontology: типы, материализация, связи, actions через HTTP")
        r = httpx.post(f"{srv.base_url}/v1/ontology/types",
                       json={"name": "Контрагент", "gold_entity_type": "counterparty",
                            "attributes_schema": [{"name": "name", "type": "string",
                                                   "required": True}]}, timeout=5)
        check("создание ObjectType -> 200", r.status_code == 200)
        ot_id = r.json()["id"]

        r = httpx.get(f"{srv.base_url}/v1/ontology/types", timeout=5)
        check("список типов видит созданный", any(t["id"] == ot_id for t in r.json()))

        r = httpx.post(f"{srv.base_url}/v1/ontology/types/{ot_id}/actions",
                       json={"name": "correct_attribute",
                            "handler": "ontology.actions.correct_attribute"}, timeout=5)
        check("определение action -> 200", r.status_code == 200)

        r = httpx.get(f"{srv.base_url}/v1/ontology/types/{ot_id}", timeout=5)
        check("детали типа содержат action_defs", len(r.json()["action_defs"]) == 1)

        r = httpx.get(f"{srv.base_url}/v1/ontology/types/999999", timeout=5)
        check("несуществующий ObjectType -> 404", r.status_code == 404)

        r = httpx.post(f"{srv.base_url}/v1/ontology/materialize",
                       json={"gold_entity_id": gold_id}, timeout=5)
        check("материализация из golden record -> 200", r.status_code == 200)
        instance_id = r.json()["id"]
        check("gold_entity_id привязан", r.json()["gold_entity_id"] == gold_id)

        r = httpx.post(f"{srv.base_url}/v1/ontology/materialize",
                       json={"gold_entity_id": 999999}, timeout=5)
        check("материализация несуществующей golden record -> 400", r.status_code == 400)

        r = httpx.get(f"{srv.base_url}/v1/ontology/instances", timeout=5)
        check("список экземпляров видит созданный", any(i["id"] == instance_id for i in r.json()))

        r = httpx.get(f"{srv.base_url}/v1/ontology/instances/{instance_id}", timeout=5)
        check("карточка объекта -> 200 с instance/object_type", r.status_code == 200
             and r.json()["instance"]["id"] == instance_id)

        r = httpx.post(f"{srv.base_url}/v1/ontology/types",
                       json={"name": "Деталь", "gold_entity_type": "part"}, timeout=5)
        check("создание второго ObjectType (Деталь) -> 200", r.status_code == 200)

        gold_part_id = None
        # golden record для второго типа создаём напрямую через Store — у API
        # нет отдельного эндпоинта "создать golden record вручную" (он
        # появляется только из MDM-слияния), это ожидаемо для honest MVP.
        from dataforge.db.store import Store as _StoreForTest
        _st = _StoreForTest(srv.cfg.db_dsn)
        gold_part_id = _st.create_gold_entity("part", {"sku": "A1"})
        _st.close()

        r = httpx.post(f"{srv.base_url}/v1/ontology/materialize",
                       json={"gold_entity_id": gold_part_id}, timeout=5)
        check("материализация второго объекта -> 200", r.status_code == 200)
        instance_part_id = r.json()["id"]

        r = httpx.post(f"{srv.base_url}/v1/ontology/links",
                       json={"link_type": "поставляет", "from_instance_id": instance_id,
                            "to_instance_id": instance_part_id, "actor": "human:api-test"},
                       timeout=5)
        check("создание связи через API -> 200", r.status_code == 200)

        r = httpx.get(f"{srv.base_url}/v1/ontology/instances/{instance_id}", timeout=5)
        check("исходящая связь видна в карточке объекта",
             any(link["to_instance_id"] == instance_part_id
                for link in r.json()["outgoing_links"]))

        r = httpx.post(f"{srv.base_url}/v1/ontology/links",
                       json={"link_type": "x", "from_instance_id": 999999,
                            "to_instance_id": instance_part_id, "actor": "human:x"},
                       timeout=5)
        check("связь с несуществующим объектом -> 400", r.status_code == 400)

        r = httpx.post(f"{srv.base_url}/v1/ontology/instances/{instance_id}/actions",
                       json={"action": "correct_attribute",
                            "params": {"field": "name", "value": "Новое имя",
                                      "reason": "правка через API-тест"},
                            "actor": "human:api-test"}, timeout=5)
        check("выполнение действия correct_attribute -> 200", r.status_code == 200)
        check("явно изменённое поле видно в результате", r.json()["new_value"] == "Новое имя")

        r = httpx.post(f"{srv.base_url}/v1/ontology/instances/{instance_id}/actions",
                       json={"action": "correct_attribute",
                            "params": {"field": "name", "value": "X"},
                            "actor": "human:api-test"}, timeout=5)
        check("действие без обязательного params.reason -> 400 (guardrail)",
             r.status_code == 400)

        r = httpx.post(f"{srv.base_url}/v1/ontology/instances/{instance_id}/actions",
                       json={"action": "no_such_action", "params": {},
                            "actor": "human:api-test"}, timeout=5)
        check("несуществующее действие -> 400", r.status_code == 400)

        section("Process Orchestrator: сквозной процесс с write-back через HTTP (K3)")
        sqlite_path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(sqlite_path)
        conn.execute("CREATE TABLE customers(id TEXT PRIMARY KEY, name TEXT, inn TEXT)")
        conn.execute("INSERT INTO customers VALUES ('p1', 'ООО Процесс', '')")
        conn.commit()
        conn.close()
        os.environ["TEST_API_PROCESS_DSN"] = f"sqlite:///{sqlite_path}"

        r = httpx.post(f"{srv.base_url}/v1/sources",
                       json={"name": "erp_proc", "kind": "sql",
                            "config": {"dsn_env": "TEST_API_PROCESS_DSN",
                                      "table": "customers", "id_field": "id"}}, timeout=5)
        proc_sid = r.json()["id"]

        r = httpx.post(f"{srv.base_url}/v1/sources/{proc_sid}/ingest/full",
                       json={"dataset": "customers", "id_field": "id"}, timeout=5)
        proc_did = r.json()["dataset_id"]

        r = httpx.post(f"{srv.base_url}/v1/datasets/{proc_did}/quality-rules",
                       json={"rule_type": "not_null", "field_name": "inn",
                            "severity": "error"}, timeout=5)
        r = httpx.post(f"{srv.base_url}/v1/datasets/{proc_did}/quality-run", timeout=5)
        check("quality-run отправил запись в карантин", r.json()["quarantined_count"] == 1)

        r = httpx.get(f"{srv.base_url}/v1/datasets/{proc_did}/quarantine", timeout=5)
        proc_qid = r.json()[0]["id"]

        r = httpx.post(f"{srv.base_url}/v1/processes/quarantine-correction",
                       json={"quarantine_id": proc_qid, "assignee": "human:steward"},
                       timeout=5)
        check("процесс запущен -> 200", r.status_code == 200)
        proc_pid = r.json()["id"]
        check("статус процесса awaiting_task", r.json()["status"] == "awaiting_task")

        r = httpx.post(f"{srv.base_url}/v1/processes/quarantine-correction",
                       json={"quarantine_id": proc_qid}, timeout=5)
        check("повторный запуск на том же карантине идемпотентен",
             r.json()["id"] == proc_pid)

        r = httpx.get(f"{srv.base_url}/v1/processes/{proc_pid}", timeout=5)
        check("детали процесса содержат задачи и audit_trail", len(r.json()["tasks"]) == 1
             and len(r.json()["audit_trail"]) >= 1)
        check("assignee из запроса передан в задачу",
             r.json()["tasks"][0]["assignee"] == "human:steward")

        r = httpx.post(f"{srv.base_url}/v1/processes/{proc_pid}/correct",
                       json={"corrected_payload": {"id": "p1", "name": "ООО Процесс",
                                                   "inn": ""}, "actor": "human:steward"},
                       timeout=5)
        check("невалидное исправление отклонено (guardrail)", r.json()["accepted"] is False)

        r = httpx.post(f"{srv.base_url}/v1/processes/{proc_pid}/correct",
                       json={"corrected_payload": {"id": "p1", "name": "ООО Процесс",
                                                   "inn": "9998887766"}, "actor": "human:steward"},
                       timeout=5)
        check("валидное исправление принято", r.json()["accepted"] is True)

        r = httpx.post(f"{srv.base_url}/v1/processes/{proc_pid}/write-back",
                       json={"dataset_name": "customers", "natural_key": "p1",
                            "actor": "human:steward"}, timeout=5)
        check("write-back -> 200", r.status_code == 200)
        check("write-back успешен", r.json()["ok"] is True)

        conn2 = sqlite3.connect(sqlite_path)
        row = conn2.execute("SELECT inn FROM customers WHERE id='p1'").fetchone()
        conn2.close()
        check("значение РЕАЛЬНО записалось в источник через HTTP-сценарий",
             row[0] == "9998887766")

        r = httpx.post(f"{srv.base_url}/v1/processes/{proc_pid}/rollback",
                       json={"actor": "human:x"}, timeout=5)
        check("откат после успешного write-back -> 400", r.status_code == 400)

        r = httpx.get(f"{srv.base_url}/v1/processes",
                      params={"process_type": "quarantine_correction"}, timeout=5)
        check("список процессов видит наш процесс", any(p["id"] == proc_pid for p in r.json()))

        r = httpx.post(f"{srv.base_url}/v1/processes/{proc_pid}/correct",
                       json={"corrected_payload": {}, "actor": "human:x"}, timeout=5)
        check("подача исправления для завершённого процесса -> 400", r.status_code == 400)

        os.unlink(sqlite_path)
        os.environ.pop("TEST_API_PROCESS_DSN", None)

        section("Lineage: цепочка через HTTP")
        r = httpx.get(f"{srv.base_url}/v1/lineage/trace",
                      params={"asset": f"gold:entity:{gold_id}"}, timeout=5)
        check("lineage trace -> 200", r.status_code == 200)
        check("цепочка непуста", len(r.json()) > 0)

        section("Аудит через HTTP")
        r = httpx.get(f"{srv.base_url}/v1/audit", timeout=5)
        check("аудит содержит записи", len(r.json()) > 0)
        r = httpx.get(f"{srv.base_url}/v1/audit/gold_entity/{gold_id}", timeout=5)
        check("аудит по конкретному объекту", len(r.json()) > 0)

        section("Дашборд")
        r = httpx.get(f"{srv.base_url}/dashboard", timeout=5)
        check("дашборд отдаётся -> 200", r.status_code == 200)
        check("это HTML DataForge", "<title>DataForge" in r.text)
        r = httpx.get(f"{srv.base_url}/v1/dashboard/stats", timeout=5)
        check("статистика дашборда -> 200", r.status_code == 200)
        check("статистика отражает реальные данные",
             r.json()["sources"] >= 1 and r.json()["gold_entities"] >= 1)

        section("Survivorship через HTTP")
        r = httpx.post(f"{srv.base_url}/v1/mdm/survivorship",
                       json={"entity_type": "counterparty", "field_name": "name",
                            "source_priority": ["crm"]}, timeout=5)
        check("survivorship -> 200", r.status_code == 200)

        section("Auto-merge через HTTP (guardrail — порог явно передаётся)")
        r = httpx.post(f"{srv.base_url}/v1/mdm/auto-merge",
                       json={"entity_type": "counterparty", "auto_threshold": 1.5},
                       timeout=5)
        check("auto-merge с недостижимым порогом -> 0 слияний", r.json()["count"] == 0)

        section("Неизвестный маршрут -> 404")
        r = httpx.get(f"{srv.base_url}/v1/nonexistent", timeout=5)
        check("неизвестный GET -> 404", r.status_code == 404)

        section("Info: конфиг маскирует секреты")
        r = httpx.get(f"{srv.base_url}/info", timeout=5)
        check("info -> 200", r.status_code == 200)
        cfg_dump = json.dumps(r.json()["config"])
        check("db_dsn не содержит открытого текста без маскирования пароля",
             "***" in r.json()["config"]["db_dsn"] or "@" not in r.json()["config"]["db_dsn"])
        del cfg_dump
    finally:
        srv.stop()

    section("Токен: без Authorization -> 401, с верным -> 200")
    srv2 = _ApiServer(_fresh_dsn(), token="secret-token")
    try:
        r = httpx.get(f"{srv2.base_url}/v1/sources", timeout=5)
        check("без токена -> 401", r.status_code == 401)
        r = httpx.get(f"{srv2.base_url}/v1/sources",
                      headers={"Authorization": "Bearer secret-token"}, timeout=5)
        check("с верным токеном -> 200", r.status_code == 200)
        r = httpx.get(f"{srv2.base_url}/health", timeout=5)
        check("/health не требует токена", r.status_code == 200)
        r = httpx.get(f"{srv2.base_url}/dashboard", timeout=5)
        check("/dashboard не требует токена", r.status_code == 200)
    finally:
        srv2.stop()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
