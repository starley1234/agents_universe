"""Каркас тестов agent_toolkit: проверки, секции, временная рабочая среда.

Работает как напрямую (python3 tests/test_X.py), так и при запуске
через pytest.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: Any, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
        return True
    FAIL += 1
    FAILURES.append(name)
    print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
    return False


def check_raises(name: str, exc_type: type, fn, *args: Any, **kwargs: Any) -> bool:
    """Проверка, что вызов функции вызывает ожидаемое исключение."""
    try:
        fn(*args, **kwargs)
    except exc_type:
        return check(name, True)
    except Exception as exc:  # noqa: BLE001
        return check(
            name,
            False,
            f"ожидали {exc_type.__name__}, получили {type(exc).__name__}: {exc}",
        )
    return check(name, False, f"ожидали {exc_type.__name__}, ошибки не было")


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def summary(title: str) -> int:
    print(f"\n{title}: {PASS} ок, {FAIL} провалов")
    if FAILURES:
        for name in FAILURES:
            print(f"  ✗ {name}")
        return 1
    return 0


class TempWorkspace:
    """Временная рабочая область для изолированных тестов."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="agent_toolkit_test_"))

    def __enter__(self) -> "TempWorkspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)
