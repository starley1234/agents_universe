"""HTTP API DataForge на FastAPI.

Эндпоинты REST (ТЗ §4.2 — только REST в этой сборке, без GraphQL, см.
README.md "Честная граница объёма"):

  GET  /health                          — жив ли сервис (без токена)
  GET  /dashboard, /                    — веб-интерфейс

  GET  /v1/sources                      — список источников
  POST /v1/sources                      — зарегистрировать источник
  POST /v1/sources/{id}/discover        — профилирование схемы источника (FR-1.5)
  POST /v1/sources/{id}/ingest/full     — полная выгрузка датасета в Bronze
  POST /v1/sources/{id}/ingest/changes  — инкрементальная выгрузка

  GET  /v1/datasets                     — список датасетов
  GET  /v1/datasets/{id}                — детали + профиль полей
  POST /v1/datasets/{id}/profile        — профилирование Bronze (K2)
  GET  /v1/datasets/{id}/bronze         — сырые записи (постранично)
  GET  /v1/datasets/{id}/silver         — очищенные записи

  GET  /v1/datasets/{id}/quality-rules  — правила качества
  POST /v1/datasets/{id}/quality-rules  — создать правило
  POST /v1/datasets/{id}/quality-run    — прогнать проверки (Bronze->Silver/карантин)
  GET  /v1/datasets/{id}/quarantine     — карантин датасета
  POST /v1/quarantine/{id}/resolve      — отметить решённым

  POST /v1/mdm/match                    — найти кандидатов на дубли (K1)
  GET  /v1/mdm/candidates               — очередь stewardship
  POST /v1/mdm/candidates/{id}/merge    — подтвердить слияние -> golden record
  POST /v1/mdm/candidates/{id}/reject   — отклонить кандидата
  POST /v1/mdm/auto-merge               — авто-слияние выше порога (guardrail)
  POST /v1/mdm/survivorship             — задать приоритет источников для поля

  GET  /v1/gold                         — золотые записи
  GET  /v1/gold/{id}                    — детали + связанные исходные записи

  GET  /v1/ontology/types               — типы бизнес-объектов
  POST /v1/ontology/types               — определить тип объекта (ТЗ §3.2)
  GET  /v1/ontology/types/{id}          — детали + определённые actions
  POST /v1/ontology/types/{id}/actions  — определить действие для типа
  POST /v1/ontology/materialize         — материализовать объект из golden record
  GET  /v1/ontology/instances           — список экземпляров объектов
  GET  /v1/ontology/instances/{id}      — карточка объекта: связи + источники
  POST /v1/ontology/links               — связать два объекта
  POST /v1/ontology/instances/{id}/actions — выполнить действие над объектом

  GET  /v1/lineage/trace                — цепочка lineage по asset (K4)

  GET  /v1/audit                        — журнал аудита (неизменяемый)
  GET  /v1/audit/{entity_type}/{entity_id}
  GET  /v1/dashboard/stats

Токен: если задан FORGE_API_TOKEN, все маршруты кроме /health и
/dashboard требуют заголовок Authorization: Bearer <token>.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..config import Config
from ..connectors.base import ConnectorError
from ..connectors.factory import build_connector
from ..db.store import Store, StoreError
from ..mdm import matching as mdm
from ..ontology import model as ontology
from ..ontology.actions import ActionError, execute_action
from ..pipeline import ingest as pipeline
from ..quality import engine as quality

app = FastAPI(title="DataForge", version="0.1.0")

_STATE: dict[str, Any] = {"cfg": None, "token": None}


def configure(cfg: Config, token: str | None = None) -> None:
    _STATE["cfg"] = cfg
    _STATE["token"] = token or os.getenv("FORGE_API_TOKEN") or cfg.api_token or None


def get_effective_token() -> str | None:
    return _STATE.get("token")


def get_cfg() -> Config:
    if _STATE["cfg"] is None:
        raise HTTPException(500, "Сервер не сконфигурирован (вызовите configure())")
    return _STATE["cfg"]


def get_store(cfg: Config = Depends(get_cfg)) -> Store:
    store = Store(cfg.require_dsn())
    try:
        yield store
    finally:
        store.close()


async def require_auth(request: Request) -> None:
    token = _STATE.get("token")
    if not token:
        return
    header = request.headers.get("Authorization", "")
    if header != f"Bearer {token}":
        raise HTTPException(401, "Нужен заголовок Authorization: Bearer <token>")


@app.exception_handler(StoreError)
async def _store_error_handler(request: Request, exc: StoreError):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.exception_handler(ConnectorError)
async def _connector_error_handler(request: Request, exc: ConnectorError):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.exception_handler(pipeline.PipelineError)
async def _pipeline_error_handler(request: Request, exc: pipeline.PipelineError):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.exception_handler(quality.QualityError)
async def _quality_error_handler(request: Request, exc: quality.QualityError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(mdm.MdmError)
async def _mdm_error_handler(request: Request, exc: mdm.MdmError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(ontology.OntologyError)
async def _ontology_error_handler(request: Request, exc: ontology.OntologyError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(ActionError)
async def _action_error_handler(request: Request, exc: ActionError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


# ---------------------------------------------------------------- health
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "dataforge"}


_DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "web" / "dashboard.html"


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    if not _DASHBOARD_PATH.exists():
        return "<h1>DataForge</h1><p>dashboard.html не найден</p>"
    return _DASHBOARD_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------- models
class SourceIn(BaseModel):
    name: str
    kind: str  # file | sql | onec_odata
    config: dict[str, Any] = Field(default_factory=dict)


class IngestFullIn(BaseModel):
    dataset: str
    id_field: str = ""


class IngestChangesIn(BaseModel):
    dataset: str
    cursor: str = ""
    id_field: str = ""


class QualityRuleIn(BaseModel):
    rule_type: str
    field_name: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    severity: str = "error"


class QuarantineResolveIn(BaseModel):
    resolution: str = ""


class MatchIn(BaseModel):
    entity_type: str
    dataset_id: int
    fields: list[str]
    weights: dict[str, float] | None = None
    review_threshold: float | None = None


class DecisionIn(BaseModel):
    decided_by: str
    reason: str = ""


class AutoMergeIn(BaseModel):
    entity_type: str
    auto_threshold: float | None = None
    decided_by: str = "system:mdm_auto"


class SurvivorshipIn(BaseModel):
    entity_type: str
    field_name: str
    source_priority: list[str]


class ObjectTypeIn(BaseModel):
    name: str
    gold_entity_type: str = ""
    attributes_schema: list[dict[str, Any]] = Field(default_factory=list)


class MaterializeIn(BaseModel):
    gold_entity_id: int
    strict: bool = False


class LinkInstancesIn(BaseModel):
    link_type: str
    from_instance_id: int
    to_instance_id: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:api"


class ActionDefIn(BaseModel):
    name: str
    handler: str
    params_schema: list[dict[str, Any]] = Field(default_factory=list)


class ExecuteActionIn(BaseModel):
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    actor: str


# --------------------------------------------------------------- sources
@app.get("/v1/sources", dependencies=[Depends(require_auth)])
async def list_sources(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_sources()


@app.post("/v1/sources", dependencies=[Depends(require_auth)])
async def create_source(body: SourceIn, store: Store = Depends(get_store)
                        ) -> dict[str, Any]:
    sid = store.upsert_source(body.name, body.kind, body.config)
    store.log_audit("human:api", "create_source", "source", sid,
                    {"name": body.name, "kind": body.kind})
    return store.get_source(sid)


@app.post("/v1/sources/{source_id}/discover", dependencies=[Depends(require_auth)])
async def discover_source(source_id: int, cfg: Config = Depends(get_cfg),
                          store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    source = store.get_source(source_id)
    if not source:
        raise HTTPException(404, f"Источник #{source_id} не найден")
    connector = build_connector(source, cfg.onec_base_url, cfg.onec_api_key,
                                cfg.onec_timeout)
    schemas = connector.discover()
    for schema in schemas:
        store.upsert_dataset(source_id, schema.name, layer="bronze",
                             schema_json=schema.to_dict()["fields"])
    return [s.to_dict() for s in schemas]


@app.post("/v1/sources/{source_id}/ingest/full", dependencies=[Depends(require_auth)])
async def ingest_full_route(source_id: int, body: IngestFullIn,
                            cfg: Config = Depends(get_cfg),
                            store: Store = Depends(get_store)) -> dict[str, Any]:
    source = store.get_source(source_id)
    if not source:
        raise HTTPException(404, f"Источник #{source_id} не найден")
    connector = build_connector(source, cfg.onec_base_url, cfg.onec_api_key,
                                cfg.onec_timeout)
    return pipeline.ingest_full(store, source_id, source["name"], connector,
                                body.dataset, body.id_field)


@app.post("/v1/sources/{source_id}/ingest/changes", dependencies=[Depends(require_auth)])
async def ingest_changes_route(source_id: int, body: IngestChangesIn,
                               cfg: Config = Depends(get_cfg),
                               store: Store = Depends(get_store)) -> dict[str, Any]:
    source = store.get_source(source_id)
    if not source:
        raise HTTPException(404, f"Источник #{source_id} не найден")
    connector = build_connector(source, cfg.onec_base_url, cfg.onec_api_key,
                                cfg.onec_timeout)
    return pipeline.ingest_changes(store, source_id, source["name"], connector,
                                   body.dataset, body.cursor, body.id_field)


# -------------------------------------------------------------- datasets
@app.get("/v1/datasets", dependencies=[Depends(require_auth)])
async def list_datasets(source_id: int | None = None,
                        store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_datasets(source_id)


@app.get("/v1/datasets/{dataset_id}", dependencies=[Depends(require_auth)])
async def get_dataset(dataset_id: int, store: Store = Depends(get_store)
                      ) -> dict[str, Any]:
    ds = store.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, f"Датасет #{dataset_id} не найден")
    ds["profiles"] = store.list_profiles(dataset_id)
    return ds


@app.post("/v1/datasets/{dataset_id}/profile", dependencies=[Depends(require_auth)])
async def profile_dataset_route(dataset_id: int, store: Store = Depends(get_store)
                                ) -> list[dict[str, Any]]:
    if not store.get_dataset(dataset_id):
        raise HTTPException(404, f"Датасет #{dataset_id} не найден")
    return quality.profile_dataset(store, dataset_id)


@app.get("/v1/datasets/{dataset_id}/bronze", dependencies=[Depends(require_auth)])
async def bronze_records(dataset_id: int, limit: int = 200,
                         store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_bronze(dataset_id, limit)


@app.get("/v1/datasets/{dataset_id}/silver", dependencies=[Depends(require_auth)])
async def silver_records(dataset_id: int, limit: int = 200,
                         store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_silver(dataset_id, limit)


# ---------------------------------------------------------------- quality
@app.get("/v1/datasets/{dataset_id}/quality-rules", dependencies=[Depends(require_auth)])
async def list_rules(dataset_id: int, store: Store = Depends(get_store)
                     ) -> list[dict[str, Any]]:
    return store.list_quality_rules(dataset_id)


@app.post("/v1/datasets/{dataset_id}/quality-rules", dependencies=[Depends(require_auth)])
async def create_rule(dataset_id: int, body: QualityRuleIn,
                      store: Store = Depends(get_store)) -> dict[str, Any]:
    if not store.get_dataset(dataset_id):
        raise HTTPException(404, f"Датасет #{dataset_id} не найден")
    rid = store.create_quality_rule(dataset_id, body.rule_type, body.field_name,
                                    body.params, body.severity)
    store.log_audit("human:api", "create_quality_rule", "quality_rule", rid,
                    body.model_dump())
    return store.get_quality_rule(rid)


@app.post("/v1/datasets/{dataset_id}/quality-run", dependencies=[Depends(require_auth)])
async def run_quality(dataset_id: int, store: Store = Depends(get_store)
                      ) -> dict[str, Any]:
    if not store.get_dataset(dataset_id):
        raise HTTPException(404, f"Датасет #{dataset_id} не найден")
    return pipeline.promote_quality(store, dataset_id)


@app.get("/v1/datasets/{dataset_id}/quarantine", dependencies=[Depends(require_auth)])
async def dataset_quarantine(dataset_id: int, resolved: bool | None = None,
                             store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_quarantine(dataset_id, resolved)


@app.post("/v1/quarantine/{quarantine_id}/resolve", dependencies=[Depends(require_auth)])
async def resolve_quarantine_route(quarantine_id: int, body: QuarantineResolveIn,
                                   store: Store = Depends(get_store)) -> dict[str, Any]:
    ok = store.resolve_quarantine(quarantine_id, body.resolution)
    if not ok:
        raise HTTPException(404, f"Запись карантина #{quarantine_id} не найдена")
    store.log_audit("human:api", "resolve_quarantine", "quarantine_record",
                    quarantine_id, {"resolution": body.resolution})
    return {"ok": True}


# ------------------------------------------------------------------- mdm
@app.post("/v1/mdm/match", dependencies=[Depends(require_auth)])
async def mdm_match(body: MatchIn, cfg: Config = Depends(get_cfg),
                    store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    threshold = (body.review_threshold if body.review_threshold is not None
                else cfg.match_review_threshold)
    return mdm.find_match_candidates(store, body.entity_type, body.dataset_id,
                                     body.fields, threshold, body.weights)


@app.get("/v1/mdm/candidates", dependencies=[Depends(require_auth)])
async def mdm_candidates(decision: str = "", store: Store = Depends(get_store)
                         ) -> list[dict[str, Any]]:
    return store.list_match_candidates(decision)


@app.post("/v1/mdm/candidates/{candidate_id}/merge", dependencies=[Depends(require_auth)])
async def mdm_merge(candidate_id: int, body: DecisionIn,
                    store: Store = Depends(get_store)) -> dict[str, Any]:
    cand = store.get_match_candidate(candidate_id)
    if not cand:
        raise HTTPException(404, f"Кандидат #{candidate_id} не найден")
    gold_id = mdm.merge_candidate(store, candidate_id, cand["entity_type"],
                                  body.decided_by, auto=False)
    return store.get_gold_entity(gold_id)


@app.post("/v1/mdm/candidates/{candidate_id}/reject", dependencies=[Depends(require_auth)])
async def mdm_reject(candidate_id: int, body: DecisionIn,
                     store: Store = Depends(get_store)) -> dict[str, Any]:
    ok = mdm.reject_candidate(store, candidate_id, body.decided_by, body.reason)
    return {"ok": ok}


@app.post("/v1/mdm/auto-merge", dependencies=[Depends(require_auth)])
async def mdm_auto_merge(body: AutoMergeIn, cfg: Config = Depends(get_cfg),
                         store: Store = Depends(get_store)) -> dict[str, Any]:
    threshold = (body.auto_threshold if body.auto_threshold is not None
                else cfg.match_auto_threshold)
    merged = mdm.auto_merge_high_confidence(store, body.entity_type, threshold,
                                            body.decided_by)
    return {"merged_gold_ids": merged, "count": len(merged)}


@app.post("/v1/mdm/survivorship", dependencies=[Depends(require_auth)])
async def mdm_survivorship(body: SurvivorshipIn, store: Store = Depends(get_store)
                           ) -> dict[str, Any]:
    rid = store.set_survivorship_rule(body.entity_type, body.field_name,
                                      body.source_priority)
    return {"id": rid, **body.model_dump()}


# ----------------------------------------------------------------- gold
@app.get("/v1/gold", dependencies=[Depends(require_auth)])
async def list_gold(entity_type: str = "", store: Store = Depends(get_store)
                    ) -> list[dict[str, Any]]:
    return store.list_gold_entities(entity_type)


@app.get("/v1/gold/{gold_id}", dependencies=[Depends(require_auth)])
async def get_gold(gold_id: int, store: Store = Depends(get_store)) -> dict[str, Any]:
    gold = store.get_gold_entity(gold_id)
    if not gold:
        raise HTTPException(404, f"Золотая запись #{gold_id} не найдена")
    gold["links"] = store.links_for_gold(gold_id)
    return gold


# ------------------------------------------------------------- ontology
@app.get("/v1/ontology/types", dependencies=[Depends(require_auth)])
async def list_object_types(store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_object_types()


@app.post("/v1/ontology/types", dependencies=[Depends(require_auth)])
async def create_object_type(body: ObjectTypeIn, store: Store = Depends(get_store)
                             ) -> dict[str, Any]:
    return ontology.define_object_type(store, body.name, body.gold_entity_type,
                                       body.attributes_schema)


@app.get("/v1/ontology/types/{object_type_id}", dependencies=[Depends(require_auth)])
async def get_object_type(object_type_id: int, store: Store = Depends(get_store)
                          ) -> dict[str, Any]:
    ot = store.get_object_type(object_type_id)
    if not ot:
        raise HTTPException(404, f"ObjectType #{object_type_id} не найден")
    ot["action_defs"] = store.list_action_defs(object_type_id)
    return ot


@app.post("/v1/ontology/types/{object_type_id}/actions", dependencies=[Depends(require_auth)])
async def create_action_def_route(object_type_id: int, body: ActionDefIn,
                                  store: Store = Depends(get_store)) -> dict[str, Any]:
    if not store.get_object_type(object_type_id):
        raise HTTPException(404, f"ObjectType #{object_type_id} не найден")
    aid = store.create_action_def(object_type_id, body.name, body.handler,
                                  body.params_schema)
    return store.get_action_def(aid)


@app.post("/v1/ontology/materialize", dependencies=[Depends(require_auth)])
async def materialize_route(body: MaterializeIn, store: Store = Depends(get_store)
                            ) -> dict[str, Any]:
    return ontology.materialize_from_gold(store, body.gold_entity_id, body.strict)


@app.get("/v1/ontology/instances", dependencies=[Depends(require_auth)])
async def list_object_instances(object_type_id: int | None = None,
                                store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.list_object_instances(object_type_id)


@app.get("/v1/ontology/instances/{instance_id}", dependencies=[Depends(require_auth)])
async def get_object_instance_route(instance_id: int, store: Store = Depends(get_store)
                                    ) -> dict[str, Any]:
    return ontology.instance_neighborhood(store, instance_id)


@app.post("/v1/ontology/links", dependencies=[Depends(require_auth)])
async def link_instances_route(body: LinkInstancesIn, store: Store = Depends(get_store)
                               ) -> dict[str, Any]:
    return ontology.link_instances(store, body.link_type, body.from_instance_id,
                                   body.to_instance_id, body.attributes, body.actor)


@app.post("/v1/ontology/instances/{instance_id}/actions", dependencies=[Depends(require_auth)])
async def execute_action_route(instance_id: int, body: ExecuteActionIn,
                               store: Store = Depends(get_store)) -> dict[str, Any]:
    return execute_action(store, instance_id, body.action, body.params, body.actor)


# -------------------------------------------------------------- lineage
@app.get("/v1/lineage/trace", dependencies=[Depends(require_auth)])
async def lineage_trace(asset: str, store: Store = Depends(get_store)
                        ) -> list[dict[str, Any]]:
    return store.trace_lineage(asset)


# ---------------------------------------------------------------- audit
@app.get("/v1/audit", dependencies=[Depends(require_auth)])
async def audit_recent(limit: int = 100, store: Store = Depends(get_store)
                       ) -> list[dict[str, Any]]:
    return store.recent_audit(limit)


@app.get("/v1/audit/{entity_type}/{entity_id}", dependencies=[Depends(require_auth)])
async def audit_for_entity(entity_type: str, entity_id: int,
                           store: Store = Depends(get_store)) -> list[dict[str, Any]]:
    return store.audit_trail_for(entity_type, entity_id)


@app.get("/v1/dashboard/stats", dependencies=[Depends(require_auth)])
async def dashboard_stats(store: Store = Depends(get_store)) -> dict[str, Any]:
    return store.dashboard_stats()


@app.get("/info")
async def info(cfg: Config = Depends(get_cfg)) -> dict[str, Any]:
    return {"service": "dataforge", "version": "0.1.0", "config": cfg.to_dict()}
