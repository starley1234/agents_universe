"""REST API и веб-интерфейс для двенадцати сервисов.

Изображения принимаются двумя способами: `multipart/form-data` (файлы с
формы) и JSON с data-URI. Первый удобен людям и мобильным клиентам,
второй — серверным интеграциям.

Аутентификация — bearer-токен. Без него сервис поднимается только на
localhost: VLM-запросы стоят денег, и открытый эндпоинт быстро найдут.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .core import REGISTRY, ServiceError, get_service, load_registry
from .images import ImageError, load

log = logging.getLogger("vlmkit.api")

WEB_DIR = Path(__file__).parent / "web"
API_TOKEN = os.getenv("VLM_API_TOKEN", "").strip()
if API_TOKEN and not API_TOKEN.isascii():
    # HTTP-заголовок обязан быть latin-1: не-ASCII токен клиент физически
    # не сможет отправить. Падаем на старте, а не на первом запросе.
    raise SystemExit("VLM_API_TOKEN должен состоять из ASCII-символов")

app = FastAPI(title="VLM Services", version=__version__,
              description="Двенадцать продуктовых сервисов на одной VLM-инфраструктуре")


@app.on_event("startup")
def _startup() -> None:
    load_registry()
    if not API_TOKEN:
        log.warning("VLM_API_TOKEN не задан — публикуйте только на localhost")


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


def _cfg(provider: str | None = None, model: str | None = None):
    from dataclasses import replace

    cfg = settings()
    if provider:
        cfg = replace(cfg, provider=provider)
    if model:
        cfg = replace(cfg, model=model)
    return cfg


def _run(slug: str, images: Any, params: dict, provider: str | None,
         model: str | None) -> dict[str, Any]:
    try:
        svc = get_service(slug, _cfg(provider, model))
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from None
    if images is None:
        demo = svc.demo()
        images, params = demo.get("images"), {**demo.get("params", {}), **params}
    try:
        return svc.run(images, **params).as_dict()
    except (ServiceError, ImageError) as e:
        raise HTTPException(400, str(e)) from None
    except TypeError as e:  # неизвестный параметр в params
        raise HTTPException(400, f"некорректные параметры: {e}") from None


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
            "max_images": svc.max_images, "schema": svc.schema,
            "system": svc.system, "demo_params": demo.get("params", {}),
            "demo_images": len(demo.get("images") or [])}


# --- запуск ----------------------------------------------------------------
@app.post("/api/services/{slug}/run", tags=["запуск"])
def run_json(slug: str, req: RunRequest, _: None = Depends(auth)) -> dict[str, Any]:
    return _run(slug, req.images, dict(req.params), req.provider, req.model)


@app.post("/api/services/{slug}/upload", tags=["запуск"])
async def run_upload(slug: str, files: list[UploadFile] = File(default=[]),
                     params: str = Form(default="{}"),
                     provider: str | None = Form(default=None),
                     model: str | None = Form(default=None),
                     _: None = Depends(auth)) -> dict[str, Any]:
    """Запуск с загрузкой файлов формой."""
    try:
        parsed = json.loads(params or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("params должен быть объектом JSON")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"некорректный JSON в params: {e}") from None

    cfg = _cfg(provider, model)
    images = []
    for f in files:
        raw = await f.read()
        try:
            images.append(load(raw, name=f.filename or "", max_mb=cfg.max_upload_mb))
        except ImageError as e:
            raise HTTPException(400, f"{f.filename}: {e}") from None
    return _run(slug, images or None, parsed, provider, model)


@app.post("/api/services/{slug}/demo", tags=["запуск"])
def run_demo(slug: str, _: None = Depends(auth)) -> dict[str, Any]:
    return _run(slug, None, {}, None, None)


# --- эксплуатация ----------------------------------------------------------
@app.get("/health", tags=["эксплуатация"])
def health() -> dict[str, Any]:
    cfg = settings()
    try:
        n = len(load_registry())
    except Exception as exc:  # noqa: BLE001
        log.exception("health: реестр не загрузился")
        return {"status": "unhealthy", "error": str(exc)}
    from .images import HAVE_PILLOW

    return {"status": "ok", "version": __version__, "services": n,
            "provider": cfg.provider, "model": cfg.resolved_model(),
            "pillow": HAVE_PILLOW, "auth": "on" if API_TOKEN else "off",
            "max_side_px": cfg.max_side_px}


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
