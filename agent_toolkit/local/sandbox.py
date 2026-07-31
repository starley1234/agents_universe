"""Инструменты песочницы: выполнение shell-команд и сниппетов Python.

ОПАСНЫЕ ДЕЙСТВИЯ: выполнение команд и произвольного кода помечено dangerous=True.
Регулируются политиками безопасности (allow_dangerous=True).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


def build_sandbox_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты песочницы для выполнения Shell и Python команд."""

    def run_command(command: str, timeout: int = 10, work_dir: str = "") -> str:
        if not command.strip():
            raise ToolError("Команда shell не может быть пустой")

        cwd = ws.resolve(work_dir) if work_dir else ws.root
        if not cwd.exists() or not cwd.is_dir():
            raise ToolError(f"Рабочая директория {work_dir!r} не найдена")

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            status = f"Код завершения: {res.returncode}"
            return f"### Результат выполнения `{command}`:\n{status}\n```\n{out}\n```"
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"Превышено время выполнения команды ({timeout} с): {command!r}"
            ) from exc
        except OSError as exc:
            raise ToolError(f"Ошибка запуска команды {command!r}: {exc}") from exc

    def exec_snippet(code: str, timeout: int = 5) -> str:
        if not code.strip():
            raise ToolError("Код Python для выполнения не может быть пустым")

        tmp_py = ws.resolve(".tmp_snippet.py")
        tmp_py.write_text(code, encoding="utf-8")
        try:
            res = subprocess.run(
                ["python3", str(tmp_py)],
                cwd=str(ws.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            if res.returncode == 0:
                return f"### Выполнение Python-сниппета прошло успешно:\n```\n{out}\n```"
            return f"### ⚠ Ошибка выполнения Python (код {res.returncode}):\n```\n{out}\n```"
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"Превышено время выполнения Python ({timeout} с)"
            ) from exc
        except OSError as exc:
            raise ToolError(f"Ошибка запуска Python: {exc}") from exc
        finally:
            if tmp_py.exists():
                tmp_py.unlink()

    return [
        Tool(
            name="shell.run_command",
            description="Выполнить команду командной строки Shell/Bash в песочнице. Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Команда для выполнения (например, 'ls -la')",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Таймаут в секундах (по умолчанию 10)",
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "Рабочая директория (относительно корня Workspace)",
                    },
                },
                "required": ["command"],
            },
            fn=run_command,
            skills=["shell", "exec", "system", "local", "sandbox", "bash"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": True,
                "resource_type": "shell_command",
                "speed": "fast",
                "tags": ["shell", "cmd", "bash", "exec", "command", "sandbox"],
            },
            example='shell.run_command(command="echo Hello")',
        ),
        Tool(
            name="python.exec_snippet",
            description="Выполнить фрагмент Python-кода в изолированном субпроцессе. Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python-код для запуска",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Таймаут выполнения в секундах (по умолчанию 5)",
                    },
                },
                "required": ["code"],
            },
            fn=exec_snippet,
            skills=["python", "exec", "code", "local", "sandbox", "dev"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": True,
                "resource_type": "python_snippet",
                "speed": "fast",
                "tags": ["python", "exec", "code", "snippet", "sandbox"],
            },
            example='python.exec_snippet(code="print(2 + 2)")',
        ),
    ]
