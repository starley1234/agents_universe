from app.config import settings
from app.db.models import TaskStatus

DANGEROUS_ACTIONS = {"delete_file", "send_form", "payment", "publish", "network_scan", "production_change"}

def check_budget(state: dict, budget: dict) -> tuple[bool, str]:
    if state.get("iteration", 0) >= budget.get("max_iterations", settings.default_max_iterations):
        return False, "max_iterations reached"
    if budget.get("tokens_used", 0) >= budget.get("token_budget", settings.default_token_budget):
        return False, "token budget reached"
    if budget.get("cost_used_usd", 0) >= budget.get("cost_budget_usd", settings.default_cost_budget_usd):
        return False, "cost budget reached"
    return True, "ok"

def needs_human_for_action(action: str) -> bool:
    return action in DANGEROUS_ACTIONS

def route_after_reflection(state: dict, budget: dict) -> TaskStatus:
    ok, _ = check_budget(state, budget)
    if not ok:
        return TaskStatus.SLEEPING
    if state.get("awaiting_user"):
        return TaskStatus.AWAITING_USER
    if state.get("goal_completed"):
        return TaskStatus.COMPLETED
    if state.get("low_confidence_streak", 0) >= settings.low_confidence_streak_limit:
        return TaskStatus.AWAITING_USER
    if state.get("fatal_error"):
        return TaskStatus.FAILED
    return TaskStatus.RUNNING
