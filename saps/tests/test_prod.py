"""Тесты эксплуатационной части: миграции, пробы, устойчивость, копии.

Проверяется не логика предметной области, а то, что система переживает
реальную эксплуатацию: перезапуск базы, одновременный старт нескольких
инстансов после деплоя, остановку по SIGTERM, восстановление из копии.

Всё на настоящем PostgreSQL и настоящих процессах: сценарии вроде
«переподключился после обрыва» невозможно проверить моком — именно
поведение драйвера и составляет суть проверки.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, check_raises, fresh_dsn, make_store,   # noqa: E402
                     section, skip_section, summary)

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _env_for(dsn: str, **over: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAPS_")}
    env.update({
        "SAPS_DB_DSN": dsn,
        "SAPS_EMBEDDING_DIM": "64",
        "SAPS_EMBEDDING_MODEL": "hash-64",
        "SAPS_WORKDIR": tempfile.mkdtemp(prefix="saps_prod_wd_"),
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(over)
    return env


def _saps(env: dict[str, str], *args: str, timeout: int = 180):
    return subprocess.run([sys.executable, "-m", "saps", *args], env=env,
                          capture_output=True, text=True, cwd=str(ROOT),
                          timeout=timeout)


def _get(port: int, path: str, timeout: int = 15):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:                                     # noqa: BLE001
        return 0, str(exc)


def main() -> int:
    section("Конфигурация логирования")
    from saps.config import Config, ConfigError
    cfg = Config()
    check("уровень по умолчанию INFO", cfg.log_level == "INFO")
    check("логи текстовые по умолчанию", cfg.log_json is False,
          "первым в журнал смотрит администратор, а не парсер")
    check_raises("неизвестный уровень отвергается", ConfigError,
                 Config(log_level="БОЛТЛИВО").validate)
    for level in ("DEBUG", "info", "WARNING"):
        Config(log_level=level).validate()
    check("регистр уровня не важен", True)

    from saps.api.server import setup_logging
    setup_logging("DEBUG", json_format=True)
    setup_logging("INFO", json_format=False)
    check("setup_logging не падает в обоих режимах", True)

    if harness.server() is None:
        skip_section("Эксплуатационные тесты", harness.SKIP_REASON)
        return summary("Эксплуатация")

    import psycopg

    from saps.db.migrate import (MigrationError, SCHEMA_VERSION,
                                 check_compatible, detect_version, history,
                                 migrate)
    from saps.db.store import Store

    section("Версионирование схемы")
    dsn = fresh_dsn()
    st = Store(dsn, schema="saps", dim=64)
    check("пустая база: версия 0", detect_version(st.conn, "saps") == 0)
    check_raises("работа на пустой базе запрещена", MigrationError,
                 check_compatible, st.conn, "saps")

    plan = migrate(st.conn, "saps", dry_run=True)
    check("dry-run показывает план", plan["pending"] and plan["to"] == SCHEMA_VERSION)
    check("dry-run ничего не применил", detect_version(st.conn, "saps") == 0,
          "проверка не должна менять базу")

    report = migrate(st.conn, "saps", app_version="test")
    check("миграция применена", report["to"] == SCHEMA_VERSION)
    check("версия записана", detect_version(st.conn, "saps") == SCHEMA_VERSION)

    tables = {r["table_name"] for r in st.conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='saps'").fetchall()}
    for t in ("requirement", "rule_clause", "compliance_item", "suggestion",
              "audit_log", "schema_version"):
        check(f"таблица {t} создана", t in tables)
    check("миграция СОЗДАЁТ схему, а не только помечает версию",
          "requirement" in tables,
          "ранняя версия писала версию без таблиц — сервер падал на /ready")

    check_compatible(st.conn, "saps")
    check("проверка совместимости проходит", True)
    check("повторная миграция идемпотентна",
          migrate(st.conn, "saps")["applied"] == [])
    check("журнал версий без дублей", len(history(st.conn, "saps")) == 1)

    section("Миграция под нагрузкой: одновременный старт инстансов")
    dsn2 = fresh_dsn()
    errors: list[str] = []

    def worker() -> None:
        try:
            conn = psycopg.connect(dsn2, autocommit=True,
                                   row_factory=psycopg.rows.dict_row)
            migrate(conn, "saps", app_version="parallel")
            conn.close()
        except Exception as exc:                                 # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("пять инстансов мигрируют без ошибок", not errors, str(errors[:1]))
    probe = Store(dsn2, schema="saps", dim=64)
    check("схема создана ровно один раз",
          len(history(probe.conn, "saps")) == 1,
          "advisory-блокировка не дала выполнить миграцию дважды")
    probe.close()

    section("Схема из будущего: старый код на новой базе")
    st.conn.execute(
        "INSERT INTO saps.schema_version(version,title) VALUES(99,'будущее')")
    check_raises("работа на неизвестной схеме запрещена", MigrationError,
                 check_compatible, st.conn, "saps")
    st.conn.execute("DELETE FROM saps.schema_version WHERE version=99")

    section("Устойчивость соединения с базой")
    check("ping на живом соединении", st.ping() is True)
    health = st.health()
    check("health: база доступна", health["database"] == "ok")
    check("health: pgvector найден", health["pgvector"] is True)
    check("health: версия схемы совпадает",
          health["schema_version"] == health["expected_schema_version"])

    st.create_requirement("REQ-1", "Требование до обрыва связи")
    admin = psycopg.connect(dsn, autocommit=True)
    pid = st.conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    admin.close()
    check("после обрыва ping=False", st.ping() is False)
    st.ensure_alive()
    check("ensure_alive восстановил соединение", st.ping() is True,
          "иначе после планового обслуживания БД пришлось бы рестартовать САПС")
    check("данные на месте", st.stats()["requirements"] == 1)
    check("health снова ok", st.health()["database"] == "ok")
    st.close()

    section("HTTP-пробы: /health, /ready, /metrics")
    dsn3 = fresh_dsn()
    port = _free_port()
    env = _env_for(dsn3, SAPS_PORT=str(port))

    rc = _saps(env, "serve", timeout=60).returncode
    check("сервер не стартует на немигрированной базе", rc == 3,
          f"код {rc}, ожидали 3 — инстанс без схемы не должен принимать запросы")

    _saps(env, "migrate")
    proc = subprocess.Popen([sys.executable, "-m", "saps", "serve"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=str(ROOT))
    try:
        ready = False
        for _ in range(40):
            if _get(port, "/health")[0] == 200:
                ready = True
                break
            time.sleep(0.5)
        check("сервер поднялся", ready)

        code, body = _get(port, "/health")
        check("/health отдаёт 200", code == 200)
        payload = json.loads(body)
        check("/health без токена", payload.get("status") == "ok")
        check("/health сообщает uptime", "uptime_seconds" in payload)

        code, body = _get(port, "/ready")
        check("/ready отдаёт 200", code == 200, body[:120])
        payload = json.loads(body)
        check("/ready: база проверена", payload.get("database") == "ok")
        check("/ready: версия схемы указана",
              payload.get("schema_version") == SCHEMA_VERSION)

        code, body = _get(port, "/metrics")
        check("/metrics отдаёт 200", code == 200)
        check("формат Prometheus", "# HELP saps_up" in body)
        check("saps_up=1 при живой базе", "saps_up 1" in body)
        for metric in ("saps_requirements", "saps_clauses",
                       "saps_suggestions_pending"):
            check(f"метрика {metric} присутствует", metric in body)

        section("Пробы при упавшей базе")
        killer = psycopg.connect(dsn3, autocommit=True)
        killer.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=current_database() AND pid<>pg_backend_pid()")
        killer.close()
        code, _ = _get(port, "/health")
        check("/health отвечает и после обрыва БД", code == 200,
              "liveness не должен зависеть от базы: иначе оркестратор "
              "перезапустит исправное приложение из-за чужого сбоя")
        code, body = _get(port, "/ready")
        check("/ready переподключается сам", code == 200, body[:120])

        section("Остановка по SIGTERM")
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
            elapsed = time.time() - started
            check("процесс остановился", True)
            check("остановка быстрая (< 10 с)", elapsed < 10,
                  f"{elapsed:.1f} с — Docker ждёт до SIGKILL считанные секунды")
            check("код возврата 0", proc.returncode == 0,
                  str(proc.returncode))
        except subprocess.TimeoutExpired:
            proc.kill()
            check("процесс остановился", False, "не завершился за 20 с")
        logs = proc.stdout.read() if proc.stdout else ""
        check("в логах виден сигнал", "SIGTERM" in logs)
        check("в логах есть отметка об остановке", "Остановлено" in logs)
        check("логи со временем и уровнем",
              bool(re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+INFO", logs)),
              logs[:200])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    section("JSON-логи для сборщиков")
    dsn4 = fresh_dsn()
    port2 = _free_port()
    env2 = _env_for(dsn4, SAPS_PORT=str(port2), SAPS_LOG_JSON="true")
    _saps(env2, "migrate")
    proc2 = subprocess.Popen([sys.executable, "-m", "saps", "serve"], env=env2,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, cwd=str(ROOT))
    try:
        for _ in range(40):
            if _get(port2, "/health")[0] == 200:
                break
            time.sleep(0.5)
        proc2.send_signal(signal.SIGTERM)
        proc2.wait(timeout=20)
        out = proc2.stdout.read() if proc2.stdout else ""
        lines = [l for l in out.splitlines() if l.strip().startswith("{")]
        check("логи в JSON", bool(lines), out[:200])
        if lines:
            rec = json.loads(lines[0])
            check("в записи есть уровень и сообщение",
                  {"level", "message", "ts"} <= set(rec))
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=10)

    section("Резервное копирование и восстановление")
    import shutil
    if not shutil.which("pg_dump"):
        print("  ⊘ pg_dump недоступен в PATH — проверка копии пропущена")
    else:
        dsn5 = fresh_dsn()
        env5 = _env_for(dsn5)
        _saps(env5, "migrate")
        _saps(env5, "rules", "load", "--builtin")
        result = _saps(env5, "backup")
        check("backup завершился успешно", result.returncode == 0,
              (result.stdout + result.stderr)[-200:])
        match = re.search(r"✓ Готово: (\S+)", result.stdout)
        check("файл копии создан", match is not None)
        if match:
            dump = Path(match.group(1))
            check("копия непустая", dump.exists() and dump.stat().st_size > 1000)

            # Восстанавливаем в ЧИСТУЮ базу: копия обязана быть
            # самодостаточной, включая расширение vector.
            target = fresh_dsn()
            restore = subprocess.run(
                ["pg_restore", "--clean", "--if-exists", "--no-owner",
                 "-d", target, str(dump)], capture_output=True, text=True)
            check("восстановление без ошибок", restore.returncode == 0,
                  restore.stderr[:300])
            conn = psycopg.connect(target, autocommit=True,
                                   row_factory=psycopg.rows.dict_row)
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM saps.rule_clause").fetchone()["n"]
            check("данные восстановлены", n >= 60, f"пунктов: {n}")
            vec = conn.execute(
                "SELECT COUNT(*) AS n FROM saps.rule_clause "
                "WHERE embedding IS NOT NULL").fetchone()["n"]
            check("векторы восстановлены (расширение в копии)", vec >= 60,
                  "без --extension=vector restore падает на public.vector")
            ver = conn.execute(
                "SELECT MAX(version) AS v FROM saps.schema_version"
            ).fetchone()["v"]
            check("версия схемы восстановлена", ver == SCHEMA_VERSION)
            conn.close()

    section("Файлы развёртывания")
    compose = ROOT / "docker-compose.yml"
    dockerfile = ROOT / "docker" / "Dockerfile"
    unit = ROOT / "deploy" / "saps.service"
    ignore = ROOT / ".dockerignore"
    for path, name in ((compose, "docker-compose.yml"),
                       (dockerfile, "docker/Dockerfile"),
                       (unit, "deploy/saps.service"),
                       (ignore, ".dockerignore")):
        check(f"{name} на месте", path.exists())

    compose_text = compose.read_text()
    check("порт публикуется только на localhost",
          "127.0.0.1:${SAPS_PORT:-8090}:8090" in compose_text,
          "иначе данные сертификации уйдут в сеть по HTTP")
    check("пароль БД обязателен", "POSTGRES_PASSWORD:?" in compose_text)
    check("токен API обязателен", "SAPS_API_TOKEN:?" in compose_text)
    check("образ базы с pgvector", "pgvector/pgvector" in compose_text)
    check("приложение ждёт готовности базы",
          "condition: service_healthy" in compose_text)
    check("задан запас на остановку", "stop_grace_period" in compose_text)

    df = dockerfile.read_text()
    check("контейнер не от root", "USER saps" in df)
    check("pgserver исключён из образа", "grep -v '^pgserver'" in df,
          "тестовая зависимость в проде только вводит в заблуждение")
    check("есть HEALTHCHECK", "HEALTHCHECK" in df)

    di = ignore.read_text()
    check(".env не попадёт в образ", ".env" in di)
    check(".git не попадёт в образ", ".git" in di)

    unit_text = unit.read_text()
    check("systemd: секреты в EnvironmentFile",
          "EnvironmentFile=/etc/saps.env" in unit_text,
          "в юните их увидел бы любой через systemctl show")
    check("systemd: корректная остановка", "KillSignal=SIGTERM" in unit_text)
    check("systemd: ограничение прав", "NoNewPrivileges=yes" in unit_text)

    section("QUICKSTART: команды существуют")
    qs = (ROOT / "QUICKSTART.md").read_text()
    from saps.cli import build_parser
    parser = build_parser()
    known = set(parser._subparsers._group_actions[0].choices)
    used = set(re.findall(
        r"saps\s+(migrate|backup|load|embeddings|index|rules|suggestions|"
        r"health|export|check|serve|init|agent)\b", qs))
    check("все упомянутые команды реальны", used <= known,
          f"отсутствуют: {used - known}")
    flags = set(re.findall(r"saps\s+(\w+)\s+(--[\w-]+)", qs))
    bad = []
    for cmd, flag in flags:
        if cmd not in known:
            continue
        sub = parser._subparsers._group_actions[0].choices[cmd]
        opts = {o for a in sub._actions for o in a.option_strings}
        if flag not in opts:
            bad.append(f"{cmd} {flag}")
    check("все упомянутые флаги реальны", not bad, str(bad))

    harness.cleanup()
    return summary("Эксплуатация")


if __name__ == "__main__":
    raise SystemExit(main())
