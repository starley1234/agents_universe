from app.services.mcp_client import (
    INTERNAL_FETCH_TOOL,
    INTERNAL_FETCH_MANY_TOOL,
    INTERNAL_LIST_DIR_TOOL,
    INTERNAL_PYTHON_TOOL,
    INTERNAL_READ_FILE_TOOL,
    INTERNAL_SERVER_NAME,
    INTERNAL_WRITE_FILE_TOOL,
    _url_candidates,
    call_mcp_tool_sync,
    list_mcp_tools_sync,
)


def test_internal_mcp_tools_are_discoverable():
    tools = list_mcp_tools_sync([], include_internal=True)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_FETCH_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_FETCH_MANY_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_PYTHON_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_WRITE_FILE_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_READ_FILE_TOOL for tool in tools)
    assert any(tool["server_name"] == INTERNAL_SERVER_NAME and tool["name"] == INTERNAL_LIST_DIR_TOOL for tool in tools)


def test_internal_python_tool_runs(tmp_path):
    result = call_mcp_tool_sync(
        {"name": INTERNAL_SERVER_NAME, "url": "builtin://python", "enabled": True},
        INTERNAL_PYTHON_TOOL,
        {"code": "print(2 + 2)"},
        workspace_path=str(tmp_path),
    )
    assert result["is_error"] is False
    assert "4" in result["content"][0]["json"]["stdout"]


def test_mcp_url_candidates_rewrite_localhost_for_docker():
    candidates = _url_candidates("http://localhost:8090/sse/group/files")
    assert "http://host.docker.internal:8090/sse/group/files" in candidates
    assert "http://host.docker.internal:8090/mcp/group/files" in candidates


def test_internal_filesystem_tools(tmp_path):
    server = {"name": INTERNAL_SERVER_NAME, "url": "builtin://filesystem", "enabled": True}
    write = call_mcp_tool_sync(server, INTERNAL_WRITE_FILE_TOOL, {"path": "artifacts/demo.txt", "content": "hello"}, workspace_path=str(tmp_path))
    assert write["is_error"] is False
    read = call_mcp_tool_sync(server, INTERNAL_READ_FILE_TOOL, {"path": "artifacts/demo.txt"}, workspace_path=str(tmp_path))
    assert read["content"][0]["json"]["content"] == "hello"
    listing = call_mcp_tool_sync(server, INTERNAL_LIST_DIR_TOOL, {"path": "artifacts"}, workspace_path=str(tmp_path))
    assert "demo.txt" in listing["content"][0]["json"]["entries"]


def test_agent_extracts_multiline_and_array_mcp_calls(tmp_path):
    from app.agent.graph import AgentGraph

    graph = AgentGraph(tmp_path)
    content = '''Нужно создать файлы.
MCP_CALL_JSON:
{
  "server_name": "__internal__",
  "tool_name": "write_file",
  "arguments": {"path": "artifacts/a.md", "content": "A"}
}
Еще вызовы:
MCP_CALL_JSON: [
  {"server_name":"__internal__","tool_name":"write_file","arguments":{"path":"artifacts/b.md","content":"B"}},
  {"server_name":"__internal__","tool_name":"list_dir","arguments":{"path":"artifacts"}}
]
'''
    requests = graph._extract_mcp_call_requests(content)
    assert [request["tool_name"] for request in requests] == ["write_file", "write_file", "list_dir"]
    assert requests[1]["arguments"]["path"] == "artifacts/b.md"


def test_mcp_diagnostics_reports_disabled_server():
    from app.services.mcp_client import diagnose_mcp_servers_sync

    diagnostics = diagnose_mcp_servers_sync([
        {"name": "disabled", "url": "http://localhost:9999/sse", "transport": "sse", "enabled": False}
    ])
    assert diagnostics[0]["summary"] == "disabled"
    assert diagnostics[0]["attempts"] == []


def test_mcp_argument_normalization_reads_code_from_path(tmp_path):
    from app.services.mcp_client import _coerce_arguments_for_schema

    scad = tmp_path / "code" / "model.scad"
    scad.parent.mkdir()
    scad.write_text("cube([1,2,3]);", encoding="utf-8")
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "quality": {"type": "string", "enum": ["low", "medium", "high"], "default": "low"},
        },
        "required": ["code"],
    }
    args = _coerce_arguments_for_schema(schema, {"path": "code/model.scad"}, str(tmp_path), "openscad.render")
    assert args["code"] == "cube([1,2,3]);"
    assert args["quality"] == "low"
    assert args["source_path"] == "code/model.scad"


def test_mcp_argument_normalization_reports_missing_required():
    from app.services.mcp_client import MCPClientError, _coerce_arguments_for_schema

    schema = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}
    try:
        _coerce_arguments_for_schema(schema, {"path": "missing.scad"}, None, "openscad.render")
    except MCPClientError as exc:
        assert "отсутствуют обязательные поля" in str(exc)
    else:
        raise AssertionError("Expected MCPClientError")


def test_openscad_render_alias_prefers_schema_tool():
    from app.services.mcp_client import _choose_tool_name

    tools = [
        {"name": "render"},
        {"name": "render_2d_png"},
        {"name": "generate_and_analyze"},
    ]
    assert _choose_tool_name("render", tools, "openscad") == "render_2d_png"


def test_synthetic_schema_repairs_render_path_to_code(tmp_path):
    from app.services.mcp_client import _coerce_arguments_for_schema, _synthetic_schema_for_tool

    scad = tmp_path / "code" / "model.scad"
    scad.parent.mkdir()
    scad.write_text("sphere(5);", encoding="utf-8")
    args = _coerce_arguments_for_schema(_synthetic_schema_for_tool("render"), {"path": "code/model.scad"}, str(tmp_path), "openscad.render")
    assert args["code"] == "sphere(5);"
    assert args["quality"] == "low"
