"""Профиль агента: КТО выполняет роль. Специализация живёт здесь.

Профиль — это данные, а не код: имя, системный промпт, модель, набор
инструментов. Среда не знает, что такое «маркетолог» или «инженер» —
она знает, что у шага есть профиль, у профиля есть промпт и лимиты.
Благодаря этому одна и та же среда обслуживает любую предметную область,
а добавление специалиста — это JSON-файл, а не правка ядра.

ГРАНИЦЫ ПОЛНОМОЧИЙ ПРОФИЛЯ. Профиль может: задать промпт, выбрать
модель (провайдер/имя/температуру), СУЗИТЬ список инструментов. Профиль
НЕ может: расширить набор инструментов сверх грантов среды, включить
shell, изменить политику HITL, поднять лимит доработок. Всё это —
решения администратора среды, иначе профиль, полученный от третьей
стороны, сам себе выпишет права.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config

#: Роли, которые понимает среда. Это КОНТРАКТ, а не список профессий:
#: worker делает, critic ищет дефекты, supervisor решает судьбу работы.
ROLES = ("worker", "critic", "supervisor")


class ProfileError(Exception):
    """Ошибка описания профиля агента."""


@dataclass
class Profile:
    name: str
    role: str = "worker"
    title: str = ""
    system: str = ""
    #: Модель: пусто — берём из конфига среды. Профиль вправе выбрать
    #: другую (дешёвую для черновика, сильную для критики), но не вправе
    #: получить ключ, которого у среды нет.
    provider: str = ""
    model: str = ""
    temperature: float | None = None
    #: Только СУЖЕНИЕ набора инструментов среды.
    tools: list[str] = field(default_factory=list)
    max_tool_steps: int | None = None

    @classmethod
    def parse(cls, data: dict[str, Any], *, source: str = "<inline>") -> "Profile":
        if not isinstance(data, dict):
            raise ProfileError(f"{source}: ожидался объект JSON")
        name = str(data.get("name", "") or "").strip()
        if not name:
            raise ProfileError(f"{source}: не задано 'name'")
        role = str(data.get("role", "worker") or "worker").strip().lower()
        if role not in ROLES:
            raise ProfileError(
                f"{source}: role={role!r}, допустимо {', '.join(ROLES)}")
        system = str(data.get("system", "") or "").strip()
        if not system:
            raise ProfileError(
                f"{source}: пустой 'system' — профиль без системного промпта "
                "ничем не отличается от модели по умолчанию")
        temp = data.get("temperature")
        if temp is not None:
            try:
                temp = float(temp)
            except (TypeError, ValueError) as exc:
                raise ProfileError(f"{source}: temperature — не число") from exc
        raw_tools = data.get("tools", []) or []
        if isinstance(raw_tools, str):
            raw_tools = [raw_tools]
        if not isinstance(raw_tools, list):
            raise ProfileError(f"{source}: 'tools' — список имён инструментов")
        steps = data.get("max_tool_steps")
        if steps is not None:
            try:
                steps = int(steps)
            except (TypeError, ValueError) as exc:
                raise ProfileError(f"{source}: max_tool_steps — не целое") from exc
        return cls(name=name, role=role,
                   title=str(data.get("title", "") or ""), system=system,
                   provider=str(data.get("provider", "") or ""),
                   model=str(data.get("model", "") or ""), temperature=temp,
                   tools=[str(t) for t in raw_tools], max_tool_steps=steps)

    def llm_kwargs(self, cfg: Config) -> dict[str, Any]:
        """Параметры модели для этого профиля: профиль > конфиг среды."""
        provider = self.provider or cfg.provider
        kwargs: dict[str, Any] = {
            "retries": cfg.llm_retries,
            "retry_base": cfg.llm_retry_base,
        }
        if provider.lower() not in ("stub", "fake", "offline"):
            kwargs.update({
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
                "timeout": cfg.request_timeout,
                "temperature": (cfg.temperature if self.temperature is None
                                else self.temperature),
            })
        return kwargs

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role, "title": self.title,
                "system": self.system, "provider": self.provider,
                "model": self.model, "temperature": self.temperature,
                "tools": list(self.tools), "max_tool_steps": self.max_tool_steps}


def load_profile(name: str, directory: Path | None = None) -> Profile:
    base = directory or (Path(__file__).resolve().parent.parent / "profiles")
    path = base / f"{name}.json"
    if not path.exists():
        known = ", ".join(list_profiles(base)) or "—"
        raise ProfileError(
            f"Профиль {name!r} не найден в {base}. Известные: {known}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path}: не разбирается как JSON — {exc}") from exc
    profile = Profile.parse(data, source=str(path))
    if profile.name != path.stem:
        raise ProfileError(
            f"{path}: поле name={profile.name!r} не совпадает с именем файла")
    return profile


def list_profiles(directory: Path | None = None) -> list[str]:
    base = directory or (Path(__file__).resolve().parent.parent / "profiles")
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))


def describe_profiles(directory: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = directory or (Path(__file__).resolve().parent.parent / "profiles")
    for name in list_profiles(base):
        try:
            p = load_profile(name, base)
        except ProfileError as exc:
            out.append({"name": name, "error": str(exc)})
            continue
        out.append({"name": p.name, "role": p.role, "title": p.title,
                    "model": p.model or "(из конфига)", "tools": p.tools})
    return out


#: Профиль по умолчанию для роли — если шаг не назвал исполнителя.
#: Среда обязана уметь работать «из коробки», без единого JSON-файла:
#: платформу пробуют до того, как заводят своих специалистов.
DEFAULT_SYSTEM = {
    "worker": (
        "Ты — Исполнитель в производственной среде AWOS. Делай ровно то, что "
        "просит задача, опираясь на данные из контекста.\n"
        "Правила:\n"
        "- Не выдумывай факты. Нет данных — так и скажи и объясни, чего не хватает.\n"
        "- Если есть инструменты — пользуйся ими вместо предположений.\n"
        "- Отвечай содержанием результата, а не рассказом о том, как ты старался.\n"
        "- Твою работу проверит Критик; заведомо неполный ответ вернётся к тебе же."
    ),
    "critic": (
        "Ты — Критик в производственной среде AWOS. Твоя работа — искать "
        "дефекты в результате Исполнителя, а не хвалить его.\n"
        "Проверяй: соответствие задаче, фактические ошибки, выдуманные данные, "
        "пропущенные требования, внутренние противоречия.\n"
        "Не переписывай работу за Исполнителя — называй проблемы."
    ),
    "supervisor": (
        "Ты — Контролёр в производственной среде AWOS. У тебя есть результат "
        "Исполнителя и разбор Критика. Решение простое: принять результат "
        "или вернуть на доработку.\n"
        "Возвращай, только если дефект реально мешает использовать результат. "
        "Бесконечная шлифовка стоит денег и времени."
    ),
}


def default_profile(role: str) -> Profile:
    role = role if role in ROLES else "worker"
    return Profile(name=f"default_{role}", role=role,
                   title=f"Профиль по умолчанию ({role})",
                   system=DEFAULT_SYSTEM[role])


def resolve_profile(name: str, role: str, directory: Path | None = None) -> Profile:
    """Профиль по имени; пусто или отсутствует -> встроенный для роли.

    Отсутствующий файл НЕ роняет прогон: среда обязана работать без
    предварительной настройки. Но подмена молча тоже плоха — вызывающий
    код (engine) пишет об этом в журнал прогона.
    """
    if not name:
        return default_profile(role)
    try:
        return load_profile(name, directory)
    except ProfileError:
        return default_profile(role)
