"""Запуск всего набора тестов agent_toolkit (33 модуля).

Выполняется командой:
  make test  (или python3 tests/test_all.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_adapters
import test_audit
import test_benchmark
import test_cad
import test_code
import test_crypto
import test_data_tools
import test_diagnostics
import test_docker
import test_erp
import test_files
import test_hitl
import test_inventory
import test_jobs
import test_mcp
import test_memory
import test_messaging
import test_patch
import test_pdf
import test_pdf_vlm
import test_physics
import test_prod
import test_quota
import test_registry
import test_sandbox
import test_scraper
import test_site_qa
import test_sql
import test_subagent
import test_teamcenter
import test_tts
import test_web
import test_web_builder
import test_webui


def main() -> int:
    print("=== ЗАПУСК ПОЛНОГО НАБОРА ТЕСТОВ AGENT_TOOLKIT (34 МОДУЛЯ) ===")
    failures = 0
    modules = [
        ("Файлы и Workspace", test_files),
        ("Интеграция с MCP", test_mcp),
        ("Почта и мессенджеры", test_messaging),
        ("QA и аудит сайтов", test_site_qa),
        ("Vision и ритейл-аудит", test_inventory),
        ("Реестр, поиск и политики", test_registry),
        ("Управление квотами ресурсов (QuotaGuard)", test_quota),
        ("Базы данных SQLite (SQL)", test_sql),
        ("Чтение и таблицы PDF", test_pdf),
        ("Умный парсинг PDF через VLM", test_pdf_vlm),
        ("Git и анализ кода", test_code),
        ("Песочница Shell / Python", test_sandbox),
        ("Долговременная память и RAG", test_memory),
        ("Планировщик задач (Cron/Jobs)", test_jobs),
        ("Веб-поиск и HTTP клиент", test_web),
        ("Человек в контуре (HITL)", test_hitl),
        ("Субагенты и оркестрация", test_subagent),
        ("Табличные данные и CSV", test_data_tools),
        ("DOM селекторы и RSS", test_scraper),
        ("Патчи кода и RegEx", test_patch),
        ("Аудит и телеметрия", test_audit),
        ("Криптография и подписи", test_crypto),
        ("Синтез речи (TTS)", test_tts),
        ("1С / ERP OData", test_erp),
        ("Teamcenter PLM (управление требованиями)", test_teamcenter),
        ("САПР / CAD (OpenSCAD, FreeCAD, STL)", test_cad),
        ("Физика, акустика и инженерные расчёты", test_physics),
        ("Создание веб-сайтов, лендингов и SEO-аудит", test_web_builder),
        ("Продакшн, CLI и потоки", test_prod),
        ("Конфигурация Docker для продакшна", test_docker),
        ("Адаптеры фреймворков (OpenAI, LangChain, AWOS)", test_adapters),
        ("Бенчмарк релевантности поиска", test_benchmark),
        ("Визуальный веб-интерфейс (Web UI)", test_webui),
        ("Диагностика и прогон на боевой", test_diagnostics),
    ]

    for title, mod in modules:
        print(f"\n==================================================")
        print(f"Модуль: {title} ({mod.__name__}.py)")
        print(f"==================================================")
        res = mod.run_tests()
        if res != 0:
            failures += 1

    print("\n==================================================")
    if failures == 0:
        print(f"✔ ВСЕ ТЕСТОВЫЕ МОДУЛИ УСПЕШНО ПРОЙДЕНЫ ({len(modules)}/{len(modules)})")
        return 0
    print(f"✗ ОБНАРУЖЕНЫ ОШИБКИ В {failures} МОДУЛЯХ ИЗ {len(modules)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
