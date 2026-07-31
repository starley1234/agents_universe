"""Логирование и диагностика для agent_toolkit в продакшн-средах."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "agent_toolkit", level: str = "INFO") -> logging.Logger:
    """Получить настроенный логгер для сервисов agent_toolkit."""
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        if not logger.handlers:
            logger.addHandler(handler)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        _CONFIGURED = True
    return logger
