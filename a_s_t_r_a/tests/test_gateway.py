"""Tests for LLM gateway — message conversion, tool format handling."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from astra.llm.gateway import _convert_tools_for_litellm, _lc_to_litellm


def test_lc_to_litellm_system_message():
    msgs = [SystemMessage(content="hello")]
    result = _lc_to_litellm(msgs)
    assert result == [{"role": "system", "content": "hello"}]


def test_lc_to_litellm_human_message():
    msgs = [HumanMessage(content="hi")]
    result = _lc_to_litellm(msgs)
    assert result == [{"role": "user", "content": "hi"}]


def test_lc_to_litellm_ai_with_tool_calls():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "search", "args": {"q": "test"}}],
        )
    ]
    result = _lc_to_litellm(msgs)
    assert result[0]["role"] == "assistant"
    assert len(result[0]["tool_calls"]) == 1
    assert result[0]["tool_calls"][0]["id"] == "c1"
    assert result[0]["tool_calls"][0]["function"]["name"] == "search"
    # args should be JSON string
    assert json.loads(result[0]["tool_calls"][0]["function"]["arguments"]) == {"q": "test"}


def test_lc_to_litellm_tool_message():
    msgs = [ToolMessage(content="result", tool_call_id="c1", name="search")]
    result = _lc_to_litellm(msgs)
    assert result[0]["role"] == "tool"
    assert result[0]["tool_call_id"] == "c1"
    assert result[0]["content"] == "result"


def test_convert_tools_mcp_format():
    mcp_tools = [
        {"name": "search", "description": "Search the web", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}
    ]
    result = _convert_tools_for_litellm(mcp_tools)
    assert len(result) == 1
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "search"
    assert result[0]["function"]["parameters"]["type"] == "object"


def test_convert_tools_already_openai():
    openai_tools = [
        {"type": "function", "function": {"name": "test", "parameters": {}}}
    ]
    result = _convert_tools_for_litellm(openai_tools)
    assert result == openai_tools
