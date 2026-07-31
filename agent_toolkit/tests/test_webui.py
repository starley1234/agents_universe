"""Тесты визуального веб-интерфейса каталога, управления, IaC и аналитики (webui.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.webui import get_webui_html
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Визуальный веб-интерфейс каталога инструментов (Web UI)")
    html = get_webui_html()

    check("HTML5 заголовок присутствует", "<!DOCTYPE html>" in html and "Agent Toolkit Explorer" in html)
    check("Вкладки каталога и артефактов есть", "tab-tools" in html and "tab-artifacts" in html)
    check("Вкладка настроек и профилей есть", "tab-settings" in html and "Settings" in html)
    check("Холст 3D-вьювера STL мешей есть", "stl-canvas" in html and "render3dStl" in html)
    check("Кнопка создания демо-артефактов есть", "seedDemoArtifacts" in html)
    check("Элемент поисковой строки есть", 'id="search-box"' in html)
    check("Фильтры по категориям и скилсам есть", "category-filter" in html and "skill-filter" in html)
    check("Модальное окно запуска инструментов есть", "modal-overlay" in html and "executeTool" in html)
    check("Кнопки переключения статуса инструментов есть", "toggleToolStatus" in html)
    check("Функция применения профилей есть", "applyProfile" in html and "profiles-grid" in html)
    check("Кнопки скачивания и загрузки IaC-конфигурации есть", "/api/config/export?format=json" in html and "openImportModal" in html)
    check("Таблица телеметрии и тепловой карты есть", "analytics-table" in html and "fetchAnalytics" in html)
    check("Панель настроек и реквизитов интеграций есть", "int-smtp-host" in html and "saveIntegrationsUi" in html)
    check("Конструктор форм и переключатель JSON/Форма есть", "playground-form" in html and "switchModalMode" in html)
    check("Панель ограничения частоты вызовов есть", "rl-tool-name" in html and "setRateLimitUi" in html)
    check("Панель и кнопки прогона на боевой (Production Test) есть", "prod-test-table" in html and "runProductionTest" in html)
    check("JS-код взаимодействует с REST API", "/api/tools" in html and "/api/artifacts" in html and "/api/settings" in html and "/api/tools/test-production" in html)

    return summary("Тесты визуального каталога-обозревателя (Web UI)")


def test_webui_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
