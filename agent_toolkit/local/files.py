"""Локальные файловые инструменты: чтение, запись, редактирование, поиск."""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

MAX_READ = 200_000
MAX_LIST = 500


def build_file_tools(ws: Workspace) -> list[Tool]:
    """Собрать набор инструментов для работы с файлами внутри рабочей области."""

    def read_file(path: str, start: int = 1, end: int = 0) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это директория, не файл")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать {path!r}: {exc}") from exc

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

    def write_file(path: str, content: str) -> str:
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось записать в {path!r}: {exc}") from exc
        return (
            f"Записано {ws.relative(p)} "
            f"({len(content)} символов, {len(content.splitlines())} строк)"
        )

    def edit_file(path: str, old_text: str, new_text: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать {path!r}: {exc}") from exc

        count = content.count(old_text)
        if count == 0:
            raise ToolError(f"Фрагмент не найден в файле {path!r}")
        if count > 1:
            raise ToolError(
                f"Фрагмент встречается {count} раз(а), требуется уникальный участок кода"
            )

        updated = content.replace(old_text, new_text, 1)
        try:
            p.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось сохранить изменения в {path!r}: {exc}") from exc
        return f"Файл {ws.relative(p)} успешно отредактирован"

    def list_dir(path: str = ".") -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Путь {path!r} не найден")
        if not p.is_dir():
            raise ToolError(f"{path!r} — это файл, не директория")

        items: list[str] = []
        try:
            for child in sorted(p.iterdir()):
                rel = ws.relative(child)
                if child.is_dir():
                    items.append(f"[DIR]  {rel}/")
                else:
                    size = child.stat().st_size
                    items.append(f"[FILE] {rel} ({size} B)")
                if len(items) >= MAX_LIST:
                    items.append(f"... (показаны первые {MAX_LIST} элементов)")
                    break
        except OSError as exc:
            raise ToolError(f"Ошибка чтения директории {path!r}: {exc}") from exc

        return "\n".join(items) if items else "(директория пуста)"

    def find_files(pattern: str, path: str = ".") -> str:
        p = ws.resolve(path)
        if not p.exists() or not p.is_dir():
            raise ToolError(f"Директория {path!r} не найдена")

        matches: list[str] = []
        try:
            for child in p.rglob("*"):
                if fnmatch.fnmatch(child.name, pattern):
                    matches.append(ws.relative(child))
                if len(matches) >= MAX_LIST:
                    matches.append(f"... (ограничение в {MAX_LIST} результатов)")
                    break
        except OSError as exc:
            raise ToolError(f"Ошибка поиска в {path!r}: {exc}") from exc

        return "\n".join(sorted(matches)) if matches else "(ничего не найдено)"

    def file_info(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        st = p.stat()
        if p.is_dir():
            return f"Директория: {ws.relative(p)}\nРазмер: {st.st_size} B"
        sha = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(65536):
                sha.update(chunk)
        lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        return (
            f"Файл: {ws.relative(p)}\n"
            f"Размер: {st.st_size} B\n"
            f"Строк: {lines}\n"
            f"SHA256: {sha.hexdigest()}"
        )

    def remove_file(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это директория, используйте удаление папок осторожно")
        try:
            p.unlink()
        except OSError as exc:
            raise ToolError(f"Не удалось удалить файл {path!r}: {exc}") from exc
        return f"Файл {ws.relative(p)} удалён"

    return [
        Tool(
            name="files.read_file",
            description="Прочитать содержимое текстового файла с номерами строк.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "start": {"type": "integer", "description": "Номер первой строки"},
                    "end": {"type": "integer", "description": "Номер последней строки"},
                },
                "required": ["path"],
            },
            fn=read_file,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "read", "text", "filesystem"],
            },
            example='files.read_file(path="notes.txt")',
        ),
        Tool(
            name="files.write_file",
            description="Создать или перезаписать текстовый файл.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "content": {"type": "string", "description": "Полное содержимое файла"},
                },
                "required": ["path", "content"],
            },
            fn=write_file,
            skills=["files", "local", "filesystem", "write"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "write", "save", "text"],
            },
            example='files.write_file(path="report.md", content="# Заголовок")',
        ),
        Tool(
            name="files.edit_file",
            description="Точечная замена фрагмента текста в файле (по точному совпадению).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "old_text": {"type": "string", "description": "Заменяемый текст"},
                    "new_text": {"type": "string", "description": "Новый текст"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            fn=edit_file,
            skills=["files", "local", "filesystem", "edit"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "edit", "patch", "text"],
            },
            example='files.edit_file(path="main.py", old_text="x = 1", new_text="x = 2")',
        ),
        Tool(
            name="files.list_dir",
            description="Посмотреть содержимое директории с размерами файлов.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к директории"}
                },
            },
            fn=list_dir,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "directory",
                "speed": "fast",
                "tags": ["file", "list", "directory", "folder"],
            },
            example='files.list_dir(path="data")',
        ),
        Tool(
            name="files.find_files",
            description="Найти файлы по шаблону/маске (например, '*.md' или 'test_*').",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Маска файла (glob pattern)"},
                    "path": {"type": "string", "description": "Стартовая папка"},
                },
                "required": ["pattern"],
            },
            fn=find_files,
            skills=["files", "local", "filesystem", "search"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "search", "find", "glob"],
            },
            example='files.find_files(pattern="*.json")',
        ),
        Tool(
            name="files.file_info",
            description="Получить метаданные файла: размер, количество строк и SHA256.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"}
                },
                "required": ["path"],
            },
            fn=file_info,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "stat", "metadata", "hash"],
            },
            example='files.file_info(path="data/records.csv")',
        ),
        Tool(
            name="files.remove_file",
            description="Удалить файл внутри рабочей области.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"}
                },
                "required": ["path"],
            },
            fn=remove_file,
            skills=["files", "local", "filesystem", "delete"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": True,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "delete", "remove"],
            },
            example='files.remove_file(path="tmp.txt")',
        ),
        Tool(
            name="files.create_archive",
            description="Упаковать файлы и папки из workspace в ZIP-архив для скачивания. Поддерживает glob-маски.",
            parameters={
                "type": "object",
                "properties": {
                    "archive_path": {"type": "string", "description": "Имя ZIP-архива (например, 'export.zip')"},
                    "files_json": {
                        "type": "string",
                        "description": 'JSON-массив путей или glob-масок (\'["report.xlsx", "models/*.stl", "docs/"]\')',
                    },
                    "compression": {"type": "integer", "description": "Уровень сжатия 0-9 (по умолчанию 6)"},
                },
                "required": ["archive_path", "files_json"],
            },
            fn=lambda archive_path, files_json, compression=6: _create_archive(ws, archive_path, files_json, compression),
            skills=["files", "local", "filesystem", "archive", "zip", "export"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "archive",
                "speed": "medium",
                "tags": ["file", "archive", "zip", "export", "download"],
            },
            example='files.create_archive(archive_path="export.zip", files_json=\'["report.xlsx", "models/*.stl"]\')',
        ),
        Tool(
            name="files.run_script",
            description="Выполнить PHP/Python/Bash скрипт из workspace. PHP скрипты выполняются через php-cli, Python через python3, Bash через bash. Результат доступен по URL /workspace/{path}.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к скрипту в workspace (например, 'report.php' или 'script.py')"},
                    "args_json": {"type": "string", "description": "JSON-массив аргументов командной строки (по умолчанию '[]')"},
                    "interpreter": {"type": "string", "description": "Интерпретатор: auto, php, python, bash (по умолчанию auto — определяется по расширению)"},
                    "timeout": {"type": "integer", "description": "Таймаут в секундах (по умолчанию 30, макс 120)"},
                },
                "required": ["path"],
            },
            fn=lambda path, args_json="[]", interpreter="auto", timeout=30: _run_script(ws, path, args_json, interpreter, timeout),
            skills=["files", "local", "filesystem", "exec", "php", "python", "bash", "script"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": True,
                "resource_type": "script_exec",
                "speed": "medium",
                "tags": ["file", "exec", "php", "python", "bash", "script", "run"],
            },
            example='files.run_script(path="report.php")',
        ),
        Tool(
            name="files.get_links",
            description="Получить прямые веб-ссылки на файлы в workspace. Файлы доступны для скачивания и просмотра в браузере. PHP-файлы выполняются автоматически.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob-маска для фильтрации файлов (например, '*.php', 'site/*', '*.xlsx'). По умолчанию '*' — все файлы."},
                    "base_url": {"type": "string", "description": "Базовый URL сервера (по умолчанию 'http://localhost:8090')"},
                },
            },
            fn=lambda pattern="*", base_url="http://localhost:8090": _get_links(ws, pattern, base_url),
            skills=["files", "local", "filesystem", "links", "web", "download", "url"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file_links",
                "speed": "fast",
                "tags": ["file", "links", "url", "download", "web", "workspace"],
            },
            example='files.get_links(pattern="*.php")',
        ),
    ]


