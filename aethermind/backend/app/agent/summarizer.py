def summarize_state(state: dict) -> str:
    plan = state.get("plan", [])
    completed = [step.get("title") for step in plan if step.get("status") == "done"]
    current = state.get("current_step", {}).get("title", "нет активного шага")
    return (
        f"Цель: {state.get('goal')}\n"
        f"Итерация: {state.get('iteration', 0)}\n"
        f"Завершено: {', '.join(completed) if completed else 'пока ничего'}\n"
        f"Текущий шаг: {current}\n"
        f"Последнее наблюдение: {str(state.get('observation', {}))[:1000]}\n"
        "Рекомендация: продолжать незавершенный план, либо запросить человека при блокировке."
    )
