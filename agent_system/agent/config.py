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
from typing import Any, TYPE_CHECKING

from .mcp import MCPServerConfig, configs_from_dict
from .tools.messaging import MessagingConfig
from .tools.shell import SandboxConfig
from .tools.web import WebConfig

if TYPE_CHECKING:  # разрыв цикла: router.py импортирует Config только
    from .router import ProfileInfo  # локально, внутри функции


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
    # --- внешние MCP-серверы: поиск, страницы, картинки, речь ---
    mcp: list[MCPServerConfig] = field(default_factory=list)
    # --- распознавание PDF (навык "pdf") ---
    # Отдельная модель для распознавания страниц: как правило, это должна
    # быть vision-модель, а не обязательно та же, что ведёт диалог с
    # инструментами. Если ничего не задано — используется основная модель
    # (cfg.provider/cfg.model), при условии, что она умеет в изображения.
    vision_provider: str | None = None
    vision_model: str | None = None
    vision_base_url: str | None = None
    vision_api_key: str | None = None
    pdf_dpi: int = 170                  # разрешение рендера страницы в PNG
    pdf_max_pages_per_call: int = 25    # защита от случайного распознавания
                                         # сотен страниц одним вызовом

    # --- эмбеддинги (навыки "rag", "pg_ontology") ---
    # Отдельный провайдер/модель для векторизации текста. Если не задан —
    # берётся провайдер основной модели с моделью embedding_model.
    embedding_provider: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    # --- PostgreSQL + pgvector (навык "pg_ontology", бэкенд "rag") ---
    # Строка подключения вида postgresql://user:pass@host:5432/db.
    # Если не задана — pg_ontology откажется подключаться с понятной
    # ошибкой, а rag использует SQLite (agent.db) как бэкенд по умолчанию.
    pg_dsn: str | None = None
    # Размерность вектора pgvector. 0 = определить автоматически по факту
    # первого реального вызова эмбеддера (сеть/ключ тратятся один раз при
    # первом использовании навыка, а не при сборке агента) — так конфиг
    # не нужно синхронизировать вручную с моделью эмбеддинга.
    pg_vector_dim: int = 0

    # --- RAG (навык "rag") ---
    rag_chunk_size: int = 1200          # символов на фрагмент
    rag_chunk_overlap: int = 150        # перекрытие между фрагментами
    rag_top_k: int = 6                  # сколько фрагментов подтягивать

    # --- связь с внешним миром (навык "messaging"): email/Telegram/MAX ---
    # Ключи/пароли не хранятся в JSON-конфиге — только в переменных
    # окружения (см. .env.example), как и остальные секреты в системе.
    messaging: MessagingConfig = field(default_factory=MessagingConfig)

    # --- веб-поиск и загрузка страниц без MCP (навык "web") ---
    # Никаких секретов тут по умолчанию не требуется (DuckDuckGo без
    # ключа); для backend="searxng" адрес своего инстанса — не секрет,
    # поэтому в отличие от messaging остаётся в JSON-конфиге.
    web: WebConfig = field(default_factory=WebConfig)

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
    def list_profiles(cls) -> list[str]:
        d = cls.profiles_dir()
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    @classmethod
    def profile_infos(cls) -> list[ProfileInfo]:
        """Описания всех профилей для авто-выбора (см. agent/router.py).

        Читает только name/description/keywords — не поднимает skills/
        system_prompt, они роутеру не нужны и не должны на него влиять.
        """
        from .router import ProfileInfo
        out: list[ProfileInfo] = []
        for name in cls.list_profiles():
            f = cls.profiles_dir() / f"{name}.json"
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            out.append(ProfileInfo(
                name=data.get("name", name),
                description=data.get("description", ""),
                keywords=list(data.get("keywords") or []),
            ))
        return out

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
        data: dict[str, Any] = {}
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Конфиг {path} не найден")
            data = json.loads(p.read_text(encoding="utf-8"))

        sandbox_data = data.pop("sandbox", {}) or {}
        mcp_data = data.pop("mcp", {}) or {}
        messaging_data = data.pop("messaging", {}) or {}
        web_data = data.pop("web", {}) or {}
        profile_name = data.pop("profile", None)
        cfg = cls(**{k: v for k, v in data.items()
                    if v is not None and not k.startswith("_")})

        cfg.sandbox = SandboxConfig(**sandbox_data)
        cfg.mcp = configs_from_dict(mcp_data.get("servers", mcp_data))
        cfg.messaging = MessagingConfig.from_dict(messaging_data)
        cfg.web = WebConfig.from_dict(web_data)

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

        # окружение
        cfg.provider = os.getenv("AGENT_PROVIDER", cfg.provider)
        cfg.model = os.getenv("AGENT_MODEL", cfg.model)
        if os.getenv("AGENT_WORKSPACE"):
            cfg.workspace = os.environ["AGENT_WORKSPACE"]
        if os.getenv("AGENT_SANDBOX"):
            cfg.sandbox.mode = os.environ["AGENT_SANDBOX"]
        cfg.vision_provider = os.getenv("AGENT_VISION_PROVIDER", cfg.vision_provider)
        cfg.vision_model = os.getenv("AGENT_VISION_MODEL", cfg.vision_model)
        cfg.pg_dsn = os.getenv("AGENT_PG_DSN", cfg.pg_dsn)
        cfg.embedding_provider = os.getenv("AGENT_EMBEDDING_PROVIDER",
                                           cfg.embedding_provider)
        cfg.embedding_model = os.getenv("AGENT_EMBEDDING_MODEL",
                                        cfg.embedding_model)

        # секреты каналов связи — ТОЛЬКО из окружения, никогда из JSON-файла
        cfg.messaging.email.smtp_host = os.getenv(
            "SMTP_HOST", cfg.messaging.email.smtp_host)
        cfg.messaging.email.smtp_user = os.getenv(
            "SMTP_USER", cfg.messaging.email.smtp_user)
        cfg.messaging.email.smtp_password = os.getenv(
            "SMTP_PASSWORD", cfg.messaging.email.smtp_password)
        cfg.messaging.email.imap_host = os.getenv(
            "IMAP_HOST", cfg.messaging.email.imap_host)
        cfg.messaging.email.imap_user = os.getenv(
            "IMAP_USER", cfg.messaging.email.imap_user)
        cfg.messaging.email.imap_password = os.getenv(
            "IMAP_PASSWORD", cfg.messaging.email.imap_password)
        cfg.messaging.telegram.bot_token = os.getenv(
            "TELEGRAM_BOT_TOKEN", cfg.messaging.telegram.bot_token)
        cfg.messaging.telegram.webhook_secret = os.getenv(
            "TELEGRAM_WEBHOOK_SECRET", cfg.messaging.telegram.webhook_secret)
        cfg.messaging.max.bot_token = os.getenv(
            "MAX_BOT_TOKEN", cfg.messaging.max.bot_token)
        cfg.messaging.max.webhook_secret = os.getenv(
            "MAX_WEBHOOK_SECRET", cfg.messaging.max.webhook_secret)

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

    def resolve_vision(self) -> tuple[str, str, str | None, str | None]:
        """Провайдер/модель/base_url/ключ для распознавания PDF-страниц.

        Если vision_* не заданы явно — используется ОСНОВНАЯ модель
        диалога. Так навык pdf работает «из коробки» с любым провайдером,
        а отдельную (обычно более дешёвую) vision-модель можно подключить
        одной строкой в конфиге, не трогая остальное.
        """
        provider = self.vision_provider or self.provider
        model = self.vision_model or self.model
        if self.vision_base_url:
            base_url = self.vision_base_url
        elif self.vision_provider:
            base_url, _ = self._env_default(provider)
        else:
            base_url = self.base_url
        if self.vision_api_key:
            api_key = self.vision_api_key
        elif self.vision_provider:
            _, api_key = self._env_default(provider)
        else:
            api_key = self.api_key
        return provider, model, base_url, api_key

    def resolve_embedding(self) -> tuple[str, str, str | None, str | None]:
        """Провайдер/модель/base_url/ключ для эмбеддингов.

        Та же логика, что у resolve_vision: без explicit embedding_* —
        используется провайдер основной модели, но модель эмбеддинга
        своя (диалоговая модель обычно не умеет отдавать векторы).
        """
        provider = self.embedding_provider or self.provider
        model = self.embedding_model
        if self.embedding_base_url:
            base_url = self.embedding_base_url
        elif self.embedding_provider:
            base_url, _ = self._env_default(provider)
        else:
            base_url = self.base_url
        if self.embedding_api_key:
            api_key = self.embedding_api_key
        elif self.embedding_provider:
            _, api_key = self._env_default(provider)
        else:
            api_key = self.api_key
        return provider, model, base_url, api_key

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mcp"] = [m["name"] for m in d.get("mcp", [])]
        if d.get("api_key"):
            d["api_key"] = "***"          # не светим ключ в логах
        # секреты каналов связи — тоже не светим
        msg = d.get("messaging") or {}
        for section, fields in (("email", ("smtp_password", "imap_password")),
                                ("telegram", ("bot_token", "webhook_secret")),
                                ("max", ("bot_token", "webhook_secret"))):
            sec = msg.get(section) or {}
            for f in fields:
                if sec.get(f):
                    sec[f] = "***"
        return d