def _create_archive(ws: "Workspace", archive_path: str, files_json: str, compression: int = 6) -> str:
    """Упаковать файлы в ZIP-архив."""
    import json
    import zipfile
    from pathlib import Path as P

    try:
        patterns = json.loads(files_json) if files_json else []
    except ValueError as exc:
        raise ToolError(f"Некорректный JSON в files_json: {exc}") from exc

    if not isinstance(patterns, list) or not patterns:
        raise ToolError("files_json должен быть непустым JSON-массивом путей или масок")

    p_out = ws.resolve(archive_path)
    if not str(p_out).endswith(".zip"):
        p_out = p_out.with_suffix(".zip")
    p_out.parent.mkdir(parents=True, exist_ok=True)

    collected: list[P] = []
    for pattern in patterns:
        pattern = str(pattern).strip()
        if not pattern:
            continue
        # Если это директория — добавляем всё содержимое
        p = ws.resolve(pattern)
        if p.is_dir():
            collected.extend(f for f in p.rglob("*") if f.is_file())
        elif p.is_file():
            collected.append(p)
        else:
            # Glob-маска
            matches = list(ws.root.glob(pattern))
            for m in matches:
                if m.is_file():
                    collected.append(m)
                elif m.is_dir():
                    collected.extend(f for f in m.rglob("*") if f.is_file())

    if not collected:
        raise ToolError("Не найдено файлов для архивации по указанным путям/маскам")

    # Убираем дубликаты
    seen: set[str] = set()
    unique: list[P] = []
    for f in collected:
        rel = str(f.relative_to(ws.root))
        if rel not in seen:
            seen.add(rel)
            unique.append(f)

    comp = min(max(int(compression or 6), 0), 9)
    with zipfile.ZipFile(p_out, "w", zipfile.ZIP_DEFLATED, compresslevel=comp) as zf:
        for f in unique:
            arcname = str(f.relative_to(ws.root))
            zf.write(f, arcname)

    total_size = p_out.stat().st_size
    files_size = sum(f.stat().st_size for f in unique)

    return (
        f"### Архив создан: {ws.relative(p_out)}\n"
        f"- Файлов в архиве: **{len(unique)}**\n"
        f"- Размер архива: **{total_size:,} байт** ({total_size / 1024:.1f} КБ)\n"
        f"- Исходный размер: {files_size:,} байт (сжатие: {100 - round(total_size * 100 / max(files_size, 1))}%)\n"
        f"- Ссылка для скачивания: `/api/workspace/download?path={ws.relative(p_out)}`\n"
        f"- Содержимое:\n"
        + "\n".join(f"  * `{str(f.relative_to(ws.root))}` ({f.stat().st_size:,}B)" for f in unique[:20])
        + (f"\n  ... и ещё {len(unique) - 20} файлов" if len(unique) > 20 else "")
    )


