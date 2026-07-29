"""Справочник авиационных правил (АП-21, АП-25 и другие наборы)."""
from __future__ import annotations

from .loader import (RulesError, data_dir, list_builtin, load_builtin,
                     load_ruleset, load_ruleset_dict, read_ruleset)

__all__ = ["load_ruleset", "load_ruleset_dict", "load_builtin",
           "list_builtin", "read_ruleset", "data_dir", "RulesError",
           "extract_from_pdf", "ExtractionResult"]

from .pdf_rules import ExtractionResult, extract_from_pdf  # noqa: E402
