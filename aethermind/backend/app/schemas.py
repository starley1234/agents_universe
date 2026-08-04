from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, field_validator
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

class MCPServerConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    url: str = Field(min_length=1, max_length=500)
    transport: str = "sse"
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("MCP URL должен начинаться с http:// или https://")
        return value

class ToolConfig(BaseModel):
    llm: bool = True
    filesystem: bool = True
    code_interpreter: bool = True
    headless_browser: bool = False
    mcp: bool = True
    dangerous_actions: bool = False
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)

class MCPToolCallRequest(BaseModel):
    server_name: str = Field(min_length=1, max_length=80)
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: dict = Field(default_factory=dict)

class AgentSettingsRead(BaseModel):
    project_name: str
    environment: str
    llm_active_provider: str
    default_model: str
    embedding_dimensions: int
    summary_every_iterations: int
    low_confidence_threshold: float
    low_confidence_streak_limit: int
    default_max_iterations: int
    planner_min_steps: int
    workspace_path: str

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
