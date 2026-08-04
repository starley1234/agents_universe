from pathlib import Path
from app.agent.summarizer import summarize_state
from app.config import settings
from app.services.guardrails import needs_human_for_action
from app.tools.filesystem import FileSystemTools
from app.tools.code_interpreter import CodeInterpreter

DEFAULT_PLAN = [
    {"id": "understand", "title": "Clarify goal and create scratchpad", "status": "todo", "action": "write_scratchpad"},
    {"id": "research", "title": "Collect or synthesize working data", "status": "todo", "action": "write_artifact"},
    {"id": "verify", "title": "Run verification in sandbox", "status": "todo", "action": "run_python"},
    {"id": "finalize", "title": "Prepare final report", "status": "todo", "action": "write_final_report"},
]

class AgentGraph:
    """LangGraph-shaped deterministic graph.

    The class keeps node boundaries explicit, so it can be swapped to native LangGraph StateGraph
    without changing persistence and worker code.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.fs = FileSystemTools(workspace)
        self.code = CodeInterpreter(workspace)

    def run_one_iteration(self, state: dict) -> dict:
        state.setdefault("events", [])
        state = self.plan(state)
        state = self.execute(state)
        state = self.observe(state)
        state = self.reflect(state)
        if state.get("iteration", 0) % settings.summary_every_iterations == 0:
            state = self.summarize(state)
        return state

    def plan(self, state: dict) -> dict:
        if not state.get("plan"):
            state["plan"] = [dict(x) for x in DEFAULT_PLAN]
            state["events"].append({"type": "plan", "message": "Initial strategy graph created", "plan": state["plan"]})
        current = next((s for s in state["plan"] if s["status"] in {"todo", "running"}), None)
        if current is None:
            state["goal_completed"] = True
            return state
        current["status"] = "running"
        state["current_step"] = current
        state["events"].append({"type": "thought", "message": f"Selected step: {current['title']}"})
        return state

    def execute(self, state: dict) -> dict:
        step = state.get("current_step") or {}
        action = step.get("action")
        if needs_human_for_action(action):
            state["awaiting_user"] = True
            state["observation"] = {"blocked": True, "reason": "dangerous action requires confirmation"}
            return state
        goal = state.get("goal", "")
        if action == "write_scratchpad":
            result = self.fs.append_file("scratchpad.md", f"\n## Goal\n{goal}\n\nInitial assumptions captured.\n")
        elif action == "write_artifact":
            result = self.fs.write_file("artifacts/research_notes.md", f"# Research notes\n\nGoal: {goal}\n\n- Key requirement: autonomous loop.\n- Key requirement: persistence and guardrails.\n- Key requirement: Mission Control UI.\n")
            state.setdefault("artifacts", []).append({"path": "artifacts/research_notes.md", "kind": "report"})
        elif action == "run_python":
            result = self.code.run_python("print('AetherMind sandbox verification OK')")
        elif action == "write_final_report":
            result = self.fs.write_file("artifacts/final_report.md", f"# Final report\n\nGoal: {goal}\n\nThe autonomous workflow completed its MVP plan.\n\nSummary:\n{state.get('executive_summary', 'No summary yet.')}\n")
            state.setdefault("artifacts", []).append({"path": "artifacts/final_report.md", "kind": "report"})
        else:
            result = {"noop": True}
        state["observation"] = {"action": action, "result": result}
        state["events"].append({"type": "action", "message": f"Executed {action}", "result": result})
        return state

    def observe(self, state: dict) -> dict:
        obs = state.get("observation", {})
        state["events"].append({"type": "observation", "message": str(obs)[:1000]})
        return state

    def reflect(self, state: dict) -> dict:
        obs = state.get("observation", {})
        failed = bool(obs.get("result", {}).get("exit_code", 0)) or bool(obs.get("blocked"))
        confidence = 0.35 if failed else 0.86
        state["confidence"] = confidence
        state["low_confidence_streak"] = state.get("low_confidence_streak", 0) + 1 if confidence < settings.low_confidence_threshold else 0
        if not failed and state.get("current_step"):
            state["current_step"]["status"] = "done"
        state["iteration"] = state.get("iteration", 0) + 1
        state["reflection"] = {"confidence": confidence, "failed": failed}
        state["events"].append({"type": "reflection", "message": "Step accepted" if not failed else "Step needs attention", "confidence": confidence})
        if all(s.get("status") == "done" for s in state.get("plan", [])):
            state["goal_completed"] = True
        return state

    def summarize(self, state: dict) -> dict:
        state["executive_summary"] = summarize_state(state)
        state["events"].append({"type": "summary", "message": state["executive_summary"]})
        return state
