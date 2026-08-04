def summarize_state(state: dict) -> str:
    plan = state.get("plan", [])
    completed = [s.get("title") for s in plan if s.get("status") == "done"]
    current = state.get("current_step", {}).get("title", "n/a")
    return (
        f"Goal: {state.get('goal')}\n"
        f"Iteration: {state.get('iteration', 0)}\n"
        f"Completed: {', '.join(completed) if completed else 'none'}\n"
        f"Current: {current}\n"
        f"Last observation: {str(state.get('observation', {}))[:1000]}\n"
        f"Next: continue unfinished plan or request human if blocked."
    )
