"""Инструменты для разработчиков: Git (status/diff/log), проверка синтаксиса и запуск тестов."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

COMMAND_TIMEOUT = 15


def _run_git(ws: Workspace, args: list[str], path: str = ".") -> str:
    p = ws.resolve(path)
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=str(p if p.is_dir() else p.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        if res.returncode != 0 and "not a git repository" in res.stderr.lower():
            return f"(Директория {ws.relative(p)} не является Git-репозиторием)"
        if res.returncode != 0:
            raise ToolError(f"Ошибка git {' '.join(args)}: {res.stderr.strip()}")
        return res.stdout.strip() or "(чисто / нет изменений)"
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ToolError(f"Ошибка выполнения git: {exc}") from exc


def build_code_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты контроля версий (Git) и анализа кода (linter/test)."""

    def git_status(path: str = ".") -> str:
        out = _run_git(ws, ["status", "-s"], path)
        return f"### Git Status ({path}):\n{out}"

    def git_diff(path: str = ".", staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--staged")
        out = _run_git(ws, args, path)
        return f"### Git Diff ({path}):\n```diff\n{out}\n```"

    def git_log(path: str = ".", limit: int = 5) -> str:
        out = _run_git(ws, ["log", f"-n{limit}", "--oneline"], path)
        return f"### Последние коммиты ({path}):\n{out}"

    def run_linter(path: str = ".") -> str:
        p = ws.resolve(path)
        try:
            res = subprocess.run(
                [
                    "python3",
                    "-m",
                    "compileall",
                    "-q",
                    str(p),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
            if res.returncode == 0:
                return f"Синтаксическая проверка (compileall) для {ws.relative(p)} прошла успешно (✓ OK)"
            return f"⚠ Ошибки синтаксиса в {ws.relative(p)}:\n{res.stderr or res.stdout}"
        except Exception as exc:
            raise ToolError(f"Ошибка запуска проверки кода: {exc}") from exc

    def run_tests(path: str = ".", test_file: str = "") -> str:
        p = ws.resolve(path)
        target = ws.resolve(test_file) if test_file else p
        try:
            res = subprocess.run(
                ["python3", str(target)] if test_file else ["python3", "-m", "unittest", "discover", "-s", str(p)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=25,
                check=False,
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            status = "✓ ТЕСТЫ ПРОЙДЕНЫ" if res.returncode == 0 else f"✗ ОШИБКА ТЕСТОВ (код {res.returncode})"
            return f"### {status} ({ws.relative(target)}):\n```\n{out}\n```"
        except Exception as exc:
            raise ToolError(f"Ошибка выполнения тестов: {exc}") from exc

    return [
        Tool(
            name="git.status",
            description="Посмотреть статус изменений Git-репозитория.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к репозиторию"}
                },
            },
            fn=git_status,
            skills=["git", "code", "dev", "local", "vcs"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "git_repo",
                "speed": "fast",
                "tags": ["git", "status", "vcs", "code", "repo"],
            },
            example='git.status(path=".")',
        ),
        Tool(
            name="git.diff",
            description="Посмотреть diff изменений в коде.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к репозиторию"},
                    "staged": {
                        "type": "boolean",
                        "description": "Показать только staged изменения (--staged)",
                    },
                },
            },
            fn=git_diff,
            skills=["git", "code", "dev", "local", "vcs"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "git_repo",
                "speed": "fast",
                "tags": ["git", "diff", "vcs", "code", "patch"],
            },
            example='git.diff(path=".", staged=False)',
        ),
        Tool(
            name="git.log",
            description="Посмотреть историю последних коммитов репозитория.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к репозиторию"},
                    "limit": {
                        "type": "integer",
                        "description": "Количество коммитов (по умолчанию 5)",
                    },
                },
            },
            fn=git_log,
            skills=["git", "code", "dev", "local", "vcs"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "git_repo",
                "speed": "fast",
                "tags": ["git", "log", "commit", "history", "vcs"],
            },
            example='git.log(limit=5)',
        ),
        Tool(
            name="code.run_linter",
            description="Провести проверку синтаксиса Python файлов в директории (compileall).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Проверяемая директория или файл"}
                },
            },
            fn=run_linter,
            skills=["code", "dev", "linter", "local", "testing"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "code_check",
                "speed": "fast",
                "tags": ["code", "linter", "syntax", "check", "python"],
            },
            example='code.run_linter(path="src/")',
        ),
        Tool(
            name="code.run_tests",
            description="Запустить автоматические тесты в рабочей папке или конкретный файл.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Папка с тестами"},
                    "test_file": {
                        "type": "string",
                        "description": "Опционально: путь к конкретному тесту (test_*.py)",
                    },
                },
            },
            fn=run_tests,
            skills=["code", "dev", "testing", "local", "qa"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "test_runner",
                "speed": "medium",
                "tags": ["code", "test", "pytest", "unittest", "qa"],
            },
            example='code.run_tests(test_file="tests/test_math.py")',
        ),
    ]
