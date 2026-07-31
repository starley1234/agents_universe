"""TaskIQ worker configuration.

Falls back to ``InMemoryBroker`` when Redis is unavailable (dev / tests).
"""

from __future__ import annotations

from loguru import logger

from astra.config import settings

try:
    from taskiq_redis import RedisStreamBroker

    broker = RedisStreamBroker(url=settings.redis_url)
    logger.info("TaskIQ broker: Redis ({})", settings.redis_url)
except Exception:
    from taskiq import InMemoryBroker

    broker = InMemoryBroker()
    logger.info("TaskIQ broker: InMemory (Redis unavailable)")
