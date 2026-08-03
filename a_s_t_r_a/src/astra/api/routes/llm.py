"""LLM direct test and health routes — to isolate LLM connectivity from agent logic."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from astra.api.deps import db_session
from astra.auth.jwt import get_current_user
from astra.config import settings
from astra.db.models import User
from astra.llm.context import set_llm_override, reset_llm_override
from astra.llm.gateway import llm_gateway
from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/llm", tags=["llm"])


class LLMTestRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=5000, description="User prompt to test LLM")
    system: str = Field("You are a helpful assistant. Answer concisely.", max_length=2000)
    provider: Optional[str] = Field(None, description="local|openrouter|mock")
    model: Optional[str] = Field(None, description="Override model")
    url: Optional[str] = Field(None, description="Override LLM URL")
    temperature: float = Field(0.5, ge=0, le=2)
    max_tokens: int = Field(512, ge=1, le=4096)


class LLMHealthResponse(BaseModel):
    status: str
    provider: str
    model: str
    url: str
    latency_ms: Optional[int] = None
    models_available: Optional[list[str]] = None
    error: Optional[str] = None


@router.get("/providers")
async def list_providers() -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": "local",
                "name": "Local (LMStudio/Ollama/vLLM)",
                "models": [
                    "unsloth/gemma-4-12b-it",
                    "gemma-3-12b-it",
                    "qwen2.5-7b-instruct",
                    "llama-3.1-8b-instruct",
                    "mistral-7b-instruct",
                    "qwen3-0.6b",
                ],
            },
            {
                "id": "openrouter",
                "name": "OpenRouter",
                "models": [
                    "google/gemini-2.0-flash-lite:preview",
                    "anthropic/claude-3.5-sonnet",
                    "openai/gpt-4o-mini",
                    "meta-llama/llama-3.1-8b-instruct",
                ],
            },
            {"id": "mock", "name": "Mock (tests)", "models": ["mock"]},
        ],
        "current": {
            "provider": settings.llm_default_provider.value,
            "model": settings.active_llm_model,
            "url": settings.active_llm_url,
        },
    }


@router.get("/health", response_model=LLMHealthResponse)
async def llm_health(
    provider: str | None = None,
    model: str | None = None,
    url: str | None = None,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    """Try to ping LLM server's /v1/models endpoint to check connectivity."""
    from astra.llm.context import get_overrides

    # Use overrides if provided via query, else settings
    effective_provider = provider or settings.llm_default_provider.value
    effective_model = model or settings.active_llm_model
    effective_url = url or settings.active_llm_url

    # Try to fetch /v1/models
    base_url = effective_url.rstrip("/")
    # Ensure /v1/models endpoint
    if not base_url.endswith("/v1"):
        # If url is .../v1, use that, else append /v1
        if "/v1" not in base_url:
            base_url = base_url.rstrip("/") + "/v1"
    models_url = base_url.rstrip("/") + "/models"

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Try to get models
            # Some LMStudio servers require no auth, some require key
            headers = {}
            if settings.active_llm_api_key and settings.active_llm_api_key != "sk-local":
                headers["Authorization"] = f"Bearer {settings.active_llm_api_key}"
            resp = await client.get(models_url, headers=headers)
            latency = int((time.time() - start) * 1000)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    models = [m.get("id", "") for m in data.get("data", [])[:20]]
                except Exception:
                    models = []
                return LLMHealthResponse(
                    status="ok",
                    provider=effective_provider,
                    model=effective_model,
                    url=effective_url,
                    latency_ms=latency,
                    models_available=models,
                )
            else:
                return LLMHealthResponse(
                    status="error",
                    provider=effective_provider,
                    model=effective_model,
                    url=effective_url,
                    latency_ms=latency,
                    error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                )
    except Exception as exc:
        latency = int((time.time() - start) * 1000)
        return LLMHealthResponse(
            status="error",
            provider=effective_provider,
            model=effective_model,
            url=effective_url,
            latency_ms=latency,
            error=str(exc),
        )


@router.post("/test")
async def test_llm(
    body: LLMTestRequest,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    """Direct LLM call bypassing agent logic — useful to isolate LLM connectivity."""
    tokens = set_llm_override(provider=body.provider, model=body.model, url=body.url)
    try:
        messages = [
            SystemMessage(content=body.system),
            HumanMessage(content=body.prompt),
        ]
        start = time.time()
        response = await llm_gateway.chat(
            messages=messages,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            metadata={"prompt": "llm_test", "direct_test": True},
        )
        latency = int((time.time() - start) * 1000)
        return {
            "status": "ok",
            "provider": body.provider or settings.llm_default_provider.value,
            "model": body.model or settings.active_llm_model,
            "url": body.url or settings.active_llm_url,
            "latency_ms": latency,
            "response": response.content,
            "has_tool_calls": bool(response.tool_calls),
        }
    except Exception as exc:
        logger.exception("LLM direct test failed")
        return {
            "status": "error",
            "provider": body.provider or settings.llm_default_provider.value,
            "model": body.model or settings.active_llm_model,
            "url": body.url or settings.active_llm_url,
            "error": str(exc),
        }
    finally:
        reset_llm_override(tokens)
