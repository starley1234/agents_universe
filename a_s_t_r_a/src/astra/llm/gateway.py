"""Unified LLM gateway with per-request override, Langfuse, prompt registry — fixed for LMStudio."""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Optional

import litellm
from langchain_core.messages import AIMessage, BaseMessage
from loguru import logger

from astra.config import LLMProvider, settings

litellm.suppress_debug_info = True
litellm.set_verbose = False


def _lc_to_litellm(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}.get(m.type, "user")
        entry: dict[str, Any] = {"role": role, "content": m.content or ""}
        if m.type == "ai" and getattr(m, "tool_calls", None):
            entry["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"]) if isinstance(tc["args"], dict) else tc["args"],
                    },
                }
                for tc in m.tool_calls
            ]
        if m.type == "tool":
            entry["tool_call_id"] = getattr(m, "tool_call_id", "")
        out.append(entry)
    return out


def _convert_tools_for_litellm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        if "type" in t and t["type"] == "function":
            out.append(t)
        elif "function" in t:
            out.append(t)
        else:
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            })
    return out


def _normalize_model_name(model: str, provider: str) -> str:
    model = model.strip()
    if not model:
        return model
    lower = model.lower()
    if lower.startswith("openai/") or lower.startswith("openrouter/") or lower.startswith("huggingface/") or lower.startswith("ollama/") or lower.startswith("anthropic/"):
        return model
    if provider == "local":
        return f"openai/{model}"
    elif provider == "openrouter":
        return f"openrouter/{model}"
    else:
        return model


