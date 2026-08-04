from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from app.db.models import TaskStatus

class Budget(BaseModel):
    max_iterations: int = 25
    token_budget: int = 100000
    cost_budget_usd: float = 25
    time_budget_seconds: int = 14400
    tokens_used: int = 0
    cost_used_usd: float = 0
    tool_calls_used: int = 0

class TaskCreate(BaseModel):
    goal: str = Field(min_length=3)
    budget: Budget | None = None

class InterveneRequest(BaseModel):
    message: str = Field(min_length=1)
    resume: bool = True

class RollbackRequest(BaseModel):
    snapshot_id: UUID | None = None
    iteration: int | None = None
    new_instruction: str | None = None

class TaskRead(BaseModel):
    id: UUID
    goal: str
    status: TaskStatus
    current_state_json: dict
    budget_json: dict
    workspace_path: str
    created_at: datetime
    updated_at: datetime
    class Config:
        from_attributes = True

class EventRead(BaseModel):
    id: UUID
    task_id: UUID
    event_type: str
    payload_json: dict
    created_at: datetime
    class Config:
        from_attributes = True

class SnapshotRead(BaseModel):
    id: UUID
    task_id: UUID
    iteration: int
    state_json: dict
    confidence: float
    created_at: datetime
    class Config:
        from_attributes = True
