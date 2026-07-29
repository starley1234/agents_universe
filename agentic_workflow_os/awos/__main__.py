"""Точка входа `python3 -m awos`: командная строка среды."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