class LLMGateway:
    async def chat(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        retries: int = 2,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AIMessage:
        langfuse_trace = None
        langfuse_generation = None
        start_time = time.time()

        # Context override (per-request provider/model from UI cookie)
        try:
            from astra.llm.context import get_overrides
            overrides = get_overrides()
        except Exception:
            overrides = {"provider": None, "model": None, "url": None}

        # Merge with kwargs explicit override (higher priority)
        provider_override = kwargs.pop("provider_override", None) or overrides.get("provider")
        model_override = kwargs.pop("model_override", None) or overrides.get("model")
        url_override = kwargs.pop("url_override", None) or overrides.get("url")

        if settings.langfuse_enabled:
            try:
                from astra.llm.tracing.langfuse import get_langfuse_client
                client = get_langfuse_client()
                if client:
                    prompt_name = (metadata or {}).get("prompt", "generic")
                    langfuse_trace = client.trace(
                        name=f"astra-{prompt_name}",
                        metadata={**(metadata or {}), "overrides": overrides},
                        tags=[prompt_name, settings.llm_default_provider.value],
                    )
                    langfuse_generation = langfuse_trace.generation(
                        name=prompt_name,
                        input=[{"role": m.type, "content": str(m.content)[:1000]} for m in messages],
                        metadata={"model": settings.active_llm_model, **(metadata or {}), "overrides": overrides},
                    )
            except Exception as exc:
                logger.debug("Langfuse trace init failed: {}", exc)

        # Determine effective provider/model for this request
        effective_provider = (provider_override or settings.llm_default_provider.value).lower()

        # Mock handling respects override
        if effective_provider == "mock" or settings.llm_default_provider == LLMProvider.MOCK and not provider_override:
            # If override says mock, use mock
            if effective_provider == "mock":
                result = await self._mock_chat(messages, tools)
                if langfuse_generation:
                    try:
                        langfuse_generation.update(output=result.content, metadata={"mock": True, "duration": time.time() - start_time})
                        langfuse_generation.end()
                    except Exception:
                        pass
                return result

        raw_model, api_base, api_key = self._resolve_model_with_override(
            provider_override=provider_override,
            model_override=model_override,
            url_override=url_override,
        )
        model = _normalize_model_name(raw_model, effective_provider)

        lc_messages = _lc_to_litellm(messages)

        params: dict[str, Any] = {
            "model": model,
            "messages": lc_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }
        if api_base:
            params["api_base"] = api_base
        if api_key:
            params["api_key"] = api_key
        if tools:
            params["tools"] = _convert_tools_for_litellm(tools)

        logger.info(
            "LLM call: provider={} (override={}) raw_model={} normalized={} api_base={} msgs={} tools={} prompt={}",
            settings.llm_default_provider.value,
            provider_override,
            raw_model,
            model,
            api_base,
            len(lc_messages),
            len(tools or []),
            (metadata or {}).get("prompt"),
        )

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await litellm.acompletion(**params)
                choice = response.choices[0]
                content = choice.message.content or ""
                tool_calls: list[dict[str, Any]] = []
                if getattr(choice.message, "tool_calls", None):
                    for tc in choice.message.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                        tool_calls.append({"id": tc.id, "name": tc.function.name, "args": args})

                if langfuse_generation:
                    try:
                        usage = getattr(response, "usage", None)
                        usage_dict = {}
                        if usage:
                            usage_dict = {
                                "input": getattr(usage, "prompt_tokens", 0),
                                "output": getattr(usage, "completion_tokens", 0),
                                "total": getattr(usage, "total_tokens", 0),
                            }
                        langfuse_generation.update(output=content, usage=usage_dict, metadata={"model": model, "duration": time.time() - start_time, **(metadata or {})})
                        langfuse_generation.end()
                    except Exception as exc:
                        logger.debug("Langfuse log failed: {}", exc)

                return AIMessage(content=content, tool_calls=tool_calls)
            except Exception as exc:
                last_exc = exc
                err_str = str(exc).lower()
                hint = ""
                if "localhost" in str(api_base) and "connection" in err_str:
                    hint = " (Hint: inside Docker use host.docker.internal, e.g. http://host.docker.internal:1234/v1)"
                logger.warning("LLM call failed (attempt {}/{}{}): {}", attempt + 1, retries + 1, hint, exc)
                if attempt < retries:
                    await asyncio.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.3))

        if langfuse_generation:
            try:
                langfuse_generation.update(output=str(last_exc), level="ERROR", status_message=str(last_exc))
                langfuse_generation.end()
            except Exception:
                pass

        logger.error("LLM call failed after {} attempts: {}", retries + 1, last_exc)
        raise last_exc or RuntimeError("LLM call failed")

    async def _mock_chat(self, messages: list[BaseMessage], tools: list[dict[str, Any]] | None = None) -> AIMessage:
        all_content = " ".join([str(m.content).lower() for m in messages if m.content])
        await asyncio.sleep(0.05)

        if "task-planning" in all_content or ("json array" in all_content and "step" in all_content):
            return AIMessage(
                content=json.dumps(
                    [
                        "Analyze the goal and gather relevant context from memory",
                        "Break down task into sub-components and research needed info",
                        "Execute core logic and generate output",
                        "Validate results, summarize and save to memory",
                    ],
                    ensure_ascii=False,
                )
            )

        if "reflection" in all_content or ("progress" in all_content and "repeating" in all_content and "diversity" in all_content):
            return AIMessage(content=json.dumps({"progress": True, "repeating": False, "diversity": 0.85}))

        if "memory consolidation" in all_content or "contradictions" in all_content:
            return AIMessage(
                content=json.dumps(
                    {
                        "entities": [
                            {"name": "A.S.T.R.A.", "type": "system"},
                            {"name": "Agent", "type": "concept"},
                            {"name": "Memory", "type": "concept"},
                        ],
                        "relations": [
                            {"source": "A.S.T.R.A.", "target": "Agent", "relation": "is_a"},
                            {"source": "Agent", "target": "Memory", "relation": "uses"},
                        ],
                        "contradictions": [],
                    },
                    ensure_ascii=False,
                )
            )

        goal = "task"
        for m in reversed(messages):
            if m.content and len(str(m.content)) > 10:
                if m.type == "human":
                    goal = str(m.content)[:300]
                    break
                goal = str(m.content)[:300]

        if "current step" in all_content:
            step_match = "unknown step"
            for m in messages:
                if "current step" in str(m.content).lower():
                    step_match = str(m.content).split("Current step")[1][:200] if "Current step" in str(m.content) else str(m.content)[:200]
                    break
            return AIMessage(content=f"[MOCK] Executed step: {step_match[:150]}. Result looks good.", tool_calls=[])

        return AIMessage(
            content=f"[MOCK A.S.T.R.A.] Task understood: '{goal[:200]}'. Mock response — would be real LLM in production.",
            tool_calls=[],
        )

    @staticmethod
    def _resolve_model() -> tuple[str, str, str]:
        if settings.llm_default_provider == LLMProvider.OPENROUTER:
            return settings.openrouter_model, settings.openrouter_llm_url, settings.openrouter_api_key
        return settings.local_llm_model, settings.local_llm_url, settings.local_llm_api_key

    def _resolve_model_with_override(
        self,
        provider_override: str | None = None,
        model_override: str | None = None,
        url_override: str | None = None,
    ) -> tuple[str, str, str]:
        # Determine effective provider
        effective_provider = provider_override or settings.llm_default_provider.value

        if effective_provider == "openrouter":
            model = model_override or settings.openrouter_model
            url = url_override or settings.openrouter_llm_url
            key = settings.openrouter_api_key
        elif effective_provider == "mock":
            model = model_override or settings.local_llm_model
            url = url_override or settings.local_llm_url
            key = settings.local_llm_api_key
        else:  # local or anything else
            model = model_override or settings.local_llm_model
            url = url_override or settings.local_llm_url
            key = settings.local_llm_api_key

        return model, url, key


llm_gateway = LLMGateway()
