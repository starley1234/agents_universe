"""REST API и веб-интерфейс.

Изображения принимаются двумя способами: `multipart/form-data` (файлы с
формы) и JSON с data-URI. Первый удобен людям и мобильным клиентам,
второй — серверным интеграциям.

Все запросы проходят через `Runner`: кеш, повторы, бюджет и учёт
расхода. Без этого сервис, работающий за деньги, эксплуатировать нельзя.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi import UploadFile
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .core import REGISTRY, ServiceError, get_service, load_registry
from .images import ImageError, load
from .runner import BudgetExceeded, Runner
from .store import Store

log = logging.getLogger("vlmkit.api")

WEB_DIR = Path(__file__).parent / "web"
API_TOKEN = os.getenv("VLM_API_TOKEN", "").strip()
if API_TOKEN and not API_TOKEN.isascii():
    # HTTP-заголовок обязан быть latin-1: не-ASCII токен клиент физически
    # не сможет отправить. Падаем на старте, а не на первом запросе.
    raise SystemExit("VLM_API_TOKEN должен состоять из ASCII-символов")

CFG = settings()
store = Store(CFG.db_path, cache_ttl_s=CFG.cache_ttl_s)
runner = Runner(store, CFG, max_retries=CFG.max_retries,
                daily_budget_usd=CFG.daily_budget_usd, use_cache=CFG.use_cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_registry()
    if not API_TOKEN:
        log.warning("VLM_API_TOKEN не задан — публикуйте только на localhost")
    if CFG.daily_budget_usd:
        log.info("дневной лимит трат: $%.2f", CFG.daily_budget_usd)
    yield
    store.close()


app = FastAPI(title="VLM Services", version=__version__,
              description="Двенадцать продуктовых сервисов на одной VLM-инфраструктуре",
              lifespan=lifespan)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Сквозной идентификатор запроса — чтобы связать лог, журнал и жалобу клиента."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = rid
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path.startswith("/api/"):
        log.info("%s %s -> %d за %.2f с (rid=%s)", request.method, request.url.path,
                 response.status_code, time.time() - t0, rid)
    return response


def auth(request: Request) -> None:
    if not API_TOKEN:
        return
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    if not secrets.compare_digest(token.encode(), API_TOKEN.encode()):
        raise HTTPException(401, "нужен корректный bearer-токен")


class RunRequest(BaseModel):
    images: list[Any] | None = Field(default=None,
                                     description="data-URI, base64 или {data, scene}")
    params: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    no_cache: bool = Field(default=False, description="не брать ответ из кеша")


def _execute(slug: str, images: Any, params: dict, provider: str | None,
             model: str | None, request: Request, no_cache: bool = False
             ) -> dict[str, Any]:
    if slug not in load_registry():
        raise HTTPException(404, f"сервис {slug!r} не найден")
    rid = getattr(request.state, "request_id", None)
    client = request.headers.get("x-client-id")
    try:
        return runner.run(slug, images, params, provider=provider, model=model,
                          client=client, request_id=rid, no_cache=no_cache).as_dict()
    except (ServiceError, ImageError) as e:
        raise HTTPException(400, str(e)) from None
    except BudgetExceeded as e:
        # 402: клиент поймёт, что дело в деньгах, а не в его запросе.
        raise HTTPException(402, str(e)) from None
    except TypeError as e:
        raise HTTPException(400, f"некорректные параметры: {e}") from None
    except Exception as e:  # noqa: BLE001
        log.exception("сервис %s упал (rid=%s)", slug, rid)
        raise HTTPException(502, f"провайдер модели недоступен: {type(e).__name__}") from None


# --- каталог ---------------------------------------------------------------
@app.get("/api/services", tags=["каталог"])
def list_services(_: None = Depends(auth)) -> list[dict[str, Any]]:
    load_registry()
    return [{"slug": s, "title": c.title, "summary": c.summary, "tags": list(c.tags),
             "min_images": c.min_images, "max_images": c.max_images}
            for s, c in sorted(REGISTRY.items())]


@app.get("/api/services/{slug}", tags=["каталог"])
def service_detail(slug: str, _: None = Depends(auth)) -> dict[str, Any]:
    try:
        svc = get_service(slug)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from None
    demo = svc.demo()
    return {"slug": slug, "title": svc.title, "summary": svc.summary,
            "tags": list(svc.tags), "min_images": svc.min_images,
            "max_images": svc.max_images, "schema": svc.schema, "system": svc.system,
            "params": sorted(svc.known_params()),
            "demo_params": demo.get("params", {}),
            "demo_images": len(demo.get("images") or [])}


# --- запуск ----------------------------------------------------------------
@app.post("/api/services/{slug}/run", tags=["запуск"])
def run_json(slug: str, req: RunRequest, request: Request,
             _: None = Depends(auth)) -> dict[str, Any]:
    return _execute(slug, req.images, dict(req.params), req.provider, req.model,
                    request, req.no_cache)


@app.post("/api/services/{slug}/upload", tags=["запуск"])
async def run_upload(slug: str, request: Request,
                     files: list[UploadFile] = File(default=[]),
                     params: str = Form(default="{}"),
                     provider: str | None = Form(default=None),
                     model: str | None = Form(default=None),
                     no_cache: bool = Form(default=False),
                     _: None = Depends(auth)) -> dict[str, Any]:
    """Запуск с загрузкой файлов формой."""
    try:
        parsed = json.loads(params or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("params должен быть объектом JSON")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"некорректный JSON в params: {e}") from None

    images = []
    for f in files:
        raw = await f.read()
        try:
            images.append(load(raw, name=f.filename or "", max_mb=CFG.max_upload_mb))
        except ImageError as e:
            raise HTTPException(400, f"{f.filename}: {e}") from None
    return _execute(slug, images or None, parsed, provider, model, request, no_cache)


@app.post("/api/services/{slug}/demo", tags=["запуск"])
def run_demo(slug: str, request: Request, _: None = Depends(auth)) -> dict[str, Any]:
    return _execute(slug, None, {}, None, None, request, no_cache=True)


# --- эксплуатация ----------------------------------------------------------
@app.get("/api/runs", tags=["эксплуатация"])
def list_runs(service: str | None = None, status: str | None = None,
              limit: int = Query(50, le=500), _: None = Depends(auth)) -> dict[str, Any]:
    return {"runs": store.runs(service=service, status=status, limit=limit)}


@app.get("/api/stats", tags=["эксплуатация"])
def get_stats(_: None = Depends(auth)) -> dict[str, Any]:
    s = store.stats()
    s["spent_24h_usd"] = runner.spent_today()
    s["daily_budget_usd"] = CFG.daily_budget_usd or None
    return s


@app.get("/health", tags=["эксплуатация"])
def health() -> dict[str, Any]:
    """Liveness для оркестратора. Без авторизации намеренно."""
    from .images import HAVE_PILLOW

    try:
        n = len(load_registry())
        store.cache_stats()
    except Exception as exc:  # noqa: BLE001
        log.exception("health не прошёл")
        return JSONResponse({"status": "unhealthy", "error": str(exc)}, status_code=503)
    return {"status": "ok", "version": __version__, "services": n,
            "provider": CFG.provider, "model": CFG.resolved_model(),
            "pillow": HAVE_PILLOW, "auth": "on" if API_TOKEN else "off",
            "max_side_px": CFG.max_side_px, "cache": CFG.use_cache}


@app.get("/metrics", response_class=PlainTextResponse, tags=["эксплуатация"])
def metrics() -> str:
    """Prometheus text format."""
    s = store.stats()
    out = [
        "# HELP vlm_runs_total Прогонов по статусам",
        "# TYPE vlm_runs_total counter",
    ]
    for status, n in s["by_status"].items():
        out.append(f'vlm_runs_total{{status="{status}"}} {n}')
    out += [
        "# HELP vlm_cost_usd_total Суммарные траты на модель",
        "# TYPE vlm_cost_usd_total counter",
        f"vlm_cost_usd_total {s['total_cost_usd']}",
        "# HELP vlm_cache_hit_rate Доля ответов из кеша",
        "# TYPE vlm_cache_hit_rate gauge",
        f"vlm_cache_hit_rate {s['cache_hit_rate']}",
        "# HELP vlm_cache_saved_usd_total Сэкономлено кешем",
        "# TYPE vlm_cache_saved_usd_total counter",
        f"vlm_cache_saved_usd_total {s['cache']['cost_saved_usd']}",
        "# HELP vlm_images_processed_total Обработано изображений",
        "# TYPE vlm_images_processed_total counter",
        f"vlm_images_processed_total {s['images_processed']}",
        "# HELP vlm_run_duration_seconds_avg Средняя длительность прогона",
        "# TYPE vlm_run_duration_seconds_avg gauge",
        f"vlm_run_duration_seconds_avg {s['avg_duration_s']}",
        "# HELP vlm_spend_24h_usd Траты за последние сутки",
        "# TYPE vlm_spend_24h_usd gauge",
        f"vlm_spend_24h_usd {runner.spent_today()}",
    ]
    for row in s["by_service"]:
        out.append(f'vlm_service_runs{{service="{row["service"]}"}} {row["n"]}')
        out.append(f'vlm_service_cost_usd{{service="{row["service"]}"}} '
                   f'{round(row["cost"], 6)}')
    return "\n".join(out) + "\n"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def main() -> None:
    import uvicorn

    host = os.getenv("VLM_HOST", "0.0.0.0" if API_TOKEN else "127.0.0.1")
    port = int(os.getenv("VLM_PORT", "8081"))
    logging.basicConfig(level=os.getenv("VLM_LOG", "INFO"),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
