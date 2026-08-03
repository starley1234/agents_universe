"""Logging configuration powered by Loguru.

Call ``setup_logging()`` once at application startup.
Do NOT auto-call on import — it breaks test isolation.
"""

from __future__ import annotations

import sys

from loguru import logger

_configured = False


def setup_logging(log_level: str = "DEBUG", environment: str = "development") -> None:
    """Configure Loguru sinks.  Idempotent — safe to call multiple times."""
    global _configured
    if _configured:
        return

    logger.remove()

    # ── Console ──────────────────────────────────────────────
    if environment == "development":
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
                "<level>{message}</level>"
            ),
            level=log_level,
            colorize=True,
        )
    else:
        logger.add(
            sys.stderr,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} — {message}",
            level=log_level,
        )

    # ── File (always) ────────────────────────────────────────
    logger.add(
        "logs/astra_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        compression="gz",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
    )

    _configured = True
    logger.info("Logging initialised (env={}, level={})", environment, log_level)
