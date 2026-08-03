"""LLM / Embeddings abstraction — works with Ollama, OpenRouter, vLLM, etc."""
from __future__ import annotations

import logging
from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from src.config import Settings, get_settings

log = logging.getLogger(__name__)


def get_llm(settings: Settings | None = None, *,
            temperature: float | None = None,
            max_tokens: int = 4096,
            streaming: bool = True) -> BaseChatModel:
    s = settings or get_settings()
    temp = temperature if temperature is not None else s.LLM_TEMPERATURE
    headers = {}
    if s.LLM_PROVIDER.value == "openrouter":
        headers = {"HTTP-Referer": "https://agent.local", "X-Title": s.APP_ENV.value}
    llm = ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=temp,
        max_tokens=max_tokens,
        streaming=streaming,
        default_headers=headers or None,
        max_retries=3,
        request_timeout=120.0,
    )
    log.info("LLM: %s/%s @ %s", s.LLM_PROVIDER.value, s.llm_model, s.llm_base_url)
    return llm


def get_embeddings(settings: Settings | None = None) -> Embeddings:
    s = settings or get_settings()
    return OpenAIEmbeddings(
        model=s.EMBEDDING_MODEL,
        base_url=s.emb_base_url,
        api_key=s.emb_api_key,
        check_embedding_ctx_length=False,
    )
