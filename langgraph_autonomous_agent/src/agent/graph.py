"""LangGraph autonomous agent — plan → retrieve → execute → reflect → (loop|finalize).

  ┌──────┐   ┌──────────────┐   ┌─────────┐   ┌─────────┐
  │ plan │──▶│ retrieve_mem │──▶│ execute │──▶│ reflect │──┐
  └──────┘   └──────────────┘   └─────────┘   └─────────┘  │
     ▲                                            │         │
     │         ┌──────────┐                       │         │
     └─────────│ finalize │◀──────────────────────┘         │
               └──────────┘      conditional                │
     ▲                                                       │
     └───────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from src.agent.state import AgentState
from src.config import get_settings

log = logging.getLogger(__name__)
_cfg = get_settings()


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────
def _parse_json(text: str) -> list | dict:
    """Extract JSON from LLM response (may be wrapped in ```json ... ```)."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    s = m.group(1) if m else text
    if not m:
        m2 = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if m2:
            s = m2.group(0)
    return json.loads(s.strip())


# ────────────────────────────────────────────────────────────────────────
# plan
# ────────────────────────────────────────────────────────────────────────
async def _plan(state: AgentState) -> dict[str, Any]:
    from src.agent.llm import get_llm

    llm = get_llm(temperature=0.7, max_tokens=8192)
    task = state["task_description"]
    it = state.get("iteration", 0)
    results = state.get("results", [])
    errors = state.get("errors", [])
    mem = state.get("memory_context", "")

    if it == 0:
        sys = (
            "You are an expert autonomous agent planner.\n"
            "Break the task into 3-15 concrete, executable steps.\n"
            "Return ONLY a JSON array:\n"
            '[{"id":1,"description":"...","expected_output":"...","status":"pending"}]'
        )
        usr = f"Task: {task}"
        if mem:
            usr += f"\n\nRelevant context:\n{mem}"
    else:
        recent = "\n".join(f"Step {r.get('step','?')}: {str(r.get('result',''))[:200]}" for r in results[-5:])
        errs = "\n".join(errors[-3:]) or "None"
        sys = (
            "Re-plan after partial execution. Remove completed steps, add corrective steps for errors.\n"
            "Return ONLY a JSON array of REMAINING steps (re-numbered from 1)."
        )
        usr = f"Task: {task}\nIteration: {it}\nCompleted:\n{recent}\nErrors:\n{errs}"

    try:
        resp = await llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=usr)])
        raw = _parse_json(resp.content)
        if not isinstance(raw, list):
            raw = [raw]
        plan = [{"id": s.get("id", i + 1),
                 "description": s.get("description", str(s)),
                 "expected_output": s.get("expected_output", ""),
                 "status": "pending"} for i, s in enumerate(raw)]
    except Exception as e:
        log.error("Planning failed: %s", e)
        plan = [{"id": 1, "description": task, "expected_output": "Complete", "status": "pending"}]

    done = sum(1 for r in results)
    progress = done / max(len(plan), 1) * 100
    log.info("Plan: %d steps (iter %d)", len(plan), it)
    return {"plan": plan, "current_step": 0, "status": "planning",
            "progress": min(progress, 95),
            "messages": [AIMessage(content=f"📋 Plan: {len(plan)} steps (iter {it})")]}


# ────────────────────────────────────────────────────────────────────────
# retrieve_memory
# ────────────────────────────────────────────────────────────────────────
async def _retrieve(state: AgentState) -> dict[str, Any]:
    from src.db.engine import db_session
    from src.memory.combined_rag import CombinedRAG

    query = state["task_description"]
    plan = state.get("plan", [])
    idx = state.get("current_step", 0)
    if plan and idx < len(plan):
        query += " " + plan[idx].get("description", "")

    try:
        async with db_session() as db:
            rag = CombinedRAG(db)
            ctx = await rag.retrieve(query, top_k=5)
        text = "\n".join(f"  • [{c.get('source','?')}] {c['content'][:200]}" for c in ctx[:5]) if ctx else "(none)"
        return {"memory_context": "\n".join(c["content"] for c in ctx) if ctx else "",
                "messages": [AIMessage(content=f"🧠 Retrieved {len(ctx)} memories:\n{text}")]}
    except Exception as e:
        log.warning("Memory retrieval failed: %s", e)
        return {"memory_context": "", "messages": [AIMessage(content=f"⚠️ Memory skipped: {e}")]}


