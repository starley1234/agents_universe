"""Тесты интеграции с Model Context Protocol (MCP: клиент и сервер)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import ToolRegistry
from agent_toolkit.integrations import MCPClient, MCPServer, build_mcp_tools
from agent_toolkit.local import build_template_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        section("1. MCP Сервер (MCPServer JSON-RPC 2.0)")
        reg = ToolRegistry()
        for t in build_template_tools(tmp):
            reg.add(t)

        srv = MCPServer(registry=reg, server_name="test-mcp")

        # 1) initialize
        init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        init_res = srv.handle_rpc(init_req)
        check("initialize возвращает id=1", init_res.get("id") == 1)
        check("initialize содержит serverInfo", init_res["result"]["serverInfo"]["name"] == "test-mcp")

        # 2) tools/list
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        list_res = srv.handle_rpc(list_req)
        tools = list_res["result"]["tools"]
        check("tools/list возвращает 4 инструмента", len(tools) == 4)
        check("инструменты содержат inputSchema", "inputSchema" in tools[0])
        check("инструменты содержат скилсы в metadata", "skills" in tools[0]["metadata"])

        # 3) tools/call
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "templates.list_templates",
                "arguments": {},
            },
        }
        call_res = srv.handle_rpc(call_req)
        check("tools/call успешно выполнен (isError=False)", call_res["result"]["isError"] is False)
        check("tools/call возвращает текст", "report_md" in call_res["result"]["content"][0]["text"])

        # 4) Неизвестный метод
        err_req = {"jsonrpc": "2.0", "id": 4, "method": "unknown/method", "params": {}}
        err_res = srv.handle_rpc(err_req)
        check("неизвестный метод возвращает ошибку -32601", err_res["error"]["code"] == -32601)

        section("2. MCP Клиент (MCPClient и инструменты)")
        client = MCPClient()
        client.register_mock_tool("calc", "Сумма двух чисел", lambda a, b: a + b)
        client_tools = client.list_tools()
        check("клиент видит зарегистрированный мок", len(client_tools) == 1 and client_tools[0]["name"] == "calc")
        res_calc = client.call_tool("calc", {"a": 10, "b": 15})
        check("клиент успешно вызывает инструмент", res_calc == "25")

        mcp_tools = {t.name: t for t in build_mcp_tools(client=client)}
        check("инструменты MCP зарегистрированы", len(mcp_tools) == 2)

        res_list_tool = mcp_tools["mcp.list_remote_tools"].execute()
        check("mcp.list_remote_tools показывает calc", "calc" in res_list_tool)

        res_call_tool = mcp_tools["mcp.call_remote_tool"].execute(
            name="calc", arguments_json='{"a": 2, "b": 3}'
        )
        check("mcp.call_remote_tool выполняет вызов", res_call_tool == "5")

    return summary("Тесты интеграции MCP")


def test_mcp_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
