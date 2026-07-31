"""LangGraph-based agent — Plan ➜ Act ➜ Reflect loop.

Graph topology::

    START → retrieve_context → plan → act → reflect → router
                                                     ├─ "continue" → advance_step → act …
                                                     ├─ "done"     → END
                                                     └─ "halt"     → END
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from loguru import logger

from astra.core.circuit_breaker import should_halt
from astra.core.planner import generate_plan
from astra.core.reflector import reflect as do_reflect
from astra.core.state import AgentState
from astra.llm.gateway import llm_gateway
from astra.memory.semantic import semantic_memory
from astra.mcp.tool_registry import tool_registry

MAX_TOOL_ROUNDS = 5  # max tool-call sub-iterations inside ``act``


# ── Node: Retrieve Context ───────────────────────────────────
async def retrieve_context(state: AgentState) -> dict[str, Any]:
    """Pull relevant facts from semantic + ontology memory."""
    last_msg = state["messages"][-1].content if state["messages"] else ""
    logger.debug("Retrieving context for: {}", str(last_msg)[:80])

    semantic_ctx = await semantic_memory.search(
        query=str(last_msg),
        project_id=state["project_id"],
        top_k=5,
    )

    return {
        "retrieved_context": semantic_ctx,
        "ontology_context": "",
    }


# ── Node: Plan ───────────────────────────────────────────────
async def plan(state: AgentState) -> dict[str, Any]:
    """Generate the task plan (list of steps)."""
    goal = state["messages"][0].content if state["messages"] else "unknown"
    logger.info("📝  Planning…")
    steps = await generate_plan(
        goal=str(goal),
        completed_steps=state["completed_steps"],
        context=state["retrieved_context"],
    )
    return {
        "current_plan": steps,
        "current_step_index": 0,
        "completed_steps": [],
    }


# ── Node: Act ────────────────────────────────────────────────
async def act(state: AgentState) -> dict[str, Any]:
    """Execute the current plan step.

    Handles a sub-loop of tool calls: if the LLM requests tools they
    are executed and results fed back until the LLM produces a final
    text answer (or we exhaust ``MAX_TOOL_ROUNDS``).
    """
    plan_steps = state["current_plan"]
    idx = state["current_step_index"]
    step = plan_steps[idx] if idx < len(plan_steps) else "finish"

    logger.info("⚡  Acting on step [{}/{}]: {}", idx + 1, len(plan_steps), step)

    system_prompt = (
        f"You are A.S.T.R.A., an autonomous reasoning agent.\n"
        f"Current step ({idx + 1}/{len(plan_steps)}): {step}\n"
        f"Context from memory:\n{state.get('retrieved_context', '')}\n"
        f"Ontology:\n{state.get('ontology_context', '')}"
    )

    tools = await tool_registry.get_tools_for_project(state["project_id"])

    # Working copy — we don't mutate state directly
    working_messages: list = [SystemMessage(content=system_prompt)] + list(state["messages"])
    new_messages: list = []

    for round_idx in range(MAX_TOOL_ROUNDS):
        response: AIMessage = await llm_gateway.chat(
            messages=working_messages,
            tools=tools if tools else None,
        )
        new_messages.append(response)
        working_messages.append(response)

        # No tool calls → we're done for this step
        if not response.tool_calls:
            break

        logger.info("  🔧  Executing {} tool call(s)", len(response.tool_calls))

        for tc in response.tool_calls:
            try:
                result = await tool_registry.call_tool(tc["name"], tc["args"])
                tool_msg = ToolMessage(
                    content=str(result),
                    tool_call_id=tc["id"],
                    name=tc["name"],
                )
            except Exception as exc:
                logger.warning("Tool '{}' failed: {}", tc["name"], exc)
                tool_msg = ToolMessage(
                    content=f"Error: {exc}",
                    tool_call_id=tc["id"],
                    name=tc["name"],
                )
            new_messages.append(tool_msg)
            working_messages.append(tool_msg)

    return {"messages": new_messages}


# ── Node: Reflect + Circuit Breaker ──────────────────────────
async def reflect_and_halt(state: AgentState) -> dict[str, Any]:
    """Reflect on progress and decide whether to halt."""
    logger.info("🔍  Reflecting…")
    metrics = await do_reflect(
        messages=state["messages"],
        current_repetition=state["repetition_count"],
    )

    halted = should_halt(
        repetition_count=metrics["repetition_count"],
        entropy_score=metrics["entropy_score"],
    )

    if halted:
        logger.warning("🛑  Circuit breaker triggered!")

    step_done = state["current_plan"][state["current_step_index"]] \
        if state["current_step_index"] < len(state["current_plan"]) else ""

    return {
        "completed_steps": state["completed_steps"] + [step_done],
        "repetition_count": metrics["repetition_count"],
        "entropy_score": metrics["entropy_score"],
        "is_halted": halted,
    }


# ── Node: Advance Step ───────────────────────────────────────
async def advance_step(state: AgentState) -> dict[str, Any]:
    """Move to the next plan step."""
    new_idx = state["current_step_index"] + 1
    logger.info("  ➡️  Advancing to step {}/{}", new_idx + 1, len(state["current_plan"]))
    return {"current_step_index": new_idx}


# ── Conditional router ───────────────────────────────────────
def should_continue(state: AgentState) -> str:
    if state["is_halted"]:
        return "halt"
    if state["current_step_index"] >= len(state["current_plan"]) - 1:
        return "done"
    return "continue"


# ── Build & compile graph ────────────────────────────────────
def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("retrieve", retrieve_context)
    graph.add_node("plan", plan)
    graph.add_node("act", act)
    graph.add_node("reflect", reflect_and_halt)
    graph.add_node("advance", advance_step)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "plan")
    graph.add_edge("plan", "act")
    graph.add_edge("act", "reflect")
    graph.add_edge("advance", "retrieve")  # loop back for next step

    graph.add_conditional_edges(
        "reflect",
        should_continue,
        {
            "continue": "advance",
            "done": END,
            "halt": END,
        },
    )

    return graph.compile()


# Singleton compiled graph
agent_graph = build_graph()
