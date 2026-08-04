from __future__ import annotations

import unittest

from c_o_r_t_e_x.gateway.app import create_services
from c_o_r_t_e_x.gateway.toolkit_client import UnavailableToolkitProvider
from c_o_r_t_e_x.signals import ToolDescriptor


class FakeProvider:
    name = "fake"
    endpoint = "mock://fake"

    def list_tools(self):
        return [ToolDescriptor(name="demo.echo", description="echo", input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, attributes={"read_only": True})]

    def call_tool(self, name, arguments):
        return arguments["text"]


class MCPTests(unittest.TestCase):
    def test_tools_list_and_call(self):
        services = create_services(provider=FakeProvider())
        init = services.mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["name"], "cortex-mcp")
        listed = services.mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("cortex.search_tools", names)
        called = services.mcp.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cortex.call_tool", "arguments": {"name": "demo.echo", "arguments": {"text": "hi"}}}})
        self.assertFalse(called["result"]["isError"])
        self.assertIn("hi", called["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
