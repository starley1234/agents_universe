"""Версионирование схемы: миграции с блокировкой и проверкой совместимости.

ЗАЧЕМ ЭТО НУЖНО В ПРОДЕ, ХОТЯ `init_schema()` ИДЕМПОТЕНТЕН.
`CREATE TABLE IF NOT EXISTS` создаёт таблицу, которой нет, но НЕ меняет
таблицу, которая есть. Когда через полгода в схему добавится колонка,
код новой версии начнёт обращаться к ней на старой базе и падать в
рантайме — на живых данных, у инженера посреди работы. Поэтому база
хранит НОМЕР ВЕРСИИ схемы, а приложение при старте сверяет его со своим.

ТРИ ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ СОБЛЮДАЮТСЯ.

1. МИГРАЦИИ ВЫПОЛНЯЮТСЯ ПОД БЛОКИРОВКОЙ. В проде инстансов может быть
   несколько (веб + cron + ручная команда), и все они стартуют
   одновременно после деплоя. Без блокировки два процесса выполнят одну
   миграцию дважды. `pg_advisory_lock` даёт взаимное исключение на
   уровне СУБД — ровно то, что нужно, и не требует внешнего координатора.

2. ПРИЛОЖЕНИЕ НЕ МИГРИРУЕТ БАЗУ САМО ПРИ ОБЫЧНОМ СТАРТЕ. Автоматическая
   миграция при запуске веб-сервера — частая причина инцидентов: деплой
   пятнадцати подов означает пятнадцать одновременных ALTER TABLE на
   таблице с миллионом строк. Миграция — ОТДЕЛЬНАЯ команда
   (`saps migrate`), которую администратор запускает осознанно, сделав
   бэкап. Сервер лишь ПРОВЕРЯЕТ версию и отказывается работать на
   несовместимой схеме с понятным сообщением.

3. МИГРАЦИЯ НИКОГДА НЕ УДАЛЯЕТ ДАННЫЕ МОЛЧА. Здесь только аддитивные
   изменения (новые таблицы, колонки, индексы). Если однажды понадобится
   разрушительная операция, она должна требовать явного флага и
   печатать, что именно будет потеряно.

ВЕРСИЯ 1 — это текущая схема (schema.py). Она уже развёрнута у первых
пользователей без таблицы версий, поэтому `detect_version()` умеет
распознать такую базу и проставить ей версию 1, не пересоздавая ничего.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: Версия схемы, которую понимает ЭТОТ код. Поднимается вместе с
#: добавлением новой миграции в MIGRATIONS.
SCHEMA_VERSION = 1

#: Ключ advisory-блокировки. Произвольное, но фиксированное число:
#: важно лишь, чтобы все инстансы САПС брали один и тот же ключ.
LOCK_KEY = 0x5A_05_0001


class MigrationError(RuntimeError):
    """Ошибка миграции или несовместимость версии схемы."""


@dataclass
class Migration:
    version: int
    title: str
    #: Функция получает (conn, schema) и выполняет SQL. Обязана быть
    #: идемпотентной: повторный запуск не должен ломать базу.
    apply: Callable[[Any, str], None]


def _version_table_sql(schema: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {schema}.schema_version(
  version     INTEGER PRIMARY KEY,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  title       TEXT NOT NULL DEFAULT '',
  app_version TEXT NOT NULL DEFAULT ''
);
"""


def _migration_001(conn: Any, schema: str) -> None:
    """Базовая схема САПС: расширение pgvector и все таблицы.

    Миграция ДЕЙСТВИТЕЛЬНО создаёт схему, а не только помечает версию.
    Ранняя версия ограничивалась отметкой, полагая, что таблицы уже
    создал init_schema() — в результате `saps migrate` на пустой базе
    записывал «версия 1» без единой таблицы, и сервер падал на первом же
    запросе к /ready с UndefinedTable. Поймано тестом развёртывания.

    Размерность вектора берётся из переменной окружения, потому что
    колонка vector(dim) фиксируется здесь навсегда.
    """
    import os

    from .schema import schema_sql

    try:
        dim = int(os.getenv("SAPS_EMBEDDING_DIM", "512"))
    except ValueError:
        dim = 512
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:                                     # noqa: BLE001
        raise MigrationError(
            f"Не удалось включить расширение pgvector: {exc}. САПС ищет "
            "требования и пункты АП по смыслу, без pgvector это невозможно. "
            "В Docker используйте образ pgvector/pgvector; на своём сервере "
            "установите расширение и выполните CREATE EXTENSION vector.") from exc
    conn.execute(schema_sql(schema, dim))


