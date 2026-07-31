"""Тесты скрапинга, DOM-селекторов и RSS-лент (html.*, scraper.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.local.scraper import build_scraper_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. DOM селекторы и RSS парсер (html.*, scraper.*)")
    tools = {t.name: t for t in build_scraper_tools()}
    check("зарегистрировано 2 инструмента скрапинга", len(tools) == 2)

    html_sample = "<html><body><span class='price'>120 руб</span><span class='price'>240 руб</span></body></html>"
    res_sel = tools["html.extract_by_selector"].execute(
        html_content=html_sample, selector="span.price"
    )
    check("extract_by_selector находит элементы по классу", "120 руб" in res_sel and "240 руб" in res_sel)

    rss_sample = "<rss><channel><item><title>Аудит завершён</title><link>http://test/1</link></item></channel></rss>"
    res_rss = tools["scraper.parse_feed"].execute(feed_xml=rss_sample)
    check("parse_feed извлекает заголовки и ссылки из RSS", "Аудит завершён" in res_rss and "http://test/1" in res_rss)

    return summary("Тесты скрапинга и RSS")


def test_scraper_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
