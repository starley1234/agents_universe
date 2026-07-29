"""Плагинная архитектура (ТЗ п.2.4, п.3.4).

ЧТО ТАКОЕ ПЛАГИН В САПС. Модуль, который добавляет проверку или
генерацию поверх тех же данных, что и встроенные агенты, и подчиняется
тем же правилам: результат — предложения и находки, а не молчаливые
правки. Плагин получает Config и Store — тот же контракт, что у агента,
поэтому «плагин» и «агент» здесь отличаются только происхождением, а не
правами.

ПОЧЕМУ РЕЕСТР, А НЕ АВТОЗАГРУЗКА ИЗ ПАПКИ. Автоматический импорт всего,
что лежит в каталоге, — это выполнение произвольного кода в системе,
которая ходит в промышленный PDM. Регистрация явная: либо плагин
включён в поставку и перечислен в builtin_plugins(), либо администратор
добавил его осознанно через register().

КОНТРАКТ ПЛАГИНА:
  name        — уникальное имя (используется в suggestion.agent);
  title       — что делает, человеческим языком;
  needs_llm   — нужна ли модель;
  run(**kw)   — вернуть AgentReport.
"""
from __future__ import annotations

from typing import Any, Callable

from ..agents.base import Agent, AgentReport
from ..config import Config
from ..db.store import Store


class PluginError(RuntimeError):
    """Ошибка регистрации или запуска плагина."""


class Plugin(Agent):
    """База плагина. Отличие от агента — только в происхождении."""

    name = "plugin"
    title = ""
    needs_llm = False

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title or self.__doc__ or "",
                "needs_llm": self.needs_llm}


#: Реестр: имя -> фабрика. Фабрика получает (cfg, store, **kwargs).
_REGISTRY: dict[str, Callable[..., Plugin]] = {}


def register(name: str, factory: Callable[..., Plugin], *,
             replace: bool = False) -> None:
    if not name or not name.strip():
        raise PluginError("У плагина должно быть непустое имя")
    if name in _REGISTRY and not replace:
        raise PluginError(
            f"Плагин {name!r} уже зарегистрирован. Передайте replace=True, "
            "если подмена намеренная.")
    _REGISTRY[name] = factory


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def available() -> list[str]:
    _ensure_builtin()
    return sorted(_REGISTRY)


def create(name: str, cfg: Config, store: Store, **kwargs: Any) -> Plugin:
    _ensure_builtin()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise PluginError(
            f"Плагин {name!r} не зарегистрирован. Доступны: "
            f"{', '.join(available()) or '—'}")
    return factory(cfg, store, **kwargs)


def describe_all(cfg: Config, store: Store) -> list[dict[str, Any]]:
    out = []
    for name in available():
        try:
            out.append(create(name, cfg, store).describe())
        except Exception as exc:                                 # noqa: BLE001
            out.append({"name": name, "error": str(exc)})
    return out


_BUILTIN_LOADED = False


def _ensure_builtin() -> None:
    """Ленивая регистрация встроенных плагинов.

    Ленивая, чтобы импорт saps.plugins.base не тянул за собой все
    плагины: у них могут быть свои зависимости, и падение одного не
    должно ломать реестр целиком.
    """
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    _BUILTIN_LOADED = True
    from .code_review import CodeReviewPlugin
    from .report import ReportPlugin
    register(CodeReviewPlugin.name,
             lambda cfg, store, **kw: CodeReviewPlugin(cfg, store, **kw),
             replace=True)
    register(ReportPlugin.name,
             lambda cfg, store, **kw: ReportPlugin(cfg, store, **kw),
             replace=True)
