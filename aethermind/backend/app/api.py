import asyncio
import json
import mimetypes
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from app.config import settings
from app.db.models import Artifact, Task, TaskEvent, TaskSnapshot, TaskStatus
from app.db.session import get_db
from app.schemas import AgentSettingsRead, Budget, EventRead, InterveneRequest, MCPServerConfig, MCPToolCallRequest, RollbackRequest, SnapshotRead, TaskCreate, TaskRead, TaskUpdate, ToolConfig
from app.services.events import add_event
from app.services.mcp_client import INTERNAL_SERVER_NAME, call_mcp_tool_sync, list_mcp_tools_sync
from app.services.mcp_registry import delete_global_mcp_server, load_global_mcp_servers, upsert_global_mcp_server
from app.services.workspace import safe_path, task_workspace
from app.worker import run_agent_iteration

router = APIRouter()

def default_budget() -> dict:
    return Budget(
        max_iterations=settings.default_max_iterations,
        token_budget=settings.default_token_budget,
        cost_budget_usd=settings.default_cost_budget_usd,
        time_budget_seconds=settings.default_time_budget_seconds,
    ).model_dump()

def default_tools() -> dict:
    config = ToolConfig().model_dump()
    config["mcp_servers"] = load_global_mcp_servers()
    config["mcp"] = True
    return config

def merged_tool_config(state: dict) -> dict:
    base = default_tools()
    current = dict(state.get("tool_config", {}) or {})
    global_servers = {server.get("name"): server for server in base.get("mcp_servers", []) if server.get("name")}
    local_servers = {server.get("name"): server for server in current.get("mcp_servers", []) if server.get("name")}
    merged = {**base, **current}
    merged["mcp_servers"] = list({**global_servers, **local_servers}.values())
    merged["mcp"] = bool(merged.get("mcp", True))
    return merged

@router.get("/health")
def health():
    return {"status": "ok", "project": settings.project_name}

@router.get("/settings", response_model=AgentSettingsRead)
def get_agent_settings():
    default_model = settings.custom_remote_default_model if settings.llm_active_provider == "custom_remote" else settings.openrouter_default_model
    return AgentSettingsRead(
        project_name=settings.project_name,
        environment=settings.environment,
        llm_active_provider=settings.llm_active_provider,
        default_model=default_model,
        embedding_dimensions=settings.embedding_dimensions,
        summary_every_iterations=settings.summary_every_iterations,
        low_confidence_threshold=settings.low_confidence_threshold,
        low_confidence_streak_limit=settings.low_confidence_streak_limit,
        default_max_iterations=settings.default_max_iterations,
        planner_min_steps=settings.planner_min_steps,
        workspace_path=settings.workspace_path,
    )

