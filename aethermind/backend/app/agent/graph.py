import json
import re
from pathlib import Path
from app.agent.summarizer import summarize_state
from app.config import settings
from app.llm.providers import LLMProvider, get_llm_provider
from app.services.guardrails import needs_human_for_action
from app.services.mcp_client import INTERNAL_SERVER_NAME, call_mcp_tool_sync, list_mcp_tools_sync
from app.tools.code_interpreter import CodeInterpreter
from app.tools.filesystem import FileSystemTools

DEFAULT_PLAN = [
    {"id": "scope", "title": "Уточнить цель и критерии готовности", "status": "todo", "action": "llm_scratchpad"},
    {"id": "architecture", "title": "Разобрать архитектуру и компоненты системы", "status": "todo", "action": "llm_research"},
    {"id": "risks", "title": "Проанализировать риски, ограничения и guardrails", "status": "todo", "action": "llm_research"},
    {"id": "data_model", "title": "Описать модель данных и жизненный цикл задачи", "status": "todo", "action": "llm_research"},
    {"id": "runtime", "title": "Проверить runtime и sandbox-подход", "status": "todo", "action": "run_python"},
    {"id": "critique", "title": "Провести самокритику и найти пробелы", "status": "todo", "action": "llm_reflect"},
    {"id": "report", "title": "Собрать итоговый исследовательский отчет", "status": "todo", "action": "llm_final_report"},
    {"id": "qa", "title": "Финальная проверка качества результата", "status": "todo", "action": "llm_quality_gate"},
]

SYSTEM_PROMPT = """Ты — производственный автономный агент AetherMind.
Работай на русском языке. Не имитируй работу: каждый шаг должен создавать полезный текстовый результат.
Если данных недостаточно, явно перечисли допущения и следующий проверочный шаг.
Не раскрывай скрытую chain-of-thought; давай краткое управленческое объяснение решения.
"""


