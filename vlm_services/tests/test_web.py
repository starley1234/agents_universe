"""Веб-интерфейс: синтаксис JS и рендер markdown (в node, если он есть)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "vlmkit" / "web" / "index.html"
node = pytest.mark.skipif(shutil.which("node") is None, reason="нет node")

SHIM = ("globalThis.localStorage={getItem:()=>null,setItem:()=>{}};"
        "globalThis.document={getElementById:()=>null,createElement:()=>({}),"
        "body:{appendChild(){}}};globalThis.location={hash:''};"
        "globalThis.fetch=()=>Promise.reject(new Error('no net'));\n")


def ui_script() -> str:
    html = WEB.read_text(encoding="utf-8")
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    return js.split("(async function init()")[0]


def run_js(body: str) -> str:
    p = subprocess.run(["node", "-e", SHIM + ui_script() + "\n" + body],
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_ui_has_no_external_dependencies():
    """Интерфейс должен работать в закрытом контуре — без CDN."""
    html = WEB.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "cdn." not in html
    assert "https://" not in html


@node
def test_js_syntax_valid(tmp_path):
    f = tmp_path / "ui.js"
    f.write_text(SHIM + ui_script(), encoding="utf-8")
    p = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@node
def test_markdown_renders_report():
    rep = "\n".join(["# Отчёт", "", "| A | B |", "|---|---|", "| 1 | 2 |", "",
                     "## Раздел", "Текст **жирный** и `код`.", "- пункт",
                     "> предупреждение"])
    out = run_js("console.log(md(" + json.dumps(rep) + "));")
    assert "<h1>Отчёт</h1>" in out and "<h2>Раздел</h2>" in out
    assert out.count("<table>") == 1 and out.count("<tr>") == 2
    assert "<b>жирный</b>" in out and "<code>код</code>" in out
    assert "<li>пункт</li>" in out and "<blockquote>" in out
    assert "---" not in out


@node
def test_markdown_escapes_injection():
    """Отчёты строятся из данных клиента — экранирование обязательно."""
    evil = "# <script>alert(1)</script>\n- <img src=x onerror=alert(2)>"
    out = run_js("console.log(md(" + json.dumps(evil) + "));")
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "onerror=alert" not in out or "&lt;img" in out


@node
def test_markdown_handles_numbered_lists_and_empty():
    out = run_js("console.log(md('1. первый\\n2. второй') + '|' + md(''));")
    assert "<li>первый</li>" in out and "<li>второй</li>" in out


# --- скрипт диагностики ----------------------------------------------------
def test_live_check_explains_wrong_interpreter():
    """Запуск системным Python — частая ошибка; нужна подсказка, а не трейсбек."""
    import subprocess
    import sys as _sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "live_check.py"
    # /usr/bin/python3 — интерпретатор без наших зависимостей
    p = subprocess.run(["/usr/bin/python3", str(script), "--save", "out/"],
                       capture_output=True, text=True, timeout=60)
    if "langchain_core" not in p.stdout and p.returncode == 0:
        pytest.skip("в системном python3 зависимости установлены")
    assert p.returncode == 2, "должен быть внятный выход, а не падение"
    assert "Traceback" not in p.stdout
    assert ".venv" in p.stdout, "нужно показать правильный интерпретатор"
    assert "--save out/" in p.stdout, "аргументы пользователя надо сохранить"


def test_live_check_is_valid_python():
    import py_compile

    script = Path(__file__).resolve().parent.parent / "scripts" / "live_check.py"
    py_compile.compile(str(script), doraise=True)
