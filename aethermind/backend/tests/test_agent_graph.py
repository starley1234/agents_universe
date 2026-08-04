from app.agent.graph import AgentGraph

def test_agent_graph_completes_step(tmp_path):
    state = {"goal": "test goal", "iteration": 0, "events": []}
    next_state = AgentGraph(tmp_path).run_one_iteration(state)
    assert next_state["iteration"] == 1
    assert next_state["confidence"] > 0.5
    assert (tmp_path / "scratchpad.md").exists()
