"""Конфигурация ERP AI: переменные окружения + необязательный JSON-файл.

Приоритет: переменные окружения > файл > умолчания. Секреты (ключи 1С,
LLM) — только в окружении, никогда в JSON-конфиге (тот же принцип, что
в agent_system/multi_agent_system_ontology этого репозитория).

PostgreSQL ОБЯЗАТЕЛЕН (без DB_DSN приложение отказывается стартовать) —
ERP-данные и неизменяемый журнал аудита должны жить в настоящей
транзакционной СУБД, а не в файле.
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
    # --- обязательное хранилище ---
    db_dsn: str = ""                    # postgresql://user:pass@host:5432/db

    # --- guardrails агента-снабженца (см. app/agents/procurement.py) ---
    # Сумма заказа выше этого порога НИКОГДА не может быть проведена
    # автоматически (Auto-with-review/Full-auto) — принудительно
    # понижается до Draft, независимо от настроенного режима автономности.
    procurement_max_auto_amount: float = 50_000.0
    # Режим автономности по умолчанию для новых предложений агента.
    procurement_default_autonomy: str = "draft"

    # --- интеграция с 1С (EnterpriseData-подобный протокол) ---
    onec_base_url: str = ""
    onec_api_key: str = ""
    onec_timeout: int = 30
    # Мастер-система для каждого справочника: чьи данные побеждают при
    # конфликте (обе стороны изменили запись после последней синхронизации).
    onec_master_counterparty: str = "erp"     # erp | 1c
    onec_master_nomenclature: str = "erp"     # erp | 1c

    # --- опциональный LLM для более естественной формулировки объяснений
    # предложений агента (см. app/agents/narrator.py). Без настройки —
    # используется детерминированный текст объяснения (не хуже, только
    # менее "гладкий"): агент никогда не должен зависеть от LLM для
    # базовой работы guardrails/audit/confirmation gates.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 30

    # --- HTTP API/дашборд ---
    host: str = "127.0.0.1"
    port: int = 8100
    api_token: str = ""                 # обязателен, если host не 127.0.0.1

    #: поля-секреты: разрешены ТОЛЬКО из переменных окружения
    _SECRET_FIELDS = ("api_token", "onec_api_key", "llm_api_key")

    def __post_init__(self) -> None:
        if not self.db_dsn:
            self.db_dsn = os.getenv("DB_DSN", "")

    def require_dsn(self) -> str:
        if not self.db_dsn:
            raise ConfigError(
                "DB_DSN не задан. ERP AI не работает без PostgreSQL — "
                "укажите переменную окружения DB_DSN или поле db_dsn, "
                "например postgresql://erp:erp@localhost:5432/erp_ai."
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
            "onec_master_counterparty": "ONEC_MASTER_COUNTERPARTY",
            "onec_master_nomenclature": "ONEC_MASTER_NOMENCLATURE",
            "llm_base_url": "ERP_LLM_BASE_URL",
            "llm_api_key": "ERP_LLM_API_KEY",
            "llm_model": "ERP_LLM_MODEL",
            "host": "ERP_HOST",
            "api_token": "ERP_API_TOKEN",
            "procurement_default_autonomy": "PROCUREMENT_DEFAULT_AUTONOMY",
        }
        for field_name, env_name in env_map.items():
            val = os.getenv(env_name)
            if val:
                setattr(cfg, field_name, val)
        if os.getenv("ERP_PORT"):
            cfg.port = int(os.environ["ERP_PORT"])
        if os.getenv("PROCUREMENT_MAX_AUTO_AMOUNT"):
            cfg.procurement_max_auto_amount = float(
                os.environ["PROCUREMENT_MAX_AUTO_AMOUNT"])
        if os.getenv("ONEC_TIMEOUT"):
            cfg.onec_timeout = int(os.environ["ONEC_TIMEOUT"])
        if os.getenv("ERP_LLM_TIMEOUT"):
            cfg.llm_timeout = int(os.environ["ERP_LLM_TIMEOUT"])

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