def _get_links(ws: "Workspace", pattern: str = "*", base_url: str = "http://localhost:8090") -> str:
    """Получить веб-ссылки на файлы workspace."""
    from pathlib import Path as P

    base_url = base_url.rstrip("/")
    root = P(ws.root)

    # Собираем файлы
    files = []
    if pattern and "*" in pattern:
        matches = sorted(root.rglob(pattern.replace("**/", "").replace("**", "*")))
    elif pattern and pattern != "*":
        matches = sorted(root.rglob(f"*{pattern}*"))
    else:
        matches = sorted(root.rglob("*"))

    for f in matches:
        if not f.is_file():
            continue
        rel = str(f.relative_to(root))
        ext = f.suffix.lower()
        size = f.stat().st_size

        # Определяем тип ссылки
        if ext == ".php":
            link_type = "execute"
            url = f"{base_url}/workspace/{rel}"
        elif ext in (".html", ".htm"):
            link_type = "view"
            url = f"{base_url}/workspace/{rel}"
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            link_type = "image"
            url = f"{base_url}/workspace/{rel}"
        elif ext in (".css", ".js", ".json", ".xml", ".csv", ".txt", ".md"):
            link_type = "view"
            url = f"{base_url}/workspace/{rel}"
        else:
            link_type = "download"
            url = f"{base_url}/api/workspace/download?path={rel}"

        files.append({
            "path": rel,
            "url": url,
            "type": link_type,
            "size": size,
            "ext": ext,
        })

    if not files:
        return f"Файлов по маске '{pattern}' не найдено в workspace."

    # Группируем по типу
    by_type: dict[str, list] = {}
    for f in files:
        by_type.setdefault(f["type"], []).append(f)

    lines = [f"### 📎 Ссылки на файлы workspace ({len(files)} файлов):\n"]

    type_labels = {
        "execute": "🔧 PHP (выполняются)",
        "view": "👁 Просмотр",
        "image": "🖼 Изображения",
        "download": "⬇️ Скачивание",
    }

    for type_key in ["execute", "view", "image", "download"]:
        group = by_type.get(type_key, [])
        if not group:
            continue
        lines.append(f"#### {type_labels.get(type_key, type_key)} ({len(group)}):")
        for f in group[:20]:
            size_str = f"{f['size']:,} B" if f['size'] < 1024 else f"{f['size']/1024:.1f} KB"
            lines.append(f"- [{f['path']}]({f['url']}) ({size_str})")
        if len(group) > 20:
            lines.append(f"- ... и ещё {len(group) - 20} файлов")
        lines.append("")

    # Краткая сводка для LLM
    lines.append(f"#### Для LLM:")
    for f in files[:10]:
        lines.append(f"- `{f['path']}` → {f['url']}")
    if len(files) > 10:
        lines.append(f"- (ещё {len(files) - 10} файлов)")

    return "\n".join(lines)


