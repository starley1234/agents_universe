"""Тесты инструментов создания сайтов и SEO-аудита (web.build_static_site, web.create_landing_page)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.web_builder import build_web_builder_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты создания сайтов и SEO-аудита (web.build_*)")
        tools = {t.name: t for t in build_web_builder_tools(ws)}
        check("зарегистрировано 3 инструмента web_builder", len(tools) == 3)

        res_site = tools["web.build_static_site"].execute(
            site_dir="mysite", title="Тестовый сайт", pages_json='[{"filename": "index.html", "title": "Главная", "content": "<h1>Привет</h1>"}]'
        )
        check("build_static_site создаёт статический сайт", "успешно создан" in res_site and "index.html" in res_site)
        check("файл index.html создан на диске", ws.exists("mysite/index.html"))

        res_landing = tools["web.create_landing_page"].execute(
            path="landing/index.html", hero_title="AI Платформа", cta_text="Регистрация"
        )
        check("create_landing_page создаёт лендинг с Hero и CTA", "AI Платформа" in res_landing and "Регистрация" in res_landing)
        check("файл лендинга создан", ws.exists("landing/index.html"))

        html_seo = '<html lang="ru"><head><title>Мой сайт</title><meta name="description" content="О нас"><meta name="viewport" content="width=device-width"><h1 id="top">Заголовок</h1></head></html>'
        res_seo = tools["web.audit_site_seo_performance"].execute(html_content=html_seo)
        check("audit_site_seo_performance проверяет SEO и ставит высший балл 100/100", "100/100" in res_seo and "ОТЛИЧНО" in res_seo)

    return summary("Тесты создания сайтов и SEO")


def test_web_builder_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