class AgentGraph:
    """Производственный агентный цикл: plan -> execute -> observe -> reflect -> summarize."""

    def __init__(self, workspace: Path, llm: LLMProvider | None = None):
        self.workspace = workspace
        self.fs = FileSystemTools(workspace)
        self.code = CodeInterpreter(workspace)
        self.llm = llm or get_llm_provider()

    def run_one_iteration(self, state: dict) -> dict:
        state.setdefault("events", [])
        try:
            state = self.plan(state)
            if state.get("goal_completed") or state.get("awaiting_user"):
                return state
            state = self.execute(state)
            state = self.observe(state)
            state = self.reflect(state)
            if state.get("iteration", 0) and state.get("iteration", 0) % settings.summary_every_iterations == 0:
                state = self.summarize(state)
            state["artifacts"] = self._dedupe_artifacts(state.get("artifacts", []))
            return state
        except Exception as exc:  # noqa: BLE001 - ошибка должна стать состоянием, а не молчаливым успехом
            state["awaiting_user"] = True
            state["confidence"] = 0.0
            state["low_confidence_streak"] = state.get("low_confidence_streak", 0) + 1
            state["fatal_error"] = None
            state["observation"] = {"failed": True, "reason": "llm_or_runtime_error", "error": str(exc)}
            state["events"].append(
                {
                    "type": "error",
                    "message": "LLM или runtime недоступен. Работа остановлена, требуется вмешательство.",
                    "error": str(exc),
                }
            )
            return state

    def plan(self, state: dict) -> dict:
        state.setdefault("tool_config", self._default_tool_config())
        if not self._tool_enabled(state, "llm"):
            state["awaiting_user"] = True
            state["events"].append({"type": "tools", "message": "LLM-инструмент выключен. Включите его в панели инструментов для продолжения."})
            return state
        if not state.get("plan"):
            state["events"].append({"type": "thought", "message": "Запрашиваю у LLM производственный план выполнения задачи."})
            state["plan"] = self._build_llm_plan(state)
            state["events"].append({"type": "plan", "message": "LLM сформировал дерево стратегии.", "plan": state["plan"]})

        current = next((step for step in state["plan"] if step["status"] in {"todo", "running"}), None)
        if current is None:
            state["goal_completed"] = True
            state["events"].append({"type": "reflection", "message": "Все шаги плана завершены. Задача помечена как выполненная."})
            return state

        current["status"] = "running"
        state["current_step"] = current
        state["events"].append({"type": "thought", "message": f"Выбран следующий шаг: {current['title']}"})
        return state

    def execute(self, state: dict) -> dict:
        step = state.get("current_step") or {}
        action = step.get("action")
        if needs_human_for_action(action):
            state["awaiting_user"] = True
            state["observation"] = {"blocked": True, "reason": "опасное действие требует подтверждения человека"}
            return state

        if action == "run_python":
            if not self._tool_enabled(state, "code_interpreter"):
                state["awaiting_user"] = True
                state["observation"] = {"blocked": True, "reason": "Инструмент Code Interpreter выключен пользователем"}
                return state
            result = self.code.run_python(
                "from pathlib import Path\n"
                "print('Проверка sandbox: OK')\n"
                "print('Рабочая директория:', Path.cwd())\n"
            )
        else:
            if not self._tool_enabled(state, "filesystem"):
                state["awaiting_user"] = True
                state["observation"] = {"blocked": True, "reason": "Инструмент File System выключен пользователем"}
                return state
            content = self._run_llm_step(state, step)
            target = self._target_file_for_action(action)
            result = self.fs.write_file(target, content)
            self._remember_artifact(state, target, "report")

            if action in {"llm_scratchpad", "llm_research", "llm_reflect"}:
                self.fs.append_file("scratchpad.md", f"\n\n## {step.get('title')}\n\n{content[:4000]}\n")

        state["observation"] = {"action": action, "result": result}
        state["events"].append({"type": "action", "message": f"Выполнено действие: {action}", "result": result})
        return state

    def observe(self, state: dict) -> dict:
        observation = state.get("observation", {})
        state["events"].append({"type": "observation", "message": str(observation)[:1200]})
        return state

    def reflect(self, state: dict) -> dict:
        observation = state.get("observation", {})
        result = observation.get("result", {})
        failed = bool(result.get("exit_code", 0)) or bool(observation.get("blocked")) or bool(observation.get("failed"))

        reflection_prompt = (
            f"Цель: {state.get('goal')}\n"
            f"Текущий шаг: {state.get('current_step', {}).get('title')}\n"
            f"Наблюдение: {json.dumps(observation, ensure_ascii=False)[:3000]}\n\n"
            "Оцени качество результата. Верни JSON строго вида: "
            "{\"confidence\": 0.0-1.0, \"accepted\": true/false, \"reason\": \"...\"}."
        )
        llm_result = self.llm.complete_sync(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": reflection_prompt}]
        )
        verdict = self._extract_json(llm_result.content) or {}
        confidence = float(verdict.get("confidence", 0.35 if failed else 0.75))
        accepted = bool(verdict.get("accepted", not failed and confidence >= settings.low_confidence_threshold))

        state["confidence"] = max(0.0, min(1.0, confidence))
        state["low_confidence_streak"] = (
            state.get("low_confidence_streak", 0) + 1
            if state["confidence"] < settings.low_confidence_threshold or not accepted
            else 0
        )
        state["iteration"] = state.get("iteration", 0) + 1
        state["reflection"] = {
            "confidence": state["confidence"],
            "accepted": accepted,
            "reason": verdict.get("reason", llm_result.content[:500]),
            "model": llm_result.model,
        }
        self._account_llm_usage(state, llm_result)

        if accepted and state.get("current_step"):
            state["current_step"]["status"] = "done"
        elif state.get("current_step"):
            state["current_step"]["status"] = "todo"
            state["awaiting_user"] = state.get("low_confidence_streak", 0) >= settings.low_confidence_streak_limit

        state["events"].append(
            {
                "type": "reflection",
                "message": "Шаг принят LLM-критиком." if accepted else "LLM-критик отклонил результат шага.",
                "confidence": state["confidence"],
                "reason": state["reflection"]["reason"],
            }
        )

        if all(step.get("status") == "done" for step in state.get("plan", [])):
            state["goal_completed"] = True
        return state

    def summarize(self, state: dict) -> dict:
        base_summary = summarize_state(state)
        llm_result = self.llm.complete_sync(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Сожми историю работы агента в executive summary на русском. "
                        "Укажи: сделано, артефакты, проблемы, следующий шаг.\n\n"
                        f"{base_summary}"
                    ),
                },
            ]
        )
        self._account_llm_usage(state, llm_result)
        state["executive_summary"] = llm_result.content
        self.fs.write_file("artifacts/executive_summary.md", llm_result.content)
        self._remember_artifact(state, "artifacts/executive_summary.md", "summary")
        state["events"].append({"type": "summary", "message": llm_result.content[:1200]})
        return state

    def _default_tool_config(self) -> dict:
        return {
            "llm": True,
            "filesystem": True,
            "code_interpreter": True,
            "headless_browser": False,
            "mcp": True,
            "dangerous_actions": False,
            "mcp_servers": [],
        }

    def _tool_enabled(self, state: dict, tool_name: str) -> bool:
        config = {**self._default_tool_config(), **state.get("tool_config", {})}
        return bool(config.get(tool_name, False))

    def _build_llm_plan(self, state: dict) -> list[dict]:
        prompt = (
            f"Цель пользователя: {state.get('goal')}\n\n"
            f"Составь производственный план из минимум {settings.planner_min_steps} шагов для автономного агента. "
            "Верни только JSON-массив объектов: id, title, action. "
            "action выбирай из: llm_scratchpad, llm_research, run_python, llm_reflect, llm_final_report, llm_quality_gate. "
            "Не завершай задачу слишком быстро: план должен включать исследование, проверку, самокритику и финальный отчет."
        )
        result = self.llm.complete_sync([{ "role": "system", "content": SYSTEM_PROMPT }, {"role": "user", "content": prompt}])
        self._account_llm_usage(state, result)
        parsed = self._extract_json(result.content)
        if not isinstance(parsed, list) or len(parsed) < 3:
            # Это fallback структуры, но не fallback работы: LLM уже был вызван; если он недоступен, сюда не попадем.
            parsed = [dict(step) for step in DEFAULT_PLAN]
        allowed = {"llm_scratchpad", "llm_research", "run_python", "llm_reflect", "llm_final_report", "llm_quality_gate"}
        plan = []
        for index, item in enumerate(parsed[:12], start=1):
            action = item.get("action") if isinstance(item, dict) else None
            plan.append(
                {
                    "id": str(item.get("id", f"step_{index}")) if isinstance(item, dict) else f"step_{index}",
                    "title": str(item.get("title", f"Шаг {index}")) if isinstance(item, dict) else f"Шаг {index}",
                    "status": "todo",
                    "action": action if action in allowed else "llm_research",
                }
            )
        return plan

    def _run_llm_step(self, state: dict, step: dict) -> str:
        scratchpad = ""
        try:
            scratchpad = self.fs.read_file("scratchpad.md")["content"][-8000:]
        except Exception:
            scratchpad = ""
        tool_config = {**self._default_tool_config(), **state.get("tool_config", {})}
        mcp_servers = [server for server in tool_config.get("mcp_servers", []) if server.get("enabled")]
        mcp_tools_hint = "MCP выключен"
        if tool_config.get("mcp"):
            try:
                tools = list_mcp_tools_sync(tool_config.get("mcp_servers", []), include_internal=True)
                ok_tools = [tool for tool in tools if tool.get("status") == "ok"]
                mcp_tools_hint = "\n".join(
                    f"- {tool.get('server_name')}.{tool.get('name')}: {tool.get('description', '')} schema={tool.get('input_schema', {})}"
                    for tool in ok_tools[:12]
                ) or "MCP включен, но инструменты не найдены"
            except Exception as exc:  # noqa: BLE001
                mcp_tools_hint = f"Ошибка discovery MCP tools: {exc}"
        mcp_hint = "\n".join(f"- {server.get('name')}: {server.get('url')} ({server.get('transport', 'sse')})" for server in mcp_servers) or "нет подключенных внешних MCP серверов"
        prompt = (
            f"Цель: {state.get('goal')}\n"
            f"Итерация: {state.get('iteration', 0) + 1}\n"
            f"Шаг: {step.get('title')}\n"
            f"Executive summary: {state.get('executive_summary', 'пока отсутствует')}\n"
            f"Доступные внешние MCP серверы:\n{mcp_hint}\n"
            f"Доступные MCP инструменты:\n{mcp_tools_hint}\n"
            f"Scratchpad:\n{scratchpad}\n\n"
            "Выполни этот шаг как автономный исследователь. "
            "Сформируй содержательный Markdown-результат на русском: факты, выводы, риски, следующие действия. "
            "Если нужен инструмент, добавь отдельную строку MCP_CALL_JSON: "
            "{\"server_name\":\"__internal__\",\"tool_name\":\"fetch_url\",\"arguments\":{\"url\":\"https://example.com\"}}. "
            "После вызова инструмента среда добавит результат к артефакту. "
            "Если это финальный отчет — сделай структурированный отчет с разделами и чек-листом проверки."
        )
        result = self.llm.complete_sync([{ "role": "system", "content": SYSTEM_PROMPT }, {"role": "user", "content": prompt}])
        self._account_llm_usage(state, result)
        content = self._execute_mcp_requests_if_any(state, result.content)
        state["events"].append({"type": "llm", "message": f"LLM ответила моделью {result.model}", "tokens": result.tokens_used})
        return content

    def _execute_mcp_requests_if_any(self, state: dict, content: str) -> str:
        tool_config = {**self._default_tool_config(), **state.get("tool_config", {})}
        if not tool_config.get("mcp"):
            return content
        matches = re.findall(r"MCP_CALL_JSON:\s*(\{.*?\})(?:\n|$)", content, flags=re.DOTALL)
        if not matches:
            return content
        additions = []
        for raw in matches[:3]:
            try:
                request = json.loads(raw)
                server_name = request.get("server_name")
                tool_name = request.get("tool_name")
                arguments = request.get("arguments") or {}
                if server_name == INTERNAL_SERVER_NAME:
                    server = {"name": INTERNAL_SERVER_NAME, "url": "builtin://fetch", "transport": "builtin", "enabled": True}
                else:
                    server = next((item for item in tool_config.get("mcp_servers", []) if item.get("name") == server_name), None)
                if not server or not tool_name:
                    raise ValueError(f"MCP server/tool не найден: {server_name}.{tool_name}")
                result = call_mcp_tool_sync(server, tool_name, arguments, workspace_path=str(self.workspace))
                state.setdefault("mcp_calls", []).append({"request": request, "result": result})
                state["events"].append({"type": "mcp_call", "message": f"Агент вызвал MCP инструмент: {server_name}.{tool_name}", "arguments": arguments})
                additions.append(f"\n\n## Результат MCP: {server_name}.{tool_name}\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2, default=str)[:12000]}\n```\n")
            except Exception as exc:  # noqa: BLE001
                state["events"].append({"type": "mcp_error", "message": "Ошибка автоматического MCP вызова", "error": str(exc)})
                additions.append(f"\n\n> Ошибка MCP вызова: {exc}\n")
        return content + "".join(additions)

    def _target_file_for_action(self, action: str | None) -> str:
        mapping = {
            "llm_scratchpad": "artifacts/01_goal_and_criteria.md",
            "llm_research": "artifacts/research_notes.md",
            "llm_reflect": "artifacts/self_critique.md",
            "llm_final_report": "artifacts/final_report.md",
            "llm_quality_gate": "artifacts/quality_gate.md",
        }
        return mapping.get(action or "", "artifacts/step_result.md")

    def _remember_artifact(self, state: dict, path: str, kind: str) -> None:
        artifacts = state.setdefault("artifacts", [])
        if not any(item.get("path") == path for item in artifacts):
            artifacts.append({"path": path, "kind": kind})

    def _dedupe_artifacts(self, artifacts: list[dict]) -> list[dict]:
        seen = set()
        result = []
        for artifact in artifacts:
            path = artifact.get("path")
            if path and path not in seen:
                seen.add(path)
                result.append(artifact)
        return result

    def _account_llm_usage(self, state: dict, result) -> None:
        usage = state.setdefault("llm_usage", {"tokens_used": 0, "cost_used_usd": 0.0, "calls": 0})
        usage["tokens_used"] = usage.get("tokens_used", 0) + int(result.tokens_used or 0)
        usage["cost_used_usd"] = usage.get("cost_used_usd", 0.0) + float(result.cost_usd or 0.0)
        usage["calls"] = usage.get("calls", 0) + 1

    def _extract_json(self, text: str):
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"(\[.*\]|\{.*\})", text, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    return None
        return None
