"""Langfuse tracing integration — optional, enabled via env."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Optional

from loguru import logger

from astra.config import settings

_client: Optional[Any] = None
_enabled = False


def get_langfuse_client() -> Optional[Any]:
    global _client, _enabled

    if _client is not None:
        return _client if _enabled else None

    if not settings.langfuse_enabled:
        logger.debug("Langfuse disabled via config")
        return None

    if not settings.langfuse_secret_key or not settings.langfuse_public_key:
        logger.warning("Langfuse enabled but keys missing, disabling")
        return None

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            secret_key=settings.langfuse_secret_key,
            public_key=settings.langfuse_public_key,
            host=settings.langfuse_host,
        )
        _enabled = True
        logger.info("✅ Langfuse tracing enabled (host={})", settings.langfuse_host)
        return _client
    except ImportError:
        logger.warning("Langfuse package not installed")
        return None
    except Exception as exc:
        logger.warning("Langfuse init failed: {}", exc)
        return None


@contextmanager
def trace_llm_call(
    name: str,
    metadata: Optional[dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
) -> Generator[Optional[Any], None, None]:
    """Context manager that creates a Langfuse trace if enabled, else no-op.

    Usage:
        with trace_llm_call("planner", metadata={"goal": goal}) as trace:
            # trace is Langfuse trace object or None
            response = await llm_gateway.chat(...)
            if trace:
                trace.update(output=response.content)
    """
    client = get_langfuse_client()
    if not client:
        yield None
        return

    try:
        trace = client.trace(name=name, metadata=metadata or {}, tags=tags or [])
        yield trace
    except Exception as exc:
        logger.debug("Langfuse trace creation failed: {}", exc)
        yield None
    finally:
        try:
            if client:
                client.flush()
        except Exception:
            pass


def log_generation(
    trace: Optional[Any],
    name: str,
    input_data: Any,
    output_data: Any,
    metadata: Optional[dict] = None,
    usage: Optional[dict] = None,
) -> None:
    """Log a generation within a trace."""
    if not trace:
        return
    try:
        generation = trace.generation(
            name=name,
            input=input_data,
            output=output_data,
            metadata=metadata or {},
            usage=usage,
        )
        # Generation will be flushed with trace
    except Exception as exc:
        logger.debug("Langfuse log_generation failed: {}", exc)
