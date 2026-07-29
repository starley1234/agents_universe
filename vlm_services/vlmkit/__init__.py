"""VLM-сервисы: двенадцать продуктов на одной инфраструктуре."""

from .config import Settings, settings
from .core import (REGISTRY, Result, Service, ServiceError, get_service,
                   load_registry, register, run_service)
from .images import ImageError, ImageRef, load, load_many, normalize
from .vlm import FakeVLM, get_vlm

__all__ = ["REGISTRY", "FakeVLM", "ImageError", "ImageRef", "Result", "Service",
           "ServiceError", "Settings", "get_service", "get_vlm", "load", "load_many",
           "load_registry", "normalize", "register", "run_service", "settings"]
__version__ = "0.1.0"
