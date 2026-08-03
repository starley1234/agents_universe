"""Streaming agent runner — uses same logic as agent.py, always calls LLM, math as fallback."""

from __future__ import annotations

import ast
import operator
import re
from typing import AsyncIterator, Any
from uuid import UUID

from loguru import logger

from astra.core.state import make_initial_state
from astra.core.planner import generate_plan
from astra.core.reflector import reflect as do_reflect
from astra.core.circuit_breaker import should_halt
from astra.llm.gateway import llm_gateway
from astra.memory.semantic import semantic_memory
from astra.memory.ontology import ontology_store
from astra.mcp.tool_registry import tool_registry
from astra.prompts.registry import prompt_registry

from langchain_core.messages import SystemMessage, AIMessage, ToolMessage, HumanMessage


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


def _get_act_prompt() -> str:
    try:
        p = prompt_registry.get("agent_system")
        if p:
            return p
    except Exception:
        pass
    return "You are A.S.T.R.A. NEVER return empty. Execute step concisely."


async def stream_agent(
    session_id: UUID,
    project_id: UUID,
    goal: str,
) -> AsyncIterator[dict[str, Any]]:
    yield {"event": "start", "data": {"session_id": str(session_id), "project_id": str(project_id), "goal": goal}}

    state: dict[str, Any] = make_initial_state(session_id=session_id, project_id=project_id, goal=goal)

    try:
        yield {"event": "retrieve_context", "data": {"status": "started", "goal": goal}}
        try:
            semantic_ctx = await semantic_memory.search(query=goal, project_id=project_id, top_k=5)
        except Exception as exc:
            semantic_ctx = ""
            logger.warning("Retrieve failed: {}", exc)

        try:
            kw = goal.split()[0] if goal else ""
            ontology_ctx = ontology_store.get_subgraph_text(project_id, kw, depth=1) if kw else ""
        except Exception:
            ontology_ctx = ""

        state["retrieved_context"] = semantic_ctx[:1000] if semantic_ctx else ""
        state["ontology_context"] = ontology_ctx[:600] if ontology_ctx else ""
        yield {"event": "retrieve_context", "data": {"status": "done", "context_chars": len(state["retrieved_context"])}}

        yield {"event": "plan_start", "data": {"goal": goal}}
        steps = await generate_plan(goal=goal, completed_steps=[], context=state["retrieved_context"])
        state["current_plan"] = steps
        state["current_step_index"] = 0
        yield {"event": "plan_generated", "data": {"steps": steps, "count": len(steps)}}

        act_system_base = _get_act_prompt()

        for idx, step in enumerate(steps):
            state["current_step_index"] = idx
            yield {"event": "step_start", "data": {"index": idx, "total": len(steps), "step": step}}

            try:
                extra_ctx = await semantic_memory.search(query=step, project_id=project_id, top_k=2)
            except Exception:
                extra_ctx = ""

            system_prompt = (
                f"{act_system_base}\n\n"
                f"Goal: {goal}\n"
                f"Step {idx+1}/{len(steps)}: {step}\n"
                f"Completed: {state.get('completed_steps', [])}\n"
                f"Memory: {state.get('retrieved_context','')[:600]}\n"
                f"Extra: {extra_ctx[:400]}\n"
            )

            try:
                tools = await tool_registry.get_tools_for_project(project_id)
            except Exception:
                tools = []

            working_messages = [SystemMessage(content=system_prompt), HumanMessage(content=goal)]
            if state.get("messages"):
                working_messages.extend(list(state["messages"])[-6:])

            for round_idx in range(5):
                yield {"event": "llm_call", "data": {"step": idx, "round": round_idx, "provider": "local"}}

                try:
                    response: AIMessage = await llm_gateway.chat(
                        messages=working_messages,
                        tools=tools if tools else None,
                        temperature=0.6,
                        max_tokens=1024,
                        metadata={"prompt": "agent_act_stream", "step": step, "index": idx},
                    )
                except Exception as exc:
                    logger.error("LLM failed in streaming act: {}", exc)
                    response = AIMessage(content=f"Step '{step}' error: {exc}. Continuing.")

                content = (response.content or "").strip()
                has_tools = bool(response.tool_calls)

                is_empty = False
                if not content and not has_tools:
                    is_empty = True
                elif content and len(content) < 60 and ("awaiting" in content.lower() or "system_mode" in content.lower()):
                    is_empty = True

                if is_empty and round_idx == 0:
                    # Try math fallback as part of retry prompt
                    math_fallback = _safe_math_eval(goal.strip()) or _safe_math_eval(step.strip())
                    if math_fallback:
                        fallback_msg = (
                            f"You MUST execute this step. Step is math: {step} / Goal: {goal}. "
                            f"Computed result via calculator is {math_fallback}. "
                            f"Explain it in 2-3 sentences, e.g. '{goal.strip()} = {math_fallback}' and what it means. "
                            f"LLM previously returned empty, now use math result."
                        )
                    else:
                        fallback_msg = f"Execute this step, never empty: {step}. Goal: {goal}. Give 3-5 sentences."
                    yield {"event": "retry", "data": {"index": idx, "reason": "empty response, retrying with fallback"}}
                    fallback = SystemMessage(content=fallback_msg)
                    working_messages = [fallback, HumanMessage(content=goal)]
                    continue
                elif is_empty:
                    math_result = _safe_math_eval(goal.strip()) or _safe_math_eval(step.strip())
                    if math_result is not None:
                        synthetic = f"Вычисление: {goal.strip() or step.strip()} = {math_result} (fallback, LLM вернул пусто, но математика посчитана)."
                    else:
                        synthetic = f"Выполнен шаг {idx+1}/{len(steps)}: {step}. Цель: {goal}. LLM вернул пусто, использован fallback."
                    response = AIMessage(content=synthetic)
                    content = synthetic

                yield {
                    "event": "step_act",
                    "data": {"index": idx, "content": content, "has_tools": has_tools, "thinking": f"Executing {step}"},
                }

                working_messages.append(response)
                state["messages"] = state.get("messages", []) + [response]

                if not response.tool_calls:
                    break

                for tc in response.tool_calls:
                    yield {"event": "tool_call", "data": {"name": tc["name"], "args": tc["args"]}}
                    try:
                        result = await tool_registry.call_tool(tc["name"], tc["args"])
                        tool_msg = ToolMessage(content=str(result)[:3000], tool_call_id=tc["id"], name=tc["name"])
                        yield {"event": "tool_result", "data": {"name": tc["name"], "result": str(result)[:1500]}}
                    except Exception as exc:
                        tool_msg = ToolMessage(content=f"Error: {exc}", tool_call_id=tc["id"], name=tc["name"])
                        yield {"event": "tool_result", "data": {"name": tc["name"], "error": str(exc)}}

                    working_messages.append(tool_msg)
                    state["messages"] = state["messages"] + [tool_msg]

            yield {"event": "reflect", "data": {"index": idx, "status": "started"}}
            try:
                metrics = await do_reflect(messages=state["messages"], current_repetition=state["repetition_count"])
            except Exception as exc:
                logger.warning("Reflect failed: {}", exc)
                metrics = {"repetition_count": state["repetition_count"], "entropy_score": 0.7}

            halted = should_halt(metrics["repetition_count"], metrics["entropy_score"])
            state["repetition_count"] = metrics["repetition_count"]
            state["entropy_score"] = metrics["entropy_score"]
            state["is_halted"] = halted
            state["completed_steps"] = state.get("completed_steps", []) + [step]

            yield {
                "event": "reflect",
                "data": {
                    "index": idx,
                    "repetition": metrics["repetition_count"],
                    "entropy": metrics["entropy_score"],
                    "halted": halted,
                },
            }

            if halted:
                yield {"event": "halted", "data": {"reason": "circuit breaker"}}
                break

        ai_messages = [m for m in state.get("messages", []) if getattr(m, "type", None) == "ai" and (m.content or "").strip()]
        if not ai_messages:
            ai_messages = [m for m in state.get("messages", []) if getattr(m, "type", None) == "ai"]
        final_result = ai_messages[-1].content if ai_messages else "Agent completed but result empty — check logs."

        yield {
            "event": "done",
            "data": {
                "status": "halted" if state.get("is_halted") else "completed",
                "result": final_result,
                "steps_completed": len(state.get("completed_steps", [])),
            },
        }

    except Exception as exc:
        logger.exception("Streaming agent failed")
        yield {"event": "error", "data": {"error": str(exc)}}
