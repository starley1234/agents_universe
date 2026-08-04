from app.db.models import TaskStatus
from app.services.guardrails import check_budget, needs_human_for_action, route_after_reflection

def test_budget_max_iterations():
    ok, reason = check_budget({"iteration": 5}, {"max_iterations": 5})
    assert not ok
    assert "max_iterations" in reason

def test_dangerous_action_requires_human():
    assert needs_human_for_action("delete_file")
    assert not needs_human_for_action("write_file")

def test_route_completed():
    assert route_after_reflection({"goal_completed": True}, {"max_iterations": 10}) == TaskStatus.COMPLETED
