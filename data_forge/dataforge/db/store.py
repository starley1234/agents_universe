"""Хранилище DataForge: PostgreSQL.

ОБЯЗАТЕЛЬНОЕ хранилище (см. dataforge/config.py: без DB_DSN приложение
не стартует) — каталог источников, слои Bronze/Silver/Gold (метаданные;
сами полезные данные тоже хранятся здесь как JSONB — для MVP этого
достаточно, полноценный Lakehouse на объектном хранилище — сознательно
не реализован, см. README.md).

Схема отражает модель данных из ТЗ (§4), в объёме выбранного контура
(Connect Hub + Quality Engine + MDM/матчинг + Lineage + Ontology +
Process Orchestrator (кейс K3) + AI Copilot — см. README.md, "Честная
граница объёма" за тем, что осознанно НЕ реализовано в остальных
разделах ТЗ):

  source              — зарегистрированный источник данных (файл/SQL/1С)
  dataset             — набор данных источника, привязан к слою
                        (bronze/silver/gold)
  bronze_record       — сырая запись «как есть» из источника
  silver_record       — очищенная и типизированная запись (после Quality
                        Engine), либо помещённая в карантин
  quarantine_record   — запись, не прошедшая обязательные правила
                        качества (не попала в Silver)
  data_profile        — статистика профилирования поля датасета
  quality_rule        — декларативное правило качества данных
  quality_run         — запуск проверки качества по датасету
  quality_result      — результат проверки одного правила на одной записи
  gold_entity         — «золотая запись» бизнес-сущности (K1)
  source_record_link  — какие сырые/silver-записи слились в golden record
  match_candidate     — кандидат на дубль (stewardship-очередь, K1)
  survivorship_rule   — приоритет источников для конкретного поля сущности
  lineage_edge        — ребро графа прослеживаемости (K4): откуда -> куда
  audit_log           — НЕИЗМЕНЯЕМЫЙ журнал действий (только INSERT)
  object_type         — тип бизнес-объекта Ontology (ТЗ §3.2): "Контрагент",
                        "Деталь" и т.п., со схемой атрибутов и опциональной
                        привязкой к entity_type Gold-слоя
  object_instance     — конкретный экземпляр ObjectType, материализованный
                        из golden record (или существующий самостоятельно)
  object_link         — типизированная связь между двумя ObjectInstance
                        ("поставщик", "содержит" и т.п.)
  action_def          — определение действия, которое можно выполнить над
                        объектами данного ObjectType ("скорректировать
                        атрибут", "согласовать", "связать")
  process_instance    — запущенный экземпляр сквозного процесса (ТЗ K3):
                        сейчас единственный процесс "quarantine_correction"
                        (карантин -> задача -> корректировка -> write-back),
                        но таблица общая для будущих процессов
  task                — задача человеку внутри процесса (stewardship):
                        что нужно сделать, кто исполнитель, статус
  write_back_log      — журнал попыток обратной записи в источник
                        (идемпотентность, ошибки, статус) — тот же принцип,
                        что onec_sync_log в erp_ai, но для ЛЮБОГО источника
  ai_interaction      — НЕИЗМЕНЯЕМЫЙ аудит взаимодействий с AI Copilot
                        (ТЗ §4: "AiInteraction... аудит AI")

Импорт psycopg — ленивый, как в остальных проектах этого репозитория.
"""
from __future__ import annotations

import json
import time
from typing import Any


class StoreError(RuntimeError):
    """Ошибка работы с хранилищем: подключение и т.п."""


