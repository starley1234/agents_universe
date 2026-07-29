"""Общий каркас тестов AWOS: счётчики, секции, временная среда.

Философия та же, что у соседних проектов репозитория (agent_system,
multi_agent_system_ontology): никаких внешних фреймворков, скрипт на
стандартной библиотеке, реальная инфраструктура (настоящий SQLite,
настоящие сокеты), заглушка только на месте самой модели. Запуск любого
файла — `python3 tests/test_X.py`, код возврата 1 при падении.

Почему не pytest: у ядра среды ноль зависимостей, и тесты не должны
быть единственной причиной ставить пакет. Плюс такой файл читается как
описание поведения сверху вниз.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awos.config import Config                                    # noqa: E402

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
    """Негативный сценарий: обязан упасть ИМЕННО этим типом ошибки.

    Отдельная функция, потому что «ожидаемая ошибка» — половина
    контракта среды: платформа обязана отказывать понятно и предсказуемо,
    а не падать трейсбеком или, хуже, продолжать работу молча.
    """
    try:
        fn(*args, **kwargs)
    except exc_type:
        return check(name, True)
    except Exception as exc:                                      # noqa: BLE001
        return check(name, False,
                     f"ожидали {exc_type.__name__}, получили "
                     f"{type(exc).__name__}: {exc}")
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


class TempEnv:
    """Временная среда: своя база, своя рабочая папка, stub-модель.

    Каждый тест получает чистое состояние — иначе прогоны и точки
    контроля из предыдущих проверок дают ложные срабатывания.
    """

    def __init__(self, **overrides: Any) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="awos_test_"))
        params: dict[str, Any] = {
            "db_path": str(self.dir / "awos.db"),
            "workspace": str(self.dir / "workspace"),
            "provider": "stub",
            "model": "stub",
            "hitl_mode": "off",
            "llm_retries": 0,
        }
        params.update(overrides)
        self.cfg = Config(**params)

    def __enter__(self) -> "TempEnv":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def write_workflow(self, name: str, data: dict[str, Any]) -> Path:
        """Положить определение во ВРЕМЕННЫЙ каталог.

        Тесты не должны трогать встроенные awos/workflows/*.json: они
        часть поставки, и правка их ради теста ломает demo у пользователя.
        """
        import json
        d = self.dir / "workflows"
        d.mkdir(exist_ok=True)
        # Имя файла и поле name обязаны совпадать (это проверяет
        # load_workflow), поэтому подставляем имя файла автоматически:
        # тесту не должно приходиться помнить про это ограничение.
        data = {**data, "name": name}
        p = d / f"{name}.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.cfg.workflows_dir = str(d)
        return p

    def write_profile(self, name: str, data: dict[str, Any]) -> Path:
        import json
        d = self.dir / "profiles"
        d.mkdir(exist_ok=True)
        p = d / f"{name}.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.cfg.profiles_dir = str(d)
        return p


def simple_workflow(name: str = "wf", *, steps: list[dict[str, Any]] | None = None,
                    inputs: dict[str, str] | None = None) -> dict[str, Any]:
    """Минимальное валидное определение — основа для большинства тестов."""
    return {
        "name": name,
        "title": "тестовый workflow",
        "inputs": inputs if inputs is not None else {},
        "steps": steps or [
            {"name": "only", "task": "Сделай что-нибудь по цели: {goal}",
             "writes": "result"},
        ],
    }