# ────────────────────────────────────────────────────────────────────────
# execute
# ────────────────────────────────────────────────────────────────────────
async def _execute(state: AgentState) -> dict[str, Any]:
    from src.agent.llm import get_llm
    from src.agent.tools import get_all_tools, run_tool_calls

    plan = state.get("plan", [])
    idx = state.get("current_step", 0)
    if idx >= len(plan):
        return {"status": "completed", "messages": [AIMessage(content="✅ All steps done.")]}

    step = plan[idx]
    desc = step.get("description", "")
    sid = step.get("id", idx + 1)
    t0 = time.time()

    tools = await get_all_tools()
    llm = get_llm()
    llm_t = llm.bind_tools(tools) if tools else llm

    sys = (
        f"You are executing step {sid} of a plan.\n"
        f"Task: {state['task_description']}\n"
        f"Context: {state.get('memory_context', '')[:2000]}\n"
        "Use tools as needed. Be thorough."
    )
    try:
        resp = await llm_t.ainvoke([SystemMessage(content=sys),
                                    HumanMessage(content=f"Execute: {desc}")])
        tool_res: list[dict] = []
        if hasattr(resp, "tool_calls") and resp.tool_calls:
            tool_res = await run_tool_calls(resp.tool_calls, tools)

        dur = time.time() - t0
        out = resp.content if hasattr(resp, "content") else str(resp)
        if tool_res:
            out += "\nTools:\n" + "\n".join(
                f"  🔧 {t['tool']}: {t['result'][:300]}" for t in tool_res)

        new_plan = list(plan)
        new_plan[idx] = {**step, "status": "completed", "result": out[:2000], "duration_s": dur}
        done = sum(1 for s in new_plan if s.get("status") == "completed")
        progress = done / max(len(new_plan), 1) * 100
        log.info("Step %d done in %.1fs (progress %.0f%%)", sid, dur, progress)
        return {"plan": new_plan, "current_step": idx + 1,
                "results": [{"step": sid, "description": desc, "result": out[:2000], "duration_s": dur}],
                "status": "executing", "progress": progress,
                "messages": [AIMessage(content=f"⚙️ Step {sid} ({dur:.1f}s):\n{out[:500]}")]}
    except Exception as e:
        dur = time.time() - t0
        log.error("Step %d failed: %s", sid, e)
        new_plan = list(plan)
        new_plan[idx] = {**step, "status": "failed", "error": str(e)}
        return {"plan": new_plan,
                "errors": [f"Step {sid}: {str(e)[:500]}"],
                "status": "executing",
                "messages": [AIMessage(content=f"❌ Step {sid} failed: {e}")]}


# ────────────────────────────────────────────────────────────────────────
# reflect
# ────────────────────────────────────────────────────────────────────────
async def _reflect(state: AgentState) -> dict[str, Any]:
    from src.agent.llm import get_llm

    plan = state.get("plan", [])
    results = state.get("results", [])
    errors = state.get("errors", [])
    it = state.get("iteration", 0)
    mx = state.get("max_iterations", _cfg.AGENT_MAX_ITERATIONS)

    done = [s for s in plan if s.get("status") == "completed"]
    failed = [s for s in plan if s.get("status") == "failed"]
    pending = [s for s in plan if s.get("status") == "pending"]
    recent = "\n".join(f"Step {r.get('step','?')}: {str(r.get('result',''))[:300]}" for r in results[-3:])

    sys = (
        "Evaluate agent execution. Return JSON:\n"
        '{"quality":0.7,"decision":"continue","feedback":"..."}\n'
        "decision: continue|complete\n"
        "Prefer 'complete' if good enough or many iterations passed."
    )
    usr = (
        f"Task: {state['task_description']}\nIter: {it}/{mx}\n"
        f"Done: {len(done)}/{len(plan)} | Failed: {len(failed)} | Pending: {len(pending)}\n"
        f"Results:\n{recent}\nErrors:\n{chr(10).join(errors[-3:]) or 'None'}"
    )

    llm = get_llm(temperature=0.1)
    try:
        resp = await llm.ainvoke([SystemMessage(content=sys), HumanMessage(content=usr)])
        ref = _parse_json(resp.content)
        q = float(ref.get("quality", 0.5))
        decision = ref.get("decision", "continue")
        fb = ref.get("feedback", "")
    except Exception as e:
        log.error("Reflection failed: %s", e)
        q, decision, fb = 0.5, "continue" if it < mx - 2 else "complete", str(e)

    if it >= mx - 1 or (not pending and not failed):
        decision = "complete"

    log.info("Reflect: q=%.2f decision=%s iter=%d", q, decision, it)
    return {"quality": q, "iteration": it + 1, "current_step": state.get("current_step", 0) + 1,
            "status": "reflecting",
            "messages": [AIMessage(content=f"🔍 Reflect (q={q:.2f}): {decision}\n{fb}")]}


