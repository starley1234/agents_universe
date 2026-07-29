"""Загрузка конфигурации: файл JSON + переменные окружения.

Приоритет: аргументы CLI > переменные окружения > файл > умолчания.
Ключи в файле не храним — только в окружении, чтобы конфиг можно было
класть в git.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .mcp import MCPServerConfig, configs_from_dict
from .skills.comms import CommsConfig
from .tools.shell import SandboxConfig


def load_dotenv(path: str | os.PathLike[str] | None = None,
                override: bool = False) -> int:
    """Загрузить .env в окружение. Возвращает число прочитанных ключей.

    Файл .env лежал в проекте с примером и был описан в документации, но
    его никто не читал: код брал только os.getenv. Человек прописывал
    AGENT_PORT и получал прежний порт — молча, без единого намёка.

    Формат нарочно простой: KEY=VALUE, решётка — комментарий, кавычки
    вокруг значения снимаются. Ни подстановок, ни многострочных
    значений: у переменных окружения их тоже нет, а сложный разбор
    потянул бы зависимость.

    override=False: настоящее окружение сильнее файла. Иначе
    `AGENT_PORT=9000 make serve` не сработал бы — файл бы его перебил.
    """
    f = Path(path) if path else Path.cwd() / ".env"
    if not f.is_file():
        return 0
    n = 0
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


@dataclass
class Config:
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.0
    workspace: str = "./workspace"
    max_steps: int = 30
    # memory включена по умолчанию: без неё агент не помнит прошлые
    # запуски и, что хуже, склонен это выдумывать.
    skills: list[str] = field(
        default_factory=lambda: ["files", "shell", "memory", "present"])
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    system_prompt: str | None = None
    profile: str | None = None          # роль агента: coder | cad | research | ...
    # --- экономия токенов ---
    tool_result_limit: int = 4000       # обрезка результата инструмента, символов
    keep_last_results: int = 3          # полностью хранить только N последних
    # --- постоянное состояние и автономный режим ---
    db: str = "agent.db"                # SQLite: память, онтология, план
    max_hours: float = 1.0              # бюджет времени автономного прогона
    max_iterations: int = 50
    max_usd: float = 0.0                # предел расхода за прогон, 0 = без предела
    # --- внешние MCP-серверы: поиск, страницы, картинки, речь ---
    mcp: list[MCPServerConfig] = field(default_factory=list)
    # --- PostgreSQL + pgvector (навык pg) ---
    pg_dsn: str = ""                    # postgresql://user:pass@host/db
    embed_url: str = ""                 # OpenAI-совместимый /v1 для векторов
    embed_model: str = ""
    embed_key: str = ""
    embed_dim: int = 768
    # --- встроенный fetch ---
    fetch_allow_private: bool = False   # ходить во внутреннюю сеть
    # --- снимки рабочей папки (навык vcs) ---
    vcs_auto: bool = True               # снимок перед каждым шагом агента
    # --- запуск Python (навык python) ---
    python_timeout: int = 60            # предел одного запуска, секунд
    # --- связь с внешним миром (навык comms) ---
    comms: CommsConfig = field(default_factory=CommsConfig)
    # --- разбор вопроса двумя моделями (--debate) ---
    debate: dict = field(default_factory=dict)
    # --- маршрутизация моделей: дешёвая на рутину, сильная на сложное ---
    model_cheap: str = ""               # пусто = маршрутизация выключена
    model_strong: str = ""
    route_long_context: int = 12_000

    # ------------------------------------------------------------------
    @staticmethod
    def _env_default(provider: str) -> tuple[str | None, str | None]:
        """Разумные умолчания base_url/ключа под провайдера."""
        p = provider.lower()
        if p in ("anthropic", "claude"):
            return "https://api.anthropic.com/v1", os.getenv("ANTHROPIC_API_KEY")
        if p == "ollama":
            return os.getenv("OLLAMA_HOST", "http://localhost:11434"), None
        return (os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                os.getenv("OPENAI_API_KEY"))

    @staticmethod
    def profiles_dir() -> Path:
        return Path(__file__).resolve().parent / "profiles"

    @classmethod
    def profile_hints(cls) -> dict[str, str]:
        """Профиль -> описание. Планировщику нужно знать, кому поручать.

        Без описаний модель назначает исполнителей по созвучию имени и
        путает `docs` (разбор входящих) с `office` (создание бумаг).
        """
        out: dict[str, str] = {}
        for name in cls.list_profiles():
            try:
                data = json.loads(
                    (cls.profiles_dir() / f"{name}.json").read_text(
                        encoding="utf-8"))
                out[name] = str(data.get("description") or name)
            except (OSError, json.JSONDecodeError):
                out[name] = name
        return out

    @classmethod
    def list_profiles(cls) -> list[str]:
        d = cls.profiles_dir()
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def apply_profile(self, name: str) -> None:
        """Профиль задаёт роль: набор навыков, промпт и лимит шагов.

        Профиль не перебивает то, что задано явно в конфиге или CLI —
        поэтому применяется ДО пользовательских переопределений.
        """
        f = self.profiles_dir() / f"{name}.json"
        if not f.exists():
            raise FileNotFoundError(
                f"Профиль {name!r} не найден. Доступны: "
                f"{', '.join(self.list_profiles()) or '—'}"
            )
        data = json.loads(f.read_text(encoding="utf-8"))
        for key in ("skills", "max_steps", "system_prompt"):
            if key in data:
                setattr(self, key, data[key])
        self.profile = name

    @classmethod
    def load(cls, path: str | None = None, **overrides: Any) -> "Config":
        # .env читаем ДО всего: из него берутся и ключи, и умолчания.
        load_dotenv()
        data: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Конфиг {path} не найден")
            data = json.loads(p.read_text(encoding="utf-8"))

        sandbox_data = {k: v for k, v in (data.pop("sandbox", {}) or {}).items()
                        if not k.startswith("_")}
        comms_data = {k: v for k, v in (data.pop("comms", {}) or {}).items()
                      if not k.startswith("_")}
        debate_data = {k: v for k, v in (data.pop("debate", {}) or {}).items()
                       if not k.startswith("_")}
        mcp_data = data.pop("mcp", {}) or {}
        profile_name = data.pop("profile", None)
        # Ключи с подчёркиванием — комментарии автора конфига, а не поля.
        # JSON комментариев не знает, а пояснять настройки где-то надо.
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        # Опечатка в ключе раньше давала невнятный TypeError из dataclass.
        # Молча игнорировать её нельзя: настройка бы просто не применилась.
        known = {f for f in cls.__dataclass_fields__}
        bad = [k for k in data if k not in known]
        if bad:
            close = ", ".join(sorted(known))
            raise ValueError(
                f"Неизвестные ключи в конфиге: {', '.join(bad)}. "
                f"Допустимы: {close}. Пояснения пишите с подчёркивания: "
                '"_комментарий": "…"')
        cfg = cls(**{k: v for k, v in data.items() if v is not None})
        cfg.sandbox = SandboxConfig(**sandbox_data)
        unknown_comms = [k for k in comms_data
                         if k not in CommsConfig.__dataclass_fields__]
        if unknown_comms:
            raise ValueError(
                f"Неизвестные ключи в разделе comms: {', '.join(unknown_comms)}")
        cfg.comms = CommsConfig(**comms_data)
        cfg.debate = debate_data
        cfg.mcp = configs_from_dict(mcp_data.get("servers", mcp_data))

        # профиль — база; всё, что задано явно ниже, его перекрывает
        prof = overrides.pop("profile", None) or os.getenv("AGENT_PROFILE") \
            or profile_name
        if prof:
            explicit = {k for k, v in data.items() if v is not None}
            saved = {k: getattr(cfg, k) for k in
                     ("skills", "max_steps", "system_prompt") if k in explicit}
            cfg.apply_profile(prof)
            for k, v in saved.items():
                setattr(cfg, k, v)

        # Окружение — умолчание для того, что НЕ задано явно в файле.
        # Раньше оно перебивало файл: человек указывал `-c cad.json` с
        # ollama, а работала модель из .env. Явное указание должно быть
        # сильнее фонового — иначе конфиг-файл ничего не гарантирует.
        in_file = {k for k, v in data.items() if v is not None}
        if "provider" not in in_file:
            cfg.provider = os.getenv("AGENT_PROVIDER", cfg.provider)
        if "model" not in in_file:
            cfg.model = os.getenv("AGENT_MODEL", cfg.model)
        if os.getenv("AGENT_WORKSPACE") and "workspace" not in in_file:
            cfg.workspace = os.environ["AGENT_WORKSPACE"]
        if os.getenv("AGENT_SANDBOX") and not sandbox_data:
            cfg.sandbox.mode = os.environ["AGENT_SANDBOX"]

        # переопределения из CLI
        for k, v in overrides.items():
            if v is None:
                continue
            if k == "sandbox_mode":
                cfg.sandbox.mode = v
            elif hasattr(cfg, k):
                setattr(cfg, k, v)

        # подставляем умолчания провайдера, если не заданы явно
        url, key = cls._env_default(cfg.provider)
        cfg.base_url = cfg.base_url or url
        cfg.api_key = cfg.api_key or key
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mcp"] = [m["name"] for m in d.get("mcp", [])]
        if d.get("api_key"):
            d["api_key"] = "***"          # не светим ключ в логах
        return d


def replace_profile(cfg: Config, profile: str) -> Config:
    """Копия конфига с другим профилем.

    Нужна для передачи задачи между агентами: меняются навыки и промпт,
    всё остальное (модель, база, рабочая папка, MCP) остаётся общим.
    Исходный конфиг не трогаем — он ещё нужен другим пунктам плана.
    """
    import copy
    out = copy.deepcopy(cfg)
    # system_prompt не обнуляем: apply_profile перезаписывает его сам,
    # если в профиле он задан. А вот профиль БЕЗ промпта должен унаследовать
    # общий системный промпт из конфига — обнуление сломало бы этот случай.
    out.apply_profile(profile)
    return out