MIGRATIONS: list[Migration] = [
    Migration(1, "базовая схема САПС", _migration_001),
]


def table_exists(conn: Any, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s", (schema, table)).fetchone()
    return row is not None


def detect_version(conn: Any, schema: str) -> int:
    """Текущая версия схемы в базе. 0 — схемы нет вообще.

    База, развёрнутая до появления версионирования, таблицы версий не
    имеет, но имеет таблицу requirement. Такую базу считаем версией 1 —
    иначе миграция попыталась бы «создать» уже существующую схему и
    сбила бы администратора с толку.
    """
    if not table_exists(conn, schema, "schema_version"):
        return 1 if table_exists(conn, schema, "requirement") else 0
    row = conn.execute(
        f"SELECT COALESCE(MAX(version), 0) AS v FROM {schema}.schema_version"
    ).fetchone()
    value = row["v"] if isinstance(row, dict) else row[0]
    return int(value or 0)


def check_compatible(conn: Any, schema: str) -> None:
    """Проверить, что код и база сходятся по версии. Иначе — отказ.

    Вызывается при старте сервера и команд, работающих с данными.
    Отказ лучше работы на неизвестной схеме: во втором случае ошибка
    вылезет посреди операции инженера и, возможно, оставит данные
    в половинчатом состоянии.
    """
    current = detect_version(conn, schema)
    if current == 0:
        raise MigrationError(
            f"В базе нет схемы {schema!r}. Создайте её: saps migrate "
            "(или saps init --rules для первой установки).")
    if current < SCHEMA_VERSION:
        pending = [m.version for m in MIGRATIONS if m.version > current]
        raise MigrationError(
            f"Схема в базе версии {current}, код требует {SCHEMA_VERSION}. "
            f"Не применены миграции: {pending}. Сделайте бэкап и выполните: "
            "saps migrate")
    if current > SCHEMA_VERSION:
        raise MigrationError(
            f"Схема в базе версии {current}, а этот код понимает только "
            f"{SCHEMA_VERSION}. Похоже, запущена СТАРАЯ версия САПС на новой "
            "базе — обновите приложение. Работать на схеме из будущего "
            "нельзя: код не знает о её изменениях.")


def migrate(conn: Any, schema: str, *, app_version: str = "",
            dry_run: bool = False) -> dict[str, Any]:
    """Применить недостающие миграции под блокировкой.

    Возвращает отчёт: с какой версии на какую перешли и что применили.
    """
    # Блокировка на уровне СУБД: второй процесс, стартовавший
    # одновременно, будет ждать здесь, а не выполнять те же ALTER.
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn.execute(_version_table_sql(schema))
        current = detect_version(conn, schema)
        pending = [m for m in MIGRATIONS if m.version > current]

        if dry_run:
            return {"from": current, "to": SCHEMA_VERSION,
                    "pending": [{"version": m.version, "title": m.title}
                                for m in pending],
                    "applied": [], "dry_run": True}

        applied: list[dict[str, Any]] = []
        for m in pending:
            m.apply(conn, schema)
            conn.execute(
                f"INSERT INTO {schema}.schema_version(version, title, "
                "app_version) VALUES(%s,%s,%s) ON CONFLICT (version) DO NOTHING",
                (m.version, m.title, app_version))
            applied.append({"version": m.version, "title": m.title})

        # База, созданная до версионирования, могла не иметь записи о
        # версии 1 — проставляем её, чтобы дальше всё шло штатно.
        if current >= 1 and not pending:
            conn.execute(
                f"INSERT INTO {schema}.schema_version(version, title, "
                "app_version) VALUES(%s,%s,%s) ON CONFLICT (version) DO NOTHING",
                (current, "существующая схема", app_version))

        return {"from": current, "to": detect_version(conn, schema),
                "pending": [], "applied": applied, "dry_run": False}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def history(conn: Any, schema: str) -> list[dict[str, Any]]:
    """Журнал применённых миграций — для диагностики после инцидента."""
    if not table_exists(conn, schema, "schema_version"):
        return []
    rows = conn.execute(
        f"SELECT version, title, app_version, applied_at "
        f"FROM {schema}.schema_version ORDER BY version").fetchall()
    return [dict(r) for r in rows]
