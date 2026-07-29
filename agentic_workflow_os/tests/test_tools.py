"""Тесты инструментов: изоляция рабочей папки, гранты, протокол вызова.

Здесь проверяется граница безопасности среды, поэтому негативных
сценариев больше, чем позитивных: выход за пределы рабочей папки,
запрещённый хост, попытка записи через «читающий» SQL, расширение прав
профилем. Инструменты работают с настоящей файловой системой и
настоящим SQLite во временной папке — подменять их моками бессмысленно,
проверяется как раз поведение на реальном вводе-выводе.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, check_raises, section, summary          # noqa: E402
from awos.config import Config                                     # noqa: E402
from awos.tools.base import ToolError, ToolRegistry, Workspace     # noqa: E402
from awos.tools.builtin import (context_tools, file_tools, http_tool,  # noqa: E402
                                shell_tool, sql_tool)
from awos.tools.protocol import (ProtocolError, extract_call,      # noqa: E402
                                 is_final, protocol_prompt, strip_calls)
from awos.tools.registry import build_registry, granted_summary    # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="awos_tools_"))

    section("Рабочая папка: изоляция")
    ws = Workspace(tmp / "ws")
    check("корень создан", ws.root.exists())
    check("простой путь разрешается", ws.resolve("a.txt").name == "a.txt")
    check("вложенный путь разрешается",
          ws.resolve("sub/b.txt").parent.name == "sub")
    check_raises("выход через .. запрещён", ToolError, ws.resolve, "../secret")
    check_raises("глубокий выход запрещён", ToolError, ws.resolve, "a/../../x")
    check_raises("абсолютный путь запрещён", ToolError, ws.resolve, "/etc/passwd")
    check_raises("пустой путь запрещён", ToolError, ws.resolve, "  ")
    check("markdown-обёртка снимается",
          ws.clean("[main.py](http://main.py)") == "main.py",
          "модели регулярно присылают путь ссылкой")
    check("кавычки снимаются", ws.clean('"a.txt"') == "a.txt")

    section("Файловые инструменты")
    tools = {t.name: t for t in file_tools(ws)}
    check("три файловых инструмента", set(tools) ==
          {"list_files", "read_file", "write_file"})
    out = tools["write_file"].fn(path="notes.md", content="привет")
    check("запись файла", "notes.md" in out and (ws.root / "notes.md").exists())
    check("чтение файла", tools["read_file"].fn(path="notes.md") == "привет")
    check("список файлов", "notes.md" in tools["list_files"].fn(path="."))
    check_raises("чтение несуществующего", ToolError, tools["read_file"].fn,
                 path="нет.md")
    check_raises("чтение вне папки", ToolError, tools["read_file"].fn,
                 path="../../etc/passwd")
    check_raises("аргумент path обязателен", ToolError, tools["read_file"].fn,
                 path="")
    tools["write_file"].fn(path="deep/dir/file.txt", content="x")
    check("вложенные папки создаются", (ws.root / "deep/dir/file.txt").exists())
    big = "я" * 60_000
    tools["write_file"].fn(path="big.txt", content=big)
    read = tools["read_file"].fn(path="big.txt")
    check("большой файл обрезается", len(read) < len(big) and "обрезано" in read)

    section("HTTP: белый список хостов")
    http = http_tool(["api.example.com"], timeout=2)
    check_raises("запрещённый хост", ToolError, http.fn,
                 url="https://evil.example.org/x")
    check_raises("схема file:// запрещена", ToolError, http.fn,
                 url="file:///etc/passwd")
    check_raises("неподдерживаемый метод", ToolError, http.fn,
                 url="https://api.example.com/", method="TRACE")
    check("разрешённый хост виден в описании",
          "api.example.com" in http.description)

    section("SQL: только чтение")
    dbfile = tmp / "data.db"
    conn = sqlite3.connect(dbfile)
    conn.execute("CREATE TABLE clients(id INTEGER, name TEXT)")
    conn.execute("INSERT INTO clients VALUES (1, 'Иванов'), (2, 'Петров')")
    conn.commit()
    conn.close()
    sql = sql_tool({"crm": str(dbfile)})
    result = sql.fn(database="crm", query="SELECT id, name FROM clients ORDER BY id")
    check("SELECT работает", "Иванов" in result and "Петров" in result)
    check("заголовки колонок в выводе", result.splitlines()[0] == "id | name")
    check("WITH разрешён",
          "1" in sql.fn(database="crm",
                        query="WITH t AS (SELECT 1 AS x) SELECT x FROM t"))
    check_raises("INSERT запрещён", ToolError, sql.fn, database="crm",
                 query="INSERT INTO clients VALUES (3, 'Сидоров')")
    check_raises("UPDATE запрещён", ToolError, sql.fn, database="crm",
                 query="UPDATE clients SET name='x'")
    check_raises("DROP запрещён", ToolError, sql.fn, database="crm",
                 query="DROP TABLE clients")
    check_raises("SELECT с DELETE внутри запрещён", ToolError, sql.fn,
                 database="crm",
                 query="SELECT * FROM clients; DELETE FROM clients")
    check_raises("PRAGMA запрещена", ToolError, sql.fn, database="crm",
                 query="PRAGMA table_info(clients)")
    check_raises("незарегистрированная база", ToolError, sql.fn,
                 database="чужая", query="SELECT 1")
    conn = sqlite3.connect(dbfile)
    rows = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    conn.close()
    check("данные не изменились после попыток записи", rows == 2)

    section("Shell помечен как опасный")
    sh = shell_tool(ws, timeout=5)
    check("флаг dangerous выставлен", sh.dangerous is True,
          "иначе HITL не остановится перед вызовом")
    out = sh.fn(command="echo привет")
    check("команда выполняется", "привет" in out and "код возврата: 0" in out)
    out = sh.fn(command="exit 3")
    check("ненулевой код возврата виден", "код возврата: 3" in out)
    check_raises("пустая команда", ToolError, sh.fn, command="")

    section("Инструменты доски контекста")
    board: dict[str, object] = {"notes": "текст"}
    ctools = {t.name: t for t in context_tools(
        lambda k: board.get(k), lambda k, v: board.__setitem__(k, v),
        lambda: sorted(board))}
    check("чтение с доски", ctools["context_read"].fn(key="notes") == "текст")
    ctools["context_write"].fn(key="new", value="значение")
    check("запись на доску", board["new"] == "значение")
    check_raises("чтение отсутствующего ключа", ToolError,
                 ctools["context_read"].fn, key="нет")

    section("Реестр: гранты среды и сужение прав")
    cfg = Config(workspace=str(ws.root))
    reg = build_registry(cfg, workspace=ws)
    check("без грантов нет http", reg.get("http_request") is None)
    check("без грантов нет sql", reg.get("sql_query") is None)
    check("без грантов нет shell", reg.get("shell") is None)
    check("файловые инструменты есть всегда", reg.get("read_file") is not None)

    cfg2 = Config(workspace=str(ws.root), http_allow=["api.example.com"],
                  allow_shell=True, sql_databases={"crm": str(dbfile)})
    reg2 = build_registry(cfg2, workspace=ws)
    check("грант http выдаёт инструмент", reg2.get("http_request") is not None)
    check("грант shell выдаёт инструмент", reg2.get("shell") is not None)
    check("грант sql выдаёт инструмент", reg2.get("sql_query") is not None)

    narrow = reg2.subset(["read_file"])
    check("сужение оставляет одно", narrow.names() == ["read_file"])
    wider = narrow.subset(["read_file", "shell", "выдуманный"])
    check("сужение НЕ возвращает отобранное", wider.names() == ["read_file"],
          "профиль не должен уметь вернуть себе отобранный инструмент")
    check("пустое сужение = без изменений",
          reg2.subset([]).names() == reg2.names())

    summary_grants = granted_summary(cfg2)
    check("сводка грантов: хосты", summary_grants["http"] == ["api.example.com"])
    check("сводка грантов: shell", summary_grants["shell"] is True)

    section("Протокол вызова инструмента")
    known = {"read_file", "shell"}
    call = extract_call('```tool\n{"tool": "read_file", "args": {"path": "a.md"}}\n```',
                        known)
    check("канонический блок разобран",
          call and call.tool == "read_file" and call.args == {"path": "a.md"})
    check("метка json тоже принимается",
          extract_call('```json\n{"tool": "read_file", "args": {}}\n```', known)
          is not None)
    check("блок без метки принимается",
          extract_call('```\n{"tool": "read_file", "args": {}}\n```', known)
          is not None)
    check("голый JSON принимается",
          extract_call('Сейчас прочитаю: {"tool": "read_file", "args": {"path": "x"}}',
                       known) is not None)
    check("текст вокруг блока не мешает",
          extract_call('Мысли\n```tool\n{"tool": "shell", "args": {"command": "ls"}}\n```\nвсё',
                       known).tool == "shell")
    check("вложенные объекты в args",
          extract_call('```tool\n{"tool": "read_file", "args": {"a": {"b": [1,2]}}}\n```',
                       known).args == {"a": {"b": [1, 2]}})
    check("args по умолчанию пустые",
          extract_call('```tool\n{"tool": "read_file"}\n```', known).args == {})
    check("обычный текст — не вызов", extract_call("Готово, вот ответ.", known) is None)
    check("пустая строка — не вызов", extract_call("", known) is None)
    check("JSON без ключа tool — не вызов",
          extract_call('```json\n{"result": 5}\n```', known) is None)

    check_raises("неизвестный инструмент отвергается", ProtocolError,
                 extract_call,
                 '```tool\n{"tool": "выдуманный", "args": {}}\n```', known)
    check_raises("args не объект отвергается", ProtocolError, extract_call,
                 '```tool\n{"tool": "read_file", "args": "строка"}\n```', known)
    check_raises("пустое имя инструмента отвергается", ProtocolError,
                 extract_call, '```tool\n{"tool": "", "args": {}}\n```', known)
    check_raises("два вызова за ход отвергаются", ProtocolError, extract_call,
                 '```tool\n{"tool": "read_file", "args": {}}\n```\n'
                 '```tool\n{"tool": "shell", "args": {}}\n```', known)

    check("strip_calls убирает блок",
          strip_calls('Читаю файл.\n```tool\n{"tool": "read_file"}\n```') ==
          "Читаю файл.")
    check("is_final видит маркер", is_final("<final>Готово") is True)
    check("is_final не срабатывает зря", is_final("обычный текст") is False)

    prompt = protocol_prompt(reg2.prompt())
    check("в промпте есть формат вызова", "```tool" in prompt)
    check("в промпте перечислены инструменты", "read_file" in prompt)
    check("опасный инструмент помечен в промпте",
          "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ" in prompt)
    check("без инструментов промпт это объясняет",
          "нет" in protocol_prompt("").lower())

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return summary("Инструменты")


if __name__ == "__main__":
    raise SystemExit(main())
