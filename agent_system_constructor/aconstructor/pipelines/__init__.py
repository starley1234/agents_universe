"""Реестр пайплайнов. `load_all()` импортирует все семь продуктов."""

from __future__ import annotations

import importlib

MODULES = (
    "patent_clearance",
    "synthetic_buyer",
    "doc_restorer",
    "energy_hacker",
    "formula_reverse",
    "cert_validator",
    "urban_scout",
)

_loaded = False


def load_all() -> None:
    global _loaded
    if _loaded:
        return
    for name in MODULES:
        importlib.import_module(f".{name}", __package__)
    _loaded = True
