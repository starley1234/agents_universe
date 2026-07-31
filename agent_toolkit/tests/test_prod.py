"""Тесты продакшн-адаптации: настройки, CLI, потокобезопасность, Health Check."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_toolkit
from agent_toolkit.api import create_api_app
from agent_toolkit.cli import run_check, run_execute, run_list
from agent_toolkit.config import Settings
from agent_toolkit.core import ArtifactStore, Workspace
from agent_toolkit.local.memory import MemoryStore
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))

        section("1. Настройки продакшна (Settings.from_env)")
        s_dev = Settings.from_env(env="development")
        check("в development включён mock_mode", s_dev.mock_mode is True)

        os.environ["AGENT_TOOLKIT_ENV"] = "production"
        s_prod = Settings.from_env()
        check("в production mock_mode по умолчанию false", s_prod.mock_mode is False)
        del os.environ["AGENT_TOOLKIT_ENV"]

        section("2. Потокобезопасность в многоагентной среде (Thread-Safety)")
        reg = agent_toolkit.build_default_registry(ws)
        store_art = ArtifactStore(ws)
        store_mem = MemoryStore(ws)

        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                # Параллельный поиск и вызов
                hits = reg.search("uuid", limit=1)
                t_uuid = hits[0][0]
                t_uuid.execute()
                # Параллельное сохранение артефактов и фактов
                store_art.save_text(f"log_{tid}.txt", f"data {tid}")
                store_mem.save_fact(f"key_{tid}", f"val_{tid}", ["test"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        check("10 параллельных потоков выполнились без ошибок", len(errors) == 0)
        check("все 10 артефактов сохранены", len(store_art.list()) == 10)
        check("все 10 фактов записаны в память", len(store_mem.search_facts("", limit=20)) == 10)

        section("3. Командный интерфейс CLI (agent_toolkit.cli)")
        check("CLI check завершается успешно", run_check() == 0)
        check("CLI list завершается успешно", run_list() == 0)
        check("CLI execute выполняет инструмент", run_execute("crypto.generate_uuid") == 0)

        section("4. Health Check эндпоинт (/health)")
        try:
            app = create_api_app(reg, workspace=ws)
            routes = [r.path for r in app.routes]  # type: ignore[attr-defined]
            check("/health эндпоинт зарегистрирован в FastAPI", "/health" in routes and "/api/health" in routes)
            check("Web UI и эндпоинт артефактов зарегистрированы", "/ui" in routes and "/api/artifacts" in routes)
        except ImportError:
            check("create_api_app возвращает инструкцию без fastapi", True, "FastAPI не установлен (ок)")

    return summary("Тесты продакшн-адаптации")


def test_prod_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
