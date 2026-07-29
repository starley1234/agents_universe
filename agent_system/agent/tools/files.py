"""Файловые инструменты: чтение, запись, точечная правка, список, поиск."""
from __future__ import annotations

from .base import Tool, ToolError, Workspace

MAX_READ = 200_000        # символов за одно чтение
MAX_LIST = 500            # файлов в листинге


def build(ws: Workspace) -> list[Tool]:
    # ---------------------------------------------------------------- read
    def read_file(path: str, start: int = 1, end: int = 0) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это папка, не файл")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Не прочитать {path!r}: {exc}") from exc

        lines = text.splitlines()
        if start > 1 or end:
            lo = max(1, start) - 1
            hi = end if end and end > lo else len(lines)
            lines = lines[lo:hi]
            body = "\n".join(f"{lo + i + 1:>6}\t{ln}" for i, ln in enumerate(lines))
        else:
            body = text
        if len(body) > MAX_READ:
            body = body[:MAX_READ] + f"\n... обрезано на {MAX_READ} символах"
        return body or "(файл пуст)"

    # --------------------------------------------------------------- write
    def write_file(path: str, content: str) -> str:
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не записать {path!r}: {exc}") from exc
        return f"Записано {ws.relative(p)} ({len(content)} символов, " \
               f"{len(content.splitlines())} строк)"

    # ---------------------------------------------------------------- edit
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """Замена по точному совпадению.

        Требуем УНИКАЛЬНОСТИ фрагмента: если он встречается несколько раз,
        отказываемся и просим расширить контекст. Молчаливая замена не того
        вхождения — источник трудноуловимых поломок.
        """
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old_text)
        if n == 0:
            raise ToolError(
                f"Фрагмент не найден в {path!r}. Сначала прочитайте файл "
                "и скопируйте текст точно, включая отступы."
            )
        if n > 1:
            raise ToolError(
                f"Фрагмент встречается {n} раз в {path!r}. Добавьте "
                "окружающие строки, чтобы совпадение стало единственным."
            )
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Изменён {ws.relative(p)}: заменён 1 фрагмент"

    # ---------------------------------------------------------------- list
    def list_files(path: str = ".", pattern: str = "*") -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Путь {path!r} не найден")
        if p.is_file():
            return ws.relative(p)
        hits = sorted(q for q in p.rglob(pattern)
                      if not any(part.startswith(".") for part in q.parts))
        if not hits:
            return f"По шаблону {pattern!r} в {path!r} ничего нет"
        out = []
        for q in hits[:MAX_LIST]:
            mark = "/" if q.is_dir() else ""
            size = "" if q.is_dir() else f"  {q.stat().st_size} Б"
            out.append(f"{ws.relative(q)}{mark}{size}")
        if len(hits) > MAX_LIST:
            out.append(f"... ещё {len(hits) - MAX_LIST}")
        return "\n".join(out)

    # -------------------------------------------------------------- search
    def search_text(query: str, path: str = ".", max_hits: int = 100) -> str:
        p = ws.resolve(path)
        roots = [p] if p.is_file() else sorted(
            q for q in p.rglob("*")
            if q.is_file() and not any(x.startswith(".") for x in q.parts)
        )
        hits: list[str] = []
        for q in roots:
            try:
                for i, line in enumerate(
                    q.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if query in line:
                        hits.append(f"{ws.relative(q)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= max_hits:
                            hits.append("... достигнут предел вывода")
                            return "\n".join(hits)
            except OSError:
                continue
        return "\n".join(hits) if hits else f"Совпадений с {query!r} нет"

    return [
        Tool("read_file",
             "Прочитать файл. Можно указать диапазон строк start/end — "
             "тогда вывод идёт с номерами строк.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string", "description": "Путь внутри workspace"},
                  "start": {"type": "integer", "description": "Первая строка, с 1"},
                  "end": {"type": "integer", "description": "Последняя строка"}},
              "required": ["path"]},
             read_file),
        Tool("write_file",
             "Создать файл или полностью перезаписать существующий.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "content": {"type": "string", "description": "Полное содержимое"}},
              "required": ["path", "content"]},
             write_file),
        Tool("edit_file",
             "Точечно заменить фрагмент в файле. Фрагмент должен встречаться "
             "ровно один раз, иначе будет отказ.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "old_text": {"type": "string", "description": "Что заменить"},
                  "new_text": {"type": "string", "description": "На что заменить"}},
              "required": ["path", "old_text", "new_text"]},
             edit_file),
        Tool("list_files",
             "Список файлов рекурсивно, с фильтром по шаблону (например '*.py').",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "pattern": {"type": "string"}},
              "required": []},
             list_files),
        Tool("search_text",
             "Найти подстроку во всех файлах: путь, номер строки и сама строка.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "path": {"type": "string"},
                  "max_hits": {"type": "integer"}},
              "required": ["query"]},
             search_text),
    ]
