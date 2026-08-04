from app.services.mcp_client import (
    INTERNAL_FETCH_TOOL,
    INTERNAL_PYTHON_TOOL,
    INTERNAL_SERVER_NAME,
    call_mcp_tool_sync,
    list_mcp_tools_sync,
)


def test_internal_mcp_tools_are_discoverable():
    tools = list_mcp_tools_sync([], include_internal=True)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_FETCH_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_PYTHON_TOOL for tool in tools)


def test_internal_python_tool_runs(tmp_path):
    result = call_mcp_tool_sync(
        {"name": INTERNAL_SERVER_NAME, "url": "builtin://python", "enabled": True},
        INTERNAL_PYTHON_TOOL,
        {"code": "print(2 + 2)"},
        workspace_path=str(tmp_path),
    )
    assert result["is_error"] is False
    assert "4" in result["content"][0]["json"]["stdout"]
