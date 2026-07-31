"""Главная точка входа для запуска команд из терминала (python3 -m agent_toolkit)."""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
