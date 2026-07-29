"""Конфигурация DataForge: переменные окружения + необязательный JSON-файл.

Тот же принцип, что в остальных проектах этого репозитория (agent_system,
multi_agent_system_ontology, erp_ai): секреты — ТОЛЬКО из окружения,
никогда из JSON-файла; `Config.load()` фильтрует `_SECRET_FIELDS` при
чтении файла, `to_dict()` маскирует их звёздочками.

PostgreSQL ОБЯЗАТЕЛЕН (без DB_DSN приложение отказывается стартовать) —
это платформа данных: метаданные каталога, лог качества, MDM и
неизменяемый аудит должны жить в настоящей транзакционной СУБД, а не в
файле или in-memory структуре.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствует обязательный параметр и т.п."""


@dataclass
class Config:
    # --- обязательное хранилище метаданных/каталога/MDM/аудита ---
    db_dsn: str = ""                    # postgresql://user:pass@host:5432/db

    # --- MDM: пороги вероятностного матчинга (см. dataforge/mdm/) ---
    # score >= match_auto_threshold        -> автоматическое слияние в Gold
    # match_review_threshold <= score <     -> stewardship-очередь (человек)
    #                            auto_threshold
    # score < match_review_threshold       -> не считается совпадением
    match_auto_threshold: float = 0.92
    match_review_threshold: float = 0.65

    # --- Quality Engine ---
    # Правила с severity="error" переводят запись в карантин вместо
    # продвижения в Silver; severity="warning" — запись продвигается, но
    # нарушение фиксируется в quality_result для отчёта.
    quality_default_severity: str = "error"

    # --- интеграция с 1С через OData (см. dataforge/connectors/onec_odata.py)
    onec_base_url: str = ""
    onec_api_key: str = ""
    onec_timeout: int = 30

    # --- AI Copilot (ТЗ §3.6, K6) — LLM через OpenAI-совместимый протокол.
    # Без настройки (llm_base_url пуст) Copilot отвечает явной ошибкой,
    # а НЕ имитирует ответ — модуль полностью отключаем без влияния на
    # ядро платформы (ТЗ: "Модуль полностью отключаем без влияния на ядро").
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 60

    # --- HTTP API/дашборд ---
    host: str = "127.0.0.1"
    port: int = 8200
    api_token: str = ""                 # обязателен, если host не 127.0.0.1

    #: поля-секреты: разрешены ТОЛЬКО из переменных окружения
    _SECRET_FIELDS = ("api_token", "onec_api_key", "llm_api_key")

    def __post_init__(self) -> None:
        if not self.db_dsn:
            self.db_dsn = os.getenv("DB_DSN", "")

    def require_dsn(self) -> str:
        if not self.db_dsn:
            raise ConfigError(
                "DB_DSN не задан. DataForge не работает без PostgreSQL — "
                "укажите переменную окружения DB_DSN или поле db_dsn, "
                "например postgresql://forge:forge@localhost:5432/dataforge."
            )
        return self.db_dsn

    @classmethod
    def load(cls, path: str | None = None, **overrides: Any) -> "Config":
        data: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Конфиг {path} не найден")
            data = json.loads(p.read_text(encoding="utf-8"))
        data = {k: v for k, v in data.items()
               if v is not None and not k.startswith("_")
               and k not in cls._SECRET_FIELDS}
        cfg = cls(**data)

        env_map = {
            "db_dsn": "DB_DSN",
            "onec_base_url": "ONEC_BASE_URL",
            "onec_api_key": "ONEC_API_KEY",
            "llm_base_url": "FORGE_LLM_BASE_URL",
            "llm_api_key": "FORGE_LLM_API_KEY",
            "llm_model": "FORGE_LLM_MODEL",
            "host": "FORGE_HOST",
            "api_token": "FORGE_API_TOKEN",
            "quality_default_severity": "QUALITY_DEFAULT_SEVERITY",
        }
        for field_name, env_name in env_map.items():
            val = os.getenv(env_name)
            if val:
                setattr(cfg, field_name, val)
        if os.getenv("FORGE_PORT"):
            cfg.port = int(os.environ["FORGE_PORT"])
        if os.getenv("ONEC_TIMEOUT"):
            cfg.onec_timeout = int(os.environ["ONEC_TIMEOUT"])
        if os.getenv("FORGE_LLM_TIMEOUT"):
            cfg.llm_timeout = int(os.environ["FORGE_LLM_TIMEOUT"])
        if os.getenv("MATCH_AUTO_THRESHOLD"):
            cfg.match_auto_threshold = float(os.environ["MATCH_AUTO_THRESHOLD"])
        if os.getenv("MATCH_REVIEW_THRESHOLD"):
            cfg.match_review_threshold = float(os.environ["MATCH_REVIEW_THRESHOLD"])

        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("db_dsn"):
            d["db_dsn"] = _mask_dsn(d["db_dsn"])
        for f in self._SECRET_FIELDS:
            if d.get(f):
                d[f] = "***"
        return d


def _mask_dsn(dsn: str) -> str:
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, _, hostpart = rest.partition("@")
    if ":" not in creds:
        return dsn
    user, _, _pwd = creds.partition(":")
    return f"{scheme}://{user}:***@{hostpart}"
