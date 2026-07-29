"""REST API и веб-интерфейс.

Аутентификация — bearer-токен из `ACONSTRUCTOR_API_TOKEN`. Если токен не
задан, сервис поднимается только на localhost и громко предупреждает: не
хочется, чтобы прогоны за деньги оказались доступны из интернета по
умолчанию.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .core import REGISTRY, get_pipeline, load_registry, mermaid
from .runner import Runner
from .store import RunStore

log = logging.getLogger("aconstructor.api")

WEB_DIR = Path(__file__).parent / "web"
API_TOKEN = os.getenv("ACONSTRUCTOR_API_TOKEN", "").strip()
if API_TOKEN and not API_TOKEN.isascii():
    # HTTP-заголовок обязан быть latin-1: не-ASCII токен клиент физически
    # не сможет отправить. Падаем на старте, а не на первом запросе.
    raise SystemExit("ACONSTRUCTOR_API_TOKEN должен состоять из ASCII-символов")
DB_PATH = os.getenv("ACONSTRUCTOR_DB", "data/aconstructor.db")
WORKERS = int(os.getenv("ACONSTRUCTOR_WORKERS", "2"))
TIMEOUT_S = float(os.getenv("ACONSTRUCTOR_TIMEOUT", "600"))

store = RunStore(DB_PATH)
runner = Runner(store, settings(), workers=WORKERS, timeout_s=TIMEOUT_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_registry()
    runner.start()
    if not API_TOKEN:
        log.warning("ACONSTRUCTOR_API_TOKEN не задан — публикуйте только на localhost")
    yield
    runner.stop()
    store.close()


app = FastAPI(
    title="Aconstructor",
    description="Среда агентов на LangGraph: семь продуктовых пайплайнов",
    version=__version__,
    lifespan=lifespan,
)


def auth(request: Request) -> None:
    """Bearer-токен. Сравнение постоянного времени — против подбора."""
    if not API_TOKEN:
        return
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    # сравниваем байты: compare_digest на не-ASCII строках бросает TypeError
    if not secrets.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8")):
        raise HTTPException(401, "нужен корректный bearer-токен")


# --- модели запросов -------------------------------------------------------
class RunRequest(BaseModel):
    task: dict[str, Any] | None = Field(
        default=None, description="Входные данные; при null берутся демо-данные")
    provider: str | None = Field(default=None, description="fake | openai | anthropic | ollama")
    model: str | None = None
    sync: bool = Field(default=False, description="Выполнить сразу и вернуть результат")


# --- каталог ---------------------------------------------------------------
@app.get("/api/pipelines", tags=["каталог"])
def list_pipelines(_: None = Depends(auth)) -> list[dict[str, Any]]:
    load_registry()
    return [
        {"slug": s, "title": p.title, "summary": p.summary,
         "agents": list(p.agents), "tags": list(p.tags)}
        for s, p in sorted(REGISTRY.items())
    ]


@app.get("/api/pipelines/{slug}", tags=["каталог"])
def pipeline_detail(slug: str, _: None = Depends(auth)) -> dict[str, Any]:
    try:
        p = get_pipeline(slug)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    return {"slug": slug, "title": p.title, "summary": p.summary,
            "agents": list(p.agents), "tags": list(p.tags),
            "demo_task": p.demo_task(), "graph_mermaid": mermaid(slug)}


# --- прогоны ---------------------------------------------------------------
@app.post("/api/pipelines/{slug}/run", status_code=202, tags=["прогоны"])
def start_run(slug: str, req: RunRequest, _: None = Depends(auth)) -> dict[str, Any]:
    try:
        p = get_pipeline(slug)
    except KeyError as e:
        raise HTTPException(404, str(e)) from None
    task = req.task if req.task is not None else p.demo_task()
    try:
        if req.sync:
            run = runner.run_sync(slug, task, req.provider, req.model)
            return {**run.summary(), "report": run.report, "result": run.result}
        run = runner.submit(slug, task, req.provider, req.model)
    except RuntimeError as e:
        raise HTTPException(429, str(e)) from None
    return run.summary()


@app.get("/api/runs", tags=["прогоны"])
def list_runs(pipeline: str | None = None, status: str | None = None,
              limit: int = Query(50, le=500), offset: int = 0,
              _: None = Depends(auth)) -> dict[str, Any]:
    runs = store.list(pipeline=pipeline, status=status, limit=limit, offset=offset)
    return {"runs": [r.summary() for r in runs], "limit": limit, "offset": offset}


@app.get("/api/runs/{run_id}", tags=["прогоны"])
def get_run(run_id: str, include_result: bool = True,
            _: None = Depends(auth)) -> dict[str, Any]:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404, "прогон не найден")
    body = {**run.summary(), "task": run.task, "report": run.report,
            "artifacts": store.artifacts(run_id)}
    if include_result:
        body["result"] = run.result
    return body


@app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse, tags=["прогоны"])
def get_report(run_id: str, _: None = Depends(auth)) -> str:
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404, "прогон не найден")
    return run.report or "(отчёт ещё не готов)"


@app.get("/api/runs/{run_id}/artifacts/{name}", tags=["прогоны"])
def get_artifact(run_id: str, name: str, _: None = Depends(auth)) -> Response:
    art = store.artifact(run_id, name)
    if art is None:
        raise HTTPException(404, "артефакт не найден")
    ext = art["kind"]
    return Response(
        content=art["content"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.{name}.{ext}"'},
    )


@app.post("/api/runs/{run_id}/cancel", tags=["прогоны"])
def cancel_run(run_id: str, _: None = Depends(auth)) -> dict[str, Any]:
    if store.get(run_id) is None:
        raise HTTPException(404, "прогон не найден")
    if not store.cancel(run_id):
        raise HTTPException(409, "отменить можно только прогон в очереди")
    return {"cancelled": True, "id": run_id}


# --- эксплуатация ----------------------------------------------------------
@app.get("/api/stats", tags=["эксплуатация"])
def get_stats(_: None = Depends(auth)) -> dict[str, Any]:
    return {**store.stats(), "queue_depth": runner.queue_depth,
            "active_runs": runner.active}


@app.get("/health", tags=["эксплуатация"])
def health() -> dict[str, Any]:
    """Liveness+readiness для оркестратора. Без авторизации намеренно."""
    cfg = settings()
    try:
        n = len(load_registry())
        store.stats()
        ok = True
    except Exception as exc:  # noqa: BLE001
        log.exception("health-проверка не прошла")
        return {"status": "unhealthy", "error": str(exc)}
    return {"status": "ok" if ok else "degraded", "version": __version__,
            "pipelines": n, "provider": cfg.provider, "model": cfg.resolved_model(),
            "queue_depth": runner.queue_depth, "workers": WORKERS,
            "auth": "on" if API_TOKEN else "off"}


@app.get("/metrics", response_class=PlainTextResponse, tags=["эксплуатация"])
def metrics() -> str:
    """Prometheus text format."""
    s = store.stats()
    lines = [
        "# HELP aconstructor_runs_total Прогонов всего по статусам",
        "# TYPE aconstructor_runs_total counter",
    ]
    for status, n in s["by_status"].items():
        lines.append(f'aconstructor_runs_total{{status="{status}"}} {n}')
    lines += [
        "# HELP aconstructor_queue_depth Глубина очереди",
        "# TYPE aconstructor_queue_depth gauge",
        f"aconstructor_queue_depth {runner.queue_depth}",
        "# HELP aconstructor_cost_usd_total Суммарная стоимость прогонов",
        "# TYPE aconstructor_cost_usd_total counter",
        f"aconstructor_cost_usd_total {s['total_cost_usd']}",
        "# HELP aconstructor_run_duration_seconds_avg Средняя длительность",
        "# TYPE aconstructor_run_duration_seconds_avg gauge",
        f"aconstructor_run_duration_seconds_avg {s['avg_duration_s']}",
        "# HELP aconstructor_findings_total Находок всего",
        "# TYPE aconstructor_findings_total counter",
        f"aconstructor_findings_total {s['total_findings']}",
    ]
    for row in s["by_pipeline"]:
        lines.append(f'aconstructor_pipeline_runs{{pipeline="{row["pipeline"]}"}} {row["n"]}')
    return "\n".join(lines) + "\n"


# --- веб -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def main() -> None:
    import uvicorn

    host = os.getenv("ACONSTRUCTOR_HOST", "127.0.0.1" if not API_TOKEN else "0.0.0.0")
    port = int(os.getenv("ACONSTRUCTOR_PORT", "8080"))
    logging.basicConfig(
        level=os.getenv("ACONSTRUCTOR_LOG", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