def _run_script(ws: "Workspace", path: str, args_json: str = "[]", interpreter: str = "auto", timeout: int = 30) -> str:
    """Выполнить PHP/Python/Bash скрипт из workspace."""
    import json as _json
    import shutil
    import subprocess
    import os as _os

    p = ws.resolve(path)
    if not p.exists():
        raise ToolError(f"Скрипт {path!r} не найден в workspace")

    try:
        args = _json.loads(args_json) if args_json else []
        if not isinstance(args, list):
            args = []
    except ValueError as exc:
        raise ToolError(f"Некорректный JSON аргументов: {exc}") from exc

    timeout = min(max(int(timeout or 30), 1), 120)

    # Определяем интерпретатор
    if interpreter == "auto":
        ext = p.suffix.lower()
        if ext == ".php":
            interpreter = "php"
        elif ext == ".py":
            interpreter = "python"
        elif ext in (".sh", ".bash"):
            interpreter = "bash"
        else:
            interpreter = "bash"

    bin_map = {"php": "php", "python": "python3", "bash": "bash"}
    exe = shutil.which(bin_map.get(interpreter, interpreter))
    if not exe:
        raise ToolError(
            f"Интерпретатор {interpreter!r} не найден. "
            f"Установите: apt install {interpreter + '-cli' if interpreter == 'php' else interpreter}"
        )

    cmd = [exe, str(p)] + [str(a) for a in args]
    env = dict(_os.environ)
    env["DOCUMENT_ROOT"] = str(ws.root)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(p.parent), env=env,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Скрипт {path!r} превысил таймаут {timeout}с")
    except Exception as exc:
        raise ToolError(f"Ошибка запуска {path!r}: {exc}") from exc

    # Формируем ответ
    status = "✅ Успешно" if result.returncode == 0 else f"❌ Ошибка (код {result.returncode})"
    stdout_text = result.stdout.strip()
    if len(stdout_text) > 3000:
        stdout_text = stdout_text[:3000] + "\n... (обрезано)"
    stderr_text = result.stderr.strip()
    if len(stderr_text) > 1000:
        stderr_text = stderr_text[:1000] + "\n... (обрезано)"

    web_url = f"/workspace/{ws.relative(p)}"
    lines = [
        f"### Выполнение скрипта: {ws.relative(p)}",
        f"- **Интерпретатор:** {interpreter} ({exe})",
        f"- **Статус:** {status}",
        f"- **Веб-доступ:** `{web_url}`",
    ]
    if stdout_text:
        lines.append(f"- **Вывод (stdout):**\n```\n{stdout_text}\n```")
    if stderr_text:
        lines.append(f"- **Ошибки (stderr):**\n```\n{stderr_text}\n```")
    if not stdout_text and not stderr_text:
        lines.append("- (скрипт не вывел данных)")

    return "\n".join(lines)
