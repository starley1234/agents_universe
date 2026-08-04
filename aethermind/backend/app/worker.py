from celery import Celery
from sqlalchemy import select
from uuid import UUID
from app.config import settings
from app.db.session import SessionLocal
from app.db.models import Artifact, Task, TaskSnapshot, TaskStatus
from app.agent.graph import AgentGraph
from app.services.events import add_event
from app.services.guardrails import route_after_reflection
from app.services.workspace import task_workspace

celery_app = Celery("aethermind", broker=settings.celery_broker_url, backend=settings.celery_result_backend)
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1

@celery_app.task(name="run_agent_iteration", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def run_agent_iteration(task_id: str) -> dict:
    db = SessionLocal()
    try:
        task = db.get(Task, UUID(task_id))
        if not task:
            return {"status": "missing"}
        if task.status in {TaskStatus.PAUSED, TaskStatus.AWAITING_USER, TaskStatus.SLEEPING, TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return {"status": task.status.value}
        task.status = TaskStatus.RUNNING
        workspace = task_workspace(task.id)
        state = dict(task.current_state_json or {})
        state.setdefault("task_id", str(task.id))
        state.setdefault("goal", task.goal)
        state.setdefault("iteration", 0)
        state["events"] = []
        graph = AgentGraph(workspace)
        state = graph.run_one_iteration(state)
        budget = dict(task.budget_json or {})
        llm_usage = state.get("llm_usage", {})
        budget["tokens_used"] = int(llm_usage.get("tokens_used", budget.get("tokens_used", 0)) or 0)
        budget["cost_used_usd"] = float(llm_usage.get("cost_used_usd", budget.get("cost_used_usd", 0.0)) or 0.0)
        budget["llm_calls"] = int(llm_usage.get("calls", budget.get("llm_calls", 0)) or 0)
        task.budget_json = budget
        task.current_state_json = state
        task.status = route_after_reflection(state, budget)
        db.add(TaskSnapshot(task_id=task.id, iteration=state.get("iteration", 0), state_json=state, confidence=state.get("confidence", 1)))
        for artifact in state.get("artifacts", []):
            exists = db.execute(select(Artifact).where(Artifact.task_id == task.id, Artifact.path == artifact["path"])).scalar_one_or_none()
            if not exists:
                db.add(Artifact(task_id=task.id, path=artifact["path"], kind=artifact.get("kind", "file"), metadata_json=artifact))
        for event in state.get("events", []):
            add_event(db, task.id, event.get("type", "event"), event)
        db.commit()
        if task.status == TaskStatus.RUNNING:
            run_agent_iteration.apply_async(args=[str(task.id)], countdown=1)
        return {"status": task.status.value, "iteration": state.get("iteration", 0)}
    finally:
        db.close()
