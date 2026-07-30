"""Конфигурация MAOS: файл JSON (необязательный) + переменные окружения.

Приоритет: переменные окружения > файл > умолчания. Секреты (API-ключи)
только в окружении — см. .env.example, никогда в JSON.

PostgreSQL + pgvector ОБЯЗАТЕЛЕН: модель агентов, память трёх уровней
и онтологический граф хранятся в одной БД, разделяемой всеми процессами.
Без DB_DSN приложение отказывается стартовать с явной ошибкой.
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
    # --- 1. Обязательное хранилище ---
    db_dsn: str = ""                    # postgresql://user:pass@host:5432/db

    # --- 2. Гибридный LLM-роутинг (provider::model) ---
    default_local_model: str = "local::llama3"
    default_cloud_model: str = "openrouter::openai/gpt-4o-mini"
    # Порог длины запроса (символы) для выбора облачной модели вместо локальной.
    complexity_char_threshold: int = 600
    # Автоматически переключаться на локальную модель при сбое облака.
    fallback_to_local: bool = True
    # Число повторов HTTP-вызова к LLM перед откатом на резервную модель.
    llm_retries: int = 2
    llm_retry_base: float = 1.0

    # --- 3. Эмбеддинги (векторная mid/long-term память и роутер) ---
    embedding_provider: str = "hash"    # hash | openai | local | lmstudio
    embedding_model: str = "hash-256"
    embedding_dim: int = 256            # размерность вектора vector(dim) в БД
    # Адрес и ключ внешнего сервера эмбеддингов (например, LM Studio / Ollama).
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    # Таймаут HTTP-запроса к серверу эмбеддингов (секунды).
    embedding_timeout: int = 60

    # --- 4. Трёхуровневая память и Graph-RAG ---
    # Порог контекстного окна модели для включения суммаризации short-term истории.
    small_context_window: int = 4096
    # Число последних сообщений диалога, сохраняемых дословно перед суммаризацией.
    short_term_keep_last: int = 6
    # Число mid-term квантов памяти, подмешиваемых в контекст по сходству.
    mid_term_top_k: int = 5
    mid_term_min_score: float = 0.15
    # Число long-term сущностей и связей из онтологии (Graph-RAG).
    long_term_top_k: int = 3
    long_term_min_score: float = 0.10
    # Глубина многошагового обхода графа (k-hop traversal: 1=соседи, 2=соседи соседей).
    long_term_max_hops: int = 2

    # --- 5. Фоновое обслуживание ("Deep Thinking") ---
    maintenance_interval_seconds: int = 300
    maintenance_distill_after_messages: int = 20  # длина диалога для дистилляции
    maintenance_dedup_similarity: float = 0.97     # порог сходства для удаления дублей
    maintenance_extract_entities: bool = True      # автоэкстракция сущностей из диалогов

    # --- 6. Голосовой интерфейс (TTS OmniVoice) ---
    tts_provider: str = "none"          # none | omnivoice | openai | elevenlabs | piper
    tts_default_voice: str = ""
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_timeout: int = 60
    tts_audio_format: str = "mp3"       # mp3 | wav | ogg | opus | flac

    # --- 7. Инструменты агентов (files, web, office, mcp, messaging) ---
    # Корневая директория изолированных рабочих папок агентов (<slug>).
    workspace_root: str = "./workspace"
    # Директория долговременных артефактов (отчёты, загрузки, DOCX).
    artifact_root: str = "./artifacts"
    artifact_upload_max_bytes: int = 20_000_000
    # Максимальное число шагов вызова инструментов за один ход диалога.
    max_tool_steps: int = 8
    # Максимальный размер (символы) результата инструмента в истории чата.
    tool_result_limit: int = 4000

    # Навык "web" (веб-поиск и загрузка страниц)
    web_backend: str = "duckduckgo_lite"   # duckduckgo_lite | duckduckgo_html | searxng
    web_search_base_url: str = ""          # обязателен для backend=searxng
    web_timeout: int = 15
    web_rate_limit: float = 1.0
    web_max_results: int = 8
    web_allow_local: bool = False          # разрешить локальные адреса (только для тестов)

    # Настройки внешних MCP-серверов. Секреты задавайте через MAOS_MCP_SERVERS в .env.
    mcp_servers: dict[str, Any] | None = None

    # Настройки почты и мессенджеров (навык messaging). Пароли только из .env.
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

    # --- 8. Мультимодальность (Vision, см. maos/tools/vision.py) ---
    vision_base_url: str = ""           # адрес Vision API (например, https://openrouter.ai/api/v1)
    vision_api_key: str = ""
    vision_model: str = "openrouter::openai/gpt-4o-mini"
    vision_timeout: int = 60

    # --- 9. HTTP-сервер и безопасность ---
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
    _SECRET_FIELDS = ("api_token", "embedding_api_key", "tts_api_key", "vision_api_key", "smtp_host", "smtp_user", "smtp_password", "max_bot_token")

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
            "vision_base_url": "MAOS_VISION_BASE_URL",
            "vision_api_key": "MAOS_VISION_API_KEY",
            "vision_model": "MAOS_VISION_MODEL",
            "tts_provider": "TTS_PROVIDER",
            "tts_default_voice": "TTS_DEFAULT_VOICE",
            "tts_base_url": "TTS_BASE_URL",
            "tts_api_key": "TTS_API_KEY",
            "tts_audio_format": "TTS_AUDIO_FORMAT",
            "workspace_root": "MAOS_WORKSPACE_ROOT",
            "artifact_root": "MAOS_ARTIFACT_ROOT",
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

        int_envs = (
            ("embedding_dim", "MAOS_EMBEDDING_DIM"),
            ("embedding_timeout", "MAOS_EMBEDDING_TIMEOUT"),
            ("vision_timeout", "MAOS_VISION_TIMEOUT"),
            ("tts_timeout", "TTS_TIMEOUT"),
            ("port", "MAOS_PORT"),
            ("small_context_window", "MAOS_SMALL_CONTEXT_WINDOW"),
            ("short_term_keep_last", "MAOS_SHORT_TERM_KEEP_LAST"),
            ("mid_term_top_k", "MAOS_MID_TERM_TOP_K"),
            ("long_term_top_k", "MAOS_LONG_TERM_TOP_K"),
            ("long_term_max_hops", "MAOS_LONG_TERM_MAX_HOPS"),
            ("maintenance_interval_seconds", "MAOS_MAINTENANCE_INTERVAL_SECONDS"),
            ("maintenance_distill_after_messages", "MAOS_MAINTENANCE_DISTILL_AFTER_MESSAGES"),
            ("complexity_char_threshold", "MAOS_COMPLEXITY_CHAR_THRESHOLD"),
            ("artifact_upload_max_bytes", "MAOS_ARTIFACT_UPLOAD_MAX_BYTES"),
            ("smtp_port", "SMTP_PORT"),
        )
        for field_name, env_name in int_envs:
            if os.getenv(env_name):
                try:
                    setattr(cfg, field_name, int(os.environ[env_name]))
                except ValueError:
                    pass

        float_envs = (
            ("mid_term_min_score", "MAOS_MID_TERM_MIN_SCORE"),
            ("long_term_min_score", "MAOS_LONG_TERM_MIN_SCORE"),
            ("maintenance_dedup_similarity", "MAOS_MAINTENANCE_DEDUP_SIMILARITY"),
            ("web_rate_limit", "MAOS_WEB_RATE_LIMIT"),
        )
        for field_name, env_name in float_envs:
            if os.getenv(env_name):
                try:
                    setattr(cfg, field_name, float(os.environ[env_name]))
                except ValueError:
                    pass

        bool_envs = (
            ("smtp_use_ssl", "SMTP_USE_SSL"),
            ("smtp_starttls", "SMTP_STARTTLS"),
            ("messaging_confirm_sends", "MAOS_CONFIRM_SENDS"),
            ("fallback_to_local", "MAOS_FALLBACK_TO_LOCAL"),
            ("maintenance_extract_entities", "MAOS_MAINTENANCE_EXTRACT_ENTITIES"),
            ("web_allow_local", "MAOS_WEB_ALLOW_LOCAL"),
        )
        for field_name, env_name in bool_envs:
            if os.getenv(env_name):
                setattr(cfg, field_name, os.environ[env_name].strip().lower() in ("1", "true", "yes", "on"))

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
