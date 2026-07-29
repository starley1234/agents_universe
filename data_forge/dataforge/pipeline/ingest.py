"""Ingest-пайплайн: Connector -> Bronze + lineage-рёбра (ТЗ §2, medallion).

Не Dagster (решение пользователя: минимум зависимостей) — простой
детерминированный Python-оркестратор одного шага "источник -> Bronze".
Дальше по конвейеру идут независимо вызываемые `quality.engine.
run_quality_checks()` (Bronze -> Silver/карантин) и `mdm.matching`
(Silver -> Gold) — каждый шаг САМ пишет свои рёбра lineage, поэтому
полная цепочка восстанавливается через `Store.trace_lineage()` (K4)
без централизованного "движка оркестрации лишнего слоя".

Каждый вызов `ingest_full()`/`ingest_changes()` создаёт `ingest_run`
(для наблюдаемости — сколько записей, статус, ошибка) и одно ребро
lineage "source:<name>:<dataset>" -> "bronze:dataset:<id>".
"""
from __future__ import annotations

from typing import Any

from ..connectors.base import Connector, Cursor
from ..db.store import Store


class PipelineError(RuntimeError):
    """Ошибка ingest-пайплайна: коннектор бросил исключение и т.п."""


def ingest_full(store: Store, source_id: int, source_name: str,
                connector: Connector, dataset_name: str,
                id_field: str = "") -> dict[str, Any]:
    """Полная выгрузка датасета (batch, FR-1.2) в Bronze. Каждый вызов
    ДОБАВЛЯЕТ новые bronze_record (append-only Bronze — история
    выгрузок сохраняется, это осознанное решение ради lineage: видно,
    какая именно выгрузка принесла конкретное сырое значение)."""
    dataset_id = store.upsert_dataset(source_id, dataset_name, layer="bronze")
    run_id = store.start_ingest_run(source_id, dataset_name)
    try:
        records = list(connector.read_full(dataset_name))
        count = store.insert_bronze_batch(dataset_id, records, run_id, id_field)
        store.set_dataset_row_count(dataset_id, store.count_bronze(dataset_id))
        store.finish_ingest_run(run_id, "ok", records_ingested=count)
        store.add_lineage_edge(
            from_asset=f"source:{source_name}:{dataset_name}",
            to_asset=f"bronze:dataset:{dataset_id}",
            transform_ref="ingest_full", run_ref=f"ingest_run:{run_id}")
        store.log_audit("system:pipeline", "ingest_full", "dataset", dataset_id,
                        {"source": source_name, "records": count})
        return {"dataset_id": dataset_id, "run_id": run_id, "status": "ok",
               "records_ingested": count}
    except Exception as exc:  # noqa: BLE001 - фиксируем ЛЮБОЙ сбой коннектора
        store.finish_ingest_run(run_id, "error", error=str(exc))
        raise PipelineError(f"Ошибка полной выгрузки '{dataset_name}': {exc}") from exc


def ingest_changes(store: Store, source_id: int, source_name: str,
                   connector: Connector, dataset_name: str,
                   cursor_value: str = "", id_field: str = "") -> dict[str, Any]:
    """Инкрементальная выгрузка (CDC-подобная/filter_by_date, FR-1.4) —
    для источников, чей коннектор реализует `read_changes`. Возвращает
    новый курсор — вызывающий код (или AI Copilot/оператор) обязан
    сохранить его для следующего вызова (в этой сборке — на стороне
    клиента API, отдельного механизма хранения курсоров в БД нет, см.
    README.md)."""
    dataset_id = store.upsert_dataset(source_id, dataset_name, layer="bronze")
    run_id = store.start_ingest_run(source_id, dataset_name)
    try:
        batch = connector.read_changes(dataset_name, Cursor(value=cursor_value))
        count = store.insert_bronze_batch(dataset_id, batch.records, run_id, id_field)
        store.set_dataset_row_count(dataset_id, store.count_bronze(dataset_id))
        store.finish_ingest_run(run_id, "ok", records_ingested=count)
        store.add_lineage_edge(
            from_asset=f"source:{source_name}:{dataset_name}",
            to_asset=f"bronze:dataset:{dataset_id}",
            transform_ref="ingest_changes", run_ref=f"ingest_run:{run_id}")
        store.log_audit("system:pipeline", "ingest_changes", "dataset", dataset_id,
                        {"source": source_name, "records": count,
                         "next_cursor": batch.next_cursor.value})
        return {"dataset_id": dataset_id, "run_id": run_id, "status": "ok",
               "records_ingested": count, "next_cursor": batch.next_cursor.value,
               "deletes": batch.deletes}
    except Exception as exc:  # noqa: BLE001
        store.finish_ingest_run(run_id, "error", error=str(exc))
        raise PipelineError(
            f"Ошибка инкрементальной выгрузки '{dataset_name}': {exc}") from exc


def promote_quality(store: Store, dataset_id: int) -> dict[str, Any]:
    """Bronze -> Silver/карантин через Quality Engine + запись lineage
    для каждой продвинутой записи (используем silver_record_id как
    гранулярность рёбер — по любому значению в Silver можно проследить
    ИМЕННО ту Bronze-запись и прогон качества, из которых оно взято)."""
    from ..quality.engine import run_quality_checks
    result = run_quality_checks(store, dataset_id)
    for silver in store.list_silver(dataset_id):
        if silver["quality_run_id"] != result["run_id"]:
            continue    # рёбра для этой записи уже добавлены в прошлый прогон
        store.add_lineage_edge(
            from_asset=f"bronze:record:{silver['bronze_record_id']}",
            to_asset=f"silver:record:{silver['id']}",
            transform_ref="quality_engine.run_quality_checks",
            run_ref=f"quality_run:{result['run_id']}")
    store.log_audit("system:pipeline", "promote_quality", "dataset", dataset_id,
                    result)
    return result
