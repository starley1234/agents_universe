"""Unified LLM gateway powered by LiteLLM.

Converts between LangChain message objects and LiteLLM's dict format,
handles tool-calls round-trip, and provides a single ``.chat()`` entry
point used by every module in the project.
"""

from __future__ import annotations

import json
from typing import Any

import litellm
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, HumanMessage
from loguru import logger

from astra.config import LLMProvider, settings

# Suppress LiteLLM's own logging noise
litellm.suppress_debug_info = True


def _lc_to_litellm(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """LangChain messages → LiteLLM/OpenAI dict format."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = {
            "system": "system",
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
        }.get(m.type, "user")

        entry: dict[str, Any] = {"role": role, "content": m.content}

        # Preserve tool_calls on assistant messages
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

        # Tool messages need tool_call_id
        if m.type == "tool":
            entry["tool_call_id"] = getattr(m, "tool_call_id", "")

        out.append(entry)
    return out


def _convert_tools_for_litellm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure tools are in OpenAI format for LiteLLM.

    MCP tools arrive as ``{"name": ..., "description": ..., "input_schema": ...}``.
    LiteLLM expects ``{"type": "function", "function": {"name": ..., "parameters": ...}}``.
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        if "type" in t and t["type"] == "function":
            # Already OpenAI format
            out.append(t)
        elif "function" in t:
            out.append(t)
        else:
            # MCP format → OpenAI format
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            })
    return out


class LLMGateway:
    """Single interface for all LLM calls, routing through LiteLLM."""

    async def chat(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AIMessage:
        """Send a chat completion request and return an AIMessage."""
        model, api_base, api_key = self._resolve_model()
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

        logger.debug("LLM call: model={} msgs={} tools={}", model, len(lc_messages), len(tools or []))

        try:
            response = await litellm.acompletion(**params)
            choice = response.choices[0]
            content = choice.message.content or ""

            # Parse tool_calls into LangChain format
            tool_calls: list[dict[str, Any]] = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "args": args,
                    })

            logger.debug(
                "LLM response: {} chars, {} tool_calls",
                len(content),
                len(tool_calls),
            )
            return AIMessage(content=content, tool_calls=tool_calls)

        except Exception as exc:
            logger.error("LLM call failed: {}", exc)
            raise

    @staticmethod
    def _resolve_model() -> tuple[str, str, str]:
        """Return ``(model_name, api_base, api_key)`` for the active provider."""
        if settings.llm_default_provider == LLMProvider.OPENROUTER:
            return settings.openrouter_model, settings.openrouter_llm_url, settings.openrouter_api_key
        return settings.local_llm_model, settings.local_llm_url, settings.local_llm_api_key


llm_gateway = LLMGateway()
