"""REST API: внешний интерфейс для интеграции с внешним миром."""

from .app import create_app

__all__ = ["create_app"]
