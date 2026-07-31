"""Тесты проверки качества веб-сайтов (QA, ссылки, доступность WCAG, SEO, Workflow)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local import build_site_qa_tools
from agent_toolkit.workflows import WebsiteAuditWorkflow, build_website_workflow_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        section("1. Инструменты контроля качества сайта (site_qa.*)")
        ws = tmp.path("ws")
        qa_tools = {t.name: t for t in build_site_qa_tools()}
        check("зарегистрировано 4 инструмента site_qa", len(qa_tools) == 4)

        # 1) check_url (mock режим)
        res_url = qa_tools["site_qa.check_url"].execute(url="mock://example.com")
        check("check_url с mock:// работает", "Статус 200 OK" in res_url)

        # 2) check_links
        html_links = (
            "<html><body>"
            "<a href='/about'>О нас</a>"
            "<a href='https://external.com'>External</a>"
            "<a href='#top'>Вверх</a>"
            "<a href='#'>Пустая</a>"
            "</body></html>"
        )
        res_links = qa_tools["site_qa.check_links"].execute(html_content=html_links)
        check("check_links считает общее число ссылок", "Всего найдено ссылок: 4" in res_links)
        check("check_links выделяет внешние ссылки", "Внешних ссылок: 1" in res_links)
        check("check_links находит якоря", "Якорей (#): 2" in res_links)

        # 3) check_accessibility (WCAG 2.1)
        html_bad_wcag = (
            "<html><body>"
            "<h3>Сразу H3 без H1 и H2</h3>"
            "<img src='photo.jpg'>"
            "</body></html>"
        )
        res_wcag = qa_tools["site_qa.check_accessibility"].execute(html_content=html_bad_wcag)
        check("check_accessibility находит отсутствие H1", "Отсутствует тег <h1>" in res_wcag)
        check("check_accessibility находит отсутствие alt у img", "без атрибута alt" in res_wcag)
        check("check_accessibility снижает оценку", "Оценка доступности:" in res_wcag)

        html_good_wcag = "<html><body><h1>Главная</h1><img src='ok.jpg' alt='Логотип'></body></html>"
        res_good = qa_tools["site_qa.check_accessibility"].execute(html_content=html_good_wcag)
        check("check_accessibility ставит 100 при хорошей вёрстке", "100/100" in res_good)

        # 4) check_seo_meta
        html_seo = (
            "<html><head>"
            "<title>Хороший заголовок страницы</title>"
            "<meta name='description' content='Понятное описание'>"
            "<link rel='canonical' href='https://mysite.com/page'>"
            "<meta property='og:title' content='OG заголовок'>"
            "</head><body></body></html>"
        )
        res_seo = qa_tools["site_qa.check_seo_meta"].execute(html_content=html_seo)
        check("check_seo_meta находит title", "Хороший заголовок страницы" in res_seo)
        check("check_seo_meta находит canonical", "https://mysite.com/page" in res_seo)

        section("2. Рабочий процесс аудита сайта (WebsiteAuditWorkflow)")
        ws = Workspace(tmp.path("ws"))
        wf = WebsiteAuditWorkflow(ws=ws)
        wf_res = wf.run_audit(url="mock://mysite.com", save_path="report.md")
        check("Workflow возвращает структуру отчёта", wf_res["url"] == "mock://mysite.com")
        check("Отчёт сохранён на диске", ws.exists("report.md"))
        check("Отчёт содержит разделы проверок", "Доступность сервера" in wf_res["report_text"])

        wf_tools = {t.name: t for t in build_website_workflow_tools(ws)}
        res_wf_tool = wf_tools["workflow.audit_website"].execute(
            url="mock://test.ru", save_path="test_report.md"
        )
        check("Инструмент workflow.audit_website выполняет полный аудит", "Протокол аудита сайта mock://test.ru" in res_wf_tool)

    return summary("Тесты QA и аудита сайтов")


def test_site_qa_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
