"""Main application entrypoint for NexusTwin MDM & Certification Service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.routes_agent import router as agent_router
from src.api.routes_duplicates import router as duplicates_router
from src.api.routes_generator import router as generator_router
from src.api.routes_health import router as health_router
from src.api.routes_mcp import router as mcp_router
from src.api.routes_mdm import router as mdm_router
from src.config import settings
from src.db.engine import init_db
from src.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize database schema, partitions, and seed data."""
    logger.info(
        f"Starting {settings.project_name} in {settings.environment} mode on port {settings.app_port}..."
    )
    try:
        await init_db()
    except Exception as exc:
        logger.error(f"Error initializing database: {exc}")
    yield
    logger.info("Shutting down NexusTwin MDM service.")


app = FastAPI(
    title=settings.project_name,
    description="Holding MDM + Certification (AP-25) + Digital Twin v5.7 with LangGraph Autonomous Agent & MCP Server",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS Middleware for web browser & preview iframe support
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API & Web Routers
app.include_router(health_router)
app.include_router(mdm_router)
app.include_router(duplicates_router)
app.include_router(generator_router)
app.include_router(agent_router)
app.include_router(mcp_router)
app.include_router(web_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.environment == "development",
    )
