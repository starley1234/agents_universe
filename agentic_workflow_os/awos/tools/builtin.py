"""Встроенные инструменты среды: файлы, HTTP, SQL, shell, доска контекста.

Набор намеренно маленький и «системный»: среда даёт доступ к внешнему
миру, а не библиотеку прикладных умений. Всё специализированное (парсер
PDF, клиент CRM) подключается снаружи через ToolRegistry.add() — так
платформа не разрастается предметной логикой.

Каждый инструмент здесь:
  * проверяет свои аргументы САМ и падает понятным ToolError, а не
    трейсбеком: текст ошибки читает модель и исправляется;
  * ограничивает объём результата — вывод инструмента конкурирует за
    контекст с самой задачей;
  * не имеет доступа шире, чем выдала среда (гранты в конфиге).
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import Tool, ToolError, Workspace

#: Ограничение на размер файла, который целиком уедет в контекст модели.
READ_LIMIT = 40_000


def _arg(args: dict[str, Any], name: str, required: bool = True,
         default: Any = "") -> Any:
    if name not in args or args[name] is None or args[name] == "":
        if required:
            raise ToolError(f"Не хватает аргумента {name!r}")
        return default
    return args[name]


# --- файлы ---------------------------------------------------------------
def file_tools(ws: Workspace) -> list[Tool]:
    def list_files(path: str = ".", **_: Any) -> str:
        root = ws.resolve(path or ".")
        if not root.exists():
            raise ToolError(f"Папки {ws.rel(root)!r} нет")
        if root.is_file():
            return f"{ws.rel(root)} ({root.stat().st_size} байт)"
        entries = []
        for p in sorted(root.iterdir())[:500]:
            mark = "/" if p.is_dir() else ""
            size = "" if p.is_dir() else f" ({p.stat().st_size} б)"
            entries.append(f"{ws.rel(p)}{mark}{size}")
        return "\n".join(entries) or "(пусто)"

    def read_file(path: str = "", **_: Any) -> str:
        p = ws.resolve(_arg({"path": path}, "path"))
        if not p.exists():
            raise ToolError(f"Файла {ws.rel(p)!r} нет. Проверьте list_files.")
        if p.is_dir():
            raise ToolError(f"{ws.rel(p)!r} — папка, не файл")
        try:
            data = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Не прочитать {ws.rel(p)!r}: {exc}") from exc
        if len(data) > READ_LIMIT:
            return (data[:READ_LIMIT]
                    + f"\n\n[...обрезано, всего {len(data)} символов]")
        return data

    def write_file(path: str = "", content: str = "", **_: Any) -> str:
        p = ws.resolve(_arg({"path": path}, "path"))
        p.parent.mkdir(parents=True, exist_ok=True)
        text = "" if content is None else str(content)
        try:
            p.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не записать {ws.rel(p)!r}: {exc}") from exc
        return f"Записано {ws.rel(p)} ({len(text)} символов)"

    return [
        Tool("list_files", "Показать содержимое папки в рабочем каталоге",
             {"path": "путь относительно рабочей папки, по умолчанию ."},
             list_files,
             example='{"tool": "list_files", "args": {"path": "."}}'),
        Tool("read_file", "Прочитать текстовый файл из рабочей папки",
             {"path": "путь к файлу"}, read_file,
             example='{"tool": "read_file", "args": {"path": "notes.md"}}'),
        Tool("write_file", "Создать или перезаписать файл в рабочей папке",
             {"path": "путь к файлу", "content": "содержимое"}, write_file,
             example='{"tool": "write_file", "args": {"path": "out.md", '
                     '"content": "# Отчёт"}}'),
    ]


# --- HTTP ----------------------------------------------------------------
def http_tool(allow: list[str], timeout: int = 30) -> Tool:
    """Доступ к внешним API — строго по белому списку хостов.

    Белый список, а не чёрный: платформа исполняет текст, сгенерированный
    моделью, и «запретить лишнее» здесь принципиально ненадёжно. Пустой
    список = инструмент вообще не создаётся (см. registry.py).
    """
    allowed = {h.strip().lower() for h in allow if h.strip()}

    def http_request(url: str = "", method: str = "GET", body: str = "",
                     headers: Any = None, **_: Any) -> str:
        url = str(_arg({"url": url}, "url")).strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"Схема {parsed.scheme!r} не поддерживается, "
                            "нужен http или https")
        host = (parsed.hostname or "").lower()
        if host not in allowed:
            raise ToolError(
                f"Хост {host!r} не разрешён средой. Разрешены: "
                f"{', '.join(sorted(allowed))}. Это гранты платформы, изменить "
                "их из задачи нельзя.")
        method = str(method or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise ToolError(f"Метод {method!r} не поддерживается")
        hdrs: dict[str, str] = {"User-Agent": "AWOS/1.0"}
        if isinstance(headers, dict):
            hdrs.update({str(k): str(v) for k, v in headers.items()})
        data = str(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read(READ_LIMIT).decode("utf-8", "replace")
                return f"HTTP {resp.status}\n{payload}"
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            return f"HTTP {exc.code}\n{detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolError(f"Запрос к {host} не удался: {exc}") from exc

    return Tool("http_request",
                "Запрос к внешнему HTTP API (только разрешённые средой хосты: "
                + ", ".join(sorted(allowed)) + ")",
                {"url": "полный URL", "method": "GET/POST/...",
                 "body": "тело запроса, если нужно",
                 "headers": "объект с заголовками, если нужно"},
                http_request,
                example='{"tool": "http_request", "args": {"url": '
                        '"https://api.example.com/v1/items"}}')


# --- SQL (только чтение) --------------------------------------------------
_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|pragma|vacuum)\b",
    re.IGNORECASE)


def sql_tool(databases: dict[str, str], row_limit: int = 200) -> Tool:
    """SELECT по зарегистрированным SQLite-базам.

    Только чтение — и проверка двойная: запрос обязан начинаться с SELECT
    или WITH, и в нём не должно быть модифицирующих ключевых слов. Плюс
    соединение открывается в режиме file:...?mode=ro, что не даёт записать
    даже при обходе текстовой проверки. Одной защиты здесь мало: текст
    приходит от модели, а разбирать SQL целиком среда не обязана.
    """
    def sql_query(database: str = "", query: str = "", **_: Any) -> str:
        alias = str(_arg({"database": database}, "database")).strip()
        if alias not in databases:
            raise ToolError(
                f"База {alias!r} не зарегистрирована. Доступны: "
                f"{', '.join(sorted(databases)) or '— ни одной'}")
        sql = str(_arg({"query": query}, "query")).strip().rstrip(";")
        head = sql.lstrip("(").split(None, 1)[0].lower() if sql else ""
        if head not in ("select", "with"):
            raise ToolError("Разрешены только SELECT/WITH — среда даёт доступ "
                            "к базам только на чтение")
        if _WRITE_SQL.search(sql):
            raise ToolError("В запросе есть модифицирующее ключевое слово — "
                            "доступ только на чтение")
        path = Path(databases[alias]).expanduser()
        if not path.exists():
            raise ToolError(f"Файл базы {alias!r} не найден: {path}")
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchmany(row_limit)
            conn.close()
        except sqlite3.Error as exc:
            raise ToolError(f"Ошибка SQL: {exc}") from exc
        if not rows:
            return "(пусто)"
        out = [" | ".join(rows[0].keys())]
        out += [" | ".join("" if v is None else str(v) for v in tuple(r))
                for r in rows]
        if len(rows) == row_limit:
            out.append(f"[...показаны первые {row_limit} строк]")
        return "\n".join(out)

    return Tool("sql_query",
                "SELECT к зарегистрированной базе (только чтение). Базы: "
                + (", ".join(sorted(databases)) or "—"),
                {"database": "алиас базы", "query": "SQL-запрос SELECT"},
                sql_query,
                example='{"tool": "sql_query", "args": {"database": "crm", '
                        '"query": "SELECT id, name FROM clients LIMIT 5"}}')


# --- shell ----------------------------------------------------------------
def shell_tool(ws: Workspace, timeout: int = 60) -> Tool:
    """Команда в рабочей папке. Единственный инструмент, помеченный
    dangerous=True: при включённом HITL среда спросит человека ПЕРЕД
    выполнением."""
    def shell(command: str = "", **_: Any) -> str:
        cmd = str(_arg({"command": command}, "command")).strip()
        if not cmd:
            raise ToolError("Пустая команда")
        try:
            proc = subprocess.run(cmd, shell=True, cwd=str(ws.root),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"Команда не уложилась в {timeout} с и была прервана")
        except OSError as exc:
            raise ToolError(f"Не запустить команду: {exc}") from exc
        out = (proc.stdout or "")[:READ_LIMIT]
        err = (proc.stderr or "")[:4000]
        parts = [f"код возврата: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)

    return Tool("shell", "Выполнить команду оболочки в рабочей папке",
                {"command": "команда"}, shell, dangerous=True,
                example='{"tool": "shell", "args": {"command": "ls -la"}}')


# --- доска контекста ------------------------------------------------------
def context_tools(read_ctx: Callable[[str], Any],
                  write_ctx: Callable[[str, Any], None],
                  keys: Callable[[], list[str]]) -> list[Tool]:
    """Чтение/запись общей доски прямо из хода агента.

    Обычно данные между шагами передаёт САМА среда (см. поля reads/writes
    в определении workflow) — это надёжнее и видно в определении. Но
    агенту иногда нужно оставить побочный факт, которого нет в контракте
    шага: например, «нашёл дубль в исходных данных». Для этого и нужны
    эти два инструмента, а не для замены контракта.
    """
    def ctx_read(key: str = "", **_: Any) -> str:
        k = str(_arg({"key": key}, "key")).strip()
        value = read_ctx(k)
        if value is None:
            known = ", ".join(keys()) or "—"
            raise ToolError(f"Ключа {k!r} на доске нет. Есть: {known}")
        return value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, indent=2)

    def ctx_write(key: str = "", value: Any = "", **_: Any) -> str:
        k = str(_arg({"key": key}, "key")).strip()
        write_ctx(k, value)
        return f"Записано на доску: {k}"

    return [
        Tool("context_read", "Прочитать значение с общей доски прогона",
             {"key": "ключ"}, ctx_read,
             example='{"tool": "context_read", "args": {"key": "research_notes"}}'),
        Tool("context_write", "Записать значение на общую доску прогона",
             {"key": "ключ", "value": "значение"}, ctx_write,
             example='{"tool": "context_write", "args": {"key": "risks", '
                     '"value": "срок сдвигается на неделю"}}'),
    ]


def now_tool() -> Tool:
    def current_time(**_: Any) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime())

    return Tool("current_time", "Текущие дата и время сервера", {}, current_time,
                example='{"tool": "current_time", "args": {}}')


def shlex_safe(cmd: str) -> list[str]:
    """Разбор команды для случаев, когда shell=True не нужен."""
    try:
        return shlex.split(cmd)
    except ValueError as exc:
        raise ToolError(f"Не разобрать команду: {exc}") from exc
