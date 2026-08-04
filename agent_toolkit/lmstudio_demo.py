#!/usr/bin/env python3
"""Демонстрация работы agent_toolkit с LM Studio через OpenAI-compatible API.

Скрипт показывает, как:
  1. Подключить реестр инструментов agent_toolkit
  2. Отправить промпт + инструменты в LM Studio (Function Calling)
  3. Выполнить вызванные LLM инструменты
  4. Вернуть результаты обратно в LLM для финального ответа

Требования:
  - LM Studio с запущенным сервером (http://localhost:1234)
  - Модель с поддержкой tool calling (Qwen 2.5, Llama 3.1+, Mistral, Hermes)
  - pip install openai  (OpenAI Python SDK)

Использование:
  python lmstudio_demo.py
  python lmstudio_demo.py --prompt "рассчитай прочность балки с нагрузкой 5000 Н"
  python lmstudio_demo.py --tools-limit 20
  python lmstudio_demo.py --base-url http://192.168.1.100:1234/v1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Добавляем корень проекта в путь для импорта
sys.path.insert(0, str(Path(__file__).parent.parent))

# Загружаем .env вручную (без python-dotenv зависимости)
def load_dotenv(env_path: str = "") -> None:
    """Загрузить переменные из .env файла."""
    p = Path(env_path) if env_path else Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key and not os.environ.get(key):
                os.environ[key] = val

load_dotenv()


# ============================================================
# Конфигурация LM Studio
# ============================================================
DEFAULT_BASE_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:1234/v1")
DEFAULT_MODEL = os.environ.get("LOCAL_LLM_MODEL", "local-model")
DEFAULT_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "lm-studio")

# Инструменты, которые безопасны для демонстрации (без опасных операций)
SAFE_TOOL_CATEGORIES = {
    "physics", "cad", "crypto", "data", "templates", "office",
    "files", "sql", "memory", "text", "html",
}


def build_registry_with_filter(max_tools: int = 0):
    """Создать реестр и опционально ограничить количество инструментов."""
    from agent_toolkit import build_default_registry
    reg = build_default_registry()

    if max_tools > 0:
        # Ограничиваем количество инструментов для LM Studio
        # (некоторые модели плохо работают с >30 tool definitions)
        all_tools = reg.list_tools()
        # Приоритет: безопасные локальные инструменты
        priority_skills = {
            "physics", "cad", "math", "crypto", "files", "data",
            "office", "templates", "engineering_calc", "openscad",
        }
        scored = []
        for t in all_tools:
            score = 0
            if any(sk in priority_skills for sk in t.skills):
                score += 10
            if not t.dangerous:
                score += 5
            scored.append((t, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Отключаем лишние инструменты
        enabled_names = {t.name for t, _ in scored[:max_tools]}
        for t in all_tools:
            if t.name not in enabled_names:
                reg.disable_tool(t.name)

    return reg


def get_openai_tools(registry) -> list[dict[str, Any]]:
    """Получить инструменты в формате OpenAI Function Calling (только включённые)."""
    tools = []
    for tool in registry.list_tools(include_disabled=False):
        tools.append(tool.schema())
    return tools


def run_lmstudio_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    api_key: str = DEFAULT_API_KEY,
    max_iterations: int = 5,
) -> str:
    """Отправить запрос в LM Studio с поддержкой Function Calling.

    Цикл:
      1. Отправляем messages + tools в LLM
      2. Если LLM возвращает tool_calls — выполняем их и добавляем результаты
      3. Повторяем, пока LLM не даст финальный текстовый ответ
    """
    try:
        from openai import OpenAI
    except ImportError:
        print("=" * 60)
        print("ОШИБКА: Требуется библиотека openai")
        print("  pip install openai")
        print("=" * 60)
        sys.exit(1)

    client = OpenAI(base_url=base_url, api_key=api_key)

    for iteration in range(max_iterations):
        print(f"\n{'─' * 60}")
        print(f"📤 Отправка запроса в LM Studio (итерация {iteration + 1}/{max_iterations})...")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            print(f"❌ Ошибка подключения к LM Studio: {exc}")
            print(f"\n💡 Убедитесь, что:")
            print(f"   1. LM Studio запущен")
            print(f"   2. Модель загружена")
            print(f"   3. Сервер включён на {base_url}")
            print(f"   4. Модель поддерживает tool calling")
            return f"Ошибка подключения: {exc}"

        choice = response.choices[0]
        assistant_msg = choice.message

        # Проверяем, есть ли tool_calls
        if assistant_msg.tool_calls:
            print(f"🔧 LLM вызывает {len(assistant_msg.tool_calls)} инструмент(ов):")

            # Добавляем ответ ассистента в историю
            messages.append(assistant_msg.model_dump())

            # Выполняем каждый tool_call
            for tc in assistant_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                print(f"   🔹 {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")

                # Выполняем инструмент через реестр
                try:
                    result = registry.execute(fn_name, **fn_args)
                    result_str = str(result)
                    print(f"   ✅ Результат: {result_str[:200]}...")
                except Exception as exc:
                    result_str = f"Ошибка: {exc}"
                    print(f"   ❌ Ошибка: {result_str}")

                # Добавляем результат в историю сообщений
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

            # Продолжаем цикл — LLM получит результаты и ответит
            continue

        # Финальный текстовый ответ
        final_text = assistant_msg.content or "(пустой ответ)"
        print(f"\n{'─' * 60}")
        print(f"💬 Ответ LLM:")
        print(f"{'─' * 60}")
        print(final_text)
        return final_text

    return "(достигнут лимит итераций)"


def main():
    parser = argparse.ArgumentParser(
        description="Демонстрация agent_toolkit + LM Studio (Function Calling)"
    )
    parser.add_argument(
        "--prompt", "-p",
        default="Рассчитай механическое напряжение и запас прочности стальной балки с нагрузкой 10000 Н, площадью сечения 50 мм² и пределом текучести 250 МПа.",
        help="Промпт для LLM",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"URL LM Studio API (по умолчанию: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Имя модели (по умолчанию: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help="API ключ (LM Studio обычно не требует, передайте 'lm-studio')",
    )
    parser.add_argument(
        "--tools-limit", "-t",
        type=int, default=0,
        help="Максимальное количество инструментов для LLM (0 = все)",
    )
    parser.add_argument(
        "--max-iterations", "-i",
        type=int, default=5,
        help="Максимум итераций tool calling (по умолчанию: 5)",
    )
    parser.add_argument(
        "--system-prompt", "-s",
        default=(
            "Ты — инженерный ассистент с доступом к набору инструментов "
            "(калькуляторы физики, САПР, файлы, данные и др.). "
            "Используй инструменты для выполнения расчётов и задач пользователя. "
            "Всегда вызывай соответствующий инструмент, если задача требует вычислений. "
            "Отвечай на русском языке."
        ),
        help="Системный промпт для LLM",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Только показать список доступных инструментов и выйти",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Проверить подключение к LM Studio и выйти",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("  🤖 Agent Toolkit + LM Studio Demo")
    print("=" * 60)

    # Создаём реестр
    reg = build_registry_with_filter(args.tools_limit)
    tools = get_openai_tools(reg)

    print(f"📦 Загружено инструментов: {len(tools)}")
    skills = reg.group_by_skill()
    print(f"🏷️  Уникальных скилсов: {len(skills)}")

    if args.list_tools:
        print(f"\n{'─' * 60}")
        print("Список доступных инструментов:")
        for tool in sorted(reg.list_tools(include_disabled=False), key=lambda t: t.name):
            print(f"  • {tool.name:40} | {tool.description[:60]}")
        return

    if args.check_connection:
        print(f"\n🔌 Проверка подключения к {args.base_url}...")
        try:
            from openai import OpenAI
            client = OpenAI(base_url=args.base_url, api_key=args.api_key)
            models = client.models.list()
            print(f"✅ LM Studio доступен! Модели:")
            for m in models.data:
                print(f"   • {m.id}")
        except Exception as exc:
            print(f"❌ Не удалось подключиться: {exc}")
            print(f"\n💡 Проверьте:")
            print(f"   1. LM Studio запущен")
            print(f"   2. Сервер включён (Local Server → Start)")
            print(f"   3. Порт {args.base_url} доступен")
        return

    # Готовим сообщения
    messages = [
        {"role": "system", "content": args.system_prompt},
        {"role": "user", "content": args.prompt},
    ]

    print(f"\n📝 Промпт: {args.prompt}")
    print(f"🌐 LM Studio: {args.base_url}")
    print(f"🤖 Модель: {args.model}")
    print(f"🔧 Инструментов доступно: {len(tools)}")

    # Запускаем цикл с LM Studio
    t0 = time.perf_counter()
    run_lmstudio_chat(
        messages=messages,
        tools=tools,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        max_iterations=args.max_iterations,
    )
    elapsed = time.perf_counter() - t0
    print(f"\n⏱️  Время выполнения: {elapsed:.1f} сек.")


# Глобальная ссылка на registry для run_lmstudio_chat
registry = None


if __name__ == "__main__":
    # Инициализируем глобальный registry перед main
    from agent_toolkit import build_default_registry
    registry = build_default_registry()

    # Перезаписываем функцию, чтобы использовать глобальный registry
    _orig_run = run_lmstudio_chat

    def run_lmstudio_chat_with_registry(
        messages, tools=None, base_url=DEFAULT_BASE_URL,
        model=DEFAULT_MODEL, api_key=DEFAULT_API_KEY,
        max_iterations=5,
    ):
        try:
            from openai import OpenAI
        except ImportError:
            print("ОШИБКА: pip install openai")
            sys.exit(1)

        client = OpenAI(base_url=base_url, api_key=api_key)

        for iteration in range(max_iterations):
            print(f"\n{'─' * 60}")
            print(f"📤 Итерация {iteration + 1}/{max_iterations}...")

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc:
                print(f"❌ Ошибка: {exc}")
                print(f"\n💡 Убедитесь, что LM Studio запущен на {base_url}")
                return f"Ошибка: {exc}"

            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.tool_calls:
                print(f"🔧 LLM вызывает {len(assistant_msg.tool_calls)} инструмент(ов):")
                messages.append(assistant_msg.model_dump())

                for tc in assistant_msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}

                    print(f"   🔹 {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")

                    try:
                        result = registry.execute(fn_name, **fn_args)
                        result_str = str(result)
                        if len(result_str) > 300:
                            result_str = result_str[:300] + "..."
                        print(f"   ✅ Результат: {result_str}")
                    except Exception as exc:
                        result_str = f"Ошибка: {exc}"
                        print(f"   ❌ {result_str}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
                continue

            final_text = assistant_msg.content or "(пустой ответ)"
            print(f"\n{'─' * 60}")
            print(f"💬 Ответ LLM:")
            print(f"{'─' * 60}")
            print(final_text)
            return final_text

        return "(достигнут лимит итераций)"

    run_lmstudio_chat = run_lmstudio_chat_with_registry
    main()