def _require_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise StoreError(
            "DataForge требует psycopg. Установите: pip install \"psycopg[binary]\""
        ) from exc
    return psycopg


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    """Единая точка доступа к PostgreSQL для DataForge."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise StoreError(
                "Не задан DB_DSN. DataForge не работает без PostgreSQL — "
                "укажите переменную окружения DB_DSN."
            )
        psycopg = _require_psycopg()
        self._psycopg = psycopg
        self.dsn = dsn
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            raise StoreError(f"Не удалось подключиться к PostgreSQL: {exc}") from exc
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _now() -> float:
        return time.time()

    # ------------------------------------------------------------ schema
    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS source(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,          -- file | sql | onec_odata
                config JSONB DEFAULT '{}',   -- НЕСЕКРЕТНАЯ часть конфига
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dataset(
                id SERIAL PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
                name TEXT NOT NULL,          -- имя набора данных у источника
                layer TEXT NOT NULL DEFAULT 'bronze',  -- bronze|silver|gold
                schema_json JSONB DEFAULT '[]',  -- [{"name":..,"type":..}]
                row_count INTEGER DEFAULT 0,
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION,
                UNIQUE(source_id, name, layer)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingest_run(
                id SERIAL PRIMARY KEY,
                source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
                dataset_name TEXT NOT NULL,
                status TEXT DEFAULT 'running',   -- running|ok|error
                records_ingested INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                started DOUBLE PRECISION,
                finished DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bronze_record(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                source_record_id TEXT DEFAULT '',  -- натуральный ключ в источнике, если есть
                payload JSONB NOT NULL,            -- запись как есть
                ingest_run_id INTEGER,
                ingested DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_bronze_dataset "
            "ON bronze_record(dataset_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS silver_record(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                bronze_record_id INTEGER NOT NULL REFERENCES bronze_record(id)
                    ON DELETE CASCADE,
                payload JSONB NOT NULL,        -- очищенная/типизированная запись
                quality_run_id INTEGER,
                created DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_silver_dataset "
            "ON silver_record(dataset_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quarantine_record(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                bronze_record_id INTEGER NOT NULL REFERENCES bronze_record(id)
                    ON DELETE CASCADE,
                reasons JSONB DEFAULT '[]',     -- список нарушенных правил
                quality_run_id INTEGER,
                quarantined DOUBLE PRECISION,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_at DOUBLE PRECISION,
                resolution TEXT DEFAULT ''
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS data_profile(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL,
                total_count INTEGER DEFAULT 0,
                null_count INTEGER DEFAULT 0,
                distinct_count INTEGER DEFAULT 0,
                min_value TEXT DEFAULT '',
                max_value TEXT DEFAULT '',
                sample_values JSONB DEFAULT '[]',
                computed DOUBLE PRECISION,
                UNIQUE(dataset_id, field_name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_rule(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                field_name TEXT DEFAULT '',   -- пусто -> правило на всю запись
                rule_type TEXT NOT NULL,
                -- not_null | unique | regex | range | allowed_values
                params JSONB DEFAULT '{}',
                severity TEXT DEFAULT 'error',  -- error | warning
                active BOOLEAN DEFAULT TRUE,
                created DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_quality_rule_dataset "
            "ON quality_rule(dataset_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_run(
                id SERIAL PRIMARY KEY,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                rules_checked INTEGER DEFAULT 0,
                records_checked INTEGER DEFAULT 0,
                violations_count INTEGER DEFAULT 0,
                quarantined_count INTEGER DEFAULT 0,
                promoted_count INTEGER DEFAULT 0,
                started DOUBLE PRECISION,
                finished DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_result(
                id SERIAL PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES quality_run(id) ON DELETE CASCADE,
                rule_id INTEGER NOT NULL REFERENCES quality_rule(id) ON DELETE CASCADE,
                bronze_record_id INTEGER NOT NULL REFERENCES bronze_record(id)
                    ON DELETE CASCADE,
                passed BOOLEAN NOT NULL,
                detail TEXT DEFAULT ''
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_quality_result_run "
            "ON quality_result(run_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gold_entity(
                id SERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL,     -- 'counterparty' | 'part' | ...
                attributes JSONB NOT NULL DEFAULT '{}',   -- согласованные поля
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_gold_entity_type "
            "ON gold_entity(entity_type)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS source_record_link(
                id SERIAL PRIMARY KEY,
                gold_entity_id INTEGER NOT NULL REFERENCES gold_entity(id)
                    ON DELETE CASCADE,
                dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
                silver_record_id INTEGER NOT NULL REFERENCES silver_record(id)
                    ON DELETE CASCADE,
                match_score REAL DEFAULT 1.0,
                created DOUBLE PRECISION,
                UNIQUE(gold_entity_id, silver_record_id)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_link_gold "
            "ON source_record_link(gold_entity_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS match_candidate(
                id SERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL,
                record_a_id INTEGER NOT NULL REFERENCES silver_record(id)
                    ON DELETE CASCADE,
                record_b_id INTEGER NOT NULL REFERENCES silver_record(id)
                    ON DELETE CASCADE,
                score REAL NOT NULL,
                decision TEXT DEFAULT 'pending',
                -- pending | confirmed_match | rejected | auto_merged
                gold_entity_id INTEGER,
                decided_by TEXT DEFAULT '',
                created DOUBLE PRECISION,
                decided DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_match_candidate_status "
            "ON match_candidate(decision)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS survivorship_rule(
                id SERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL,
                field_name TEXT NOT NULL,
                source_priority JSONB NOT NULL DEFAULT '[]',  -- [source_name,...]
                created DOUBLE PRECISION,
                UNIQUE(entity_type, field_name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lineage_edge(
                id SERIAL PRIMARY KEY,
                from_asset TEXT NOT NULL,     -- напр. "source:1:orders.csv"
                to_asset TEXT NOT NULL,       -- напр. "silver:dataset:3"
                transform_ref TEXT DEFAULT '',  -- что применено (правило/скрипт)
                run_ref TEXT DEFAULT '',        -- id прогона пайплайна
                created DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_lineage_to "
            "ON lineage_edge(to_asset)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_lineage_from "
            "ON lineage_edge(from_asset)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log(
                id SERIAL PRIMARY KEY,
                actor TEXT NOT NULL,          -- 'human:<user>' | 'system:<module>'
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                details JSONB DEFAULT '{}',
                created DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_audit_log_entity "
            "ON audit_log(entity_type, entity_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS object_type(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,      -- напр. "Контрагент", "Деталь"
                gold_entity_type TEXT DEFAULT '',  -- привязка к entity_type Gold
                attributes_schema JSONB DEFAULT '[]',
                -- [{"name":"inn","type":"string","required":true}, ...]
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS object_instance(
                id SERIAL PRIMARY KEY,
                object_type_id INTEGER NOT NULL REFERENCES object_type(id)
                    ON DELETE CASCADE,
                gold_entity_id INTEGER,          -- откуда материализован (если есть)
                attributes JSONB NOT NULL DEFAULT '{}',
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_object_instance_type "
            "ON object_instance(object_type_id)")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_object_instance_gold "
            "ON object_instance(gold_entity_id) WHERE gold_entity_id IS NOT NULL")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS object_link(
                id SERIAL PRIMARY KEY,
                link_type TEXT NOT NULL,          -- "поставщик", "содержит" и т.п.
                from_instance_id INTEGER NOT NULL REFERENCES object_instance(id)
                    ON DELETE CASCADE,
                to_instance_id INTEGER NOT NULL REFERENCES object_instance(id)
                    ON DELETE CASCADE,
                attributes JSONB DEFAULT '{}',
                created DOUBLE PRECISION NOT NULL,
                UNIQUE(link_type, from_instance_id, to_instance_id)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_object_link_from "
            "ON object_link(from_instance_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_object_link_to "
            "ON object_link(to_instance_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS action_def(
                id SERIAL PRIMARY KEY,
                object_type_id INTEGER NOT NULL REFERENCES object_type(id)
                    ON DELETE CASCADE,
                name TEXT NOT NULL,             -- "correct_attribute", "approve"
                params_schema JSONB DEFAULT '[]',
                -- [{"name":"field","type":"string","required":true}, ...]
                handler TEXT NOT NULL,          -- реестр-ключ в actions.py
                created DOUBLE PRECISION,
                UNIQUE(object_type_id, name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS process_instance(
                id SERIAL PRIMARY KEY,
                process_type TEXT NOT NULL,     -- 'quarantine_correction' и т.п.
                subject_type TEXT NOT NULL,     -- 'quarantine_record'
                subject_id INTEGER NOT NULL,    -- id записи-предмета процесса
                status TEXT DEFAULT 'open',
                -- open|awaiting_task|corrected|write_back_pending|
                -- completed|cancelled|failed
                context JSONB DEFAULT '{}',     -- рабочие данные шага (снимки и т.п.)
                created_by TEXT DEFAULT 'system',   -- 'system' | 'agent:copilot' | 'human:<u>'
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_process_instance_status "
            "ON process_instance(status)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_process_instance_subject "
            "ON process_instance(subject_type, subject_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task(
                id SERIAL PRIMARY KEY,
                process_instance_id INTEGER NOT NULL REFERENCES process_instance(id)
                    ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                assignee TEXT DEFAULT '',       -- 'human:<user>' | '' (не назначено)
                status TEXT DEFAULT 'open',     -- open|done|cancelled
                result JSONB DEFAULT '{}',      -- что сделал исполнитель
                created DOUBLE PRECISION,
                completed DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_task_process "
            "ON task(process_instance_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_task_status "
            "ON task(status)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS write_back_log(
                id SERIAL PRIMARY KEY,
                process_instance_id INTEGER REFERENCES process_instance(id)
                    ON DELETE CASCADE,
                source_id INTEGER NOT NULL REFERENCES source(id),
                dataset_name TEXT NOT NULL,
                natural_key TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT DEFAULT 'pending',  -- pending|ok|error
                error TEXT DEFAULT '',
                attempts INTEGER DEFAULT 0,
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_write_back_log_process "
            "ON write_back_log(process_instance_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_interaction(
                id SERIAL PRIMARY KEY,
                actor TEXT NOT NULL,            -- 'human:<user>' — от чьего имени спрошено
                prompt TEXT NOT NULL,
                tools_called JSONB DEFAULT '[]',  -- [{"name":..,"arguments":..,"result":..}]
                result_text TEXT DEFAULT '',
                mode TEXT NOT NULL,              -- 'setup' | 'ops' (ТЗ K6)
                created DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_ai_interaction_actor "
            "ON ai_interaction(actor)")

    # -------------------------------------------------------------- source
    def upsert_source(self, name: str, kind: str,
                      config: dict[str, Any] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM source WHERE name=%s", (name,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE source SET kind=%s, config=%s, updated=%s WHERE id=%s",
                (kind, _j(config or {}), now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO source(name,kind,config,created,updated) "
            "VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (name, kind, _j(config or {}), now, now))
        return int(cur.fetchone()[0])

    _SRC_COLS = ("id", "name", "kind", "config", "created", "updated")

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._SRC_COLS)} FROM source WHERE id=%s",
            (source_id,))
        row = cur.fetchone()
        return dict(zip(self._SRC_COLS, row)) if row else None

    def get_source_by_name(self, name: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._SRC_COLS)} FROM source WHERE name=%s",
            (name,))
        row = cur.fetchone()
        return dict(zip(self._SRC_COLS, row)) if row else None

    def list_sources(self) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT {', '.join(self._SRC_COLS)} FROM source ORDER BY name")
        return [dict(zip(self._SRC_COLS, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------- dataset
    def upsert_dataset(self, source_id: int, name: str, layer: str = "bronze",
                       schema_json: list[dict[str, Any]] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM dataset WHERE source_id=%s AND name=%s AND layer=%s",
            (source_id, name, layer))
        row = cur.fetchone()
        if row:
            if schema_json is not None:
                cur.execute(
                    "UPDATE dataset SET schema_json=%s, updated=%s WHERE id=%s",
                    (_j(schema_json), now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO dataset(source_id,name,layer,schema_json,created,updated) "
            "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (source_id, name, layer, _j(schema_json or []), now, now))
        return int(cur.fetchone()[0])

    _DS_COLS = ("id", "source_id", "name", "layer", "schema_json", "row_count",
               "created", "updated")

    def get_dataset(self, dataset_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._DS_COLS)} FROM dataset WHERE id=%s",
            (dataset_id,))
        row = cur.fetchone()
        return dict(zip(self._DS_COLS, row)) if row else None

    def list_datasets(self, source_id: int | None = None) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if source_id is not None:
            cur.execute(
                f"SELECT {', '.join(self._DS_COLS)} FROM dataset "
                "WHERE source_id=%s ORDER BY id", (source_id,))
        else:
            cur.execute(f"SELECT {', '.join(self._DS_COLS)} FROM dataset ORDER BY id")
        return [dict(zip(self._DS_COLS, r)) for r in cur.fetchall()]

    def set_dataset_row_count(self, dataset_id: int, row_count: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE dataset SET row_count=%s, updated=%s WHERE id=%s",
            (row_count, self._now(), dataset_id))

    # ---------------------------------------------------------- ingest_run
    def start_ingest_run(self, source_id: int, dataset_name: str) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ingest_run(source_id,dataset_name,status,started) "
            "VALUES(%s,%s,'running',%s) RETURNING id",
            (source_id, dataset_name, self._now()))
        return int(cur.fetchone()[0])

    def finish_ingest_run(self, run_id: int, status: str,
                          records_ingested: int = 0, error: str = "") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE ingest_run SET status=%s, records_ingested=%s, error=%s, "
            "finished=%s WHERE id=%s",
            (status, records_ingested, error, self._now(), run_id))

    _IR_COLS = ("id", "source_id", "dataset_name", "status", "records_ingested",
               "error", "started", "finished")

    def get_ingest_run(self, run_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._IR_COLS)} FROM ingest_run WHERE id=%s",
            (run_id,))
        row = cur.fetchone()
        return dict(zip(self._IR_COLS, row)) if row else None

    def list_ingest_runs(self, source_id: int | None = None,
                         limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if source_id is not None:
            cur.execute(
                f"SELECT {', '.join(self._IR_COLS)} FROM ingest_run "
                "WHERE source_id=%s ORDER BY id DESC LIMIT %s", (source_id, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._IR_COLS)} FROM ingest_run "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(self._IR_COLS, r)) for r in cur.fetchall()]

    # ------------------------------------------------------- bronze_record
    def insert_bronze(self, dataset_id: int, payload: dict[str, Any],
                      source_record_id: str = "",
                      ingest_run_id: int | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO bronze_record(dataset_id,source_record_id,payload,"
            "ingest_run_id,ingested) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (dataset_id, source_record_id, _j(payload), ingest_run_id, self._now()))
        return int(cur.fetchone()[0])

    def insert_bronze_batch(self, dataset_id: int, records: list[dict[str, Any]],
                            ingest_run_id: int | None = None,
                            id_field: str = "") -> int:
        ids = []
        for rec in records:
            src_id = str(rec.get(id_field, "")) if id_field else ""
            ids.append(self.insert_bronze(dataset_id, rec, src_id, ingest_run_id))
        return len(ids)

    _BR_COLS = ("id", "dataset_id", "source_record_id", "payload",
               "ingest_run_id", "ingested")

    def get_bronze(self, bronze_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._BR_COLS)} FROM bronze_record WHERE id=%s",
            (bronze_id,))
        row = cur.fetchone()
        return dict(zip(self._BR_COLS, row)) if row else None

    def update_bronze_payload(self, bronze_id: int, payload: dict[str, Any]) -> bool:
        """Правка сырой записи ЗАДНИМ ЧИСЛОМ — используется ТОЛЬКО процессом
        корректировки карантина (человек подтвердил исправление), не общим
        ingest-путём (там Bronze append-only). Идемпотентность: повторный
        вызов с теми же данными просто перезаписывает тем же значением."""
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE bronze_record SET payload=%s WHERE id=%s",
            (_j(payload), bronze_id))
        return cur.rowcount > 0

    def list_bronze(self, dataset_id: int, limit: int = 10000) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._BR_COLS)} FROM bronze_record "
            "WHERE dataset_id=%s ORDER BY id LIMIT %s", (dataset_id, limit))
        return [dict(zip(self._BR_COLS, r)) for r in cur.fetchall()]

    def count_bronze(self, dataset_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bronze_record WHERE dataset_id=%s",
                    (dataset_id,))
        return int(cur.fetchone()[0])

    # ------------------------------------------------------- silver_record
    def insert_silver(self, dataset_id: int, bronze_record_id: int,
                      payload: dict[str, Any],
                      quality_run_id: int | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO silver_record(dataset_id,bronze_record_id,payload,"
            "quality_run_id,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (dataset_id, bronze_record_id, _j(payload), quality_run_id, self._now()))
        return int(cur.fetchone()[0])

    _SR_COLS = ("id", "dataset_id", "bronze_record_id", "payload",
               "quality_run_id", "created")

    def get_silver(self, silver_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._SR_COLS)} FROM silver_record WHERE id=%s",
            (silver_id,))
        row = cur.fetchone()
        return dict(zip(self._SR_COLS, row)) if row else None

    def list_silver(self, dataset_id: int, limit: int = 10000) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._SR_COLS)} FROM silver_record "
            "WHERE dataset_id=%s ORDER BY id LIMIT %s", (dataset_id, limit))
        return [dict(zip(self._SR_COLS, r)) for r in cur.fetchall()]

    # --------------------------------------------------- quarantine_record
    def insert_quarantine(self, dataset_id: int, bronze_record_id: int,
                          reasons: list[str],
                          quality_run_id: int | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO quarantine_record(dataset_id,bronze_record_id,reasons,"
            "quality_run_id,quarantined) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (dataset_id, bronze_record_id, _j(reasons), quality_run_id, self._now()))
        return int(cur.fetchone()[0])

    _QR_COLS = ("id", "dataset_id", "bronze_record_id", "reasons",
               "quality_run_id", "quarantined", "resolved", "resolved_at",
               "resolution")

    def get_quarantine(self, quarantine_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._QR_COLS)} FROM quarantine_record WHERE id=%s",
            (quarantine_id,))
        row = cur.fetchone()
        return dict(zip(self._QR_COLS, row)) if row else None

    def list_quarantine(self, dataset_id: int | None = None,
                        resolved: bool | None = None,
                        limit: int = 500) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        clauses, params = [], []
        if dataset_id is not None:
            clauses.append("dataset_id=%s")
            params.append(dataset_id)
        if resolved is not None:
            clauses.append("resolved=%s")
            params.append(resolved)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(
            f"SELECT {', '.join(self._QR_COLS)} FROM quarantine_record "
            f"{where} ORDER BY id DESC LIMIT %s", params)
        return [dict(zip(self._QR_COLS, r)) for r in cur.fetchall()]

    def resolve_quarantine(self, quarantine_id: int, resolution: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE quarantine_record SET resolved=TRUE, resolved_at=%s, "
            "resolution=%s WHERE id=%s",
            (self._now(), resolution, quarantine_id))
        return cur.rowcount > 0

    # ------------------------------------------------------- data_profile
    def upsert_profile(self, dataset_id: int, field_name: str,
                       total_count: int, null_count: int, distinct_count: int,
                       min_value: str = "", max_value: str = "",
                       sample_values: list[Any] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM data_profile WHERE dataset_id=%s AND field_name=%s",
            (dataset_id, field_name))
        row = cur.fetchone()
        vals = (total_count, null_count, distinct_count, min_value, max_value,
               _j(sample_values or []), now)
        if row:
            cur.execute(
                "UPDATE data_profile SET total_count=%s, null_count=%s, "
                "distinct_count=%s, min_value=%s, max_value=%s, "
                "sample_values=%s, computed=%s WHERE id=%s", (*vals, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO data_profile(dataset_id,field_name,total_count,"
            "null_count,distinct_count,min_value,max_value,sample_values,"
            "computed) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (dataset_id, field_name, *vals))
        return int(cur.fetchone()[0])

    _PROFILE_COLS = ("id", "dataset_id", "field_name", "total_count",
                     "null_count", "distinct_count", "min_value", "max_value",
                     "sample_values", "computed")

    def list_profiles(self, dataset_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PROFILE_COLS)} FROM data_profile "
            "WHERE dataset_id=%s ORDER BY field_name", (dataset_id,))
        return [dict(zip(self._PROFILE_COLS, r)) for r in cur.fetchall()]

    # -------------------------------------------------------- quality_rule
    def create_quality_rule(self, dataset_id: int, rule_type: str,
                            field_name: str = "", params: dict[str, Any] | None = None,
                            severity: str = "error") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO quality_rule(dataset_id,field_name,rule_type,params,"
            "severity,created) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (dataset_id, field_name, rule_type, _j(params or {}), severity,
             self._now()))
        return int(cur.fetchone()[0])

    _QRULE_COLS = ("id", "dataset_id", "field_name", "rule_type", "params",
                  "severity", "active", "created")

    def get_quality_rule(self, rule_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._QRULE_COLS)} FROM quality_rule WHERE id=%s",
            (rule_id,))
        row = cur.fetchone()
        return dict(zip(self._QRULE_COLS, row)) if row else None

    def list_quality_rules(self, dataset_id: int,
                           active_only: bool = True) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if active_only:
            cur.execute(
                f"SELECT {', '.join(self._QRULE_COLS)} FROM quality_rule "
                "WHERE dataset_id=%s AND active=TRUE ORDER BY id", (dataset_id,))
        else:
            cur.execute(
                f"SELECT {', '.join(self._QRULE_COLS)} FROM quality_rule "
                "WHERE dataset_id=%s ORDER BY id", (dataset_id,))
        return [dict(zip(self._QRULE_COLS, r)) for r in cur.fetchall()]

    def set_rule_active(self, rule_id: int, active: bool) -> bool:
        cur = self.conn.cursor()
        cur.execute("UPDATE quality_rule SET active=%s WHERE id=%s",
                    (active, rule_id))
        return cur.rowcount > 0

    # --------------------------------------------------------- quality_run
    def start_quality_run(self, dataset_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO quality_run(dataset_id,started) VALUES(%s,%s) "
            "RETURNING id", (dataset_id, self._now()))
        return int(cur.fetchone()[0])

    def finish_quality_run(self, run_id: int, rules_checked: int,
                           records_checked: int, violations_count: int,
                           quarantined_count: int, promoted_count: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE quality_run SET rules_checked=%s, records_checked=%s, "
            "violations_count=%s, quarantined_count=%s, promoted_count=%s, "
            "finished=%s WHERE id=%s",
            (rules_checked, records_checked, violations_count, quarantined_count,
             promoted_count, self._now(), run_id))

    _QRUN_COLS = ("id", "dataset_id", "rules_checked", "records_checked",
                 "violations_count", "quarantined_count", "promoted_count",
                 "started", "finished")

    def get_quality_run(self, run_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._QRUN_COLS)} FROM quality_run WHERE id=%s",
            (run_id,))
        row = cur.fetchone()
        return dict(zip(self._QRUN_COLS, row)) if row else None

    def list_quality_runs(self, dataset_id: int | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if dataset_id is not None:
            cur.execute(
                f"SELECT {', '.join(self._QRUN_COLS)} FROM quality_run "
                "WHERE dataset_id=%s ORDER BY id DESC LIMIT %s", (dataset_id, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._QRUN_COLS)} FROM quality_run "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(self._QRUN_COLS, r)) for r in cur.fetchall()]

    # ------------------------------------------------------ quality_result
    def insert_quality_result(self, run_id: int, rule_id: int,
                              bronze_record_id: int, passed: bool,
                              detail: str = "") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO quality_result(run_id,rule_id,bronze_record_id,"
            "passed,detail) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (run_id, rule_id, bronze_record_id, passed, detail))
        return int(cur.fetchone()[0])

    def quality_results_for_run(self, run_id: int,
                                passed: bool | None = None) -> list[dict[str, Any]]:
        cols = ("id", "run_id", "rule_id", "bronze_record_id", "passed", "detail")
        cur = self.conn.cursor()
        if passed is not None:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM quality_result "
                "WHERE run_id=%s AND passed=%s ORDER BY id", (run_id, passed))
        else:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM quality_result "
                "WHERE run_id=%s ORDER BY id", (run_id,))
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def quality_results_for_record(self, bronze_record_id: int) -> list[dict[str, Any]]:
        cols = ("id", "run_id", "rule_id", "bronze_record_id", "passed", "detail")
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(cols)} FROM quality_result "
            "WHERE bronze_record_id=%s ORDER BY id", (bronze_record_id,))
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --------------------------------------------------------- gold_entity
    def create_gold_entity(self, entity_type: str,
                           attributes: dict[str, Any]) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO gold_entity(entity_type,attributes,created,updated) "
            "VALUES(%s,%s,%s,%s) RETURNING id",
            (entity_type, _j(attributes), now, now))
        return int(cur.fetchone()[0])

    _GOLD_COLS = ("id", "entity_type", "attributes", "created", "updated")

    def get_gold_entity(self, entity_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._GOLD_COLS)} FROM gold_entity WHERE id=%s",
            (entity_id,))
        row = cur.fetchone()
        return dict(zip(self._GOLD_COLS, row)) if row else None

    def update_gold_attributes(self, entity_id: int,
                               attributes: dict[str, Any]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE gold_entity SET attributes=%s, updated=%s WHERE id=%s",
            (_j(attributes), self._now(), entity_id))

    def list_gold_entities(self, entity_type: str = "",
                           limit: int = 500) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if entity_type:
            cur.execute(
                f"SELECT {', '.join(self._GOLD_COLS)} FROM gold_entity "
                "WHERE entity_type=%s ORDER BY id LIMIT %s", (entity_type, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._GOLD_COLS)} FROM gold_entity "
                "ORDER BY id LIMIT %s", (limit,))
        return [dict(zip(self._GOLD_COLS, r)) for r in cur.fetchall()]

    # -------------------------------------------------- source_record_link
    def link_source_record(self, gold_entity_id: int, dataset_id: int,
                           silver_record_id: int, match_score: float = 1.0) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM source_record_link WHERE gold_entity_id=%s "
            "AND silver_record_id=%s", (gold_entity_id, silver_record_id))
        row = cur.fetchone()
        if row:
            return int(row[0])
        cur.execute(
            "INSERT INTO source_record_link(gold_entity_id,dataset_id,"
            "silver_record_id,match_score,created) VALUES(%s,%s,%s,%s,%s) "
            "RETURNING id",
            (gold_entity_id, dataset_id, silver_record_id, match_score, self._now()))
        return int(cur.fetchone()[0])

    def links_for_gold(self, gold_entity_id: int) -> list[dict[str, Any]]:
        cols = ("id", "gold_entity_id", "dataset_id", "silver_record_id",
               "match_score", "created")
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(cols)} FROM source_record_link "
            "WHERE gold_entity_id=%s ORDER BY id", (gold_entity_id,))
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def gold_for_silver(self, silver_record_id: int) -> int | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT gold_entity_id FROM source_record_link "
            "WHERE silver_record_id=%s LIMIT 1", (silver_record_id,))
        row = cur.fetchone()
        return int(row[0]) if row else None

    # ---------------------------------------------------------- match_candidate
    def create_match_candidate(self, entity_type: str, record_a_id: int,
                               record_b_id: int, score: float) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO match_candidate(entity_type,record_a_id,record_b_id,"
            "score,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (entity_type, record_a_id, record_b_id, score, self._now()))
        return int(cur.fetchone()[0])

    _MC_COLS = ("id", "entity_type", "record_a_id", "record_b_id", "score",
               "decision", "gold_entity_id", "decided_by", "created", "decided")

    def get_match_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._MC_COLS)} FROM match_candidate WHERE id=%s",
            (candidate_id,))
        row = cur.fetchone()
        return dict(zip(self._MC_COLS, row)) if row else None

    def list_match_candidates(self, decision: str = "",
                              limit: int = 500) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if decision:
            cur.execute(
                f"SELECT {', '.join(self._MC_COLS)} FROM match_candidate "
                "WHERE decision=%s ORDER BY id DESC LIMIT %s", (decision, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._MC_COLS)} FROM match_candidate "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(self._MC_COLS, r)) for r in cur.fetchall()]

    def set_match_decision(self, candidate_id: int, decision: str,
                           decided_by: str = "",
                           gold_entity_id: int | None = None) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE match_candidate SET decision=%s, decided_by=%s, "
            "gold_entity_id=%s, decided=%s WHERE id=%s",
            (decision, decided_by, gold_entity_id, self._now(), candidate_id))
        return cur.rowcount > 0

    # ------------------------------------------------------ survivorship
    def set_survivorship_rule(self, entity_type: str, field_name: str,
                              source_priority: list[str]) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM survivorship_rule WHERE entity_type=%s AND field_name=%s",
            (entity_type, field_name))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE survivorship_rule SET source_priority=%s WHERE id=%s",
                (_j(source_priority), row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO survivorship_rule(entity_type,field_name,"
            "source_priority,created) VALUES(%s,%s,%s,%s) RETURNING id",
            (entity_type, field_name, _j(source_priority), now))
        return int(cur.fetchone()[0])

    def survivorship_rules_for(self, entity_type: str) -> dict[str, list[str]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT field_name, source_priority FROM survivorship_rule "
            "WHERE entity_type=%s", (entity_type,))
        return {r[0]: r[1] for r in cur.fetchall()}

    # ----------------------------------------------------------- lineage
    def add_lineage_edge(self, from_asset: str, to_asset: str,
                         transform_ref: str = "", run_ref: str = "") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO lineage_edge(from_asset,to_asset,transform_ref,"
            "run_ref,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (from_asset, to_asset, transform_ref, run_ref, self._now()))
        return int(cur.fetchone()[0])

    _LIN_COLS = ("id", "from_asset", "to_asset", "transform_ref", "run_ref",
                "created")

    def lineage_edges_into(self, asset: str) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._LIN_COLS)} FROM lineage_edge "
            "WHERE to_asset=%s ORDER BY id", (asset,))
        return [dict(zip(self._LIN_COLS, r)) for r in cur.fetchall()]

    def lineage_edges_from(self, asset: str) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._LIN_COLS)} FROM lineage_edge "
            "WHERE from_asset=%s ORDER BY id", (asset,))
        return [dict(zip(self._LIN_COLS, r)) for r in cur.fetchall()]

    def trace_lineage(self, asset: str, _seen: set[str] | None = None
                      ) -> list[dict[str, Any]]:
        """Обратный обход графа lineage от `asset` к его источникам (BFS).
        Возвращает список рёбер, отсортированный от истока к `asset`."""
        seen = _seen if _seen is not None else set()
        if asset in seen:
            return []
        seen.add(asset)
        edges = self.lineage_edges_into(asset)
        result: list[dict[str, Any]] = []
        for e in edges:
            result.extend(self.trace_lineage(e["from_asset"], seen))
            result.append(e)
        return result

    # ------------------------------------------------------------- audit
    def log_audit(self, actor: str, action: str, entity_type: str,
                 entity_id: int | None, details: dict[str, Any] | None = None) -> int:
        """Добавляет запись в НЕИЗМЕНЯЕМЫЙ журнал аудита. В коде Store
        сознательно нет ни одного метода update/delete для audit_log."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,"
            "details,created) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (actor, action, entity_type, entity_id, _j(details or {}), self._now()))
        return int(cur.fetchone()[0])

    def audit_trail_for(self, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, actor, action, entity_type, entity_id, details, created "
            "FROM audit_log WHERE entity_type=%s AND entity_id=%s ORDER BY id",
            (entity_type, entity_id))
        cols = ("id", "actor", "action", "entity_type", "entity_id", "details",
               "created")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, actor, action, entity_type, entity_id, details, created "
            "FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
        cols = ("id", "actor", "action", "entity_type", "entity_id", "details",
               "created")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------------------------------------------------------- ontology
    def upsert_object_type(self, name: str, gold_entity_type: str = "",
                           attributes_schema: list[dict[str, Any]] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM object_type WHERE name=%s", (name,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE object_type SET gold_entity_type=%s, attributes_schema=%s, "
                "updated=%s WHERE id=%s",
                (gold_entity_type, _j(attributes_schema or []), now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO object_type(name,gold_entity_type,attributes_schema,"
            "created,updated) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (name, gold_entity_type, _j(attributes_schema or []), now, now))
        return int(cur.fetchone()[0])

    _OT_COLS = ("id", "name", "gold_entity_type", "attributes_schema", "created",
               "updated")

    def get_object_type(self, object_type_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._OT_COLS)} FROM object_type WHERE id=%s",
            (object_type_id,))
        row = cur.fetchone()
        return dict(zip(self._OT_COLS, row)) if row else None

    def get_object_type_by_name(self, name: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._OT_COLS)} FROM object_type WHERE name=%s",
            (name,))
        row = cur.fetchone()
        return dict(zip(self._OT_COLS, row)) if row else None

    def get_object_type_by_gold_entity_type(self, gold_entity_type: str
                                            ) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._OT_COLS)} FROM object_type "
            "WHERE gold_entity_type=%s", (gold_entity_type,))
        row = cur.fetchone()
        return dict(zip(self._OT_COLS, row)) if row else None

    def list_object_types(self) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT {', '.join(self._OT_COLS)} FROM object_type ORDER BY name")
        return [dict(zip(self._OT_COLS, r)) for r in cur.fetchall()]

    def create_object_instance(self, object_type_id: int,
                               attributes: dict[str, Any],
                               gold_entity_id: int | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO object_instance(object_type_id,gold_entity_id,attributes,"
            "created,updated) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (object_type_id, gold_entity_id, _j(attributes), now, now))
        return int(cur.fetchone()[0])

    _OI_COLS = ("id", "object_type_id", "gold_entity_id", "attributes", "created",
               "updated")

    def get_object_instance(self, instance_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._OI_COLS)} FROM object_instance WHERE id=%s",
            (instance_id,))
        row = cur.fetchone()
        return dict(zip(self._OI_COLS, row)) if row else None

    def get_object_instance_by_gold(self, gold_entity_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._OI_COLS)} FROM object_instance "
            "WHERE gold_entity_id=%s", (gold_entity_id,))
        row = cur.fetchone()
        return dict(zip(self._OI_COLS, row)) if row else None

    def update_object_instance_attributes(self, instance_id: int,
                                          attributes: dict[str, Any]) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE object_instance SET attributes=%s, updated=%s WHERE id=%s",
            (_j(attributes), self._now(), instance_id))

    def list_object_instances(self, object_type_id: int | None = None,
                              limit: int = 500) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if object_type_id is not None:
            cur.execute(
                f"SELECT {', '.join(self._OI_COLS)} FROM object_instance "
                "WHERE object_type_id=%s ORDER BY id LIMIT %s",
                (object_type_id, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._OI_COLS)} FROM object_instance "
                "ORDER BY id LIMIT %s", (limit,))
        return [dict(zip(self._OI_COLS, r)) for r in cur.fetchall()]

    def create_object_link(self, link_type: str, from_instance_id: int,
                           to_instance_id: int,
                           attributes: dict[str, Any] | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM object_link WHERE link_type=%s AND from_instance_id=%s "
            "AND to_instance_id=%s", (link_type, from_instance_id, to_instance_id))
        row = cur.fetchone()
        if row:
            return int(row[0])
        cur.execute(
            "INSERT INTO object_link(link_type,from_instance_id,to_instance_id,"
            "attributes,created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (link_type, from_instance_id, to_instance_id,
             _j(attributes or {}), self._now()))
        return int(cur.fetchone()[0])

    _OL_COLS = ("id", "link_type", "from_instance_id", "to_instance_id",
               "attributes", "created")

    def links_from(self, instance_id: int, link_type: str = "") -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if link_type:
            cur.execute(
                f"SELECT {', '.join(self._OL_COLS)} FROM object_link "
                "WHERE from_instance_id=%s AND link_type=%s",
                (instance_id, link_type))
        else:
            cur.execute(
                f"SELECT {', '.join(self._OL_COLS)} FROM object_link "
                "WHERE from_instance_id=%s", (instance_id,))
        return [dict(zip(self._OL_COLS, r)) for r in cur.fetchall()]

    def links_to(self, instance_id: int, link_type: str = "") -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if link_type:
            cur.execute(
                f"SELECT {', '.join(self._OL_COLS)} FROM object_link "
                "WHERE to_instance_id=%s AND link_type=%s",
                (instance_id, link_type))
        else:
            cur.execute(
                f"SELECT {', '.join(self._OL_COLS)} FROM object_link "
                "WHERE to_instance_id=%s", (instance_id,))
        return [dict(zip(self._OL_COLS, r)) for r in cur.fetchall()]

    def create_action_def(self, object_type_id: int, name: str, handler: str,
                          params_schema: list[dict[str, Any]] | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM action_def WHERE object_type_id=%s AND name=%s",
            (object_type_id, name))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE action_def SET handler=%s, params_schema=%s WHERE id=%s",
                (handler, _j(params_schema or []), row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO action_def(object_type_id,name,params_schema,handler,"
            "created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (object_type_id, name, _j(params_schema or []), handler, self._now()))
        return int(cur.fetchone()[0])

    _AD_COLS = ("id", "object_type_id", "name", "params_schema", "handler", "created")

    def get_action_def(self, action_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._AD_COLS)} FROM action_def WHERE id=%s",
            (action_id,))
        row = cur.fetchone()
        return dict(zip(self._AD_COLS, row)) if row else None

    def get_action_def_by_name(self, object_type_id: int, name: str
                               ) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._AD_COLS)} FROM action_def "
            "WHERE object_type_id=%s AND name=%s", (object_type_id, name))
        row = cur.fetchone()
        return dict(zip(self._AD_COLS, row)) if row else None

    def list_action_defs(self, object_type_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._AD_COLS)} FROM action_def "
            "WHERE object_type_id=%s ORDER BY name", (object_type_id,))
        return [dict(zip(self._AD_COLS, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ process
    def create_process_instance(self, process_type: str, subject_type: str,
                                subject_id: int, context: dict[str, Any] | None = None,
                                created_by: str = "system") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO process_instance(process_type,subject_type,subject_id,"
            "context,created_by,created,updated) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (process_type, subject_type, subject_id, _j(context or {}), created_by,
             now, now))
        return int(cur.fetchone()[0])

    _PI_COLS = ("id", "process_type", "subject_type", "subject_id", "status",
               "context", "created_by", "created", "updated")

    def get_process_instance(self, process_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PI_COLS)} FROM process_instance WHERE id=%s",
            (process_id,))
        row = cur.fetchone()
        return dict(zip(self._PI_COLS, row)) if row else None

    def set_process_status(self, process_id: int, status: str,
                           context: dict[str, Any] | None = None) -> bool:
        cur = self.conn.cursor()
        if context is not None:
            cur.execute(
                "UPDATE process_instance SET status=%s, context=%s, updated=%s "
                "WHERE id=%s", (status, _j(context), self._now(), process_id))
        else:
            cur.execute(
                "UPDATE process_instance SET status=%s, updated=%s WHERE id=%s",
                (status, self._now(), process_id))
        return cur.rowcount > 0

    def list_process_instances(self, process_type: str = "", status: str = "",
                               limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        clauses, params = [], []
        if process_type:
            clauses.append("process_type=%s")
            params.append(process_type)
        if status:
            clauses.append("status=%s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(
            f"SELECT {', '.join(self._PI_COLS)} FROM process_instance "
            f"{where} ORDER BY id DESC LIMIT %s", params)
        return [dict(zip(self._PI_COLS, r)) for r in cur.fetchall()]

    def find_open_process_for_subject(self, subject_type: str, subject_id: int
                                      ) -> dict[str, Any] | None:
        """Находит НЕзавершённый процесс для предмета — используется для
        идемпотентности запуска (повторный запуск на том же предмете не
        плодит параллельные процессы)."""
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PI_COLS)} FROM process_instance "
            "WHERE subject_type=%s AND subject_id=%s AND status NOT IN "
            "('completed','cancelled','failed') ORDER BY id DESC LIMIT 1",
            (subject_type, subject_id))
        row = cur.fetchone()
        return dict(zip(self._PI_COLS, row)) if row else None

    # --------------------------------------------------------------- task
    def create_task(self, process_instance_id: int, title: str,
                    description: str = "", assignee: str = "") -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO task(process_instance_id,title,description,assignee,"
            "created) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (process_instance_id, title, description, assignee, self._now()))
        return int(cur.fetchone()[0])

    _TASK_COLS = ("id", "process_instance_id", "title", "description", "assignee",
                 "status", "result", "created", "completed")

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._TASK_COLS)} FROM task WHERE id=%s", (task_id,))
        row = cur.fetchone()
        return dict(zip(self._TASK_COLS, row)) if row else None

    def complete_task(self, task_id: int, result: dict[str, Any] | None = None) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE task SET status='done', result=%s, completed=%s WHERE id=%s",
            (_j(result or {}), self._now(), task_id))
        return cur.rowcount > 0

    def cancel_task(self, task_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE task SET status='cancelled', completed=%s WHERE id=%s",
            (self._now(), task_id))
        return cur.rowcount > 0

    def list_tasks(self, process_instance_id: int | None = None, status: str = "",
                  assignee: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        clauses, params = [], []
        if process_instance_id is not None:
            clauses.append("process_instance_id=%s")
            params.append(process_instance_id)
        if status:
            clauses.append("status=%s")
            params.append(status)
        if assignee:
            clauses.append("assignee=%s")
            params.append(assignee)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(
            f"SELECT {', '.join(self._TASK_COLS)} FROM task {where} "
            "ORDER BY id DESC LIMIT %s", params)
        return [dict(zip(self._TASK_COLS, r)) for r in cur.fetchall()]

    # -------------------------------------------------------- write_back
    def write_back_log_attempt(self, process_instance_id: int | None, source_id: int,
                               dataset_name: str, natural_key: str,
                               idempotency_key: str) -> tuple[int, bool]:
        """Регистрирует попытку write-back. Возвращает (id, is_new) —
        is_new=False означает, что запись с таким idempotency_key уже
        была (тот же принцип, что onec_log_attempt в erp_ai)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, status FROM write_back_log WHERE idempotency_key=%s",
            (idempotency_key,))
        row = cur.fetchone()
        if row:
            return int(row[0]), False
        now = self._now()
        cur.execute(
            "INSERT INTO write_back_log(process_instance_id,source_id,dataset_name,"
            "natural_key,idempotency_key,created,updated) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (process_instance_id, source_id, dataset_name, natural_key,
             idempotency_key, now, now))
        return int(cur.fetchone()[0]), True

    def write_back_mark_result(self, log_id: int, status: str, error: str = "") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE write_back_log SET status=%s, error=%s, attempts=attempts+1, "
            "updated=%s WHERE id=%s", (status, error, self._now(), log_id))

    _WBL_COLS = ("id", "process_instance_id", "source_id", "dataset_name",
                "natural_key", "idempotency_key", "status", "error", "attempts",
                "created", "updated")

    def get_write_back_log(self, log_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._WBL_COLS)} FROM write_back_log WHERE id=%s",
            (log_id,))
        row = cur.fetchone()
        return dict(zip(self._WBL_COLS, row)) if row else None

    def list_write_back_log(self, process_instance_id: int | None = None,
                            status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        clauses, params = [], []
        if process_instance_id is not None:
            clauses.append("process_instance_id=%s")
            params.append(process_instance_id)
        if status:
            clauses.append("status=%s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cur.execute(
            f"SELECT {', '.join(self._WBL_COLS)} FROM write_back_log {where} "
            "ORDER BY id DESC LIMIT %s", params)
        return [dict(zip(self._WBL_COLS, r)) for r in cur.fetchall()]

    # -------------------------------------------------------- ai_interaction
    def log_ai_interaction(self, actor: str, prompt: str, mode: str,
                           tools_called: list[dict[str, Any]] | None = None,
                           result_text: str = "") -> int:
        """Добавляет запись в НЕИЗМЕНЯЕМЫЙ журнал взаимодействий с AI
        Copilot (ТЗ §4: "AiInteraction... аудит AI"). В коде Store
        сознательно нет ни одного метода update/delete для этой таблицы."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ai_interaction(actor,prompt,tools_called,result_text,"
            "mode,created) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (actor, prompt, _j(tools_called or []), result_text, mode, self._now()))
        return int(cur.fetchone()[0])

    def list_ai_interactions(self, actor: str = "", limit: int = 200
                             ) -> list[dict[str, Any]]:
        cols = ("id", "actor", "prompt", "tools_called", "result_text", "mode",
                "created")
        cur = self.conn.cursor()
        if actor:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM ai_interaction WHERE actor=%s "
                "ORDER BY id DESC LIMIT %s", (actor, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM ai_interaction "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ metrics
    def dashboard_stats(self) -> dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM source")
        sources = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM dataset")
        datasets = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM bronze_record")
        bronze = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM silver_record")
        silver = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM quarantine_record WHERE resolved=FALSE")
        quarantine_open = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM gold_entity")
        gold = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM match_candidate WHERE decision='pending'")
        pending_matches = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM audit_log")
        audit_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM object_type")
        object_types = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM object_instance")
        object_instances = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM process_instance WHERE status NOT IN "
            "('completed','cancelled','failed')")
        open_processes = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM task WHERE status='open'")
        open_tasks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ai_interaction")
        ai_interactions = cur.fetchone()[0]
        return {
            "sources": int(sources), "datasets": int(datasets),
            "bronze_records": int(bronze), "silver_records": int(silver),
            "quarantine_open": int(quarantine_open), "gold_entities": int(gold),
            "pending_matches": int(pending_matches),
            "audit_entries": int(audit_count),
            "object_types": int(object_types),
            "object_instances": int(object_instances),
            "open_processes": int(open_processes),
            "open_tasks": int(open_tasks),
            "ai_interactions": int(ai_interactions),
        }