@router.post("/tasks", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    task = Task(goal=payload.goal, status=TaskStatus.PENDING, budget_json=(payload.budget.model_dump() if payload.budget else default_budget()), workspace_path="")
    db.add(task)
    db.flush()
    ws = task_workspace(task.id)
    task.workspace_path = str(ws)
    task.current_state_json = {
        "task_id": str(task.id),
        "goal": task.goal,
        "iteration": 0,
        "events": [],
        "artifacts": [],
        "tool_config": default_tools(),
    }
    add_event(db, task.id, "created", {"message": "Задача создана и поставлена в очередь.", "goal": task.goal})
    db.commit()
    db.refresh(task)
    run_agent_iteration.delay(str(task.id))
    return task

@router.get("/tasks", response_model=list[TaskRead])
def list_tasks(db: Session = Depends(get_db)):
    return db.execute(select(Task).order_by(desc(Task.created_at))).scalars().all()

@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return task

@router.patch("/tasks/{task_id}", response_model=TaskRead)
def update_task(task_id: UUID, payload: TaskUpdate, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if payload.goal is not None:
        task.goal = payload.goal
    if payload.status is not None:
        task.status = payload.status
    if payload.current_state_json is not None:
        task.current_state_json = payload.current_state_json
    if payload.budget_json is not None:
        task.budget_json = payload.budget_json
    add_event(db, task.id, "settings", {"message": "Настройки/состояние задачи обновлены пользователем."})
    db.commit(); db.refresh(task)
    return task

@router.delete("/tasks/{task_id}")
def delete_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    add_event(db, task.id, "deleted", {"message": "Задача удалена пользователем."})
    db.delete(task)
    db.commit()
    return {"ok": True, "deleted_task_id": str(task_id)}

@router.post("/tasks/{task_id}/pause", response_model=TaskRead)
def pause_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    task.status = TaskStatus.PAUSED
    add_event(db, task.id, "paused", {"message": "Пользователь поставил задачу на паузу."})
    db.commit(); db.refresh(task)
    return task

@router.post("/tasks/{task_id}/resume", response_model=TaskRead)
def resume_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if task.status == TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Задача уже завершена. Для повторного запуска используйте rollback к нужному checkpoint.",
        )
    task.status = TaskStatus.RUNNING
    state = dict(task.current_state_json or {})
    state["awaiting_user"] = False
    state["goal_completed"] = False
    task.current_state_json = state
    add_event(db, task.id, "resumed", {"message": "Пользователь возобновил задачу."})
    db.commit(); db.refresh(task)
    run_agent_iteration.delay(str(task.id))
    return task

@router.post("/tasks/{task_id}/intervene", response_model=TaskRead)
def intervene(task_id: UUID, payload: InterveneRequest, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    interventions = state.setdefault("human_interventions", [])
    interventions.append({"message": payload.message})
    state["awaiting_user"] = False
    task.current_state_json = state
    task.status = TaskStatus.RUNNING if payload.resume else TaskStatus.PAUSED
    add_event(db, task.id, "intervention", {"message": payload.message, "resume": payload.resume})
    db.commit(); db.refresh(task)
    if payload.resume:
        run_agent_iteration.delay(str(task.id))
    return task

@router.post("/tasks/{task_id}/rollback", response_model=TaskRead)
def rollback(task_id: UUID, payload: RollbackRequest, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    query = select(TaskSnapshot).where(TaskSnapshot.task_id == task.id)
    if payload.snapshot_id:
        query = query.where(TaskSnapshot.id == payload.snapshot_id)
    elif payload.iteration is not None:
        query = query.where(TaskSnapshot.iteration == payload.iteration).order_by(desc(TaskSnapshot.created_at))
    else:
        query = query.order_by(desc(TaskSnapshot.created_at))
    snapshot = db.execute(query).scalars().first()
    if not snapshot:
        raise HTTPException(404, "Снапшот не найден")
    state = dict(snapshot.state_json)
    if payload.new_instruction:
        state.setdefault("human_interventions", []).append({"message": payload.new_instruction, "rollback": True})
    task.current_state_json = state
    task.status = TaskStatus.ROLLED_BACK
    add_event(db, task.id, "rollback", {"snapshot_id": str(snapshot.id), "iteration": snapshot.iteration, "new_instruction": payload.new_instruction})
    db.commit(); db.refresh(task)
    return task

@router.get("/tasks/{task_id}/events", response_model=list[EventRead])
def get_events(task_id: UUID, db: Session = Depends(get_db)):
    return db.execute(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at)).scalars().all()

@router.get("/tasks/{task_id}/snapshots", response_model=list[SnapshotRead])
def get_snapshots(task_id: UUID, db: Session = Depends(get_db)):
    return db.execute(select(TaskSnapshot).where(TaskSnapshot.task_id == task_id).order_by(TaskSnapshot.iteration)).scalars().all()

def sync_workspace_artifacts(db: Session, task: Task) -> None:
    workspace = Path(task.workspace_path)
    if not workspace.exists():
        return
    existing = {row.path for row in db.execute(select(Artifact).where(Artifact.task_id == task.id)).scalars().all()}
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(workspace))
        if rel == "scratchpad.md" or rel.startswith("logs/"):
            kind = "log" if rel.startswith("logs/") else "scratchpad"
        elif rel.startswith("code/") or path.suffix in {".py", ".js", ".ts", ".tsx"}:
            kind = "code"
        elif rel.startswith("data/") or path.suffix in {".json", ".csv", ".xlsx"}:
            kind = "data"
        elif rel.startswith("artifacts/"):
            kind = "artifact"
        else:
            kind = "file"
        if rel not in existing:
            db.add(Artifact(task_id=task.id, path=rel, kind=kind, metadata_json={"synced": True, "size": path.stat().st_size}))
            existing.add(rel)
    db.flush()

@router.get("/tasks/{task_id}/artifacts")
def get_artifacts(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    sync_workspace_artifacts(db, task)
    db.commit()
    artifacts = db.execute(select(Artifact).where(Artifact.task_id == task_id).order_by(desc(Artifact.created_at))).scalars().all()
    return [
        {"id": str(a.id), "task_id": str(a.task_id), "path": a.path, "kind": a.kind, "metadata_json": a.metadata_json, "created_at": a.created_at.isoformat()}
        for a in artifacts
    ]

@router.post("/tasks/{task_id}/attachments")
async def upload_attachment(task_id: UUID, file: UploadFile = File(...), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "Сейчас поддерживаются только изображения")
    workspace = Path(task.workspace_path)
    safe_name = Path(file.filename or "image").name.replace("/", "_")
    target_rel = f"attachments/{safe_name}"
    target = safe_path(workspace, target_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await file.read())
    artifact = Artifact(task_id=task.id, path=target_rel, kind="image", metadata_json={"content_type": file.content_type, "attached_to_context": True})
    db.add(artifact)
    state = dict(task.current_state_json or {})
    state.setdefault("attachments", []).append({"path": target_rel, "content_type": file.content_type, "filename": safe_name})
    task.current_state_json = state
    add_event(db, task.id, "attachment", {"message": f"Изображение прикреплено к контексту: {safe_name}", "path": target_rel})
    db.commit(); db.refresh(artifact)
    return {"id": str(artifact.id), "path": artifact.path, "kind": artifact.kind}

@router.get("/tasks/{task_id}/tools", response_model=ToolConfig)
def get_tools(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    config = merged_tool_config(state)
    for server in config.get("mcp_servers", []):
        upsert_global_mcp_server(server)
    state["tool_config"] = config
    task.current_state_json = state
    db.commit()
    return ToolConfig(**config)

@router.put("/tasks/{task_id}/tools", response_model=ToolConfig)
def update_tools(task_id: UUID, payload: ToolConfig, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    state["tool_config"] = payload.model_dump()
    task.current_state_json = state
    add_event(db, task.id, "tools", {"message": "Настройки инструментов обновлены.", "tool_config": state["tool_config"]})
    db.commit(); db.refresh(task)
    return payload

@router.post("/tasks/{task_id}/mcp", response_model=ToolConfig)
def add_mcp_server(task_id: UUID, payload: MCPServerConfig, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    config = ToolConfig(**merged_tool_config(state)).model_dump()
    servers = [server for server in config.get("mcp_servers", []) if server.get("name") != payload.name]
    server_payload = payload.model_dump()
    servers.append(server_payload)
    upsert_global_mcp_server(server_payload)
    config["mcp_servers"] = servers
    config["mcp"] = True
    state["tool_config"] = config
    task.current_state_json = state
    add_event(db, task.id, "tools", {"message": f"MCP сервер добавлен: {payload.name}", "server": payload.model_dump()})
    db.commit(); db.refresh(task)
    return ToolConfig(**config)

@router.delete("/tasks/{task_id}/mcp/{server_name}", response_model=ToolConfig)
def delete_mcp_server(task_id: UUID, server_name: str, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    config = ToolConfig(**merged_tool_config(state)).model_dump()
    config["mcp_servers"] = [server for server in config.get("mcp_servers", []) if server.get("name") != server_name]
    delete_global_mcp_server(server_name)
    config["mcp"] = True
    state["tool_config"] = config
    task.current_state_json = state
    add_event(db, task.id, "tools", {"message": f"MCP сервер удален: {server_name}"})
    db.commit(); db.refresh(task)
    return ToolConfig(**config)

@router.get("/tasks/{task_id}/mcp/tools")
def list_task_mcp_tools(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    config = ToolConfig(**merged_tool_config(state)).model_dump()
    if not config.get("mcp"):
        return {"enabled": False, "tools": [], "message": "MCP выключен в настройках инструментов."}
    tools = list_mcp_tools_sync(config.get("mcp_servers", []), include_internal=True)
    return {"enabled": True, "tools": tools}

@router.post("/tasks/{task_id}/mcp/call")
def call_task_mcp_tool(task_id: UUID, payload: MCPToolCallRequest, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    state = dict(task.current_state_json or {})
    config = ToolConfig(**merged_tool_config(state)).model_dump()
    if not config.get("mcp"):
        raise HTTPException(409, "MCP выключен в настройках инструментов задачи")

    if payload.server_name == INTERNAL_SERVER_NAME:
        server = {"name": INTERNAL_SERVER_NAME, "url": "builtin://fetch", "transport": "builtin", "enabled": True}
    else:
        server = next((item for item in config.get("mcp_servers", []) if item.get("name") == payload.server_name), None)
        if not server:
            raise HTTPException(404, f"MCP сервер не найден: {payload.server_name}")

    try:
        result = call_mcp_tool_sync(server, payload.tool_name, payload.arguments, workspace_path=task.workspace_path)
    except Exception as exc:  # noqa: BLE001
        result = {
            "server_name": payload.server_name,
            "tool_name": payload.tool_name,
            "arguments": payload.arguments,
            "is_error": True,
            "content": [{"type": "text", "text": str(exc)}],
        }
        add_event(db, task.id, "mcp_error", {"message": f"Ошибка MCP tool call: {payload.server_name}.{payload.tool_name}", "error": str(exc)})

    calls = state.setdefault("mcp_calls", [])
    calls.append({"server_name": payload.server_name, "tool_name": payload.tool_name, "arguments": payload.arguments, "result": result})
    state["mcp_calls"] = calls[-20:]
    task.current_state_json = state

    artifact_path = f"artifacts/mcp_{payload.server_name}_{payload.tool_name}_{len(calls)}.json".replace("/", "_")
    workspace = Path(task.workspace_path)
    out_path = safe_path(workspace, artifact_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    artifact = Artifact(task_id=task.id, path=artifact_path, kind="mcp_result", metadata_json={"server_name": payload.server_name, "tool_name": payload.tool_name})
    db.add(artifact)
    add_event(db, task.id, "mcp_call", {"message": f"Выполнен MCP инструмент: {payload.server_name}.{payload.tool_name}", "arguments": payload.arguments, "artifact": artifact_path})
    db.commit(); db.refresh(artifact)
    return {"result": result, "artifact": {"id": str(artifact.id), "path": artifact.path, "kind": artifact.kind}}

@router.get("/tasks/{task_id}/artifacts/{artifact_id}/content")
def get_artifact_content(task_id: UUID, artifact_id: UUID, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    task = db.get(Task, task_id)
    if not task or not artifact or artifact.task_id != task.id:
        raise HTTPException(404, "Артефакт не найден")
    path = safe_path(Path(task.workspace_path), artifact.path)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Файл артефакта не найден")
    mime, _ = mimetypes.guess_type(path.name)
    text_types = {"text/markdown", "text/plain", "application/json", "text/csv", "text/x-python"}
    if (mime or "").startswith("text/") or mime in text_types or path.suffix.lower() in {".md", ".txt", ".json", ".csv", ".py", ".log"}:
        return {"id": str(artifact.id), "path": artifact.path, "kind": artifact.kind, "mime": mime or "text/plain", "content": path.read_text(encoding="utf-8", errors="replace")}
    return {"id": str(artifact.id), "path": artifact.path, "kind": artifact.kind, "mime": mime or "application/octet-stream", "binary": True, "message": "Бинарный файл доступен для скачивания."}

@router.get("/tasks/{task_id}/artifacts/{artifact_id}/download")
def download_artifact(task_id: UUID, artifact_id: UUID, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    task = db.get(Task, task_id)
    if not task or not artifact or artifact.task_id != task.id:
        raise HTTPException(404, "Артефакт не найден")
    path = safe_path(Path(task.workspace_path), artifact.path)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Файл артефакта не найден")
    return FileResponse(path, filename=path.name, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

@router.get("/tasks/{task_id}/stream")
def stream_events(task_id: UUID):
    async def gen():
        last = None
        while True:
            db = next(get_db())
            try:
                q = select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at)
                events = db.execute(q).scalars().all()
                for e in events:
                    marker = str(e.id)
                    if marker != last:
                        last = marker
                        yield f"event: task_event\ndata: {json.dumps({'id': marker, 'type': e.event_type, 'payload': e.payload_json, 'created_at': e.created_at.isoformat()}, default=str)}\n\n"
            finally:
                db.close()
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")
