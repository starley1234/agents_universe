"""Тесты dataforge.connectors.factory.build_connector: секреты только из
переменных окружения (никогда не хранятся в config источника), выбор
типа коннектора по `kind`, понятные ошибки при неполном конфиге.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataforge.connectors.base import ConnectorError                 # noqa: E402
from dataforge.connectors.factory import build_connector              # noqa: E402
from dataforge.connectors.files import FileConnector                  # noqa: E402
from dataforge.connectors.onec_odata import OneCODataConnector        # noqa: E402
from dataforge.connectors.sql import SqlConnector                     # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def main() -> int:
    section("build_connector: kind='file'")
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w")
    tmp.write("a,b\n1,2\n")
    tmp.close()
    source = {"kind": "file", "config": {"path": tmp.name}}
    conn = build_connector(source)
    check("возвращён FileConnector", isinstance(conn, FileConnector))
    os.unlink(tmp.name)

    section("build_connector: kind='file' без config.path -> ConnectorError")
    try:
        build_connector({"kind": "file", "config": {}})
        check("отсутствие path -> ConnectorError", False)
    except ConnectorError:
        check("отсутствие path -> ConnectorError", True)

    section("build_connector: kind='sql' — секрет ТОЛЬКО из окружения")
    os.environ.pop("TEST_FORGE_DSN", None)
    try:
        build_connector({"kind": "sql", "config": {"dsn_env": "TEST_FORGE_DSN",
                                                    "table": "customers"}})
        check("отсутствующая переменная окружения -> ConnectorError", False)
    except ConnectorError as exc:
        check("отсутствующая переменная окружения -> ConnectorError", True)
        check("сообщение называет переменную", "TEST_FORGE_DSN" in str(exc))

    os.environ["TEST_FORGE_DSN"] = "sqlite:///:memory:"
    try:
        conn_sql = build_connector({"kind": "sql", "config": {
            "dsn_env": "TEST_FORGE_DSN", "table": "customers"}})
        check("SqlConnector построен, DSN взят из окружения",
             isinstance(conn_sql, SqlConnector) and conn_sql.dsn == "sqlite:///:memory:")
    finally:
        os.environ.pop("TEST_FORGE_DSN", None)

    section("build_connector: kind='sql' без dsn_env -> ConnectorError "
           "(защита от хранения DSN в конфиге)")
    try:
        build_connector({"kind": "sql", "config": {"table": "x", "dsn": "postgresql://leak"}})
        check("прямой DSN в конфиге игнорируется, требуется dsn_env -> ConnectorError", False)
    except ConnectorError:
        check("прямой DSN в конфиге игнорируется, требуется dsn_env -> ConnectorError", True)

    section("build_connector: kind='sql' без table -> ConnectorError")
    os.environ["TEST_FORGE_DSN2"] = "sqlite:///:memory:"
    try:
        build_connector({"kind": "sql", "config": {"dsn_env": "TEST_FORGE_DSN2"}})
        check("отсутствие table -> ConnectorError", False)
    except ConnectorError:
        check("отсутствие table -> ConnectorError", True)
    finally:
        os.environ.pop("TEST_FORGE_DSN2", None)

    section("build_connector: kind='onec_odata'")
    conn_onec = build_connector({"kind": "onec_odata", "config": {}},
                                onec_base_url="http://onec.local", onec_api_key="tok")
    check("возвращён OneCODataConnector", isinstance(conn_onec, OneCODataConnector))
    check("токен передан", conn_onec.token == "tok")

    section("build_connector: kind='onec_odata' без ONEC_BASE_URL -> ConnectorError")
    try:
        build_connector({"kind": "onec_odata", "config": {}})
        check("без onec_base_url -> ConnectorError", False)
    except ConnectorError:
        check("без onec_base_url -> ConnectorError", True)

    section("build_connector: неизвестный kind -> ConnectorError")
    try:
        build_connector({"kind": "sharepoint_magic", "config": {}})
        check("неизвестный kind -> ConnectorError", False)
    except ConnectorError as exc:
        check("неизвестный kind -> ConnectorError", True)
        check("сообщение перечисляет поддерживаемые типы",
             "file" in str(exc) and "sql" in str(exc) and "onec_odata" in str(exc))

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
