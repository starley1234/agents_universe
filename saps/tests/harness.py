"""Каркас тестов САПС: счётчики, секции, временная база PostgreSQL.

Философия — как в соседних проектах репозитория: никаких фреймворков,
скрипт на стандартной библиотеке, РЕАЛЬНАЯ инфраструктура. Для САПС это
особенно важно: система построена вокруг схемы PostgreSQL с pgvector, и
тесты на моках проверяли бы моки, а не инварианты прослеживаемости,
которые держатся на транзакциях, внешних ключах и UNIQUE-ограничениях.

Каждый тестовый файл получает СВОЮ БАЗУ во временном кластере (pgserver),
поэтому наборы не влияют друг на друга и порядок запуска не важен.

Если psycopg или pgserver не установлены, наборы, которым нужна база,
корректно ПРОПУСКАЮТСЯ с указанием причины, а чистая логика (парсеры,
правила редактора, разбор ответов Teamcenter) проверяется всегда. Так
`make test` остаётся полезным на машине, где PostgreSQL ещё не поднят.
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = 0
FAIL = 0
FAILURES: list[str] = []
SKIPPED: list[str] = []

# --- доступность инфраструктуры -------------------------------------------
HAVE_DB = True
SKIP_REASON = ""
_SERVER = None
_TMPDIR = ""

try:
    import psycopg  # noqa: F401
except ImportError:
    HAVE_DB = False
    SKIP_REASON = ("psycopg не установлен — pip install 'psycopg[binary]'")


def server():
    """Поднять embedded PostgreSQL один раз на процесс."""
    global _SERVER, _TMPDIR, HAVE_DB, SKIP_REASON
    if _SERVER is not None or not HAVE_DB:
        return _SERVER
    try:
        import pgserver
    except ImportError:
        HAVE_DB = False
        SKIP_REASON = ("pgserver не установлен — нужен для тестов без "
                       "развёрнутого PostgreSQL: pip install pgserver")
        return None
    try:
        _TMPDIR = tempfile.mkdtemp(prefix="saps_pg_")
        _SERVER = pgserver.get_server(_TMPDIR)
    except Exception as exc:                                     # noqa: BLE001
        HAVE_DB = False
        SKIP_REASON = f"не удалось поднять тестовый PostgreSQL: {exc}"
        return None
    return _SERVER


def fresh_dsn() -> str:
    """Отдельная база в тестовом кластере — чистое состояние на набор."""
    import psycopg
    srv = server()
    if srv is None:
        raise RuntimeError(SKIP_REASON)
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", srv.get_uri())


def make_store(dim: int = 64):
    """Store на свежей базе с созданной схемой."""
    from saps.db.store import Store
    st = Store(fresh_dsn(), schema="saps", dim=dim)
    st.init_schema()
    return st


def make_config(**over: Any):
    """Config с безопасными умолчаниями для тестов."""
    from saps.config import Config
    params: dict[str, Any] = {
        "db_dsn": "", "embedding_provider": "hash",
        "embedding_model": "hash-64", "embedding_dim": 64,
        "llm_provider": "none", "workdir": tempfile.mkdtemp(prefix="saps_wd_"),
    }
    params.update(over)
    return Config(**params)


# --- отчётность ------------------------------------------------------------
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

    В САПС половина ценности — в отказах: система обязана не пустить
    брак дальше и объяснить причину, а не «как-нибудь продолжить».
    """
    try:
        fn(*args, **kwargs)
    except exc_type:
        return check(name, True)
    except Exception as exc:                                     # noqa: BLE001
        return check(name, False,
                     f"ожидали {exc_type.__name__}, получили "
                     f"{type(exc).__name__}: {exc}")
    return check(name, False, f"ожидали {exc_type.__name__}, ошибки не было")


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def skip_section(title: str, reason: str) -> None:
    SKIPPED.append(f"{title}: {reason}")
    print(f"\n{title}\n" + "─" * len(title))
    print(f"  ПРОПУЩЕНО — {reason}")


def summary(title: str) -> int:
    print(f"\n{title}: {PASS} ок, {FAIL} провалов"
          + (f", пропущено секций {len(SKIPPED)}" if SKIPPED else ""))
    for item in SKIPPED:
        print(f"  ⊘ {item}")
    if FAILURES:
        for name in FAILURES:
            print(f"  ✗ {name}")
        return 1
    return 0


def cleanup() -> None:
    if _TMPDIR:
        shutil.rmtree(_TMPDIR, ignore_errors=True)


# --- фикстуры предметной области -------------------------------------------
def sample_docx(path: str | Path) -> Path:
    """Документ, похожий на реальное ТЗ: заголовки, абзацы, таблица."""
    from saps.export.writers import write_docx
    return write_docx(path, [
        {"type": "heading", "text": "3 Требования к системе управления",
         "level": 1},
        {"type": "paragraph",
         "text": "[REQ-001] Система управления должна обеспечивать "
                 "отказобезопасность при единичном отказе любого элемента."},
        {"type": "heading", "text": "3.1 Надёжность", "level": 2},
        {"type": "paragraph",
         "text": "[REQ-002] Наработка на отказ блока управления должна быть "
                 "не менее 10000 ч."},
        {"type": "paragraph",
         "text": "Настоящий раздел разработан на основании технического "
                 "задания заказчика."},
        {"type": "paragraph",
         "text": "[REQ-003] Система должна иметь высокую надёжность и "
                 "достаточное быстродействие."},
        {"type": "heading", "text": "4 Требования к конструкции", "level": 1},
        {"type": "table",
         "header": ["Идентификатор", "Требование", "Ответственный", "Узел"],
         "rows": [
             ["REQ-010", "Масса блока не должна превышать 12 кг", "Иванов",
              "АСДБ.04.32"],
             ["REQ-011", "Конструкция должна выдерживать эксплуатационную "
                         "нагрузку 3.5 g", "Петров", "АСДБ.04.32"],
         ]},
    ], title="Техническое задание на изделие")


def sample_xlsx(path: str | Path) -> Path:
    from saps.export.writers import write_xlsx
    return write_xlsx(path, {"Требования": {
        "header": ["Идентификатор", "Требование", "Ответственный", "MoC"],
        "rows": [
            ["REQ-100", "Давление в гидросистеме должно быть не менее 21 МПа",
             "Сидоров", "MC2"],
            ["REQ-101", "Люк должен открываться усилием не более 150 Н",
             "Сидоров", "MC5"],
        ]}})
