"""Конфигурация MAOS: файл JSON (необязательный) + переменные окружения.

Приоритет: переменные окружения > файл > умолчания. Секреты (API-ключи)
только в окружении — см. .env.example, никогда в JSON.

В отличие от agent_system (SQLite по умолчанию, Postgres — опция),
здесь PostgreSQL + pgvector ОБЯЗАТЕЛЕН: вся модель агентов, память трёх
уровней и граф онтологии живут в одной базе, разделяемой несколькими
процессами (веб, API, фоновый maintenance-сервис). Без DB_DSN
приложение отказывается стартовать с понятной ошибкой, а не тихо
переключается на локальный файл.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствует обязательный параметр и т.п."""


@dataclass
class Config:
    # --- обязательное хранилище ---
    db_dsn: str = ""                    # postgresql://user:pass@host:5432/db

    # --- гибридный LLM-роутинг (см. maos/llm/registry.py: provider::model) ---
    default_local_model: str = "local::llama3"
    default_cloud_model: str = "openrouter::openai/gpt-4o-mini"
    # Порог "сложности" задачи (по длине текста запроса в символах), выше
    # которого оркестратор предпочитает облачную модель локальной — грубая,
    # но детерминированная и бесплатная эвристика без обращения к LLM.
    complexity_char_threshold: int = 600
    # Если это True — при любой ошибке облачного провайдера (сеть, лимиты,
    # 5xx) оркестратор автоматически переключается на default_local_model
    # вместо того, чтобы вернуть агенту ошибку.
    fallback_to_local: bool = True
    # Повторы отдельного HTTP-вызова к LLM ДО того, как HybridLLM решит
    # переключиться на fallback. Умеренные значения по умолчанию — чтобы
    # разовый сбой сети не приводил к преждевременному фолбэку, но и не
    # заставлял пользователя ждать минуту, прежде чем сработает откат на
    # локальную модель.
    llm_retries: int = 2
    llm_retry_base: float = 1.0

    # --- эмбеддинги (векторная mid/long-term память) ---
    # provider: hash (офлайн по умолчанию) | openai | local | lmstudio.
    # lmstudio — синоним local в реестре эмбеддингов (тот же протокол
    # /v1/embeddings, что и OpenAI) — используйте, если хотите явно
    # обозначить в конфиге, что модель развёрнута в LM Studio.
    embedding_provider: str = "hash"    # hash | openai | local | lmstudio
    embedding_model: str = "hash-256"
    embedding_dim: int = 256            # фиксирует vector(dim) в схеме БД
    # Адрес и ключ ВНЕШНЕГО сервера эмбеддингов (например, LM Studio на
    # другой машине: http://192.168.1.50:1234/v1). Если не заданы —
    # берутся умолчания провайдера (для "local"/"lmstudio" — переменная
    # окружения LOCAL_BASE_URL/LOCAL_API_KEY или localhost:11434, для
    # "openai" — OPENAI_BASE_URL/OPENAI_API_KEY). Явное значение здесь
    # ПЕРЕБИВАЕТ то, что использует основная чат-модель — эмбеддинги и
    # диалоговая модель нередко живут на разных серверах.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    # Таймаут HTTP-запроса к серверу эмбеддингов, секунды. LM Studio на
    # слабом железе может считать эмбеддинги ощутимо дольше, чем облачный
    # API — отдельная настройка вместо жёстко зашитых 60 с в драйвере.
    embedding_timeout: int = 60

    # --- трёхуровневая память ---
    # Порог контекстного окна модели, ниже которого включается агрессивная
    # суммаризация истории диалога (short-term), см. ТЗ п.4.
    small_context_window: int = 4096
    # Сколько последних сообщений диалога держим как есть перед суммаризацией.
    short_term_keep_last: int = 6
    # Сколько mid-term квантов памяти подмешивать в контекст по векторному
    # сходству с текущим запросом.
    mid_term_top_k: int = 5
    mid_term_min_score: float = 0.15

    # --- фоновое обслуживание (maintenance service) ---
    maintenance_interval_seconds: int = 300
    maintenance_distill_after_messages: int = 20  # диалог этой длины -> квант
    maintenance_dedup_similarity: float = 0.97     # выше — считать дублем

    # --- TTS (реальный клиент OmniVoice, см. maos/tts/) ---
    tts_provider: str = "none"          # none | omnivoice | openai | elevenlabs | piper
    tts_default_voice: str = ""
    # Адрес и ключ сервера OmniVoice (или совместимого) — по тому же
    # принципу, что embedding_base_url: секрет только из окружения.
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_timeout: int = 60
    tts_audio_format: str = "mp3"       # mp3 | wav | ogg | opus | flac

    # --- инструменты MAOS-агентов (опционально, см. agent.tools в БД) ---
    # Рабочая папка КАЖДОГО агента — своя подпапка workspace_root/<slug>,
    # изолированная так же строго, как agent_system/agent/tools/base.py:
    # Workspace (нельзя выйти за пределы через '..'/симлинки/абсолютный путь).
    workspace_root: str = "./workspace"
    # Предел шагов инструментального цикла ОДНОГО хода диалога (не всей
    # сессии) — без него агент со сломанной моделью мог бы звать
    # инструменты бесконечно на каждое сообщение пользователя.
    max_tool_steps: int = 8
    # Обрезка результата инструмента при добавлении в историю — то же
    # решение, что agent_system/agent/core.py: голова и хвост
    # информативны, середина длинного вывода обычно нет.
    tool_result_limit: int = 4000

    # --- навык "web" (веб-поиск/загрузка страниц без MCP) ---
    web_backend: str = "duckduckgo_lite"   # duckduckgo_lite | duckduckgo_html | searxng
    web_search_base_url: str = ""          # обязателен для backend=searxng
    web_timeout: int = 15
    web_rate_limit: float = 1.0
    web_max_results: int = 8
    web_allow_local: bool = False          # только для интранет-сценариев/тестов

    # --- внешние MCP-серверы ---
    # Несекретная конфигурация серверов из JSON-конфига либо JSON в
    # MAOS_MCP_SERVERS. Токены/Authorization-заголовки задавайте только
    # через MAOS_MCP_SERVERS в .env, а не в файле, попадающем в git.
    mcp_servers: dict[str, Any] | None = None

    # --- уведомления и доставка артефактов (навык messaging) ---
    # Пароли и токены загружаются исключительно из .env. Значения портов,
    # TLS и адрес отправителя можно хранить в JSON, если это удобно.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True
    smtp_from_addr: str = ""
    max_bot_token: str = ""
    max_api_base: str = "https://platform-api2.max.ru"
    messaging_confirm_sends: bool = True

    # --- HTTP API/веб ---
    host: str = "127.0.0.1"
    port: int = 8090
    api_token: str = ""                 # обязателен, если host не 127.0.0.1

    def __post_init__(self) -> None:
        if not self.db_dsn:
            self.db_dsn = os.getenv("DB_DSN", "")

    def require_dsn(self) -> str:
        if not self.db_dsn:
            raise ConfigError(
                "DB_DSN не задан. MAOS не работает без PostgreSQL+pgvector "
                "(см. ТЗ п.1 и п.8). Укажите переменную окружения DB_DSN "
                "или поле db_dsn в конфиге, например "
                "postgresql://maos:maos@localhost:5432/maos."
            )
        return self.db_dsn

    def resolve_embedding(self) -> tuple[str, str, str | None, str | None, int]:
        """Провайдер/модель/base_url/ключ/таймаут для эмбеддингов.

        Явно заданный embedding_base_url (обычно — адрес ВНЕШНЕГО сервера
        вроде LM Studio, отдельного от диалоговой модели) передаётся как
        есть; пустая строка превращается в None, чтобы build_embedder()
        применил умолчание провайдера (переменные окружения
        LOCAL_BASE_URL/OPENAI_BASE_URL и т.п., см. maos/llm/embeddings.py).
        """
        return (self.embedding_provider, self.embedding_model,
               self.embedding_base_url or None, self.embedding_api_key or None,
               self.embedding_timeout)

    def resolve_web_config(self):
        """WebConfig (maos/tools/web.py) из полей web_* этого конфига."""
        from .tools.web import WebConfig
        return WebConfig(
            backend=self.web_backend, search_base_url=self.web_search_base_url,
            timeout=self.web_timeout, rate_limit=self.web_rate_limit,
            max_results=self.web_max_results, allow_local=self.web_allow_local)

    #: поля-секреты: разрешены ТОЛЬКО из переменных окружения, никогда
    #: из JSON-конфига — тот же принцип, что у messaging.* в agent_system
    #: (MessagingConfig.from_dict фильтрует пароли/токены). Конфиг можно
    #: класть в git, не боясь утечки ключа внешнего сервера эмбеддингов.
    _SECRET_FIELDS = ("api_token", "embedding_api_key", "tts_api_key", "smtp_host", "smtp_user", "smtp_password", "max_bot_token")

    @classmethod
    def load(cls, path: str | None = None, **overrides: Any) -> "Config":
        data: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Конфиг {path} не найден")
            data = json.loads(p.read_text(encoding="utf-8"))
        # ключи-комментарии с префиксом "_" игнорируются, как в agent_system;
        # поля-секреты из JSON тоже отбрасываются — только из окружения.
        data = {k: v for k, v in data.items()
               if v is not None and not k.startswith("_")
               and k not in cls._SECRET_FIELDS}
        cfg = cls(**data)

        env_map = {
            "db_dsn": "DB_DSN",
            "default_local_model": "DEFAULT_LOCAL_MODEL",
            "default_cloud_model": "DEFAULT_CLOUD_MODEL",
            "embedding_provider": "MAOS_EMBEDDING_PROVIDER",
            "embedding_model": "MAOS_EMBEDDING_MODEL",
            "embedding_base_url": "MAOS_EMBEDDING_BASE_URL",
            "embedding_api_key": "MAOS_EMBEDDING_API_KEY",
            "tts_provider": "TTS_PROVIDER",
            "tts_default_voice": "TTS_DEFAULT_VOICE",
            "tts_base_url": "TTS_BASE_URL",
            "tts_api_key": "TTS_API_KEY",
            "tts_audio_format": "TTS_AUDIO_FORMAT",
            "workspace_root": "MAOS_WORKSPACE_ROOT",
            "web_search_base_url": "MAOS_WEB_SEARCH_BASE_URL",
            "smtp_host": "SMTP_HOST",
            "smtp_user": "SMTP_USER",
            "smtp_password": "SMTP_PASSWORD",
            "smtp_from_addr": "SMTP_FROM_ADDR",
            "max_bot_token": "MAX_BOT_TOKEN",
            "max_api_base": "MAX_API_BASE",
            "host": "MAOS_HOST",
            "api_token": "MAOS_API_TOKEN",
        }
        for field_name, env_name in env_map.items():
            val = os.getenv(env_name)
            if val:
                setattr(cfg, field_name, val)
        raw_mcp = os.getenv("MAOS_MCP_SERVERS")
        if raw_mcp:
            try:
                parsed_mcp = json.loads(raw_mcp)
            except json.JSONDecodeError as exc:
                raise ConfigError("MAOS_MCP_SERVERS должен содержать JSON") from exc
            if not isinstance(parsed_mcp, dict):
                raise ConfigError("MAOS_MCP_SERVERS должен быть JSON-объектом серверов")
            cfg.mcp_servers = parsed_mcp.get("mcpServers", parsed_mcp)
        if os.getenv("SMTP_PORT"):
            cfg.smtp_port = int(os.environ["SMTP_PORT"])
        for field_name, env_name in (("smtp_use_ssl", "SMTP_USE_SSL"),
                                     ("smtp_starttls", "SMTP_STARTTLS"),
                                     ("messaging_confirm_sends", "MAOS_CONFIRM_SENDS")):
            if os.getenv(env_name):
                setattr(cfg, field_name, os.environ[env_name].strip().lower() in ("1", "true", "yes", "on"))
        if os.getenv("MAOS_PORT"):
            cfg.port = int(os.environ["MAOS_PORT"])
        if os.getenv("MAOS_EMBEDDING_DIM"):
            cfg.embedding_dim = int(os.environ["MAOS_EMBEDDING_DIM"])
        if os.getenv("MAOS_EMBEDDING_TIMEOUT"):
            cfg.embedding_timeout = int(os.environ["MAOS_EMBEDDING_TIMEOUT"])
        if os.getenv("TTS_TIMEOUT"):
            cfg.tts_timeout = int(os.environ["TTS_TIMEOUT"])

        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d.get("db_dsn"):
            d["db_dsn"] = _mask_dsn(d["db_dsn"])
        if d.get("api_token"):
            d["api_token"] = "***"
        if d.get("embedding_api_key"):
            d["embedding_api_key"] = "***"
        if d.get("tts_api_key"):
            d["tts_api_key"] = "***"
        for field_name in ("smtp_host", "smtp_user", "smtp_password", "max_bot_token"):
            if d.get(field_name):
                d[field_name] = "***"
        if d.get("mcp_servers"):
            d["mcp_servers"] = "***"
        return d


def _mask_dsn(dsn: str) -> str:
    """Маскирует пароль в DSN для логов/API — postgresql://user:***@host/db."""
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, _, hostpart = rest.partition("@")
    if ":" not in creds:
        return dsn
    user, _, _pwd = creds.partition(":")
    return f"{scheme}://{user}:***@{hostpart}"
