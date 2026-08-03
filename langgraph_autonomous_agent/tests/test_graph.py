"""Graph structure test — verifies LangGraph compiles without errors."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_graph_compiles():
    from src.agent.graph import get_graph
    g = get_graph()
    assert g is not None
    # Should be callable
    assert hasattr(g, "ainvoke")


def test_state_schema():
    from src.agent.state import AgentState
    assert "task_id" in AgentState.__annotations__
    assert "plan" in AgentState.__annotations__
    assert "quality" in AgentState.__annotations__
    assert "messages" in AgentState.__annotations__


if __name__ == "__main__":
    test_graph_compiles()
    test_state_schema()
    print("All graph tests passed ✓")
