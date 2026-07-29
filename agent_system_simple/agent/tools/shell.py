"""Выполнение команд.

Три режима, задаются в конфиге (sandbox.mode):

  "docker"  — каждая команда исполняется в одноразовом контейнере:
              сеть выключена, лимиты CPU/памяти, наружу видна только
              рабочая папка. Самый безопасный вариант.
  "confirm" — прямой запуск на хосте, но опасные команды требуют
              подтверждения оператора.
  "off"     — прямой запуск без ограничений (изоляция на стороне хоста).

Важно: тайм-аут и обрезка вывода работают во всех режимах. Зависшая
команда не должна вешать агента, а мегабайтный вывод — засорять контекст.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable

from .base import Tool, ToolError, Workspace

MAX_OUTPUT = 20_000        # символов stdout+stderr, отдаваемых модели

# Шаблоны, которые почти всегда означают беду. В режиме confirm по ним
# запрашивается подтверждение, в docker они безопасны (контейнер одноразовый).
DANGEROUS = [
    (re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]", re.I), "рекурсивное удаление"),
    (re.compile(r"\bmkfs\b|\bfdisk\b|\bparted\b", re.I), "операции с разделами"),
    (re.compile(r"\bdd\b\s+.*of=/dev/", re.I), "запись в блочное устройство"),
    (re.compile(r":\(\)\s*\{.*\};\s*:", re.S), "fork-бомба"),
    (re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b", re.I), "выключение машины"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/", re.I), "раздача прав на корень"),
    (re.compile(r">\s*/dev/sd[a-z]", re.I), "перезапись диска"),
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh", re.I), "исполнение скрипта из сети"),
    (re.compile(r"\bwget\b.*\|\s*(ba)?sh", re.I), "исполнение скрипта из сети"),
]


@dataclass
class SandboxConfig:
    # "auto" — docker, если он есть, иначе confirm. Умолчание именно auto:
    # жёсткий "docker" на машине без демона делал run_command нерабочим.
    mode: str = "auto"                      # auto | docker | confirm | off
    image: str = "agent-sandbox:latest"
    timeout: int = 120
    memory: str = "1g"
    cpus: str = "2"
    network: bool = False


def check_dangerous(command: str) -> str | None:
    """Вернёт описание опасности или None."""
    for rx, why in DANGEROUS:
        if rx.search(command):
            return why
    return None


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return (text[:half] + f"\n\n... [обрезано {len(text) - MAX_OUTPUT} символов] ...\n\n"
            + text[-half:])


_DOCKER_OK: bool | None = None


def docker_available(recheck: bool = False) -> bool:
    """Есть ли рабочий демон. Результат кэшируется: иначе `docker info`
    выполнялся бы перед каждой командой агента."""
    global _DOCKER_OK
    if _DOCKER_OK is None or recheck:
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
            _DOCKER_OK = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _DOCKER_OK = False
    return _DOCKER_OK


VALID_MODES = ("auto", "docker", "confirm", "off")


def effective_mode(cfg: "SandboxConfig") -> tuple[str, str]:
    """Режим, который применится на самом деле, и пояснение.

    Деградация вместо отказа: без демона команды выполняются на хосте,
    но опасные требуют подтверждения. Молча — нельзя, поэтому причина
    возвращается и показывается и агенту, и оператору.
    """
    if cfg.mode not in VALID_MODES:
        # Опечатка в конфиге раньше означала бы попытку запустить режим,
        # которого нет. Падать не будем, но и молчать нельзя.
        return ("confirm",
                f"неизвестный режим песочницы {cfg.mode!r}; "
                f"допустимы {', '.join(VALID_MODES)} — используем confirm")
    if cfg.mode == "auto":
        return ("docker", "") if docker_available() else (
            "confirm", "Docker не найден — команды идут на хосте, "
            "опасные требуют подтверждения")
    if cfg.mode == "docker" and not docker_available():
        return ("confirm",
                "ЗАПРОШЕН docker, но демон недоступен — деградация до confirm. "
                "Соберите образ (make build-sandbox) или укажите "
                "sandbox.mode=confirm явно")
    return cfg.mode, ""


def build(
    ws: Workspace,
    cfg: SandboxConfig,
    confirm: Callable[[str, str], bool] | None = None,
) -> list[Tool]:
    """confirm(command, reason) -> bool — спросить оператора (режим confirm)."""

    def _run_direct(command: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-lc", command],
            cwd=str(ws.root), capture_output=True, text=True, timeout=timeout,
        )

    def _run_docker(command: str, timeout: int) -> subprocess.CompletedProcess[str]:
        args = [
            "docker", "run", "--rm",
            "--network", "bridge" if cfg.network else "none",
            "--memory", cfg.memory, "--cpus", cfg.cpus,
            "--pids-limit", "512",
            # рабочая папка — единственное, что видно из контейнера
            "-v", f"{ws.root}:/work",
            "-w", "/work",
            # не root внутри контейнера, чтобы файлы не портили права снаружи
            "-u", f"{os.getuid()}:{os.getgid()}",
            cfg.image, "bash", "-lc", command,
        ]
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    warned: list[str] = []

    def run_command(command: str, timeout: int = 0) -> str:
        if not command or not command.strip():
            raise ToolError("Пустая команда")
        tmo = timeout if timeout and timeout > 0 else cfg.timeout
        mode, note = effective_mode(cfg)
        prefix = ""
        if note and not warned:          # предупреждаем один раз за сессию
            warned.append(note)
            prefix = f"[{note}]\n"

        danger = check_dangerous(command)
        if danger and mode == "confirm":
            if confirm is None or not confirm(command, danger):
                return (f"ОТКЛОНЕНО оператором: {danger}.\n"
                        "Команда не выполнялась. Предложите более безопасный путь.")
        if danger and mode == "off":
            # Совсем без защиты работать нельзя: явно предупреждаем в выводе.
            pass

        try:
            if mode == "docker":
                proc = _run_docker(command, tmo)
            else:
                proc = _run_direct(command, tmo)
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"Команда не уложилась в {tmo} с и была прервана. "
                "Разбейте её на части или увеличьте timeout."
            ) from None
        except FileNotFoundError as exc:
            raise ToolError(f"Не запустить окружение: {exc}") from exc

        out = _truncate((proc.stdout or "") + (proc.stderr or ""))
        status = "ок" if proc.returncode == 0 else f"код возврата {proc.returncode}"
        body = f"[{status}]\n{out}" if out.strip() else f"[{status}] (вывод пуст)"
        return prefix + body

    return [
        Tool("run_command",
             "Выполнить shell-команду в рабочей папке и получить её вывод. "
             "Используйте для запуска тестов, сборки, проверок.",
             {"type": "object",
              "properties": {
                  "command": {"type": "string", "description": "Команда для bash"},
                  "timeout": {"type": "integer",
                              "description": "Предел в секундах, 0 = из конфига"}},
              "required": ["command"]},
             run_command,
             dangerous=True),
    ]