# ────────────────────────────────────────────────────────────────────────
# finalize
# ────────────────────────────────────────────────────────────────────────
async def _finalize(state: AgentState) -> dict[str, Any]:
    from src.agent.llm import get_llm
    from src.db.engine import db_session
    from src.db.repository import store_knowledge, store_vector, upsert_concept

    plan = state.get("plan", [])
    results = state.get("results", [])
    quality = state.get("quality", 0)
    task = state["task_description"]

    results_text = "\n".join(
        f"**Step {r.get('step','?')}**: {r.get('description','')}\n→ {str(r.get('result',''))[:500]}"
        for r in results)

    try:
        llm = get_llm()
        resp = await llm.ainvoke([
            SystemMessage(content="Compile a clear, comprehensive final result in markdown."),
            HumanMessage(content=f"Task: {task}\nQuality: {quality:.2f}\n\n{results_text}")])
        final = resp.content
    except Exception:
        final = f"Quality: {quality:.2f}\n\n{results_text}"

    # persist to memory
    try:
        async with db_session() as db:
            node = await upsert_concept(db, task[:200], category="task_result")
            await store_knowledge(db, final[:5000], node_id=node.id, source="agent", confidence=quality)
            await store_vector(db, f"Task: {task}\nResult: {final[:2000]}",
                               source_type="task_result",
                               source_id=state.get("task_id", ""),
                               meta={"quality": quality, "iter": state.get("iteration", 0)})
    except Exception as e:
        log.warning("Memory store failed: %s", e)

    return {"final_result": final, "status": "completed", "progress": 100.0,
            "messages": [AIMessage(content=f"✅ Done (q={quality:.2f})\n\n{final[:1000]}")]}


# ────────────────────────────────────────────────────────────────────────
# Routing
# ────────────────────────────────────────────────────────────────────────
def _route_after_reflect(s: AgentState) -> str:
    if s.get("iteration", 0) >= s.get("max_iterations", _cfg.AGENT_MAX_ITERATIONS):
        return "finalize"
    if s.get("quality", 0) >= _cfg.AGENT_QUALITY_THRESHOLD:
        pending = [p for p in s.get("plan", []) if p.get("status") == "pending"]
        if not pending:
            return "finalize"
    if s.get("current_step", 0) >= len(s.get("plan", [])):
        return "finalize"
    return "plan"


# ────────────────────────────────────────────────────────────────────────
# Build graph
# ────────────────────────────────────────────────────────────────────────
_graph = None


def get_graph():
    global _graph
    if _graph is not None:
        return _graph
    g = StateGraph(AgentState)
    g.add_node("plan", _plan)
    g.add_node("retrieve_memory", _retrieve)
    g.add_node("execute", _execute)
    g.add_node("reflect", _reflect)
    g.add_node("finalize", _finalize)
    g.set_entry_point("plan")
    g.add_edge("plan", "retrieve_memory")
    g.add_edge("retrieve_memory", "execute")
    g.add_edge("execute", "reflect")
    g.add_conditional_edges("reflect", _route_after_reflect,
                            {"plan": "plan", "finalize": "finalize"})
    g.add_edge("finalize", END)
    _graph = g.compile()
    log.info("Agent graph compiled")
    return _graph
