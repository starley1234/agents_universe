"""AI Copilot: ассистирующий слой поверх платформы (ТЗ §3.6).

Работает через инструменты (function calling, `dataforge/copilot/
tools.py`) над публичными REST API — НЕ имеет собственного доступа к
`Store`/БД. Действует в рамках токена вызывающего пользователя (тот же
`Authorization: Bearer`, что и у человека — если платформа защищена
токеном, Copilot защищён им же). Каждое взаимодействие пишется в
неизменяемый `ai_interaction` (см. `Store.log_ai_interaction`) — полный
аудит промпта, вызванных инструментов и итогового ответа.

Два режима (ТЗ K6):
  setup — помощь в развёртывании/настройке (в этой сборке отличается
          только системным промптом; полноценный визард настройки не
          реализован, см. README.md)
  ops   — эксплуатация: ответы на вопросы о текущем состоянии
          платформы, диагностика, запуск процессов через инструменты

Без `cfg.llm_base_url` бросает `CopilotError` — модуль полностью
отключаем без влияния на ядро (остальной DataForge работает
независимо от того, настроен ли Copilot).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import Config
from ..db.store import Store
from .llm import LLMError, OpenAILikeClient
from .tools import TOOL_SCHEMAS, ApiTools, ToolError

_SYSTEM_PROMPTS = {
    "setup": (
        "Ты — AI Copilot платформы DataForge в режиме настройки. Помогаешь "
        "оператору подключать источники данных, настраивать правила "
        "качества и типы объектов Ontology. Отвечай кратко и по делу. "
        "Для получения актуальной информации о платформе используй "
        "доступные инструменты, не выдумывай данные."
    ),
    "ops": (
        "Ты — AI Copilot платформы DataForge в режиме эксплуатации. "
        "Помогаешь оператору отвечать на вопросы о текущем состоянии "
        "платформы (источники, датасеты, карантин, дубли, процессы) и "
        "выполнять рутинные операции через инструменты. Никогда не "
        "утверждай факты о данных платформы без вызова соответствующего "
        "инструмента. Отвечай кратко и по делу."
    ),
}

_MAX_TOOL_STEPS = 6


class CopilotError(RuntimeError):
    """AI Copilot не настроен или LLM недоступен — сервис временно
    неработоспособен (мапится в HTTP 503, не ошибка запроса клиента)."""


class CopilotRequestError(ValueError):
    """Неверные параметры запроса к Copilot (например, неизвестный
    режим) — ошибка клиента (мапится в HTTP 400)."""


def _require_llm(cfg: Config) -> None:
    if not cfg.llm_base_url:
        raise CopilotError(
            "AI Copilot не настроен — задайте FORGE_LLM_BASE_URL (и, если "
            "нужно, FORGE_LLM_API_KEY/FORGE_LLM_MODEL). Без этого модуль "
            "полностью отключён, остальная платформа работает как обычно.")


def ask(store: Store, cfg: Config, actor: str, prompt: str, mode: str,
       self_base_url: str, auth_token: str | None) -> dict[str, Any]:
    """Выполняет один запрос к Copilot: цикл модель -> инструмент ->
    модель поверх REST API DataForge, пишет `ai_interaction`."""
    _require_llm(cfg)
    if mode not in _SYSTEM_PROMPTS:
        raise CopilotRequestError(f"Неизвестный режим {mode!r}, допустимы: setup, ops")

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    http_client = httpx.Client(base_url=self_base_url, headers=headers, timeout=10)
    tools = ApiTools(http_client)

    llm = OpenAILikeClient(cfg.llm_base_url, cfg.llm_model or "default",
                          cfg.llm_api_key, cfg.llm_timeout)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPTS[mode]},
        {"role": "user", "content": prompt},
    ]
    tools_called: list[dict[str, Any]] = []
    final_text = ""
    stopped_by = "max_steps"

    try:
        for _ in range(_MAX_TOOL_STEPS):
            try:
                reply = llm.chat(messages, tools=TOOL_SCHEMAS)
            except LLMError as exc:
                raise CopilotError(f"Ошибка обращения к LLM: {exc}") from exc

            if not reply.wants_tools:
                final_text = reply.text or "(пустой ответ модели)"
                stopped_by = "done"
                break

            messages.append({
                "role": "assistant", "content": reply.text or None,
                "tool_calls": [{
                    "id": c.id, "type": "function",
                    "function": {"name": c.name,
                                 "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                } for c in reply.tool_calls],
            })
            for call in reply.tool_calls:
                try:
                    result = tools.call(call.name, call.arguments)
                    result_str = json.dumps(result, ensure_ascii=False)[:2000]
                except ToolError as exc:
                    result_str = f"ОШИБКА: {exc}"
                tools_called.append({"name": call.name, "arguments": call.arguments,
                                     "result": result_str})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": result_str})
        else:
            final_text = (f"Достигнут предел в {_MAX_TOOL_STEPS} шагов вызова "
                          "инструментов — ответ не получен.")
    finally:
        http_client.close()

    store.log_ai_interaction(actor, prompt, mode, tools_called, final_text)
    return {"text": final_text, "tools_called": tools_called, "stopped_by": stopped_by}
