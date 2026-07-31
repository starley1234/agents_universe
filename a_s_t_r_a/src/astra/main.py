"""FastAPI application — entry point for A.S.T.R.A."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from astra import __version__
from astra.api.routes import agents, health, projects
from astra.config import settings
from astra.db.engine import init_db
from astra.mcp.tool_registry import tool_registry
from astra.utils.logger import setup_logging
from astra.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    setup_logging(settings.log_level, settings.environment.value)
    logger.info("🚀  Starting A.S.T.R.A. v{}", __version__)

    # Database
    await init_db()
    logger.info("✅  Database initialised")

    # MCP servers (non-blocking — failures are tolerated)
    await tool_registry.init_global_servers()

    # Ensure workspace directory exists
    settings.workspace_path.mkdir(parents=True, exist_ok=True)

    yield

    # Shutdown
    logger.info("👋  Shutting down A.S.T.R.A.")
    await tool_registry.shutdown()


app = FastAPI(
    title=settings.project_name,
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url="/redoc" if settings.environment != "production" else None,
)

# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.environment == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global error handler ─────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on {} {}: {}", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── API Routers ───────────────────────────────────────────────
app.include_router(health.router, tags=["health"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects-api"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents-api"])

# ── Web UI (HTML pages + extra JSON endpoints) ───────────────
app.include_router(web_router, tags=["web"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "astra.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.environment == "development",
    )
