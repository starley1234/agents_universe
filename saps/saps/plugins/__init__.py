"""Модуль расширения (ТЗ п.2.4, п.3.4): плагины поверх данных САПС."""
from __future__ import annotations

from .base import (Plugin, PluginError, available, create, describe_all,
                   register, unregister)

__all__ = ["Plugin", "PluginError", "register", "unregister", "available",
           "create", "describe_all"]
