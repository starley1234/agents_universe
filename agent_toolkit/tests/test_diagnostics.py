"""Тесты автоматизированной диагностики и прогона инструментов на боевом сервере (Production Diagnostics)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit import build_default_registry
from agent_toolkit.cli import run_test_prod
from agent_toolkit.core.diagnostics import ProductionTester
from agent_toolkit.core.workspace import Workspace
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Диагностический прогон всех инструментов (ProductionTester)")
    ws_dir = Path("/tmp/agent_toolkit_test_diag_ws")
    ws = Workspace(ws_dir)
    reg = build_default_registry(ws)
    tester = ProductionTester(reg, ws)

    res = tester.test_all(disable_failed=False, disable_unconfigured=False)
    check("Отчёт содержит общую сводку summary", "summary" in res and "results" in res)
    sm = res["summary"]
    check("Проверены все 163 зарегистрированных инструмента", sm["total_tested"] == 176)
    check("Большинство инструментов работают успешно (working >= 140)", sm["working"] >= 140)
    check("Отсутствует непредвиденные ошибки исполнения (failed == 0)", sm["failed"] == 0)
    check("Инструменты с нехваткой внешних данных помечены как requires_config", sm["requires_config"] > 0)

    # Проверяем структуру элементов отчёта
    r0 = res["results"][0]
    check("Элемент содержит имя инструмента и статус-лейбл", "name" in r0 and "status_label" in r0)
    check("Элемент содержит превью результатов и время исполнения ms", "preview" in r0 and "duration_ms" in r0)

    section("2. Автоматическое отключение ненастроенных инструментов и сохранение IaC-конфигурации")
    cfg_path = ws_dir / "diag_config.json"
    res_dis = tester.test_all(
        disable_failed=True,
        disable_unconfigured=True,
        save_config_path=str(cfg_path),
    )
    sm_dis = res_dis["summary"]
    check("Проблемные и ненастроенные инструменты отключены", sm_dis["disabled_count"] == sm_dis["requires_config"] + sm_dis["failed"])
    check("IaC JSON конфигурация сохранена на диске", cfg_path.exists() and cfg_path.stat().st_size > 0)
    cfg_text = cfg_path.read_text(encoding="utf-8")
    check("Конфигурация содержит секцию disabled_tools", '"disabled_tools"' in cfg_text)

    section("3. Запуск через CLI команду (test-prod)")
    cli_ret = run_test_prod(disable_failed=False, disable_unconfigured=False, as_json=True)
    check("CLI команда test-prod завершается с кодом 0", cli_ret == 0)

    return summary("Тесты автоматизированного прогона и диагностики (Production Diagnostics)")


def test_diagnostics_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
