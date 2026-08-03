"""LangGraph Autonomous Agent — FastAPI application entry point."""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import get_settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)
cfg = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────
    log.info("🚀 %s (%s)", cfg.APP_ENV.value, cfg.APP_ENV.value)
    cfg.require_token()
    try:
        from src.db.engine import check_db, init_db
        if await check_db():
            await init_db()
            log.info("✅ Database ready")
        else:
            log.warning("⚠️ DB unreachable — running degraded")
    except Exception as e:
        log.warning("⚠️ DB init failed: %s", e)
    os.makedirs(cfg.WORKSPACE_PATH, exist_ok=True)
    os.makedirs(os.path.join(cfg.WORKSPACE_PATH, ".logs"), exist_ok=True)
    log.info("✅ http://%s:%d", cfg.APP_HOST, cfg.APP_PORT)
    yield
    # ── Shutdown ─────────────────────────────────────────
    log.info("🛑 Shutting down")
    from src.db.engine import shutdown_db
    await shutdown_db()


app = FastAPI(title="LangGraph Autonomous Agent", version="0.1.0", lifespan=lifespan)

# ── Auth middleware ──────────────────────────────────────────────────────
if cfg.APP_API_TOKEN:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class AuthMiddleware(BaseHTTPMiddleware):
        SKIP = {"/health", "/", "/static"}

        async def dispatch(self, request: Request, call_next):
            if request.url.path.startswith("/static") or request.url.path in self.SKIP:
                return await call_next(request)
            if request.url.path.startswith("/ws/"):
                # WebSocket auth via query param
                token = request.query_params.get("token", "")
                if token != cfg.APP_API_TOKEN:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {cfg.APP_API_TOKEN}":
                return JSONResponse({"error": "Authorization: Bearer <token> required"}, status_code=401)
            return await call_next(request)

    app.add_middleware(AuthMiddleware)

# ── Static / templates ──────────────────────────────────────────────────
_static = os.path.join(os.path.dirname(__file__), "web", "static")
_templates = os.path.join(os.path.dirname(__file__), "web", "templates")
os.makedirs(os.path.join(_static, "css"), exist_ok=True)
os.makedirs(os.path.join(_static, "js"), exist_ok=True)
app.mount("/static", StaticFiles(directory=_static), name="static")
tpl = Jinja2Templates(directory=_templates)

# ── API routes ──────────────────────────────────────────────────────────
from src.api.routes import router as api_router  # noqa: E402

app.include_router(api_router, prefix="/api")


# ── Web UI ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return tpl.TemplateResponse("index.html", {"request": request, "cfg": cfg})


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(request: Request, task_id: str):
    return tpl.TemplateResponse("task.html", {"request": request, "task_id": task_id, "cfg": cfg})


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    return tpl.TemplateResponse("knowledge.html", {"request": request, "cfg": cfg})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return tpl.TemplateResponse("status.html", {"request": request, "cfg": cfg})


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    from src.db.engine import check_db
    db_ok = await check_db()
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


# ── Entrypoint ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=cfg.APP_HOST, port=cfg.APP_PORT,
                reload=cfg.is_dev, log_level="info")
