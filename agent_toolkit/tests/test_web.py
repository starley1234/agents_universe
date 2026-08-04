"""Тесты инструментов веб-поиска (web.*) и HTTP/REST клиента (http.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.integrations.http import build_http_tools
from agent_toolkit.integrations.web import build_web_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Веб-поиск и скачивание страниц (web.*)")
    web_tools = {t.name: t for t in build_web_tools()}
    check("зарегистрировано 24 инструментов web", len(web_tools) == 24)

    # 1.1 Общий поиск и скачивание
    res_search = web_tools["web.search"].execute(query="авиационные правила")
    check("web.search возвращает список результатов", "Авиационные правила" in res_search)

    res_fetch = web_tools["web.fetch_page"].execute(url="mock://test.ru")
    check("web.fetch_page скачивает страницу в mock-режиме", "Тестовая страница" in res_fetch)

    # 1.2 DuckDuckGo поиск, новости и быстрые ответы
    res_ddg = web_tools["web.search_duckduckgo"].execute(query="mock: share of shelf")
    check("web.search_duckduckgo работает в mock-режиме", "DuckDuckGo Official Privacy Search" in res_ddg)

    res_news = web_tools["web.search_news"].execute(query="mock: ai agents")
    check("web.search_news возвращает новости с датой", "Инновации в автоматизации ИИ-агентов" in res_news)

    res_ans = web_tools["web.search_duckduckgo_answers"].execute(query="mock: Share of Shelf")
    check("web.search_duckduckgo_answers возвращает определение", "Определение:" in res_ans)

    # 1.3 Извлечение Markdown, ссылок, таблиц и метаданных
    res_md = web_tools["web.fetch_markdown"].execute(url="mock://example.com/doc")
    check("web.fetch_markdown конвертирует HTML в Markdown", "# Заголовок страницы" in res_md and " | " in res_md)

    res_links = web_tools["web.extract_links"].execute(html_or_url="mock://example.com")
    check("web.extract_links извлекает ссылки из страницы", "Документация" in res_links and "GitHub Repo" in res_links)

    res_tables_md = web_tools["web.extract_tables_html"].execute(
        html_or_url="mock://example.com/prices", output_format="markdown"
    )
    check("web.extract_tables_html возвращает таблицу в Markdown", "| Товар | Цена |" in res_tables_md)

    res_tables_csv = web_tools["web.extract_tables_html"].execute(
        html_or_url="mock://example.com/prices", output_format="csv"
    )
    check("web.extract_tables_html возвращает таблицу в CSV", '"Молоко 1л","85.00","120"' in res_tables_csv)

    res_meta = web_tools["web.extract_metadata_html"].execute(html_or_url="mock://example.com")
    check("web.extract_metadata_html извлекает title и SEO метатеги", "Тестовая страница" in res_meta and "Canonical URL:" in res_meta)

    # 1.4 Проверка robots.txt и карты сайта (sitemap.xml)
    res_robots = web_tools["web.check_robots_txt"].execute(url="mock://example.com/admin/test")
    check("web.check_robots_txt сообщает статус сканирования", "РАЗРЕШЕНО (Allowed)" in res_robots)

    res_sitemap = web_tools["web.fetch_sitemap"].execute(sitemap_url="mock://example.com/sitemap.xml")
    check("web.fetch_sitemap возвращает список URL сайта", "https://example.com/home" in res_sitemap)

    # 1.5 Работа с формами и валидация
    res_forms = web_tools["web.extract_forms"].execute(html_or_url="mock://example.com/login")
    check("web.extract_forms находит формы и поля ввода", "action: `/login`" in res_forms and "username" in res_forms)

    res_submit = web_tools["web.submit_form"].execute(
        action_url="mock://example.com/submit", method="POST", form_data_json='{"query": "test"}'
    )
    check("web.submit_form отправляет данные формы", "200 OK" in res_submit and '{"query": "test"}' in res_submit)

    form_html = '<form action="/test" method="POST"><input name="user" required><input name="email" type="email" required></form>'
    res_fill_ok = web_tools["web.simulate_form_fill"].execute(
        form_html=form_html, values_json='{"user": "admin", "email": "test@example.com"}'
    )
    check("web.simulate_form_fill подтверждает валидную форму", "✅ УСПЕШНО" in res_fill_ok)

    res_fill_err = web_tools["web.simulate_form_fill"].execute(
        form_html=form_html, values_json='{"user": "", "email": "wrong_email"}'
    )
    check("web.simulate_form_fill обнаруживает ошибки валидации", "⚠️ ОБНАРУЖЕНЫ ОШИБКИ" in res_fill_err and "обязательное" in res_fill_err.lower())

    # 1.6 Моделирование действий браузера (Browser Automation)
    actions = '[{"action": "goto", "url": "https://example.com"}, {"action": "fill", "selector": "#user", "value": "test"}, {"action": "click", "selector": "#btn"}]'
    res_browser = web_tools["web.simulate_browser_action"].execute(actions_json=actions)
    check("web.simulate_browser_action генерирует журнал шагов", "[GOTO]" in res_browser and "[FILL]" in res_browser and "[CLICK]" in res_browser)

    # 1.7 Продвинутая браузерная автоматизация и сессии (Web & Browser Automation AI)
    res_pw = web_tools["web.playwright_session"].execute(url="mock://spa.example.com", script_json='[{"action": "click", "selector": "#btn"}]')
    check("web.playwright_session работает в mock-режиме", "Сессия Playwright" in res_pw and "200 OK" in res_pw)

    res_pup = web_tools["web.puppeteer_action"].execute(action="click", selector="#submit", url="mock://spa.example.com")
    check("web.puppeteer_action выполняет действие", "CLICK" in res_pup and "успешно" in res_pup)

    res_schema = web_tools["web.extract_schema_org"].execute(html_or_url="mock://example.com/item")
    check("web.extract_schema_org извлекает JSON-LD микроразметку", "Product" in res_schema and "Schema.org" in res_schema)

    res_shot = web_tools["web.capture_full_screenshot"].execute(url="mock://example.com", output_path="full_screen.png")
    check("web.capture_full_screenshot сохраняет снимок в Workspace", "full_screen.png" in res_shot and "1920x4000" in res_shot)

    res_ck_set = web_tools["web.cookie_session_manager"].execute(action="set", domain="test.com", name="sid", value="123")
    check("web.cookie_session_manager устанавливает cookie", "set" in res_ck_set and "sid=123" in res_ck_set)

    section("2. Универсальный HTTP/REST клиент (http.*)")
    http_tools = {t.name: t for t in build_http_tools()}
    check("зарегистрирован 1 инструмент http", len(http_tools) == 1)

    res_http = http_tools["http.request"].execute(
        method="GET", url="mock://api.test.ru/v1/status"
    )
    check("http.request выполняет запрос к mock://", "200 OK" in res_http and "success" in res_http)

    return summary("Тесты веб-поиска и HTTP клиента")


def test_web_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
