"""Справочник авиационных правил (АП-21, АП-25 и другие наборы)."""
from __future__ import annotations

from .loader import (RulesError, data_dir, list_builtin, load_builtin,
                     load_ruleset, read_ruleset)

__all__ = ["load_ruleset", "load_builtin", "list_builtin", "read_ruleset",
           "data_dir", "RulesError"]
