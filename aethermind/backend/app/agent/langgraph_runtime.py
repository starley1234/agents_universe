"""Optional native LangGraph runtime wiring.

AetherMind keeps durable persistence in PostgreSQL task snapshots. This module
adds a native LangGraph StateGraph facade over the same node functions so the
runtime can be switched gradually without changing agent logic.
"""

from typing import Any


def build_langgraph_app(agent_graph: Any):
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise RuntimeError("LangGraph is not installed. Install backend dependency `langgraph`.") from exc

    graph = StateGraph(dict)
    graph.add_node("plan", agent_graph.plan)
    graph.add_node("execute", agent_graph.execute)
    graph.add_node("observe", agent_graph.observe)
    graph.add_node("reflect", agent_graph.reflect)
    graph.add_node("summarize", agent_graph.summarize)

    graph.set_entry_point("plan")

    def after_plan(state: dict) -> str:
        if state.get("goal_completed") or state.get("awaiting_user"):
            return "end"
        return "execute"

    def after_reflect(state: dict) -> str:
        if state.get("iteration", 0) and state.get("iteration", 0) % 5 == 0:
            return "summarize"
        return "end"

    graph.add_conditional_edges("plan", after_plan, {"execute": "execute", "end": END})
    graph.add_edge("execute", "observe")
    graph.add_edge("observe", "reflect")
    graph.add_conditional_edges("reflect", after_reflect, {"summarize": "summarize", "end": END})
    graph.add_edge("summarize", END)
    return graph.compile()
