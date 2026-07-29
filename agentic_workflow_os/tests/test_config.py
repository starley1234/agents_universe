"""Тесты конфигурации: приоритеты, секреты, валидация.

Отдельный набор, потому что конфиг — граница доверия среды: он решает,
куда пускать агентов по сети, можно ли запускать shell и слушать ли
внешний интерфейс. Ошибка здесь стоит дороже, чем в любом промпте.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, check_raises, section, summary          # noqa: E402
from awos.config import Config, ConfigError                        # noqa: E402


def clean_env() -> None:
    for key in list(os.environ):
        if key.startswith("AWOS_"):
            del os.environ[key]


def main() -> int:
    clean_env()

    section("Умолчания")
    cfg = Config()
    check("SQLite по умолчанию", cfg.db_path == "awos.db")
    check("HITL по умолчанию critical", cfg.hitl_mode == "critical")
    check("порог качества 0.7", cfg.min_score == 0.7)
    check("shell выключен по умолчанию", cfg.allow_shell is False)
    check("HTTP запрещён по умолчанию", cfg.http_allow == [])
    check("слушаем только localhost", cfg.host == "127.0.0.1")

    section("Загрузка без файла и без окружения")
    cfg = Config.load()
    check("пустой AWOS_CONFIG не читает текущую папку", cfg.db_path == "awos.db")

    section("Переменные окружения перебивают умолчания")
    os.environ["AWOS_DB"] = "/tmp/x.db"
    os.environ["AWOS_HITL"] = "always"
    os.environ["AWOS_MAX_REVISIONS"] = "5"
    os.environ["AWOS_MIN_SCORE"] = "0.9"
    os.environ["AWOS_ALLOW_SHELL"] = "true"
    os.environ["AWOS_HTTP_ALLOW"] = "api.example.com, files.example.com"
    cfg = Config.load()
    check("AWOS_DB прочитан", cfg.db_path == "/tmp/x.db")
    check("AWOS_HITL прочитан", cfg.hitl_mode == "always")
    check("целое из окружения", cfg.max_revisions == 5)
    check("дробное из окружения", cfg.min_score == 0.9)
    check("булево из окружения", cfg.allow_shell is True)
    check("список хостов разобран",
          cfg.http_allow == ["api.example.com", "files.example.com"],
          str(cfg.http_allow))
    clean_env()

    section("Некорректные значения окружения — понятная ошибка")
    os.environ["AWOS_MAX_REVISIONS"] = "много"
    check_raises("нечисловое целое отвергается", ConfigError, Config.load)
    clean_env()
    os.environ["AWOS_HITL"] = "иногда"
    check_raises("неизвестный режим HITL отвергается", ConfigError, Config.load)
    clean_env()

    section("JSON-файл конфигурации")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cfg.json"
        path.write_text(json.dumps({
            "db_path": "from_file.db", "max_revisions": 4,
            "api_key": "sk-СЕКРЕТ-ИЗ-ФАЙЛА",     # обязан быть проигнорирован
            "api_token": "ТОКЕН-ИЗ-ФАЙЛА",
        }), encoding="utf-8")
        cfg = Config.load(path)
        check("значение из файла применилось", cfg.db_path == "from_file.db")
        check("целое из файла применилось", cfg.max_revisions == 4)
        check("api_key из файла ПРОИГНОРИРОВАН", cfg.api_key == "",
              f"получили {cfg.api_key!r}")
        check("api_token из файла ПРОИГНОРИРОВАН", cfg.api_token == "")

        os.environ["AWOS_DB"] = "from_env.db"
        cfg = Config.load(path)
        check("окружение перебивает файл", cfg.db_path == "from_env.db")
        clean_env()

        bad = Path(tmp) / "bad.json"
        bad.write_text("{не json}", encoding="utf-8")
        check_raises("битый JSON — понятная ошибка", ConfigError, Config.load, bad)

        unknown = Path(tmp) / "unknown.json"
        unknown.write_text(json.dumps({"нет_такого_поля": 1}), encoding="utf-8")
        check_raises("неизвестный параметр отвергается", ConfigError,
                     Config.load, unknown)

        check_raises("несуществующий файл конфига — ошибка", ConfigError,
                     Config.load, Path(tmp) / "нет.json")

    section("Секреты в окружении")
    os.environ["AWOS_API_KEY"] = "sk-из-окружения"
    cfg = Config.load()
    check("ключ из окружения прочитан", cfg.api_key == "sk-из-окружения")
    check("to_dict маскирует ключ", cfg.to_dict()["api_key"] == "***")
    check("to_dict(mask_secrets=False) отдаёт как есть",
          cfg.to_dict(mask_secrets=False)["api_key"] == "sk-из-окружения")
    clean_env()

    os.environ["OPENAI_API_KEY"] = "sk-совместимость"
    cfg = Config.load()
    check("OPENAI_API_KEY подхватывается как запасной вариант",
          cfg.api_key == "sk-совместимость")
    del os.environ["OPENAI_API_KEY"]

    section("Валидация: сеть без токена")
    check_raises("не-localhost без токена отвергается", ConfigError,
                 Config(host="0.0.0.0").validate)
    Config(host="0.0.0.0", api_token="t").validate()
    check("не-localhost с токеном разрешён", True)
    Config(host="127.0.0.1").validate()
    check("localhost без токена разрешён", True)

    section("Валидация остальных полей")
    check_raises("порог качества вне [0..1]", ConfigError,
                 Config(min_score=1.5).validate)
    check_raises("отрицательное число доработок", ConfigError,
                 Config(max_revisions=-1).validate)
    check_raises("отрицательный лимит вызовов", ConfigError,
                 Config(max_tool_steps=-2).validate)

    section("Разбор AWOS_SQL_DATABASES")
    os.environ["AWOS_SQL_DATABASES"] = "crm=/tmp/crm.db,erp=/tmp/erp.db"
    cfg = Config.load()
    check("две базы разобраны",
          cfg.sql_databases == {"crm": "/tmp/crm.db", "erp": "/tmp/erp.db"},
          str(cfg.sql_databases))
    os.environ["AWOS_SQL_DATABASES"] = "битая-строка"
    check_raises("формат без '=' отвергается", ConfigError, Config.load)
    clean_env()

    section("describe() — сводка для человека")
    text = Config(allow_shell=True, http_allow=["a.com"]).describe()
    check("в сводке видно shell", "shell" in text)
    check("в сводке видно http", "http" in text)
    check("в сводке видна политика HITL", "human-in-the-loop" in text)

    return summary("Конфигурация")


if __name__ == "__main__":
    raise SystemExit(main())
