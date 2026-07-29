"""Среда агентов на LangChain + LangGraph: семь продуктовых пайплайнов.

    from aconstructor import run_pipeline
    result = run_pipeline("patent-clearance")
    print(result["report"])
"""

from .config import Settings, settings
from .core import (
    Agent,
    BaseState,
    Pipeline,
    REGISTRY,
    get_pipeline,
    load_registry,
    mermaid,
    new_state,
    register,
    run_pipeline,
)
from .llm import get_llm

__all__ = [
    "Agent", "BaseState", "Pipeline", "REGISTRY", "Settings", "get_llm", "get_pipeline",
    "load_registry", "mermaid", "new_state", "register", "run_pipeline", "settings",
]
__version__ = "0.1.0"
