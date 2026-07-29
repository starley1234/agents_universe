"""Точка входа `python3 -m maos`: поднимает HTTP API/дашборд."""
from __future__ import annotations

from .api.server import main

if __name__ == "__main__":
    raise SystemExit(main())
