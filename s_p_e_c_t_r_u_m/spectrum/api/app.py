"""FastAPI приложение S.P.E.C.T.R.U.M.: REST API для всех операций."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("spectrum.api")

try:
    from fastapi import FastAPI, UploadFile, File, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# ---------------------------------------------------------------------------
#  Pydantic models
# ---------------------------------------------------------------------------

if HAS_FASTAPI:

    class AskRequest(BaseModel):
        question: str
        top_k: int = 5

    class IngestURLRequest(BaseModel):
        url: str
        render_js: bool = False

    class TaskRequest(BaseModel):
        task: str

    class HealthResponse(BaseModel):
        status: str
        version: str
        stats: dict[str, Any]

    class AskResponse(BaseModel):
        answer: str
        sources: list[dict[str, Any]]

    class IngestResponse(BaseModel):
        source: str
        chunks_indexed: int
        processing_time_s: float
        warnings: list[str] = []


# ---------------------------------------------------------------------------
#  App Factory
# ---------------------------------------------------------------------------

def create_app(agent: Any = None) -> Any:
    """Создаёт FastAPI приложение с маршрутами.

    Args:
        agent: экземпляр Agent (если None — создаётся из настроек)
    """
    if not HAS_FASTAPI:
        raise ImportError("fastapi not installed: pip install fastapi uvicorn")

    from ..brain.agent import Agent as SpectrumAgent

    app = FastAPI(
        title="S.P.E.C.T.R.U.M.",
        description="Semantic Processing & Extraction Cluster for ToR, Reports, Unstructured Media",
        version="0.1.0",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Ленивая инициализация агента
    _agent = agent

    def get_agent() -> SpectrumAgent:
        nonlocal _agent
        if _agent is None:
            _agent = SpectrumAgent.from_settings()
        return _agent

    # --- Health ---

    @app.get("/health", response_model=HealthResponse)
    def health():
        agent = get_agent()
        return HealthResponse(
            status="ok",
            version="0.1.0",
            stats=agent.stats(),
        )

    # --- Ask (RAG) ---

    @app.post("/api/ask", response_model=AskResponse)
    def ask(req: AskRequest):
        agent = get_agent()
        response = agent.ask(req.question, top_k=req.top_k)
        return AskResponse(
            answer=response.answer,
            sources=response.to_dict()["sources"],
        )

    # --- Ingest File ---

    @app.post("/api/ingest/file", response_model=IngestResponse)
    async def ingest_file(file: UploadFile = File(...)):
        agent = get_agent()

        # Сохраняем во временный файл
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "file").suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        try:
            result = agent.ingest_file(tmp_path)
            return IngestResponse(
                source=file.filename or "unknown",
                chunks_indexed=result.chunk_count,
                processing_time_s=result.processing_time_s,
                warnings=result.warnings + result.errors,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # --- Ingest URL ---

    @app.post("/api/ingest/url", response_model=IngestResponse)
    def ingest_url(req: IngestURLRequest):
        agent = get_agent()
        result = agent.ingest_url(req.url, render_js=req.render_js)
        return IngestResponse(
            source=req.url,
            chunks_indexed=result.chunk_count,
            processing_time_s=result.processing_time_s,
            warnings=result.warnings + result.errors,
        )

    # --- Ingest Directory ---

    @app.post("/api/ingest/directory")
    def ingest_directory(
        path: str = Query(..., description="Путь к директории"),
        recursive: bool = Query(True),
    ):
        agent = get_agent()
        dir_path = Path(path)
        if not dir_path.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

        results = agent.ingest_directory(path, recursive=recursive)
        total_chunks = sum(r.chunk_count for r in results)
        total_time = sum(r.processing_time_s for r in results)

        return {
            "directory": path,
            "files_processed": len(results),
            "total_chunks_indexed": total_chunks,
            "total_time_s": round(total_time, 2),
            "details": [
                {
                    "source": r.source_path,
                    "chunks": r.chunk_count,
                    "errors": r.errors,
                }
                for r in results
            ],
        }

    # --- Task ---

    @app.post("/api/task")
    def execute_task(req: TaskRequest):
        agent = get_agent()
        result = agent.execute_task(req.task)
        return result.to_dict()

    # --- Stats ---

    @app.get("/api/stats")
    def stats():
        agent = get_agent()
        return agent.stats()

    # --- Sources ---

    @app.get("/api/sources")
    def list_sources():
        agent = get_agent()
        files = agent._file_store.list_all()
        return {
            "count": len(files),
            "sources": [
                {
                    "file_id": f.file_id,
                    "name": f.original_name,
                    "size_bytes": f.size_bytes,
                    "content_type": f.content_type,
                    "ingested_at": f.ingested_at,
                }
                for f in files
            ],
        }

    # --- Delete Source ---

    @app.delete("/api/sources/{source_path:path}")
    def delete_source(source_path: str):
        agent = get_agent()
        count = agent.delete_source(source_path)
        return {"deleted_chunks": count, "source": source_path}

    return app
