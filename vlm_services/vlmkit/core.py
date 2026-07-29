"""Ядро: базовый сервис, разбор ответов, реестр.

Все двенадцать сервисов устроены одинаково: принимают изображения и
параметры, задают модели вопрос по схеме, а затем **проверяют и
досчитывают ответ кодом**. Модель хорошо описывает картинку и плохо
считает — поэтому доли полки, БЖУ, сметы и контраст считает Python.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage

from .config import Settings, settings as default_settings
from .images import ImageError, ImageRef, load_many, normalize
from .vlm import build_message, get_vlm, is_offline


class ServiceError(Exception):
    """Ошибка входных данных сервиса — превращается в 400, а не в 500."""


def parse_json(raw: str) -> Any | None:
    """Достать JSON из ответа модели, терпя ```-обёртки и болтовню вокруг."""
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def as_float(value: Any, default: float = 0.0) -> float:
    """Числа от модели приходят как «12,5», «~3 шт», «1 200» — приводим к float."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:[.,]\d+)?", value.replace("\u00a0", "").replace(" ", ""))
        if m:
            try:
                return float(m.group(0).replace(",", "."))
            except ValueError:
                return default
    return default


def as_int(value: Any, default: int = 0) -> int:
    return int(round(as_float(value, default)))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


@dataclass
class Result:
    """Единый ответ любого сервиса."""

    service: str
    data: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    warnings: list[str] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"service": self.service, "data": self.data, "report": self.report,
                "warnings": self.warnings, "images": self.images,
                "duration_s": self.duration_s, "model": self.model}


class Service:
    """Базовый сервис: подготовка картинок, вызов VLM, разбор ответа.

    Наследник задаёт `slug`, `title`, `system`, `schema` и реализует
    `analyze()`. Всё общее — нормализация кадров, промпт, парсинг,
    трассировка и отчёт — уже здесь.
    """

    slug: str = ""
    title: str = ""
    summary: str = ""
    tags: tuple[str, ...] = ()
    system: str = "Ты внимательный визуальный аналитик."
    schema: dict | list | None = None
    min_images: int = 1
    max_images: int = 8

    def __init__(self, cfg: Settings | None = None, vlm: BaseChatModel | None = None):
        self.cfg = cfg or default_settings()
        self._vlm = vlm

    # --- инфраструктура ---------------------------------------------------
    @property
    def vlm(self) -> BaseChatModel:
        if self._vlm is None:
            self._vlm = get_vlm(self.cfg)
        return self._vlm

    def prepare(self, images: Any) -> list[ImageRef]:
        refs = load_many(images, max_images=self.max_images, max_mb=self.cfg.max_upload_mb)
        if len(refs) < self.min_images:
            raise ServiceError(
                f"{self.slug}: нужно минимум {self.min_images} изображение(й), "
                f"получено {len(refs)}")
        return [normalize(r, self.cfg.max_side_px, self.cfg.jpeg_quality) for r in refs]

    def ask(self, prompt: str, images: Sequence[ImageRef], schema: Any = None) -> Any:
        """Задать вопрос модели и вернуть разобранный JSON."""
        schema = self.schema if schema is None else schema
        sys = self.system
        if schema is not None:
            sys += ("\n\nОтветь ТОЛЬКО валидным JSON по схеме ниже, без пояснений.\n"
                    + SCHEMA_LINE + json.dumps(schema, ensure_ascii=False))
        msg = build_message(prompt, images, scenes_for_fake=is_offline(self.cfg))
        raw = self.vlm.invoke([SystemMessage(content=sys), msg])
        parsed = parse_json(str(raw.content))
        return parsed if parsed is not None else (schema if schema is not None else {})

    def known_params(self) -> set[str]:
        """Имена параметров, которые понимает `analyze` конкретного сервиса."""
        import inspect

        sig = inspect.signature(self.analyze)
        return {name for name, p in sig.parameters.items()
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                and name not in ("self", "images")}

    def check_params(self, params: dict[str, Any]) -> None:
        """Опечатка в имени параметра — это ошибка, а не повод её проглотить.

        Без проверки `min_sos_pc=50` вместо `min_sos_pct` молча уходит в
        `**params`, клиент получает ответ с порогом по умолчанию и считает,
        что задал свой. Такую ошибку невозможно заметить по результату.
        """
        unknown = sorted(set(params) - self.known_params())
        if unknown:
            known = ", ".join(sorted(self.known_params())) or "нет"
            raise ServiceError(
                f"{self.slug}: неизвестные параметры: {', '.join(unknown)}. "
                f"Доступные: {known}")

    def run(self, images: Any = None, **params: Any) -> Result:
        """Точка входа: подготовить кадры, проанализировать, собрать отчёт."""
        t0 = time.time()
        self.check_params(params)
        refs = self.prepare(images)
        out = self.analyze(refs, **params)
        data = out if isinstance(out, dict) else {"result": out}
        warnings = list(data.pop("_warnings", []))
        return Result(
            service=self.slug,
            data=data,
            report=self.report(data, refs, **params),
            warnings=warnings,
            images=[r.info() for r in refs],
            duration_s=round(time.time() - t0, 3),
            model=self.cfg.resolved_model(),
        )

    # --- переопределяется наследником -------------------------------------
    def analyze(self, images: list[ImageRef], **params: Any) -> dict[str, Any]:
        """Реализация сервиса. Именованные параметры объявляются явно —
        по ним строится валидация `check_params`."""
        raise NotImplementedError

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2)

    def demo(self) -> dict[str, Any]:
        """Демо-запрос: картинки со `scene` и параметры. Для UI и тестов."""
        return {"images": [], "params": {}}


SCHEMA_LINE = "JSON_SCHEMA_HINT: "


# --- реестр ----------------------------------------------------------------
REGISTRY: dict[str, type[Service]] = {}


def register(cls: type[Service]) -> type[Service]:
    if not cls.slug:
        raise ValueError(f"{cls.__name__}: не задан slug")
    REGISTRY[cls.slug] = cls
    return cls


def load_registry() -> dict[str, type[Service]]:
    from . import services

    services.load_all()
    return REGISTRY


def get_service(slug: str, cfg: Settings | None = None) -> Service:
    reg = load_registry()
    if slug not in reg:
        raise KeyError(f"сервис {slug!r} не найден. Есть: {', '.join(sorted(reg))}")
    return reg[slug](cfg=cfg or default_settings())


def run_service(slug: str, images: Any = None, cfg: Settings | None = None,
                **params: Any) -> Result:
    svc = get_service(slug, cfg)
    if images is None and not params:
        d = svc.demo()
        images, params = d.get("images"), d.get("params", {})
    return svc.run(images, **params)


# --- помощники для отчётов -------------------------------------------------
def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def bullets(items: Sequence[Any], prefix: str = "- ") -> list[str]:
    return [f"{prefix}{it}" for it in items if it]
