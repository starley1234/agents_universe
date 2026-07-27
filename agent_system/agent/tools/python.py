"""Запуск Python: счёт и проверка, не требующие Docker.

Зачем отдельно от run_command. Ради арифметики, разбора файла или
короткой проверки модель зовёт shell — а тот в режиме confirm упирается
в подтверждение, в режиме docker без демона деградирует. Считать при
этом надо всегда, и это не опасная операция.

Что здесь безопасно без песочницы:

  * код исполняется отдельным процессом, а не exec() внутри агента:
    зависание или падение не трогают самого агента;
  * рабочий каталог — только workspace, туда же ограничены пути;
  * жёсткий тайм-аут и обрезка вывода;
  * ограничение памяти через resource (Linux): бесконечный список не
    съест ту самую 1 ГБ на сервере.

Чего здесь нет: изоляции сети и файловой системы. Код может открыть
любой файл, доступный пользователю. Это инструмент удобства, а не
песочница — для недоверенного кода остаётся run_command с docker.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import Tool, ToolError, Workspace

MAX_OUTPUT = 20_000
DEFAULT_TIMEOUT = 60
DEFAULT_MEMORY_MB = 512      # половина гигабайта: на сервере с 1 ГБ безопасно

#: Обёртка вокруг кода агента. Три вещи, ради которых она нужна:
#:
#:   * предел памяти ставится внутри дочернего процесса (preexec_fn
#:     нельзя: он несовместим с потоками, а сервер агента многопоточный);
#:   * код читается из stdin — не надо экранировать кавычки;
#:   * compile(..., "<код>") даёт в traceback НОМЕРА СТРОК САМОГО КОДА.
#:     Если просто приписать пролог сверху, номера съедут, и модель
#:     будет искать ошибку не в той строке.
_RUNNER = r'''
import sys, traceback
mb = int(sys.argv[1])
try:
    import resource
    _b = mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (_b, _b))
except Exception:
    pass
src = sys.stdin.read()
sys.argv = sys.argv[2:] or ["<код>"]
try:
    if sys.argv[0] == "-f":
        import runpy
        path = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_path(path, run_name="__main__")
    else:
        exec(compile(src, "<код>", "exec"), {"__name__": "__main__"})
except SystemExit as e:
    raise
except BaseException as e:
    # первый кадр — сама эта обёртка; агенту он ни о чём не говорит
    tb = e.__traceback__.tb_next if e.__traceback__ else None
    traceback.print_exception(type(e), e, tb)
    sys.exit(1)
'''


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return (text[:half]
            + f"\n\n… обрезано {len(text) - MAX_OUTPUT} символов …\n\n"
            + text[-half:])


def build(ws: Workspace, timeout: int = DEFAULT_TIMEOUT,
          memory_mb: int = DEFAULT_MEMORY_MB) -> list[Tool]:
    timeout_default = timeout

    def _exec(source: str, tmo: int, label: str, argv: list[str]) -> str:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _RUNNER, str(memory_mb), *argv],
                cwd=str(ws.root), capture_output=True, text=True,
                timeout=tmo, input=source)
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"{label} не уложился в {tmo} с и прерван. Уберите "
                "бесконечный цикл или увеличьте timeout."
            ) from None
        except OSError as exc:
            raise ToolError(f"Не запустить Python: {exc}") from exc

        out = _truncate((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode == 0:
            return out.rstrip() or "[выполнено, вывод пуст]"
        # MemoryError на пределе выглядит невнятно — поясняем.
        hint = ("\n[подсказка: превышен предел памяти "
                f"{memory_mb} МБ]" if "MemoryError" in out else "")
        return f"[код возврата {proc.returncode}]\n{out.rstrip()}{hint}"

    def run_python(code: str, timeout: int = 0) -> str:
        """Выполнить код и вернуть то, что он напечатал."""
        if not code or not code.strip():
            raise ToolError("Пустой код выполнять нечего")
        tmo = timeout if timeout > 0 else timeout_default
        return _exec(code, tmo, "Код", [])

    def run_script(path: str, args: str = "", timeout: int = 0) -> str:
        """Запустить файл .py из рабочей папки."""
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это папка, а не скрипт")
        tmo = timeout if timeout > 0 else timeout_default
        # runpy внутри обёртки запускает файл как __main__, чтобы работал
        # `if __name__ == "__main__"` — ради него скрипты и пишут.
        argv = ["-f", str(p), *(args.split() if args else [])]
        return _exec("", tmo, f"Скрипт {ws.relative(p)}", argv)

    return [
        Tool("run_python",
             "Выполнить код на Python и получить напечатанное. Годится для "
             "расчётов, разбора данных, быстрой проверки. Работает всегда, "
             "не требует Docker. Печатайте результат через print — "
             "значение последней строки само не выводится.",
             {"type": "object",
              "properties": {
                  "code": {"type": "string", "description": "Код на Python"},
                  "timeout": {"type": "integer",
                              "description": "Предел в секундах, 0 = обычный"}},
              "required": ["code"]},
             run_python),
        Tool("run_script",
             "Запустить файл .py из рабочей папки и получить его вывод.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string", "description": "Путь к .py"},
                  "args": {"type": "string",
                           "description": "Аргументы через пробел"},
                  "timeout": {"type": "integer"}},
              "required": ["path"]},
             run_script),
    ]
