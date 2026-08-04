from typing import TypedDict, Any

class AgentState(TypedDict, total=False):
    task_id: str
    goal: str
    iteration: int
    plan: list[dict[str, Any]]
    current_step: dict[str, Any]
    observation: dict[str, Any]
    reflection: dict[str, Any]
    executive_summary: str
    confidence: float
    low_confidence_streak: int
    awaiting_user: bool
    goal_completed: bool
    fatal_error: str | None
    artifacts: list[dict[str, Any]]
    events: list[dict[str, Any]]
