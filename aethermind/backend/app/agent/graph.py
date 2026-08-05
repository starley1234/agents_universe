import base64
import json
import mimetypes
import re
from pathlib import Path
from app.agent.summarizer import summarize_state
from app.config import settings
from app.llm.providers import LLMProvider, get_llm_provider
from app.services.guardrails import needs_human_for_action
from app.services.mcp_client import INTERNAL_SERVER_NAME, call_mcp_tool_sync, list_mcp_tools_sync
from app.services.mcp_registry import load_global_mcp_servers
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
Если в контексте есть изображения, анализируй их как визуальные входные данные, а не как обычные имена файлов.
Для CAD/OpenSCAD задач: сначала извлеки геометрию с изображения, затем создай .scad файл, запроси render через доступный MCP/OpenSCAD tool или внутренние артефакты, сравни рендер с исходным изображением и итеративно улучшай модель.
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
            recovery_attempts = state.get("auto_recovery_attempts", 0) + 1
            state["auto_recovery_attempts"] = recovery_attempts
            state["confidence"] = 0.0
            state["low_confidence_streak"] = state.get("low_confidence_streak", 0) + 1
            state["fatal_error"] = None
            state["observation"] = {"failed": True, "reason": "llm_or_runtime_error", "error": str(exc)}
            state["iteration"] = state.get("iteration", 0) + 1
            if recovery_attempts <= settings.auto_recovery_attempts:
                state["awaiting_user"] = False
                if state.get("current_step"):
                    state["current_step"]["status"] = "todo"
                    state["current_step"]["retry_reason"] = str(exc)[:500]
                state["events"].append(
                    {
                        "type": "auto_recovery",
                        "message": "Ошибка перехвачена. Агент автоматически откатил текущий шаг в todo и попробует перепланировать/повторить без участия человека.",
                        "attempt": recovery_attempts,
                        "error": str(exc),
                    }
                )
            else:
                state["awaiting_user"] = True
                state["human_request"] = {
                    "reason": str(exc),
                    "current_step": state.get("current_step", {}).get("title"),
                    "suggested_actions": ["retry_step", "replan", "rollback", "check_tools"],
                    "message": "Автовосстановление исчерпано. Требуется решение человека.",
                }
                state["events"].append(
                    {
                        "type": "error",
                        "message": "Автовосстановление исчерпано. Требуется вмешательство человека.",
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
            audit_script = self._build_workspace_audit_script()
            result = self.code.run_python(audit_script)
            report_path = f"artifacts/python_run_iteration_{state.get('iteration', 0) + 1}.json"
            self.fs.write_file(report_path, json.dumps(result, ensure_ascii=False, indent=2))
            self._remember_artifact(state, report_path, "code_result")
            self._remember_artifact(state, "artifacts/workspace_audit.json", "code_result")
            self._remember_artifact(state, "code/run.py", "code")
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

        latest_human = self._latest_human_intervention(state)
        reflection_prompt = (
            f"Цель: {state.get('goal')}\n"
            f"Текущий шаг: {state.get('current_step', {}).get('title')}\n"
            f"Последняя инструкция человека: {latest_human or 'нет'}\n"
            f"Наблюдение: {json.dumps(observation, ensure_ascii=False)[:3000]}\n\n"
            "Оцени качество результата. Если человек явно принял риск или разрешил продолжить, "
            "не блокируй шаг повторно без фатальной ошибки выполнения. "
            "Верни JSON строго вида: "
            "{\"confidence\": 0.0-1.0, \"accepted\": true/false, \"reason\": \"...\"}."
        )
        llm_result = self.llm.complete_sync(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": reflection_prompt}]
        )
        verdict = self._extract_json(llm_result.content) or {}
        critic_confidence = float(verdict.get("confidence", 0.35 if failed else 0.75))
        critic_accepted = bool(verdict.get("accepted", not failed and critic_confidence >= settings.low_confidence_threshold))
        critic_reason = verdict.get("reason", llm_result.content[:500])

        material_output = self._step_has_material_output(state, observation)
        action = observation.get("action") or state.get("current_step", {}).get("action")
        quality_gate = action == "llm_quality_gate"
        fatal_failure = bool(observation.get("blocked")) or bool(result.get("exit_code", 0))

        accepted = critic_accepted and not fatal_failure
        confidence = critic_confidence

        # Production mode: critic is advisory for normal productive steps.
        # It should not bounce routine work to the user just because it wanted
        # a richer result. If a file/artifact/tool result exists, continue and
        # carry the critique forward as an improvement note.
        if not fatal_failure and material_output and not quality_gate:
            if not critic_accepted:
                state.setdefault("critic_advisories", []).append(
                    {
                        "step": state.get("current_step", {}).get("title"),
                        "reason": critic_reason,
                        "iteration": state.get("iteration", 0) + 1,
                    }
                )
                critic_reason = f"Критик дал замечание, но шаг принят как продуктивный и не блокирующий: {critic_reason}"
            accepted = True
            confidence = max(confidence, 0.72)

        if self._human_accepts_risk(latest_human) and not fatal_failure:
            accepted = True
            confidence = max(confidence, 0.70)
            critic_reason = f"Человек явно разрешил продолжить/принял риск. {critic_reason}".strip()

        state["confidence"] = max(0.0, min(1.0, confidence))
        state["low_confidence_streak"] = (
            state.get("low_confidence_streak", 0) + 1
            if state["confidence"] < settings.low_confidence_threshold or (not accepted and fatal_failure)
            else 0
        )
        state["iteration"] = state.get("iteration", 0) + 1
        state["reflection"] = {
            "confidence": state["confidence"],
            "accepted": accepted,
            "critic_accepted": critic_accepted,
            "advisory_mode": bool(not fatal_failure and material_output and not quality_gate),
            "reason": critic_reason,
            "model": llm_result.model,
        }
        self._account_llm_usage(state, llm_result)

        if accepted and state.get("current_step"):
            state["current_step"]["status"] = "done"
            state["current_step"].pop("retry_count", None)
            state["auto_recovery_attempts"] = 0
        elif state.get("current_step"):
            retry_count = int(state["current_step"].get("retry_count", 0)) + 1
            state["current_step"]["retry_count"] = retry_count
            state["current_step"]["status"] = "todo"
            if retry_count <= settings.auto_recovery_attempts:
                state["awaiting_user"] = False
                state["events"].append(
                    {
                        "type": "auto_recovery",
                        "message": "Критик отклонил шаг. Агент автоматически откатил шаг в todo и повторит его с учетом замечаний.",
                        "retry_count": retry_count,
                        "reason": state["reflection"]["reason"],
                    }
                )
            else:
                state["awaiting_user"] = state.get("low_confidence_streak", 0) >= settings.low_confidence_streak_limit
                if state["awaiting_user"]:
                    state["human_request"] = {
                        "reason": state["reflection"]["reason"],
                        "current_step": state.get("current_step", {}).get("title"),
                        "confidence": state["confidence"],
                        "suggested_actions": ["retry_step", "replan", "accept_risk", "rollback", "check_tools"],
                        "message": "Критик отклонил шаг после автоматических повторов.",
                    }

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
            "mcp_servers": load_global_mcp_servers(),
        }

    def _merged_tool_config(self, state: dict) -> dict:
        base = self._default_tool_config()
        current = dict(state.get("tool_config", {}) or {})
        global_servers = {server.get("name"): server for server in base.get("mcp_servers", []) if server.get("name")}
        local_servers = {server.get("name"): server for server in current.get("mcp_servers", []) if server.get("name")}
        merged = {**base, **current}
        merged["mcp_servers"] = list({**global_servers, **local_servers}.values())
        merged["mcp"] = bool(merged.get("mcp", True))
        state["tool_config"] = merged
        return merged

    def _tool_enabled(self, state: dict, tool_name: str) -> bool:
        config = self._merged_tool_config(state)
        return bool(config.get(tool_name, False))

    def _latest_human_intervention(self, state: dict) -> str:
        interventions = state.get("human_interventions") or []
        if not interventions:
            return ""
        latest = interventions[-1]
        return str(latest.get("message", latest) if isinstance(latest, dict) else latest)

    def _human_accepts_risk(self, message: str) -> bool:
        text = (message or "").lower()
        return any(
            marker in text
            for marker in [
                "принять текущий риск",
                "принять риск",
                "риск как допустимый",
                "игнорировать риск",
                "accept_risk",
                "продолжить выполнение следующего шага",
                "продолжай автономно",
            ]
        )

    def _step_has_material_output(self, state: dict, observation: dict) -> bool:
        result = observation.get("result") or {}
        if result.get("path") or result.get("bytes") or result.get("stdout") or result.get("exit_code") == 0:
            return True
        if state.get("artifacts") or state.get("mcp_calls") or state.get("attachments"):
            return True
        current = state.get("current_step") or {}
        action = current.get("action")
        if action in {"llm_scratchpad", "llm_research", "llm_reflect", "llm_final_report", "run_python"}:
            return True
        return False

    def _build_llm_plan(self, state: dict) -> list[dict]:
        prompt = (
            f"Цель пользователя: {state.get('goal')}\n\n"
            f"Составь производственный план из минимум {settings.planner_min_steps} шагов для автономного агента. "
            "Верни только JSON-массив объектов: id, title, action. "
            "action выбирай из: llm_scratchpad, llm_research, run_python, llm_reflect, llm_final_report, llm_quality_gate. "
            "Не завершай задачу слишком быстро: план должен включать исследование, проверку, самокритику и финальный отчет."
        )
        result = self.llm.complete_sync([{ "role": "system", "content": SYSTEM_PROMPT }, self._user_message(prompt, state)])
        self._account_llm_usage(state, result)
        parsed = self._extract_json(result.content)
        if not isinstance(parsed, list) or len(parsed) < 3:
            # Это fallback структуры, но не fallback работы: LLM уже был вызван; если он недоступен, сюда не попадем.
            parsed = [dict(step) for step in DEFAULT_PLAN]
        allowed = {"llm_scratchpad", "llm_research", "run_python", "llm_reflect", "llm_final_report", "llm_quality_gate"}
        plan = []
        goal_text = str(state.get("goal", "")).lower()
        if any(marker in goal_text for marker in ["openscad", "open scad", "scad", "cad", "конструкц", "3d", "модель"]):
            plan.append({"id": "vision_geometry", "title": "Проанализировать изображение и извлечь геометрию", "status": "todo", "action": "llm_research"})
            plan.append({"id": "openscad_code", "title": "Создать OpenSCAD код модели", "status": "todo", "action": "llm_research"})
            plan.append({"id": "render_iteration", "title": "Запустить рендер OpenSCAD через MCP и оценить результат", "status": "todo", "action": "llm_research"})
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
        deduped = []
        seen = set()
        for step in plan:
            key = step.get("id") or step.get("title")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(step)
        return deduped

    def _run_llm_step(self, state: dict, step: dict) -> str:
        scratchpad = ""
        try:
            scratchpad = self.fs.read_file("scratchpad.md")["content"][-8000:]
        except Exception:
            scratchpad = ""
        tool_config = self._merged_tool_config(state)
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
        attachments_hint = "\n".join(
            f"- {item.get('path')} ({item.get('content_type', 'image')})"
            for item in state.get("attachments", [])
        ) or "нет прикрепленных изображений"
        latest_human = self._latest_human_intervention(state)
        prompt = (
            f"Цель: {state.get('goal')}\n"
            f"Итерация: {state.get('iteration', 0) + 1}\n"
            f"Шаг: {step.get('title')}\n"
            f"Последняя инструкция человека: {latest_human or 'нет'}\n"
            f"Executive summary: {state.get('executive_summary', 'пока отсутствует')}\n"
            f"Прикрепленные изображения в workspace:\n{attachments_hint}\n"
            f"Доступные внешние MCP серверы:\n{mcp_hint}\n"
            f"Доступные MCP инструменты:\n{mcp_tools_hint}\n"
            f"Scratchpad / фоновые заметки, НЕ текущая команда:\n{scratchpad}\n\n"
            "ВАЖНО: выполняй только текущий шаг из поля `Шаг`, а scratchpad используй только как справочный фон. "
            "Выполни этот шаг автономно и результативно. Не ограничивайся подготовкой среды. "
            "Если задача требует код, таблицу, JSON, CSV, отчет или иной файл — создай файл через MCP_CALL_JSON с __internal__.write_file. "
            "Если нужно проверить код/вычисления — вызови __internal__.run_python. Если нужны внешние данные — вызови __internal__.fetch_url, __internal__.fetch_many_urls или внешний MCP. "
            "Сформируй содержательный Markdown-результат на русском: что сделал, какие файлы создал, что проверил, какие следующие действия. "
            "Формат инструментального вызова: MCP_CALL_JSON: {\"server_name\":\"__internal__\",\"tool_name\":\"write_file\",\"arguments\":{\"path\":\"artifacts/result.md\",\"content\":\"...\"}}. "
            "Можно использовать однострочный JSON, многострочный JSON-блок или массив таких объектов. "
            "Можно сделать до 5 MCP вызовов. После вызова инструмента среда добавит результат к артефакту. "
            "Если это финальный отчет — сделай структурированный отчет с разделами и чек-листом проверки."
        )
        result = self.llm.complete_sync([{ "role": "system", "content": SYSTEM_PROMPT }, self._user_message(prompt, state)])
        self._account_llm_usage(state, result)
        content = self._execute_mcp_requests_if_any(state, result.content)
        state["events"].append({"type": "llm", "message": f"LLM ответила моделью {result.model}", "tokens": result.tokens_used})
        return content

    def _execute_mcp_requests_if_any(self, state: dict, content: str) -> str:
        tool_config = self._merged_tool_config(state)
        if not tool_config.get("mcp"): 
            return content
        requests = self._extract_mcp_call_requests(content)
        if not requests:
            return content
        additions = []
        for request in requests[:5]:
            try:
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
                calls = state.setdefault("mcp_calls", [])
                calls.append({"request": request, "result": result})
                artifact_path = f"artifacts/mcp_auto_{len(calls)}_{server_name}_{tool_name}.json".replace("/", "_")
                self.fs.write_file(artifact_path, json.dumps({"request": request, "result": result}, ensure_ascii=False, indent=2, default=str))
                self._remember_artifact(state, artifact_path, "mcp_result")
                state["events"].append({"type": "mcp_call", "message": f"Агент вызвал MCP инструмент: {server_name}.{tool_name}", "arguments": arguments, "artifact": artifact_path})
                additions.append(f"\n\n## Результат MCP: {server_name}.{tool_name}\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2, default=str)[:12000]}\n```\n")
            except Exception as exc:  # noqa: BLE001
                state["events"].append({"type": "mcp_error", "message": "Ошибка автоматического MCP вызова", "error": str(exc)})
                additions.append(f"\n\n> Ошибка MCP вызова: {exc}\n")
        return content + "".join(additions)

    def _extract_mcp_call_requests(self, content: str) -> list[dict]:
        decoder = json.JSONDecoder()
        requests: list[dict] = []
        marker = "MCP_CALL_JSON:"
        search_from = 0
        while True:
            marker_index = content.find(marker, search_from)
            if marker_index == -1:
                break
            cursor = marker_index + len(marker)
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
            if content.startswith("```json", cursor):
                cursor += len("```json")
            elif content.startswith("```", cursor):
                cursor += len("```")
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
            try:
                value, offset = decoder.raw_decode(content[cursor:])
                if isinstance(value, list):
                    requests.extend(item for item in value if isinstance(item, dict))
                elif isinstance(value, dict):
                    requests.append(value)
                search_from = cursor + max(offset, 1)
            except json.JSONDecodeError:
                line = content[cursor:].splitlines()[0] if content[cursor:].splitlines() else ""
                try:
                    value = json.loads(line.strip().strip("`"))
                    if isinstance(value, dict):
                        requests.append(value)
                except json.JSONDecodeError:
                    pass
                search_from = cursor + max(len(line), 1)
        return requests

    def _user_message(self, text: str, state: dict) -> dict:
        images = self._image_message_parts(state)
        if not images:
            return {"role": "user", "content": text}
        return {"role": "user", "content": [{"type": "text", "text": text}, *images]}

    def _image_message_parts(self, state: dict) -> list[dict]:
        if not settings.vision_enabled:
            return []
        parts = []
        for item in (state.get("attachments") or [])[: settings.vision_max_images]:
            rel_path = item.get("path")
            if not rel_path:
                continue
            path = (self.workspace / rel_path).resolve()
            if not str(path).startswith(str(self.workspace.resolve())) or not path.exists() or not path.is_file():
                continue
            if path.stat().st_size > settings.vision_max_image_bytes:
                state.setdefault("events", []).append({"type": "vision", "message": f"Изображение пропущено из-за размера: {rel_path}"})
                continue
            mime = item.get("content_type") or mimetypes.guess_type(path.name)[0] or "image/png"
            if not str(mime).startswith("image/"):
                continue
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        if parts:
            state.setdefault("events", []).append({"type": "vision", "message": f"В LLM отправлено изображений: {len(parts)}"})
        return parts

    def _build_workspace_audit_script(self) -> str:
        return r'''
from pathlib import Path
import csv
import json

root = Path.cwd()
files = []
csv_summaries = {}
for path in sorted(root.rglob('*')):
    if path.is_file() and '.git' not in path.parts:
        rel = str(path.relative_to(root))
        files.append({'path': rel, 'size': path.stat().st_size})
        if path.suffix.lower() == '.csv':
            try:
                with path.open(newline='', encoding='utf-8') as f:
                    rows = list(csv.DictReader(f))
                csv_summaries[rel] = {'rows': len(rows), 'columns': list(rows[0].keys()) if rows else []}
            except Exception as exc:
                csv_summaries[rel] = {'error': str(exc)}

report = {
    'workspace': str(root),
    'file_count': len(files),
    'files': files,
    'csv_summaries': csv_summaries,
}
Path('artifacts').mkdir(exist_ok=True)
Path('artifacts/workspace_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
'''

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
