"""Интерфейс командной строки (CLI) для проверки и работы с agent_toolkit в продакшне."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__, build_default_registry
from .benchmark import run_benchmark
from .config import settings
from .core import SecurityPolicy, Workspace


def run_check() -> int:
    """Самопроверка продакшн-окружения и корректности работы реестра."""
    print(f"=== САМОПРОВЕРКА AGENT_TOOLKIT (v{__version__}) ===")
    print(f"Python: {sys.version.split()[0]} ({sys.platform})")
    print(f"Режим: {settings.env.upper()} (mock_mode={settings.mock_mode})")
    print(f"Корневая директория песочницы: {settings.workspace_dir}")

    ws = Workspace(settings.workspace_dir)
    print(f" [ok] Рабочая область доступна на запись: {ws.root}")

    reg = build_default_registry(ws)
    tools = reg.list_tools()
    print(f" [ok] Зарегистрировано инструментов: {len(tools)}")
    skills = reg.group_by_skill()
    print(f" [ok] Уникальных скилсов в системе: {len(skills)}")

    # Проверка политики
    pol = SecurityPolicy(allow_dangerous=False, read_only=False)
    allowed = sum(1 for t in tools if pol.is_tool_allowed(t))
    print(f" [ok] Безопасных инструментов (allow_dangerous=False): {allowed}/{len(tools)}")

    # Валидация схем
    for t in tools:
        sch = t.schema()
        if "name" not in sch["function"] or "description" not in sch["function"]:
            print(f" [error] Некорректная схема у инструмента {t.name}")
            return 1
    print(" [ok] Схемы всех инструментов валидны (OpenAI / MCP спецификация)")
    print("=== САМОПРОВЕРКА УСПЕШНО ЗАВЕРШЕНА ===")
    return 0


def run_list(skill: str | None = None) -> int:
    """Вывести список доступных инструментов."""
    reg = build_default_registry()
    tools = reg.list_tools()
    if skill:
        tools = [t for t in tools if skill in t.skills]
        print(f"### Инструменты со скилсом '{skill}' ({len(tools)}):")
    else:
        print(f"### Все зарегистрированные инструменты ({len(tools)}):")

    for t in sorted(tools, key=lambda x: x.name):
        cat = t.attributes.get("category", "")
        print(f"- {t.name:32} | {cat:12} | {t.description}")
    return 0


def run_execute(tool_name: str, args_json: str = "{}") -> int:
    """Выполнить инструмент из командной строки."""
    reg = build_default_registry()
    try:
        args: dict[str, Any] = json.loads(args_json) if args_json else {}
    except ValueError as exc:
        print(f"Ошибка JSON-аргументов: {exc}", file=sys.stderr)
        return 1

    try:
        res = reg.execute(tool_name, **args)
        print(f"=== РЕЗУЛЬТАТ `{tool_name}` ===")
        print(res)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка выполнения `{tool_name}`: {exc}", file=sys.stderr)
        return 1


def run_serve(port: int = 8090, host: str = "127.0.0.1") -> int:
    """Запустить HTTP REST API и MCP-сервер."""
    try:
        import uvicorn

        from .api import create_api_app
    except ImportError as exc:
        print(
            "Для запуска HTTP-сервера установите fastapi и uvicorn:\n"
            "  pip install fastapi uvicorn",
            file=sys.stderr,
        )
        return 1

    reg = build_default_registry()
    app = create_api_app(reg)
    print(f"=== ЗАПУСК AGENT_TOOLKIT HTTP API & MCP НА {host}:{port} ===")
    uvicorn.run(app, host=host, port=port)
    return 0


def run_benchmark_cli() -> int:
    """Запустить бенчмарк релевантности поиска и скорости (100 запросов)."""
    print(f"=== ЗАПУСК БЕНЧМАРКА УМНОГО ПОИСКА AGENT_TOOLKIT (v{__version__}) ===")
    report = run_benchmark()
    print(f"Всего тестовых запросов: {report.total_queries}")
    print(f"Top-1 Precision@1:       {report.precision_at_1}%")
    print(f"Top-3 Recall / Prec@3:   {report.precision_at_3}%")
    print(f"Mean Reciprocal Rank:    {report.mrr}")
    print(f"Средняя задержка:        {report.avg_latency_ms} мс/запрос")
    if report.failed_queries:
        print(f"\nЗапросы, потребовавшие уточнения ({len(report.failed_queries)}):")
        for q, expected, found in report.failed_queries[:5]:
            print(f"  * '{q}' -> ожидали {expected}, в топе {found}")
    print("=== БЕНЧМАРК УСПЕШНО ЗАВЕРШЁН ===")
    return 0


def run_test_prod(
    disable_failed: bool = False,
    disable_unconfigured: bool = False,
    as_json: bool = False,
    save_config_path: str | None = None,
) -> int:
    """Прогнать все инструменты на боевом сервере и получить отчёт с превью результатов."""
    import json
    from . import build_default_registry
    from .core.diagnostics import ProductionTester
    from .core.workspace import Workspace

    ws = Workspace(settings.workspace_dir)
    reg = build_default_registry(ws)
    tester = ProductionTester(reg, ws)

    res = tester.test_all(
        disable_failed=disable_failed,
        disable_unconfigured=disable_unconfigured,
        save_config_path=save_config_path,
    )

    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    sm = res["summary"]
    print("=" * 106)
    print(" === ПРОГОН ВСЕХ ИНСТРУМЕНТОВ НА БОЕВОМ СЕРВЕРЕ (PRODUCTION DIAGNOSTICS & TEST) ===")
    print("=" * 106)
    print(f" Всего проверено инструментов : {sm['total_tested']}")
    print(f" ✅ Работает (OK)              : {sm['working']}")
    print(f" ⚠️ Требует настройки         : {sm['requires_config']}")
    print(f" ❌ Ошибок                    : {sm['failed']}")
    if sm["disabled_count"] > 0:
        print(f" ✕ Отключено инструментов     : {sm['disabled_count']}")
    print("-" * 106)
    print(f"{'ИНСТРУМЕНТ':<34} | {'СТАТУС':<22} | {'ПРЕВЬЮ РЕЗУЛЬТАТА':<42}")
    print("-" * 106)

    for r in res["results"]:
        status_lbl = r["status_label"]
        preview_short = r["preview"].replace("\n", " ")
        if len(preview_short) > 42:
            preview_short = preview_short[:39] + "..."
        print(f"{r['name']:<34} | {status_lbl:<22} | {preview_short:<42}")

    print("-" * 106)
    if res["config_saved_to"]:
        print(f"[✓] Обновлённая конфигурация с отключёнными инструментами сохранена в: {res['config_saved_to']}")
    else:
        print(
            "Подсказка: используйте флаги --disable-failed / --disable-unconfigured для автоматического "
            "отключения неработающих инструментов в реестре и сохранения toolkit_config.json."
        )
    print("=" * 106)
    return 0


def run_config_cli(
    list_ports: bool = False,
    generate_for: str | None = None,
    docker_override_for: str | None = None,
    save_path: str | None = None,
) -> int:
    """Управление единой конфигурацией монорепозитория (матрица портов, .env, docker-compose.override.yml)."""
    from .monorepo_config import (
        generate_docker_compose_override,
        generate_env_content,
        list_monorepo_ports,
    )

    if docker_override_for:
        try:
            content = generate_docker_compose_override(docker_override_for)
        except KeyError as exc:
            print(f"Ошибка: {exc}")
            return 1
        if save_path:
            from pathlib import Path

            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            print(f"[✓] Файл docker-compose.override.yml для '{docker_override_for}' сохранён в {p}")
        else:
            print(content)
        return 0

    if list_ports or not generate_for:
        ports = list_monorepo_ports()
        print("=" * 104)
        print(" === ЕДИНАЯ МАТРИЦА КОНФИГУРАЦИИ И ПОРТОВ (MONOREPO CONFIGURATION & PORTS) ===")
        print("=" * 104)
        print(f"{'КЛЮЧ ПРОЕКТА':<30} | {'НАЗВАНИЕ (PROJECT_NAME)':<28} | {'ПОРТ':<8} | {'БАЗА ДАННЫХ'}")
        print("-" * 104)
        for item in ports:
            print(
                f"{item['project_key']:<30} | {item['project_name']:<28} | {item['app_port']:<8} | {item['db_default']}"
            )
        print("=" * 104)
        print("Подсказка: используйте --generate <project_key> для создания стандартного .env-файла.")
        return 0

    try:
        content = generate_env_content(generate_for)
    except KeyError as exc:
        print(f"Ошибка: {exc}")
        return 1

    if save_path:
        from pathlib import Path

        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        print(f"[✓] Единый файл конфигурации для '{generate_for}' успешно сохранён в {p}")
    else:
        print(content)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_toolkit",
        description="Командный интерфейс для агентского инструментария agent_toolkit.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Доступные команды")

    subparsers.add_parser("check", help="Провести самопроверку продакшн-окружения")
    subparsers.add_parser("benchmark", help="Запустить бенчмарк релевантности и скорости поиска")

    p_test_prod = subparsers.add_parser("test-prod", help="Прогнать все инструменты на боевом сервере с превью и диагностикой")
    p_test_prod.add_argument("--disable-failed", action="store_true", help="Отключить инструменты с ошибкой (❌ Ошибка)")
    p_test_prod.add_argument("--disable-unconfigured", action="store_true", help="Отключить инструменты, требующие настройки (⚠️ Требует настройки)")
    p_test_prod.add_argument("--json", action="store_true", help="Вывести отчёт в формате JSON")
    p_test_prod.add_argument("--save-config", type=str, default=None, help="Путь для сохранения конфигурации")

    p_config = subparsers.add_parser("config", help="Управление единым стандартом конфигурации монорепозитория (.env и порты)")
    p_config.add_argument("--list", action="store_true", help="Вывести матрицу портов APP_PORT всех 10 проектов")
    p_config.add_argument("--generate", type=str, default=None, help="Сгенерировать .env файл для указанного проекта")
    p_config.add_argument("--docker-override", type=str, default=None, help="Сгенерировать docker-compose.override.yml для синхронизации портов")
    p_config.add_argument("--save", type=str, default=None, help="Путь сохранения файла")

    p_list = subparsers.add_parser("list", help="Вывести список всех инструментов")
    p_list.add_argument("--skill", help="Отфильтровать инструменты по скилсу")

    p_exec = subparsers.add_parser("execute", help="Выполнить инструмент с аргументами")
    p_exec.add_argument("tool", help="Имя инструмента (например, 'files.read_file')")
    p_exec.add_argument("args", nargs="?", default="{}", help="JSON-объект аргументов")

    p_serve = subparsers.add_parser("serve", help="Запустить HTTP и MCP сервер")
    p_serve.add_argument("--port", type=int, default=settings.api_port, help="Порт сервера")
    p_serve.add_argument("--host", default="127.0.0.1", help="Хост сервера")

    args = parser.parse_args(argv)

    if args.command == "check":
        return run_check()
    if args.command == "benchmark":
        return run_benchmark_cli()
    if args.command == "test-prod":
        return run_test_prod(
            disable_failed=args.disable_failed,
            disable_unconfigured=args.disable_unconfigured,
            as_json=args.json,
            save_config_path=args.save_config,
        )
    if args.command == "config":
        return run_config_cli(
            list_ports=args.list,
            generate_for=args.generate,
            docker_override_for=args.docker_override,
            save_path=args.save,
        )
    if args.command == "list":
        return run_list(args.skill)
    if args.command == "execute":
        return run_execute(args.tool, args.args)
    if args.command == "serve":
        return run_serve(args.port, args.host)

    parser.print_help()
    return 0
