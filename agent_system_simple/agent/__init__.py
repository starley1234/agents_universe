"""Универсальный агент: ядро + сменные драйверы моделей и наборы навыков."""
from .build import build_agent, known_skills
from .config import Config
from .core import Agent, Result, Step

__version__ = "1.0.0"
__all__ = ["Agent", "Config", "Result", "Step", "build_agent", "known_skills"]
