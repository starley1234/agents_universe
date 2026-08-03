"""TaskIQ worker — Redis broker with InMemory fallback, includes all tasks."""

from __future__ import annotations

import sys

from loguru import logger
from taskiq import TaskiqEvents, TaskiqState

from astra.config import settings

try:
    from taskiq_redis import ListQueueBroker

    # Use ListQueueBroker for simplicity (Redis list), or RedisStreamBroker
    try:
        from taskiq_redis import RedisStreamBroker

        broker = RedisStreamBroker(url=settings.redis_url)
        logger.info("TaskIQ broker: RedisStream ({})", settings.redis_url)
    except ImportError:
        broker = ListQueueBroker(url=settings.redis_url)
        logger.info("TaskIQ broker: Redis ListQueue ({})", settings.redis_url)
except Exception as exc:
    from taskiq import InMemoryBroker

    broker = InMemoryBroker()
    logger.info("TaskIQ broker: InMemory fallback (Redis unavailable: {})", exc)


# Import tasks so they are registered on broker
import astra.tasks.agent_tasks  # noqa: F401
import astra.tasks.dreaming_task  # noqa: F401


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def worker_startup(state: TaskiqState) -> None:
    from astra.utils.logger import setup_logging

    setup_logging(settings.log_level, settings.environment.value)
    logger.info("🚀 TaskIQ worker startup")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def worker_shutdown(state: TaskiqState) -> None:
    logger.info("👋 TaskIQ worker shutdown")


def run_worker() -> None:
    """CLI entrypoint for worker — used in Dockerfile and `astra-worker` script."""
    import asyncio

    async def _run():
        await broker.startup()
        # Keep running
        logger.info("TaskIQ worker running, waiting for jobs...")
        # In real deployment, taskiq cli runs worker via `taskiq worker astra.tasks.worker:broker`
        # Here we just idle
        while True:
            await asyncio.sleep(3600)

    try:
        import asyncio

        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # For manual run: python -m astra.tasks.worker
    # Better to use: taskiq worker astra.tasks.worker:broker --fs-discover
    print("Use: taskiq worker astra.tasks.worker:broker --fs-discover", file=sys.stderr)
    print("Or: taskiq worker astra.tasks.worker:broker", file=sys.stderr)
