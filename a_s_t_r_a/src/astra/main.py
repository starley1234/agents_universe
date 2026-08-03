"""FastAPI app — Postgres, JWT, SSE, TaskIQ, FalkorDB, Langfuse, Eval, MCP, LLM test."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from astra import __version__
from astra.api.routes import agents, health, llm, projects
from astra.api.routes.eval import router as eval_router
from astra.api.routes.mcp import router as mcp_router
from astra.auth.routes import router as auth_router
from astra.config import settings
from astra.db.engine import init_db
from astra.mcp.tool_registry import tool_registry
from astra.utils.logger import setup_logging
from astra.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.log_level, settings.environment.value)
    logger.info(
        "🚀 Starting A.S.T.R.A. v%s — Postgres+pgvector, JWT=%s, FalkorDB=%s, Langfuse=%s",
        __version__,
        settings.auth_enabled,
        settings.use_falkordb,
        settings.langfuse_enabled,
    )

    settings.resolved_workspace.mkdir(parents=True, exist_ok=True)

    try:
        await init_db()
        logger.info("✅ Database initialised")
        if settings.auth_enabled:
            try:
                from astra.db.engine import get_session
                from astra.auth.jwt import get_password_hash
                from astra.db.models import User
                from sqlalchemy import select

                async with get_session() as db:
                    result = await db.execute(select(User).where(User.username == "admin"))
                    admin = result.scalar_one_or_none()
                    if not admin:
                        admin = User(
                            username="admin",
                            email="admin@astra.local",
                            hashed_password=get_password_hash("admin"),
                            is_active=True,
                        )
                        db.add(admin)
                        await db.flush()
                        logger.info("Created default admin user (admin/admin)")
            except Exception as exc:
                logger.warning("Failed to create default admin user: {}", exc)
    except Exception as exc:
        logger.error("❌ DB init failed: {}", exc)

    try:
        await tool_registry.init_global_servers()
    except Exception as exc:
        logger.warning("MCP init failed: {}", exc)

    if settings.langfuse_enabled:
        try:
            from astra.llm.tracing.langfuse import get_langfuse_client

            client = get_langfuse_client()
            if client:
                logger.info("✅ Langfuse client ready")
        except Exception as exc:
            logger.warning("Langfuse init failed: {}", exc)

    if settings.use_falkordb:
        try:
            from astra.memory.falkor_store import falkor_store

            if falkor_store._client:
                logger.info("✅ FalkorDB ready at %s", settings.falkordb_full_url)
            else:
                logger.warning("FalkorDB enabled but not connected, using NetworkX fallback")
        except Exception as exc:
            logger.warning("FalkorDB check failed: {}", exc)

    yield

    logger.info("👋 Shutting down A.S.T.R.A.")
    try:
        await tool_registry.shutdown()
    except Exception:
        pass


app = FastAPI(
    title=settings.project_name,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on {} {}: {}", request.method, request.url.path, exc)
    if settings.is_production:
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
    return JSONResponse(status_code=500, content={"detail": str(exc), "type": type(exc).__name__})


app.include_router(health.router, tags=["health"])
app.include_router(auth_router, tags=["auth"])
app.include_router(eval_router, tags=["eval"])
app.include_router(mcp_router, tags=["mcp"])
app.include_router(llm.router, tags=["llm"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects-api"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents-api"])
app.include_router(web_router, tags=["web"])
