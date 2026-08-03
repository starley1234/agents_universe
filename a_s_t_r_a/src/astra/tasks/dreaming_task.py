"""Scheduled task: Memory Dreaming — periodic consolidation of knowledge."""

from __future__ import annotations

import asyncio

from loguru import logger

from astra.db.engine import get_session
from astra.db.repositories import ProjectRepo
from astra.memory.dreaming import consolidate_project


async def run_dreaming_cycle() -> None:
    """Iterate over all projects and run memory consolidation."""
    logger.info("💤  Starting global memory dreaming cycle…")

    async with get_session() as session:
        project_repo = ProjectRepo(session)
        projects = await project_repo.list_all()

    for project in projects:
        try:
            logger.info("Dreaming for project '{}' ({})", project.name, project.id)
            await consolidate_project(project.id)
        except Exception as exc:
            logger.error("Dreaming failed for project {}: {}", project.id, exc)

    logger.info("💤  Dreaming cycle complete for {} projects", len(projects))


def dreaming_main() -> None:
    """CLI entrypoint: ``python -m astra.tasks.dreaming_task``"""
    from astra.utils.logger import setup_logging

    setup_logging()
    asyncio.run(run_dreaming_cycle())


if __name__ == "__main__":
    dreaming_main()
