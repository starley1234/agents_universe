from uuid import UUID
from sqlalchemy.orm import Session
from app.db.models import TaskEvent

def add_event(db: Session, task_id: UUID, event_type: str, payload: dict) -> TaskEvent:
    event = TaskEvent(task_id=task_id, event_type=event_type, payload_json=payload)
    db.add(event)
    db.flush()
    return event
