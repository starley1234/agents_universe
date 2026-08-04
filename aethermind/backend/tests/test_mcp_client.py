from app.services.mcp_client import INTERNAL_FETCH_TOOL, INTERNAL_SERVER_NAME, list_mcp_tools_sync


def test_internal_mcp_fetch_tool_is_discoverable():
    tools = list_mcp_tools_sync([], include_internal=True)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_FETCH_TOOL for tool in tools)
