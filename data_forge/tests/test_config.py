"""Тесты dataforge.config.Config: обязательный DB_DSN, маскирование
секретов, переменные окружения, комментарные ключи в JSON.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataforge.config import Config, ConfigError                  # noqa: E402

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
    for var in ("DB_DSN", "ONEC_BASE_URL", "ONEC_API_KEY", "FORGE_API_TOKEN",
               "MATCH_AUTO_THRESHOLD", "MATCH_REVIEW_THRESHOLD"):
        os.environ.pop(var, None)

    section("Config: DB_DSN обязателен")
    cfg = Config.load()
    check("db_dsn пуст без переменной окружения", cfg.db_dsn == "")
    try:
        cfg.require_dsn()
        check("require_dsn кидает ConfigError без DSN", False)
    except ConfigError as exc:
        check("require_dsn кидает ConfigError без DSN", True)
        check("сообщение упоминает DB_DSN", "DB_DSN" in str(exc))

    os.environ["DB_DSN"] = "postgresql://u:p@localhost:5432/forge"
    cfg2 = Config.load()
    check("db_dsn подхватывается из окружения", cfg2.db_dsn.startswith("postgresql://"))
    os.environ.pop("DB_DSN", None)

    section("Config.to_dict: маскирование секретов")
    cfg3 = Config(db_dsn="postgresql://user:secretpass@host:5432/db",
                 api_token="topsecret", onec_api_key="onec-secret")
    d = cfg3.to_dict()
    check("пароль в DSN замаскирован", "secretpass" not in d["db_dsn"])
    check("пользователь в DSN виден", "user" in d["db_dsn"])
    check("api_token замаскирован", d["api_token"] == "***")
    check("onec_api_key замаскирован", d["onec_api_key"] == "***")

    section("Config: комментарные ключи с префиксом _ игнорируются")
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"_comment": "заметка", "port": 9999,
              "match_auto_threshold": 0.99}, tf)
    tf.close()
    try:
        cfg4 = Config.load(tf.name)
        check("поле port применилось", cfg4.port == 9999)
        check("_comment не стал атрибутом", not hasattr(cfg4, "_comment"))
        check("match_auto_threshold применилось",
             cfg4.match_auto_threshold == 0.99)
    finally:
        os.unlink(tf.name)

    section("Config: секрет из JSON НЕ применяется (только из окружения)")
    tf2 = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"api_token": "leaked-from-json",
              "onec_base_url": "http://example.local"}, tf2)
    tf2.close()
    try:
        cfg5 = Config.load(tf2.name)
        check("api_token из JSON НЕ применился", cfg5.api_token == "")
        check("onec_base_url (не секрет) из JSON применился",
             cfg5.onec_base_url == "http://example.local")
    finally:
        os.unlink(tf2.name)

    section("Config: переменные окружения для 1С и порогов MDM")
    os.environ["ONEC_BASE_URL"] = "http://onec.local:8080"
    os.environ["MATCH_AUTO_THRESHOLD"] = "0.95"
    os.environ["MATCH_REVIEW_THRESHOLD"] = "0.6"
    cfg6 = Config.load()
    check("ONEC_BASE_URL подхватился", cfg6.onec_base_url == "http://onec.local:8080")
    check("MATCH_AUTO_THRESHOLD подхватился и приведён к float",
         cfg6.match_auto_threshold == 0.95)
    check("MATCH_REVIEW_THRESHOLD подхватился и приведён к float",
         cfg6.match_review_threshold == 0.6)
    os.environ.pop("ONEC_BASE_URL", None)
    os.environ.pop("MATCH_AUTO_THRESHOLD", None)
    os.environ.pop("MATCH_REVIEW_THRESHOLD", None)

    section("Config: overrides из kwargs (например CLI) перебивают всё остальное")
    cfg7 = Config.load(host="0.0.0.0", port=1234)
    check("host из overrides применился", cfg7.host == "0.0.0.0")
    check("port из overrides применился", cfg7.port == 1234)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
