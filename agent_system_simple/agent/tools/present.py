"""Показ артефакта: собрать результат прогона в одну страницу.

Зачем: после восьми часов автономной работы результат лежит россыпью
файлов, и человек должен сам в них копаться. «Вау» рождается в момент
показа — когда открываешь одну страницу и сразу видишь, что сделано,
какие числа получены и что не получилось.

Отчёт самодостаточен: один HTML-файл без внешних ссылок, картинки
вшиты в base64. Можно отправить письмом или открыть на другой машине.
"""
from __future__ import annotations

import base64
import html
import json
import time
from pathlib import Path

from ..store import Store
from .base import Tool, ToolError, Workspace

IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}
TEXT_EXT = {".txt", ".md", ".py", ".scad", ".json", ".csv", ".html", ".css",
            ".js", ".sh", ".yml", ".yaml", ".toml", ".ini", ".sql"}
MAX_EMBED = 3_000_000        # не вшиваем картинки тяжелее 3 МБ
MAX_TEXT = 12_000            # символов текстового файла в отчёт

CSS = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
     background:#0f1115;color:#e6e8ee}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#8b91a3;font-size:14px;margin-bottom:28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
       gap:12px;margin-bottom:28px}
.card{background:#1a1d26;border:1px solid #2a2f3d;border-radius:10px;padding:14px}
.card .n{font-size:24px;font-weight:600}
.card .l{color:#8b91a3;font-size:12px;margin-top:2px}
h2{font-size:18px;margin:32px 0 12px;padding-bottom:8px;border-bottom:1px solid #2a2f3d}
.item{background:#1a1d26;border:1px solid #2a2f3d;border-radius:8px;
      padding:10px 14px;margin-bottom:8px}
.done{border-left:3px solid #3ecf8e}
.failed{border-left:3px solid #ff6b6b}
.open{border-left:3px solid #8b91a3}
.tag{display:inline-block;background:#2a2f3d;color:#a8b0c2;font-size:11px;
     padding:2px 8px;border-radius:99px;margin-left:6px}
.res{color:#8b91a3;font-size:13px;margin-top:4px}
pre{background:#12141a;border:1px solid #2a2f3d;border-radius:8px;padding:12px;
    overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace;max-height:420px}
img{max-width:100%;border-radius:8px;border:1px solid #2a2f3d;display:block}
details summary{cursor:pointer;color:#4a9eff;padding:4px 0}
.empty{color:#8b91a3;font-style:italic}
a{color:#4a9eff}
"""


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def build(ws: Workspace, store: Store | None, run_id_getter) -> list[Tool]:

    def _embed(path: Path) -> str:
        """Файл в HTML-блок: картинка вшивается, текст показывается."""
        ext = path.suffix.lower()
        rel = _esc(ws.relative(path))
        size = path.stat().st_size
        if ext in IMG_EXT:
            if size > MAX_EMBED:
                return (f"<div class='item'><b>{rel}</b>"
                        f"<div class='res'>картинка {size // 1024} КБ — "
                        f"слишком велика для вставки</div></div>")
            data = base64.b64encode(path.read_bytes()).decode()
            return (f"<h2>{rel}</h2>"
                    f"<img src='data:{IMG_EXT[ext]};base64,{data}' alt='{rel}'>")
        if ext in TEXT_EXT or size < 100_000:
            try:
                txt = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return f"<div class='item'>{rel}: не прочитать ({exc})</div>"
            clipped = txt[:MAX_TEXT]
            more = ("\n… обрезано, всего "
                    f"{len(txt)} символов" if len(txt) > MAX_TEXT else "")
            return (f"<h2>{rel}</h2><details open><summary>{size} Б</summary>"
                    f"<pre>{_esc(clipped + more)}</pre></details>")
        return (f"<div class='item'><b>{rel}</b>"
                f"<div class='res'>двоичный файл, {size} Б</div></div>")

    def present(title: str = "", files: str = "",
                summary: str = "", out_path: str = "report.html") -> str:
        """Собрать HTML-отчёт: итоги прогона + указанные файлы."""
        rid = run_id_getter() if run_id_getter else 0
        parts: list[str] = []

        run = store.get_run(rid) if (store and rid) else None
        head = title or (run["goal"] if run else "Отчёт агента")
        parts.append(f"<h1>{_esc(head)}</h1>")
        stamp = time.strftime("%d.%m.%Y %H:%M")
        parts.append(f"<div class='sub'>сформировано {stamp}"
                     + (f" · прогон #{rid}" if rid else "") + "</div>")

        # плитки со сводкой
        if store and rid:
            tasks = store.tasks(rid)
            done = sum(1 for t in tasks if t["status"] == "done")
            failed = sum(1 for t in tasks if t["status"] == "failed")
            e, r = store.graph_stats()
            mins = ((run["finished"] or time.time()) - run["started"]) / 60 \
                if run else 0
            cards = [(f"{done}/{len(tasks)}", "пунктов плана"),
                     (str(failed), "неудач"),
                     (str(store.fact_count()), "фактов в памяти"),
                     (f"{e}/{r}", "объектов/связей"),
                     (f"{run['tool_calls']}" if run else "0", "вызовов"),
                     (f"{mins:.0f} мин", "время")]
            parts.append("<div class='cards'>" + "".join(
                f"<div class='card'><div class='n'>{_esc(n)}</div>"
                f"<div class='l'>{_esc(l)}</div></div>" for n, l in cards
            ) + "</div>")

        if summary.strip():
            parts.append("<h2>Итог</h2><div class='item'>"
                         + _esc(summary).replace("\n", "<br>") + "</div>")

        # план
        if store and rid:
            tasks = store.tasks(rid)
            if tasks:
                parts.append("<h2>План</h2>")
                for t in tasks:
                    cls = t["status"] if t["status"] in ("done", "failed") else "open"
                    res = (f"<div class='res'>{_esc(t['result'][:400])}</div>"
                           if t["result"] else "")
                    parts.append(
                        f"<div class='item {cls}'>{_esc(t['title'])}"
                        f"<span class='tag'>{t['status']}</span>{res}</div>")

            facts = store.recall("", limit=40)
            if facts:
                parts.append("<h2>Что выяснено</h2>")
                for f in facts:
                    tag = (f"<span class='tag'>{_esc(f['tags'])}</span>"
                           if f["tags"] else "")
                    parts.append(f"<div class='item'>{_esc(f['text'])}{tag}</div>")

        # файлы
        names = [s.strip() for s in files.replace(",", "\n").splitlines()
                 if s.strip()]
        missing = []
        for n in names:
            try:
                p = ws.resolve(n)
            except ToolError as exc:
                missing.append(f"{n} — {exc}")
                continue
            if not p.exists():
                missing.append(f"{n} — не найден")
                continue
            if p.is_dir():
                missing.append(f"{n} — это папка")
                continue
            parts.append(_embed(p))
        if missing:
            parts.append("<h2>Не удалось приложить</h2>" + "".join(
                f"<div class='item failed'>{_esc(m)}</div>" for m in missing))

        doc = (f"<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
               f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
               f"<title>{_esc(head)}</title><style>{CSS}</style></head>"
               f"<body><div class='wrap'>{''.join(parts)}</div></body></html>")

        out = ws.resolve(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(doc, encoding="utf-8")
        return (f"Отчёт готов: {ws.relative(out)} ({len(doc) // 1024} КБ)\n"
                f"Приложено файлов: {len(names) - len(missing)}"
                + (f", не удалось: {len(missing)}" if missing else "")
                + "\nОткройте его в браузере — это итог работы для человека.")

    return [
        Tool("present",
             "Собрать итоговый HTML-отчёт: сводка прогона, план, выясненные "
             "факты и приложенные файлы (картинки вшиваются, тексты "
             "показываются). Вызывай В КОНЦЕ работы — это то, что увидит "
             "человек вместо россыпи файлов.",
             {"type": "object",
              "properties": {
                  "title": {"type": "string", "description": "Заголовок отчёта"},
                  "files": {"type": "string",
                            "description": "Файлы через запятую или с новой строки"},
                  "summary": {"type": "string",
                              "description": "Итог своими словами"},
                  "out_path": {"type": "string",
                               "description": "Куда сохранить, по умолчанию report.html"}},
              "required": []},
             present),
    ]
