"""Веб-интерфейс: синтаксис JS и поведение markdown-рендерера.

Браузер в CI не поднимаем, но логика отрисовки отчёта — код, который может
сломаться незаметно, поэтому гоняем его в node, если он доступен.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "aconstructor" / "web" / "index.html"
node = pytest.mark.skipif(shutil.which("node") is None, reason="нет node")


def ui_script() -> str:
    html = WEB.read_text(encoding="utf-8")
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    return js.split("(async function init()")[0]


SHIM = (
    "globalThis.localStorage={getItem:()=>null,setItem:()=>{}};"
    "globalThis.document={getElementById:()=>null,createElement:()=>({}),"
    "body:{appendChild(){}}};"
    "globalThis.location={hash:''};\n"
)


def run_js(body: str) -> str:
    """Скрипт передаём файлом: в шаблонных литералах JS значимы отступы."""
    src = SHIM + ui_script() + "\n" + body
    p = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_html_is_present_and_selfcontained():
    html = WEB.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    # никаких CDN: интерфейс должен работать в закрытом контуре завода
    assert "http://" not in html.replace("http://127.0.0.1", "")
    assert "cdn." not in html


@node
def test_js_syntax_is_valid(tmp_path):
    f = tmp_path / "ui.js"
    f.write_text(SHIM + ui_script(), encoding="utf-8")
    p = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@node
def test_markdown_renders_report_structures():
    rep = "\n".join([
        "# Заголовок", "", "| A | B |", "|---|---|", "| 1 | 2 |", "",
        "## Раздел", "Текст **жирный** и `код`.", "- пункт списка",
    ])
    out = run_js("console.log(md(" + json.dumps(rep) + "));")
    assert "<h1>Заголовок</h1>" in out
    assert "<h2>Раздел</h2>" in out
    assert out.count("<table>") == 1
    assert "<b>жирный</b>" in out
    assert "<code>код</code>" in out
    assert "<li>пункт списка</li>" in out
    assert "---" not in out, "строка-разделитель таблицы не должна попадать в вывод"


@node
def test_markdown_escapes_html_injection():
    """Отчёт строится из данных клиента — разметка обязана экранироваться."""
    evil = "# <script>alert(1)</script>\n- <img src=x onerror=alert(2)>"
    out = run_js("console.log(md(" + json.dumps(evil) + "));")
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "onerror=alert" not in out or "&lt;img" in out


@node
def test_markdown_handles_empty_and_plain_input():
    out = run_js("console.log(JSON.stringify([md(''), md(null), md('просто текст')]));")
    assert '"просто текст"' in out or "<p>просто текст</p>" in out
