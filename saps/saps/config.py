"""Конфигурация САПС: умолчания -> JSON-файл -> переменные окружения.

Приоритет именно такой. Секреты (пароль Teamcenter, ключ LLM, токен API)
читаются ТОЛЬКО из окружения: JSON-конфиг лежит рядом с кодом, его
коммитят, и пароль от промышленного PDM в репозитории — инцидент
безопасности, а не мелочь. `Config.load()` демонстративно сообщает, что
секретное поле из файла проигнорировано, а `to_dict()` их маскирует.

Database-First (ТЗ п.2.3): без DSN приложение не стартует. Тихо
переключиться на SQLite было бы худшим решением из возможных —
прослеживаемость требований к сертификации не может зависеть от того,
что кто-то забыл переменную окружения.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

#: Поля, которые НИКОГДА не читаются из JSON-файла.
SECRET_FIELDS = frozenset({"tc_password", "llm_api_key", "api_token",
                           "embedding_api_key"})


class ConfigError(RuntimeError):
    """Ошибка конфигурации: нет обязательного параметра, плохое значение."""


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} — ожидалось целое число") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} — ожидалось число") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass
class Config:
    # --- Database-First: PostgreSQL обязателен (ТЗ п.2.3, п.4) ---------
    db_dsn: str = ""
    #: Схема БД. Отдельный параметр, потому что САПС часто ставят в
    #: общую корпоративную базу рядом с другими системами.
    db_schema: str = "saps"

    # --- Teamcenter (ТЗ п.3.1 режим А) ---------------------------------
    tc_url: str = ""                     # http://org-tc2:8080/tc
    tc_user: str = ""
    tc_password: str = ""
    tc_group: str = ""
    tc_role: str = ""
    tc_locale: str = ""
    tc_timeout: int = 60
    #: Запись обратно в Teamcenter (ТЗ п.5, Этап 3). Выключена по
    #: умолчанию НАМЕРЕННО: обратная запись меняет данные в промышленном
    #: PDM, и включать её должен человек осознанно, а не по умолчанию.
    tc_write_enabled: bool = False

    # --- LLM для агентского слоя (ТЗ п.3.2) ----------------------------
    #: openai_like — любой OpenAI-совместимый /v1; stub — офлайн-провайдер
    #: для тестов и демо; none — агенты, требующие LLM, откажутся работать
    #: с понятным сообщением вместо тихой выдачи мусора.
    llm_provider: str = "none"
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_temperature: float = 0.0         # сертификация: воспроизводимость
    llm_timeout: int = 120
    llm_retries: int = 2

    # --- эмбеддинги для семантического поиска (ТЗ п.4: pgvector) -------
    #: hash — офлайн-эмбеддер без сети (работает всегда, не понимает
    #: синонимы); openai/local — внешний сервер /v1/embeddings.
    embedding_provider: str = "hash"
    embedding_model: str = "hash-512"
    embedding_dim: int = 512             # фиксирует vector(dim) в схеме
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_timeout: int = 60
    #: Сколько текстов слать одним запросом к внешней модели. Справочник
    #: АП — сотни пунктов; по запросу на пункт индексация занимает минуты.
    embedding_batch: int = 32

    # --- пороги качества требований (ТЗ п.3.2, Агент-Редактор) ---------
    #: Ниже этой оценки требование помечается как проблемное в индикаторе
    #: «здоровья сертификации».
    quality_min_score: float = 0.7
    #: Порог косинусного сходства, выше которого Агент-Классификатор
    #: предлагает пункт авиационных правил. Ниже — «не уверен», и это
    #: честнее, чем навязать инженеру случайный пункт АП-25.
    #:
    #: ВАЖНО ПРО ШКАЛУ. Значение по умолчанию рассчитано на СЕМАНТИЧЕСКИЕ
    #: эмбеддинги (openai/local), где перефразировка даёт 0.8+. У
    #: офлайн-эмбеддера hash шкала другая: он сравнивает мешки слов, и
    #: правильное попадание там обычно 0.25–0.45 (измерено на встроенном
    #: справочнике АП). Один и тот же порог на двух шкалах означал бы,
    #: что на hash классификатор молчит всегда — поэтому порог
    #: пересчитывается провайдером, см. effective_classify_min().
    classify_min_score: float = 0.55
    #: Порог для hash-эмбеддера. Отдельным полем, а не «магическим
    #: коэффициентом» в коде: администратор должен видеть обе цифры и
    #: иметь возможность подстроить их под свой справочник правил.
    classify_min_score_hash: float = 0.22
    #: Сколько кандидатов показывать инженеру.
    classify_top_k: int = 5

    # --- HTTP API/дашборд (ТЗ п.3.3) -----------------------------------
    host: str = "127.0.0.1"
    port: int = 8090
    api_token: str = ""                  # обязателен, если host != localhost

    # --- рабочие каталоги ------------------------------------------------
    #: Куда складывать выгрузки (ТЗ п.6.3) и откуда брать файлы импорта.
    workdir: str = "workdir"

    def __post_init__(self) -> None:
        if not self.db_dsn:
            self.db_dsn = os.getenv("SAPS_DB_DSN", "") or os.getenv("DB_DSN", "")

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        raw_path = str(path) if path else _env_str("SAPS_CONFIG", "")
        if raw_path.strip():
            file_path = Path(raw_path).expanduser()
            if not file_path.is_file():
                if path:
                    raise ConfigError(f"Файл конфигурации не найден: {file_path}")
            else:
                cfg._apply_file(file_path)
        cfg._apply_env()
        cfg.validate()
        return cfg

    def _apply_file(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: не разбирается как JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: ожидался объект JSON верхнего уровня")
        known = {f.name for f in fields(self)}
        for key, value in data.items():
            if key in SECRET_FIELDS:
                # Не молчим: иначе человек будет часами искать, почему
                # система ходит в Teamcenter без пароля.
                print(f"[saps] {path}: поле {key!r} проигнорировано — "
                      f"секреты берутся только из переменных окружения")
                continue
            if key not in known:
                raise ConfigError(f"{path}: неизвестный параметр {key!r}")
            setattr(self, key, value)

    def _apply_env(self) -> None:
        self.db_dsn = _env_str("SAPS_DB_DSN", self.db_dsn) or _env_str("DB_DSN", "")
        self.db_schema = _env_str("SAPS_DB_SCHEMA", self.db_schema)

        self.tc_url = _env_str("SAPS_TC_URL", self.tc_url)
        self.tc_user = _env_str("SAPS_TC_USER", self.tc_user)
        self.tc_password = _env_str("SAPS_TC_PASSWORD", self.tc_password)
        self.tc_group = _env_str("SAPS_TC_GROUP", self.tc_group)
        self.tc_role = _env_str("SAPS_TC_ROLE", self.tc_role)
        self.tc_locale = _env_str("SAPS_TC_LOCALE", self.tc_locale)
        self.tc_timeout = _env_int("SAPS_TC_TIMEOUT", self.tc_timeout)
        self.tc_write_enabled = _env_bool("SAPS_TC_WRITE", self.tc_write_enabled)

        self.llm_provider = _env_str("SAPS_LLM_PROVIDER", self.llm_provider)
        self.llm_model = _env_str("SAPS_LLM_MODEL", self.llm_model)
        self.llm_base_url = _env_str("SAPS_LLM_BASE_URL", self.llm_base_url)
        self.llm_api_key = (_env_str("SAPS_LLM_API_KEY", self.llm_api_key)
                            or _env_str("OPENAI_API_KEY", ""))
        self.llm_temperature = _env_float("SAPS_LLM_TEMPERATURE",
                                          self.llm_temperature)
        self.llm_timeout = _env_int("SAPS_LLM_TIMEOUT", self.llm_timeout)
        self.llm_retries = _env_int("SAPS_LLM_RETRIES", self.llm_retries)

        self.embedding_provider = _env_str("SAPS_EMBEDDING_PROVIDER",
                                           self.embedding_provider)
        self.embedding_model = _env_str("SAPS_EMBEDDING_MODEL",
                                        self.embedding_model)
        self.embedding_dim = _env_int("SAPS_EMBEDDING_DIM", self.embedding_dim)
        self.embedding_base_url = _env_str("SAPS_EMBEDDING_BASE_URL",
                                           self.embedding_base_url)
        self.embedding_api_key = _env_str("SAPS_EMBEDDING_API_KEY",
                                          self.embedding_api_key)
        self.embedding_timeout = _env_int("SAPS_EMBEDDING_TIMEOUT",
                                          self.embedding_timeout)
        self.embedding_batch = _env_int("SAPS_EMBEDDING_BATCH",
                                        self.embedding_batch)

        self.quality_min_score = _env_float("SAPS_QUALITY_MIN",
                                            self.quality_min_score)
        self.classify_min_score = _env_float("SAPS_CLASSIFY_MIN",
                                             self.classify_min_score)
        self.classify_min_score_hash = _env_float(
            "SAPS_CLASSIFY_MIN_HASH", self.classify_min_score_hash)
        self.classify_top_k = _env_int("SAPS_CLASSIFY_TOP_K",
                                       self.classify_top_k)

        self.host = _env_str("SAPS_HOST", self.host)
        self.port = _env_int("SAPS_PORT", self.port)
        self.api_token = _env_str("SAPS_API_TOKEN", self.api_token)
        self.workdir = _env_str("SAPS_WORKDIR", self.workdir)

    def validate(self) -> None:
        if not 0.0 <= self.quality_min_score <= 1.0:
            raise ConfigError("quality_min_score должен лежать в [0..1]")
        if not 0.0 <= self.classify_min_score <= 1.0:
            raise ConfigError("classify_min_score должен лежать в [0..1]")
        if not 0.0 <= self.classify_min_score_hash <= 1.0:
            raise ConfigError("classify_min_score_hash должен лежать в [0..1]")
        if self.classify_top_k < 1:
            raise ConfigError("classify_top_k должен быть >= 1")
        if self.embedding_dim < 8:
            raise ConfigError("embedding_dim слишком мал (минимум 8)")
        if self.embedding_batch < 1:
            raise ConfigError("embedding_batch должен быть >= 1")
        if self.uses_external_embeddings() and not self.embedding_model:
            raise ConfigError(
                "Для внешней модели эмбеддингов нужно имя модели "
                "(SAPS_EMBEDDING_MODEL) — сервер должен знать, что грузить.")
        if self.host not in ("127.0.0.1", "localhost") and not self.api_token:
            raise ConfigError(
                f"host={self.host!r} — не localhost. Задайте SAPS_API_TOKEN: "
                "САПС отдаёт данные сертификации и умеет писать в Teamcenter, "
                "открытый порт без авторизации недопустим.")
        if self.tc_write_enabled and not self.tc_url:
            raise ConfigError(
                "tc_write_enabled=true, но не задан tc_url — некуда писать")

    def require_dsn(self) -> str:
        """DSN или понятный отказ. Database-First — не пожелание."""
        if not self.db_dsn:
            raise ConfigError(
                "Не задан SAPS_DB_DSN. САПС не работает без PostgreSQL: вся "
                "прослеживаемость требований (источник, версии, связь с "
                "доказательствами) живёт в схеме БД. Пример: "
                "SAPS_DB_DSN=postgresql://saps:saps@localhost:5432/saps")
        return self.db_dsn

    def uses_external_embeddings(self) -> bool:
        """Работает ли система с внешней (сетевой) моделью эмбеддингов."""
        from .llm.embeddings import is_external
        return is_external(self.embedding_provider)

    def effective_classify_min(self) -> float:
        """Порог классификатора с поправкой на шкалу эмбеддера.

        hash-эмбеддер сравнивает пересечение мешков слов, и его косинус
        физически не достигает значений семантической модели: у верного
        попадания это 0.25–0.45, а не 0.8. Применять к нему порог 0.55
        значит выключить классификатор молча — худший вид поломки,
        потому что выглядит как «агент ничего не нашёл».
        """
        if (self.embedding_provider or "hash").strip().lower() in (
                "hash", "offline", ""):
            return self.classify_min_score_hash
        return self.classify_min_score

    def require_tc(self) -> None:
        missing = [n for n, v in (("tc_url", self.tc_url),
                                  ("tc_user", self.tc_user),
                                  ("tc_password", self.tc_password)) if not v]
        if missing:
            raise ConfigError(
                f"Для работы с Teamcenter не хватает: {', '.join(missing)}. "
                "Пароль задаётся только через SAPS_TC_PASSWORD.")

    # ------------------------------------------------------------------
    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if mask_secrets:
            for key in SECRET_FIELDS:
                if data.get(key):
                    data[key] = "***"
            # DSN содержит пароль — маскируем его часть, оставляя хост:
            # инженеру важно видеть, к какой базе он подключён.
            data["db_dsn"] = mask_dsn(data.get("db_dsn", ""))
        return data

    def describe(self) -> str:
        return (
            f"САПС {_version()}\n"
            f"  база:        {mask_dsn(self.db_dsn) or '— НЕ ЗАДАНА'}"
            f" (схема {self.db_schema})\n"
            f"  Teamcenter:  {self.tc_url or '— не настроен'}"
            f"{' [запись разрешена]' if self.tc_write_enabled else ' [чтение]'}\n"
            f"  LLM:         {self.llm_provider}:{self.llm_model}\n"
            f"  эмбеддинги:  {self.embedding_provider}:{self.embedding_model} "
            f"(dim={self.embedding_dim})\n"
            f"  пороги:      качество>={self.quality_min_score}, "
            f"классификация>={self.classify_min_score}"
        )


def mask_dsn(dsn: str) -> str:
    """Спрятать пароль в строке подключения, сохранив остальное читаемым."""
    if not dsn or "@" not in dsn:
        return dsn
    head, _, tail = dsn.rpartition("@")
    if "//" not in head:
        return dsn
    scheme, _, creds = head.partition("//")
    user = creds.split(":", 1)[0]
    return f"{scheme}//{user}:***@{tail}"


def _version() -> str:
    from . import __version__
    return __version__
