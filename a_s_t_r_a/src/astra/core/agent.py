"""LangGraph agent — Plan-Act-Reflect with robust empty handling and math fallback."""

from __future__ import annotations

import ast
import operator
import re
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from loguru import logger

from astra.core.circuit_breaker import should_halt
from astra.core.planner import generate_plan
from astra.core.reflector import reflect as do_reflect
from astra.core.state import AgentState
from astra.llm.gateway import llm_gateway
from astra.memory.ontology import ontology_store
from astra.memory.semantic import semantic_memory
from astra.mcp.tool_registry import tool_registry
from astra.prompts.registry import prompt_registry

MAX_TOOL_ROUNDS = 5


def _safe_math_eval(expr: str) -> str | None:
    if not re.fullmatch(r"[\d\+\-\*\/\(\)\s\.]+", expr.strip()):
        return None
    if len(expr) > 50:
        return None
    try:
        allowed_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.Mod: operator.mod,
            ast.USub: operator.neg,
        }

        def _eval(node):
            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                raise ValueError("Not number")
            if isinstance(node, ast.BinOp):
                left = _eval(node.left)
                right = _eval(node.right)
                op_type = type(node.op)
                if op_type in allowed_ops:
                    return allowed_ops[op_type](left, right)
                raise ValueError("Bad op")
            if isinstance(node, ast.UnaryOp):
                operand = _eval(node.operand)
                op_type = type(node.op)
                if op_type in allowed_ops:
                    return allowed_ops[op_type](operand)
                raise ValueError("Bad unary")
            raise ValueError("Bad node")

        tree = ast.parse(expr.strip(), mode="eval")
        result = _eval(tree.body)
        return str(result)
    except Exception:
        return None


async def retrieve_context(state: AgentState) -> dict[str, Any]:
    goal = state["messages"][0].content if state["messages"] else ""
    goal = str(goal)[:500]
    logger.debug("Retrieving context for: {}", goal[:80])

    try:
        semantic_ctx = await semantic_memory.search(query=goal, project_id=state["project_id"], top_k=5)
    except Exception as exc:
        logger.warning("Semantic search failed: {}", exc)
        semantic_ctx = ""

    ontology_ctx = ""
    try:
        kw = goal.split()[0] if goal else ""
        if kw:
            ontology_ctx = ontology_store.get_subgraph_text(state["project_id"], kw, depth=1)
    except Exception:
        pass

    return {
        "retrieved_context": semantic_ctx[:1000] if semantic_ctx else "",
        "ontology_context": ontology_ctx[:600] if ontology_ctx else "",
    }


async def plan(state: AgentState) -> dict[str, Any]:
    if state.get("current_plan"):
        logger.debug("Plan exists with {} steps, skipping re-plan", len(state["current_plan"]))
        return {}

    goal = state["messages"][0].content if state["messages"] else "unknown"
    logger.info("📝 Planning for goal: {}", str(goal)[:100])
    steps = await generate_plan(
        goal=str(goal),
        completed_steps=state.get("completed_steps", []),
        context=state.get("retrieved_context", ""),
    )
    return {"current_plan": steps, "current_step_index": 0}


def _get_act_system_prompt() -> str:
    try:
        p = prompt_registry.get("agent_system")
        if p:
            return p
    except Exception:
        pass
    return (
        "You are A.S.T.R.A., autonomous agent. NEVER return empty. "
        "Execute the current step concisely (2-5 sentences). "
    )


