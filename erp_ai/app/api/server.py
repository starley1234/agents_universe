"""HTTP API ERP AI на FastAPI.

Эндпоинты REST (см. ТЗ §4.2 "Открытый REST/GraphQL API"; GraphQL в этой
сборке не реализован — только REST, см. README.md за границей объёма):

  GET  /health                        — жив ли сервис (без токена)
  GET  /dashboard, /                  — веб-интерфейс

  GET  /v1/nomenclature               — справочник номенклатуры
  POST /v1/nomenclature               — создать/обновить позицию
  GET  /v1/nomenclature/low-stock     — позиции с дефицитом

  GET  /v1/counterparties             — справочник контрагентов
  POST /v1/counterparties             — создать/обновить контрагента
  POST /v1/counterparties/{id}/prices — задать цену поставщика

  GET  /v1/purchase-orders            — список заказов поставщикам
  GET  /v1/purchase-orders/{id}       — детали заказа
  POST /v1/purchase-orders/{id}/sync-1c — выгрузить в 1С (адаптер)

  POST /v1/agents/procurement/scan    — агент-снабженец: скан дефицита
  POST /v1/agents/procurement/propose/{nomenclature_id} — предложение по 1 позиции
  GET  /v1/proposals                  — список предложений агента
  GET  /v1/proposals/{id}             — детали + explainability
  POST /v1/proposals/{id}/approve     — confirmation gate: утвердить
  POST /v1/proposals/{id}/reject      — confirmation gate: отклонить
  POST /v1/proposals/{id}/rollback    — откат full_auto решения

  GET  /v1/audit                      — журнал аудита (неизменяемый)
  POST /v1/onec/pull/nomenclature     — 1С -> ERP: загрузить номенклатуру
  POST /v1/onec/pull/counterparties   — 1С -> ERP: загрузить контрагентов
  GET  /v1/onec/sync-log              — журнал обмена с 1С

Токен: если задан ERP_API_TOKEN, все маршруты кроме /health и /dashboard
требуют заголовок Authorization: Bearer <token>.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..agents.procurement import ProcurementAgent, ProcurementAgentError
from ..config import Config
from ..db.store import Store, StoreError
from ..integrations.onec_adapter import OneCAdapter, OneCError

app = FastAPI(title="ERP AI", version="0.1.0")

_STATE: dict[str, Any] = {"cfg": None, "token": None}


def configure(cfg: Config, token: str | None = None) -> None:
    _STATE["cfg"] = cfg
    _STATE["token"] = token or os.getenv("ERP_API_TOKEN") or cfg.api_token or None


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


@app.exception_handler(ProcurementAgentError)
async def _agent_error_handler(request: Request, exc: ProcurementAgentError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(OneCError)
async def _onec_error_handler(request: Request, exc: OneCError):
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    path = Path(__file__).resolve().parent.parent / "web" / "dashboard.html"
    if not path.exists():
        raise HTTPException(404, "страница не найдена")
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------- pydantic
class NomenclatureIn(BaseModel):
    sku: str
    name: str
    unit: str = "шт"
    min_stock: float = 0
    lead_time_days: int = 0


class CounterpartyIn(BaseModel):
    name: str
    inn: str = ""
    kind: str = "supplier"
    reliability_score: float = Field(default=1.0, ge=0, le=1)


class SupplierPriceIn(BaseModel):
    nomenclature_id: int
    price: float
    currency: str = "RUB"


class StockIn(BaseModel):
    nomenclature_id: int
    quantity: float
    warehouse: str = "основной"


class ProposalDecisionIn(BaseModel):
    actor: str
    reason: str = ""


class ProcurementScanIn(BaseModel):
    autonomy_mode: str | None = None


# --------------------------------------------------------- nomenclature
@app.get("/v1/nomenclature", dependencies=[Depends(require_auth)])
async def list_nomenclature(store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.list_nomenclature()}


@app.post("/v1/nomenclature", dependencies=[Depends(require_auth)])
async def create_nomenclature(body: NomenclatureIn,
                              store: Store = Depends(get_store)) -> dict[str, Any]:
    nid = store.upsert_nomenclature(body.sku, body.name, unit=body.unit,
                                    min_stock=body.min_stock,
                                    lead_time_days=body.lead_time_days)
    store.log_audit("human:api", "upsert", "nomenclature", nid, body.model_dump())
    return {"id": nid}


@app.get("/v1/nomenclature/low-stock", dependencies=[Depends(require_auth)])
async def low_stock(store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.low_stock_nomenclature()}


@app.post("/v1/nomenclature/stock", dependencies=[Depends(require_auth)])
async def set_stock(body: StockIn, store: Store = Depends(get_store)) -> dict[str, Any]:
    store.set_stock(body.nomenclature_id, body.quantity, warehouse=body.warehouse)
    store.log_audit("human:api", "set_stock", "nomenclature", body.nomenclature_id,
                    {"quantity": body.quantity, "warehouse": body.warehouse})
    return {"ok": True}


# --------------------------------------------------------- counterparty
@app.get("/v1/counterparties", dependencies=[Depends(require_auth)])
async def list_counterparties(kind: str = "",
                              store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.list_counterparties(kind=kind)}


@app.post("/v1/counterparties", dependencies=[Depends(require_auth)])
async def create_counterparty(body: CounterpartyIn,
                              store: Store = Depends(get_store)) -> dict[str, Any]:
    cid = store.upsert_counterparty(body.name, inn=body.inn, kind=body.kind,
                                    reliability_score=body.reliability_score)
    store.log_audit("human:api", "upsert", "counterparty", cid, body.model_dump())
    return {"id": cid}


@app.post("/v1/counterparties/{counterparty_id}/prices",
         dependencies=[Depends(require_auth)])
async def set_price(counterparty_id: int, body: SupplierPriceIn,
                    store: Store = Depends(get_store)) -> dict[str, Any]:
    if not store.get_counterparty(counterparty_id):
        raise HTTPException(404, f"Контрагент #{counterparty_id} не найден")
    price_id = store.set_supplier_price(body.nomenclature_id, counterparty_id,
                                        body.price, currency=body.currency)
    store.log_audit("human:api", "set_price", "counterparty", counterparty_id,
                    {"nomenclature_id": body.nomenclature_id, "price": body.price})
    return {"id": price_id}


# -------------------------------------------------------- purchase_order
@app.get("/v1/purchase-orders", dependencies=[Depends(require_auth)])
async def list_purchase_orders(status: str = "",
                               store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.list_purchase_orders(status=status)}


@app.get("/v1/purchase-orders/{po_id}", dependencies=[Depends(require_auth)])
async def get_purchase_order(po_id: int, store: Store = Depends(get_store)) -> dict[str, Any]:
    po = store.get_purchase_order(po_id)
    if not po:
        raise HTTPException(404, f"Заказ #{po_id} не найден")
    return po


@app.post("/v1/purchase-orders/{po_id}/sync-1c", dependencies=[Depends(require_auth)])
async def sync_purchase_order_to_1c(po_id: int, cfg: Config = Depends(get_cfg),
                                    store: Store = Depends(get_store)) -> dict[str, Any]:
    adapter = OneCAdapter(cfg, store)
    result = adapter.push_purchase_order(po_id)
    return {"ok": result.ok, "idempotency_key": result.idempotency_key,
           "external_id": result.external_id, "error": result.error,
           "skipped_duplicate": result.skipped_duplicate}


# ------------------------------------------------------------ procurement
@app.post("/v1/agents/procurement/scan", dependencies=[Depends(require_auth)])
async def procurement_scan(body: ProcurementScanIn, cfg: Config = Depends(get_cfg),
                           store: Store = Depends(get_store)) -> dict[str, Any]:
    agent = ProcurementAgent(cfg, store)
    decisions = agent.run_deficit_scan(autonomy_mode=body.autonomy_mode)
    return {"decisions": [d.__dict__ for d in decisions]}


@app.post("/v1/agents/procurement/propose/{nomenclature_id}",
         dependencies=[Depends(require_auth)])
async def procurement_propose(nomenclature_id: int, body: ProcurementScanIn,
                              cfg: Config = Depends(get_cfg),
                              store: Store = Depends(get_store)) -> dict[str, Any]:
    agent = ProcurementAgent(cfg, store)
    decision = agent.propose_for_nomenclature(nomenclature_id, body.autonomy_mode)
    return decision.__dict__


@app.get("/v1/proposals", dependencies=[Depends(require_auth)])
async def list_proposals(status: str = "",
                         store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.list_proposals(status=status)}


@app.get("/v1/proposals/{proposal_id}", dependencies=[Depends(require_auth)])
async def get_proposal(proposal_id: int, store: Store = Depends(get_store)) -> dict[str, Any]:
    proposal = store.get_proposal(proposal_id)
    if not proposal:
        raise HTTPException(404, f"Предложение #{proposal_id} не найдено")
    proposal["audit_trail"] = store.audit_trail_for("agent_proposal", proposal_id)
    return proposal


@app.post("/v1/proposals/{proposal_id}/approve", dependencies=[Depends(require_auth)])
async def approve_proposal(proposal_id: int, body: ProposalDecisionIn,
                           cfg: Config = Depends(get_cfg),
                           store: Store = Depends(get_store)) -> dict[str, Any]:
    agent = ProcurementAgent(cfg, store)
    po_id = agent.approve_proposal(proposal_id, actor=body.actor)
    return {"purchase_order_id": po_id}


@app.post("/v1/proposals/{proposal_id}/reject", dependencies=[Depends(require_auth)])
async def reject_proposal(proposal_id: int, body: ProposalDecisionIn,
                          cfg: Config = Depends(get_cfg),
                          store: Store = Depends(get_store)) -> dict[str, Any]:
    agent = ProcurementAgent(cfg, store)
    agent.reject_proposal(proposal_id, actor=body.actor, reason=body.reason)
    return {"ok": True}


@app.post("/v1/proposals/{proposal_id}/rollback", dependencies=[Depends(require_auth)])
async def rollback_proposal(proposal_id: int, body: ProposalDecisionIn,
                            cfg: Config = Depends(get_cfg),
                            store: Store = Depends(get_store)) -> dict[str, Any]:
    agent = ProcurementAgent(cfg, store)
    agent.rollback_proposal(proposal_id, actor=body.actor, reason=body.reason)
    return {"ok": True}


# ----------------------------------------------------------------- audit
@app.get("/v1/audit", dependencies=[Depends(require_auth)])
async def recent_audit(limit: int = 100,
                       store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.recent_audit(limit=limit)}


@app.get("/v1/audit/{entity_type}/{entity_id}", dependencies=[Depends(require_auth)])
async def audit_for_entity(entity_type: str, entity_id: int,
                           store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.audit_trail_for(entity_type, entity_id)}


# ------------------------------------------------------------------ 1С
@app.post("/v1/onec/pull/nomenclature", dependencies=[Depends(require_auth)])
async def onec_pull_nomenclature(cfg: Config = Depends(get_cfg),
                                 store: Store = Depends(get_store)) -> dict[str, Any]:
    adapter = OneCAdapter(cfg, store)
    return {"applied": adapter.pull_nomenclature()}


@app.post("/v1/onec/pull/counterparties", dependencies=[Depends(require_auth)])
async def onec_pull_counterparties(cfg: Config = Depends(get_cfg),
                                   store: Store = Depends(get_store)) -> dict[str, Any]:
    adapter = OneCAdapter(cfg, store)
    return {"applied": adapter.pull_counterparties()}


@app.get("/v1/onec/sync-log", dependencies=[Depends(require_auth)])
async def onec_sync_log(status: str = "",
                        store: Store = Depends(get_store)) -> dict[str, Any]:
    return {"items": store.onec_sync_log_list(status=status)}


# --------------------------------------------------------------- overview
@app.get("/v1/dashboard/stats", dependencies=[Depends(require_auth)])
async def dashboard_stats(store: Store = Depends(get_store)) -> dict[str, Any]:
    return store.dashboard_stats()


@app.get("/info", dependencies=[Depends(require_auth)])
async def info(cfg: Config = Depends(get_cfg)) -> dict[str, Any]:
    return {"config": cfg.to_dict()}
