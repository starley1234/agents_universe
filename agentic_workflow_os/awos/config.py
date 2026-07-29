"""Конфигурация среды: умолчания -> JSON-файл -> переменные окружения.

Приоритет именно такой: окружение перебивает файл, файл перебивает
умолчания. Причина обычная для этого репозитория — секреты (ключи API,
токен доступа) живут ТОЛЬКО в окружении и никогда не попадают в JSON,
который человек склонен закоммитить. `Config.load()` демонстративно
выбрасывает секретные поля, если они всё-таки оказались в файле, а
`to_dict()` их маскирует, чтобы `/v1/config` в API можно было отдавать
без риска.

Почему конфиг один на всю среду, а не на агента: AWOS — это платформа.
Агент (профиль) описывает СВОЮ модель и СВОИ инструменты, но правила
среды — сколько доработок разрешено, когда звать человека, куда пускать
HTTP, где рабочая папка — задаёт администратор среды, а не агент. Иначе
профиль, присланный со стороны, смог бы сам себе выписать права.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

#: Поля, которые НИКОГДА не читаются из JSON-файла (только окружение).
SECRET_FIELDS = frozenset({"api_key", "api_token"})

#: Режимы Human-in-the-Loop.
#:   off      — человек не нужен, среда работает автономно;
#:   critical — человека зовут только там, где среда сама сомневается:
#:              контролёр отклонил, доработки исчерпаны, опасный инструмент;
#:   always   — человек утверждает результат каждого шага.
HITL_MODES = ("off", "critical", "always")


class ConfigError(RuntimeError):
    """Ошибка конфигурации: непонятное значение, недостающий параметр."""


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} — ожидалось целое число") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} — ожидалось число") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Config:
    # --- хранилище состояния -------------------------------------------
    # Одна SQLite-база на всю среду: прогоны, доска контекста, чекпоинты,
    # журнал. SQLite выбран сознательно — состояние обязано пережить
    # перезапуск процесса (иначе HITL-пауза бессмысленна), но тащить
    # внешний сервер ради одной машины не нужно. Место, где нужен
    # PostgreSQL, — соседний проект MAOS, у него другая задача.
    db_path: str = "awos.db"
    #: Рабочая папка для файловых инструментов. Всё, что агенты пишут на
    #: диск, обязано остаться внутри неё (см. tools/base.py: Workspace).
    workspace: str = "workspace"

    # --- модель по умолчанию (профиль агента может её перебить) ---------
    # provider: openai_like — любой OpenAI-совместимый /v1 (OpenAI,
    # OpenRouter, LM Studio, llama.cpp server, vLLM, Ollama в режиме /v1);
    # stub — детерминированный офлайн-провайдер для тестов и демо.
    provider: str = "openai_like"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    temperature: float = 0.2
    request_timeout: int = 120
    llm_retries: int = 2
    llm_retry_base: float = 1.0

    # --- цикл качества (Role-Based Collaboration) -----------------------
    #: Сколько раз Исполнитель может переделать работу после Критика,
    #: прежде чем шаг признаётся неудачным (или эскалируется человеку).
    max_revisions: int = 2
    #: Порог оценки Критика [0..1], ниже которого работа возвращается.
    min_score: float = 0.7
    #: Максимум обращений к инструментам внутри одного хода Исполнителя.
    #: Защита от бесконечного цикла «модель просит инструмент — среда
    #: отвечает — модель просит снова».
    max_tool_steps: int = 8
    #: Ограничение на длину результата инструмента, попадающего в
    #: контекст модели. Длинный вывод вытесняет задачу и стоит токенов.
    tool_output_limit: int = 4000

    # --- Human-in-the-Loop ----------------------------------------------
    hitl_mode: str = "critical"          # off | critical | always
    #: Сколько секунд ждать человека в синхронном прогоне (CLI/HTTP),
    #: прежде чем остановить прогон в статусе waiting_human и выйти.
    #: 0 — не ждать вообще (сразу пауза), отрицательное — ждать вечно.
    hitl_wait_seconds: int = 0

    # --- инструменты (capability-гранты) --------------------------------
    #: Хосты, куда инструменту http_request разрешено ходить. Пустой
    #: список = HTTP запрещён целиком. Это гранты СРЕДЫ: профиль агента
    #: может только сузить набор инструментов, но не расширить сеть.
    http_allow: list[str] = field(default_factory=list)
    http_timeout: int = 30
    #: Разрешить инструмент shell. Отдельный флаг, потому что это
    #: единственный инструмент, который может сделать что угодно с
    #: машиной; по умолчанию выключен, и даже включённый требует
    #: подтверждения человека при hitl_mode != off.
    allow_shell: bool = False
    shell_timeout: int = 60
    #: SQLite-базы, доступные инструменту sql_query (только чтение).
    sql_databases: dict[str, str] = field(default_factory=dict)

    # --- HTTP API/консоль ------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8070
    api_token: str = ""                  # обязателен, если host != 127.0.0.1

    # --- каталоги определений ---------------------------------------------
    #: Где искать JSON-описания workflow и профилей агентов. Пусто —
    #: встроенные каталоги пакета (awos/workflows, awos/profiles).
    workflows_dir: str = ""
    profiles_dir: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Собрать конфиг: умолчания -> JSON (если есть) -> окружение."""
        cfg = cls()
        # Пустая строка -> Path("") == Path("."), а точка существует и это
        # каталог: наивная проверка exists() приводила к попытке прочитать
        # текущую папку как JSON. Поэтому решаем по СТРОКЕ, а не по Path.
        raw_path = str(path) if path else _env_str("AWOS_CONFIG", "")
        if raw_path.strip():
            file_path = Path(raw_path).expanduser()
            if not file_path.is_file():
                if path:                    # явно указанный файл обязан быть
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
                # Не «тихо игнорируем»: человек должен узнать, что ключ из
                # файла не действует, иначе будет часами искать, почему
                # среда ходит в API без авторизации.
                print(f"[awos] {path}: поле {key!r} в файле игнорируется — "
                      f"секреты берутся только из переменных окружения")
                continue
            if key not in known:
                raise ConfigError(f"{path}: неизвестный параметр {key!r}")
            setattr(self, key, value)

    def _apply_env(self) -> None:
        self.db_path = _env_str("AWOS_DB", self.db_path)
        self.workspace = _env_str("AWOS_WORKSPACE", self.workspace)

        self.provider = _env_str("AWOS_PROVIDER", self.provider)
        self.model = _env_str("AWOS_MODEL", self.model)
        self.base_url = _env_str("AWOS_BASE_URL", self.base_url)
        self.api_key = _env_str("AWOS_API_KEY", self.api_key) or _env_str(
            "OPENAI_API_KEY", "")
        self.temperature = _env_float("AWOS_TEMPERATURE", self.temperature)
        self.request_timeout = _env_int("AWOS_TIMEOUT", self.request_timeout)
        self.llm_retries = _env_int("AWOS_LLM_RETRIES", self.llm_retries)
        self.llm_retry_base = _env_float("AWOS_LLM_RETRY_BASE", self.llm_retry_base)

        self.max_revisions = _env_int("AWOS_MAX_REVISIONS", self.max_revisions)
        self.min_score = _env_float("AWOS_MIN_SCORE", self.min_score)
        self.max_tool_steps = _env_int("AWOS_MAX_TOOL_STEPS", self.max_tool_steps)
        self.tool_output_limit = _env_int("AWOS_TOOL_OUTPUT_LIMIT",
                                          self.tool_output_limit)

        self.hitl_mode = _env_str("AWOS_HITL", self.hitl_mode)
        self.hitl_wait_seconds = _env_int("AWOS_HITL_WAIT", self.hitl_wait_seconds)

        self.http_allow = _env_list("AWOS_HTTP_ALLOW", self.http_allow)
        self.http_timeout = _env_int("AWOS_HTTP_TIMEOUT", self.http_timeout)
        self.allow_shell = _env_bool("AWOS_ALLOW_SHELL", self.allow_shell)
        self.shell_timeout = _env_int("AWOS_SHELL_TIMEOUT", self.shell_timeout)

        self.host = _env_str("AWOS_HOST", self.host)
        self.port = _env_int("AWOS_PORT", self.port)
        self.api_token = _env_str("AWOS_API_TOKEN", self.api_token)

        self.workflows_dir = _env_str("AWOS_WORKFLOWS_DIR", self.workflows_dir)
        self.profiles_dir = _env_str("AWOS_PROFILES_DIR", self.profiles_dir)

        raw_sql = os.getenv("AWOS_SQL_DATABASES", "")
        if raw_sql:
            # формат: alias=/путь/к/базе.db,alias2=/другой.db
            pairs: dict[str, str] = {}
            for chunk in raw_sql.split(","):
                if "=" not in chunk:
                    raise ConfigError(
                        f"AWOS_SQL_DATABASES: ожидался формат alias=path, "
                        f"получено {chunk!r}")
                alias, _, p = chunk.partition("=")
                pairs[alias.strip()] = p.strip()
            self.sql_databases = pairs

    def validate(self) -> None:
        if self.hitl_mode not in HITL_MODES:
            raise ConfigError(
                f"hitl_mode={self.hitl_mode!r}: допустимо "
                f"{', '.join(HITL_MODES)}")
        if self.max_revisions < 0:
            raise ConfigError("max_revisions не может быть отрицательным")
        if not 0.0 <= self.min_score <= 1.0:
            raise ConfigError("min_score должен лежать в диапазоне [0..1]")
        if self.max_tool_steps < 0:
            raise ConfigError("max_tool_steps не может быть отрицательным")
        if self.host != "127.0.0.1" and self.host != "localhost" and not self.api_token:
            # Тот же принцип, что в agent_system/MAOS: наружу — только с
            # токеном. Среда умеет запускать shell и ходить в чужие API,
            # открытый порт без авторизации — это чужой доступ к машине.
            raise ConfigError(
                f"host={self.host!r} — не localhost. Задайте AWOS_API_TOKEN, "
                "иначе среда откажется слушать сеть без авторизации.")
        if not isinstance(self.sql_databases, dict):
            raise ConfigError("sql_databases: ожидался объект alias -> путь")

    # ------------------------------------------------------------------
    def resolved_workflows_dir(self) -> Path:
        if self.workflows_dir:
            return Path(self.workflows_dir).expanduser()
        return Path(__file__).resolve().parent / "workflows"

    def resolved_profiles_dir(self) -> Path:
        if self.profiles_dir:
            return Path(self.profiles_dir).expanduser()
        return Path(__file__).resolve().parent / "profiles"

    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if mask_secrets:
            for key in SECRET_FIELDS:
                if data.get(key):
                    data[key] = "***"
        return data

    def describe(self) -> str:
        """Человекочитаемая сводка — для `awos check` и стартового баннера."""
        tools = []
        if self.http_allow:
            tools.append(f"http({len(self.http_allow)} хост.)")
        if self.allow_shell:
            tools.append("shell")
        if self.sql_databases:
            tools.append(f"sql({len(self.sql_databases)})")
        tools.append("files")
        return (
            f"AWOS: база={self.db_path} workspace={self.workspace}\n"
            f"  модель по умолчанию: {self.provider}:{self.model}\n"
            f"  цикл качества: до {self.max_revisions} доработок, "
            f"порог {self.min_score}\n"
            f"  human-in-the-loop: {self.hitl_mode}\n"
            f"  инструменты среды: {', '.join(tools)}"
        )
