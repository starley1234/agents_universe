import asyncio
import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from app.config import settings
from app.db.models import Artifact, Task, TaskEvent, TaskSnapshot, TaskStatus
from app.db.session import get_db
from app.schemas import Budget, EventRead, InterveneRequest, RollbackRequest, SnapshotRead, TaskCreate, TaskRead
from app.services.events import add_event
from app.services.workspace import task_workspace
from app.worker import run_agent_iteration

router = APIRouter()

def default_budget() -> dict:
    return Budget(
        max_iterations=settings.default_max_iterations,
        token_budget=settings.default_token_budget,
        cost_budget_usd=settings.default_cost_budget_usd,
        time_budget_seconds=settings.default_time_budget_seconds,
    ).model_dump()

@router.get("/health")
def health():
    return {"status": "ok", "project": settings.project_name}

@router.post("/tasks", response_model=TaskRead)
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    task = Task(goal=payload.goal, status=TaskStatus.PENDING, budget_json=(payload.budget.model_dump() if payload.budget else default_budget()), workspace_path="")
    db.add(task)
    db.flush()
    ws = task_workspace(task.id)
    task.workspace_path = str(ws)
    task.current_state_json = {"task_id": str(task.id), "goal": task.goal, "iteration": 0, "events": [], "artifacts": []}
    add_event(db, task.id, "created", {"message": "Task created", "goal": task.goal})
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
        raise HTTPException(404, "Task not found")
    return task

@router.post("/tasks/{task_id}/pause", response_model=TaskRead)
def pause_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = TaskStatus.PAUSED
    add_event(db, task.id, "paused", {"message": "Paused by user"})
    db.commit(); db.refresh(task)
    return task

@router.post("/tasks/{task_id}/resume", response_model=TaskRead)
def resume_task(task_id: UUID, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    task.status = TaskStatus.RUNNING
    state = dict(task.current_state_json or {})
    state["awaiting_user"] = False
    task.current_state_json = state
    add_event(db, task.id, "resumed", {"message": "Resumed by user"})
    db.commit(); db.refresh(task)
    run_agent_iteration.delay(str(task.id))
    return task

@router.post("/tasks/{task_id}/intervene", response_model=TaskRead)
def intervene(task_id: UUID, payload: InterveneRequest, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
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
        raise HTTPException(404, "Task not found")
    query = select(TaskSnapshot).where(TaskSnapshot.task_id == task.id)
    if payload.snapshot_id:
        query = query.where(TaskSnapshot.id == payload.snapshot_id)
    elif payload.iteration is not None:
        query = query.where(TaskSnapshot.iteration == payload.iteration).order_by(desc(TaskSnapshot.created_at))
    else:
        query = query.order_by(desc(TaskSnapshot.created_at))
    snapshot = db.execute(query).scalars().first()
    if not snapshot:
        raise HTTPException(404, "Snapshot not found")
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

@router.get("/tasks/{task_id}/artifacts")
def get_artifacts(task_id: UUID, db: Session = Depends(get_db)):
    artifacts = db.execute(select(Artifact).where(Artifact.task_id == task_id).order_by(Artifact.created_at)).scalars().all()
    return [
        {"id": str(a.id), "task_id": str(a.task_id), "path": a.path, "kind": a.kind, "metadata_json": a.metadata_json, "created_at": a.created_at.isoformat()}
        for a in artifacts
    ]

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
