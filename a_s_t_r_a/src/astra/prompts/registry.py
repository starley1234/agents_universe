"""Prompt registry — versioned YAML prompts with fallback."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger


PROMPTS_DIR = Path(__file__).parent


class PromptRegistry:
    """Loads prompts from YAML files, supports versioning."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self.dir = prompts_dir or PROMPTS_DIR
        self._cache: dict[str, dict] = {}

    def _load_file(self, name: str) -> dict:
        """Load YAML file, cached."""
        if name in self._cache:
            return self._cache[name]

        # Try exact file, then .yaml, .yml, then versioned latest
        candidates = [
            self.dir / name,
            self.dir / f"{name}.yaml",
            self.dir / f"{name}.yml",
        ]

        # If name without version, find latest versioned file: name_v*.yaml
        if not any(c.exists() for c in candidates):
            # Find versioned files
            pattern = f"{name}_v*.yaml"
            files = sorted(self.dir.glob(pattern))
            if files:
                # Pick highest version
                candidates = [files[-1]]
            else:
                # Fallback to .yaml in subfolder?
                candidates = []

        for path in candidates:
            if path.exists():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    self._cache[name] = data or {}
                    logger.debug("Loaded prompt {} from {}", name, path)
                    return self._cache[name]
                except Exception as exc:
                    logger.warning("Failed to load prompt {} from {}: {}", name, path, exc)

        # Fallback — return empty, caller will use hardcoded default
        logger.debug("Prompt {} not found, using fallback", name)
        self._cache[name] = {}
        return {}

    def get(self, name: str, version: Optional[str] = None, default: str = "") -> str:
        """Get prompt text by name.

        Args:
            name: prompt name without extension, e.g. 'planner'
            version: optional version like 'v1' — if given, tries name_version.yaml first
            default: fallback text if not found
        """
        # Try versioned first
        if version:
            file_name = f"{name}_{version}"
            data = self._load_file(file_name)
            if data.get("system") or data.get("prompt"):
                return data.get("system") or data.get("prompt") or default

        # Try unversioned
        data = self._load_file(name)
        if data:
            return data.get("system") or data.get("prompt") or data.get("content") or default

        return default

    def get_with_metadata(self, name: str, version: Optional[str] = None) -> dict[str, Any]:
        """Return full YAML dict with metadata."""
        if version:
            data = self._load_file(f"{name}_{version}")
            if data:
                return data
        return self._load_file(name)

    def list_prompts(self) -> list[str]:
        """List available prompt names (without version)."""
        files = list(self.dir.glob("*.yaml")) + list(self.dir.glob("*.yml"))
        names = set()
        for f in files:
            # Strip version suffix _v<number>
            stem = f.stem
            stem = re.sub(r"_v\d+$", "", stem)
            names.add(stem)
        return sorted(names)

    def reload(self) -> None:
        self._cache.clear()


prompt_registry = PromptRegistry()