async def act(state: AgentState) -> dict[str, Any]:
    plan_steps = state["current_plan"]
    idx = state["current_step_index"]
    step = plan_steps[idx] if idx < len(plan_steps) else "finish"
    full_goal = str(state["messages"][0].content) if state["messages"] else ""

    logger.info("⚡ Acting on step [{}/{}]: {}", idx + 1, len(plan_steps), step)

    extra_ctx = ""
    try:
        extra_ctx = await semantic_memory.search(query=step, project_id=state["project_id"], top_k=2)
    except Exception:
        pass

    system_base = _get_act_system_prompt()
    system_prompt = (
        f"{system_base}\n\n"
        f"Goal: {full_goal}\n"
        f"Step {idx + 1}/{len(plan_steps)}: {step}\n"
        f"Completed: {state.get('completed_steps', [])}\n"
        f"Memory: {(state.get('retrieved_context', '') or '')[:600]}\n"
        f"Extra: {extra_ctx[:400]}\n"
    )

    try:
        tools = await tool_registry.get_tools_for_project(state["project_id"])
    except Exception:
        tools = []

    working_messages: list = [SystemMessage(content=system_prompt)] + list(state["messages"][-4:])
    new_messages: list = []

    for round_idx in range(MAX_TOOL_ROUNDS):
        try:
            response: AIMessage = await llm_gateway.chat(
                messages=working_messages,
                tools=tools if tools else None,
                temperature=0.6,
                max_tokens=1024,
                metadata={"prompt": "agent_act", "step": step, "index": idx},
            )
        except Exception as exc:
            logger.error("LLM chat failed in act: {}", exc)
            response = AIMessage(content=f"Step '{step}' attempted but LLM error: {exc}. Continuing with best effort answer for goal: {full_goal}")

        content = (response.content or "").strip()
        has_tools = bool(response.tool_calls)

        is_empty = False
        if not content and not has_tools:
            is_empty = True
        elif content and len(content) < 60 and ("awaiting" in content.lower() or "system_mode" in content.lower()):
            is_empty = True

        if is_empty and not has_tools:
            logger.warning("Empty LLM response on step {}/{}, round {}/{}", idx + 1, len(plan_steps), round_idx + 1, MAX_TOOL_ROUNDS)
            if round_idx == 0:
                # Retry with stricter prompt, try math fallback if applicable
                math_fallback = _safe_math_eval(full_goal.strip()) or _safe_math_eval(step.strip())
                if math_fallback:
                    # If it's simple math, use math result as fallback content but still continue to try LLM once more
                    fallback_system = (
                        f"You MUST execute this step. Step is math: {step} / Goal: {full_goal}. "
                        f"Computed result via calculator is {math_fallback}. "
                        f"Explain it in 2-3 sentences, e.g. '{full_goal.strip()} = {math_fallback}' and what it means."
                    )
                else:
                    fallback_system = (
                        f"You MUST execute this step, never return empty: {step}\n"
                        f"Overall goal: {full_goal}\n"
                        f"Give concise helpful answer in 3-5 sentences, no JSON, no awaiting."
                    )
                working_messages = [SystemMessage(content=fallback_system), working_messages[-1]]
                continue
            else:
                # Final fallback: math or synthetic
                math_result = _safe_math_eval(full_goal.strip()) or _safe_math_eval(step.strip())
                if math_result is not None:
                    synthetic = f"Вычисление: {full_goal.strip() or step.strip()} = {math_result} (fallback, LLM вернул пусто)."
                else:
                    synthetic = (
                        f"Выполнен шаг {idx+1}/{len(plan_steps)}: {step}. "
                        f"Цель: {full_goal}. "
                        f"Предыдущие шаги: {state.get('completed_steps', [])}. "
                        f"LLM вернул пусто, использован fallback."
                    )
                response = AIMessage(content=synthetic)

        new_messages.append(response)
        working_messages.append(response)

        if not response.tool_calls:
            break

        logger.info("  🔧 {} tool calls (round {}/{})", len(response.tool_calls), round_idx + 1, MAX_TOOL_ROUNDS)
        for tc in response.tool_calls:
            try:
                result = await tool_registry.call_tool(tc["name"], tc["args"])
                tool_msg = ToolMessage(content=str(result)[:3000], tool_call_id=tc["id"], name=tc["name"])
            except Exception as exc:
                tool_msg = ToolMessage(content=f"Error: {exc}", tool_call_id=tc["id"], name=tc["name"])
            new_messages.append(tool_msg)
            working_messages.append(tool_msg)

    return {"messages": new_messages}


async def reflect_and_halt(state: AgentState) -> dict[str, Any]:
    logger.info("🔍 Reflecting step {}/{}", state["current_step_index"] + 1, len(state["current_plan"]))
    try:
        metrics = await do_reflect(messages=state["messages"], current_repetition=state["repetition_count"])
    except Exception as exc:
        logger.warning("Reflect failed: {}", exc)
        metrics = {"repetition_count": state["repetition_count"], "entropy_score": 0.7}

    halted = should_halt(metrics["repetition_count"], metrics["entropy_score"])
    if halted:
        logger.warning("🛑 Circuit breaker: rep={}, entropy={:.2f}", metrics["repetition_count"], metrics["entropy_score"])

    step_done = state["current_plan"][state["current_step_index"]] if state["current_step_index"] < len(state["current_plan"]) else ""

    return {
        "completed_steps": state["completed_steps"] + [step_done],
        "repetition_count": metrics["repetition_count"],
        "entropy_score": metrics["entropy_score"],
        "is_halted": halted,
    }


async def advance_step(state: AgentState) -> dict[str, Any]:
    new_idx = state["current_step_index"] + 1
    logger.info("  ➡️ Advancing to {}/{}", new_idx + 1, len(state["current_plan"]))
    return {"current_step_index": new_idx}


def should_continue(state: AgentState) -> str:
    if state["is_halted"]:
        return "halt"
    if state["current_step_index"] >= len(state["current_plan"]) - 1:
        return "done"
    return "continue"


def build_graph():
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
    graph.add_conditional_edges("reflect", should_continue, {"continue": "advance", "done": END, "halt": END})
    graph.add_edge("advance", "act")
    return graph.compile()


agent_graph = build_graph()
