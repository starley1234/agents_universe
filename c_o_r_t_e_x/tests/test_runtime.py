from __future__ import annotations

import unittest

from c_o_r_t_e_x.bus import InMemoryEventBus, SharedBlackboard
from c_o_r_t_e_x.runtime import CircuitBreaker, CircuitOpenError, ToolCatalog
from c_o_r_t_e_x.signals import ToolDescriptor


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.value = 0

    def list_tools(self):
        return [ToolDescriptor(name="math.add", description="add numbers", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, skills=["math"], attributes={"read_only": True})]

    def call_tool(self, name, arguments):
        self.value += arguments["a"]
        return self.value


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_execute_and_hot_swap(self):
        bus = InMemoryEventBus()
        catalog = ToolCatalog(bus)
        provider = FakeProvider()
        catalog.mount("fake", provider)
        result = await catalog.execute("math.add", {"a": 2}, correlation_id="corr")
        self.assertTrue(result.success)
        self.assertEqual(result.result, 2)
        self.assertEqual(catalog.schemas(query="math")[0]["name"], "math.add")
        # Tool arguments are not copied into the event log verbatim.
        secret_result = await catalog.execute("math.add", {"a": 1, "api_key": "do-not-log"})
        self.assertTrue(secret_result.success)
        started = [event for event in bus.history(pattern="tool.call.started")][-1]
        self.assertEqual(started.payload["arguments"]["api_key"], "***redacted***")
        await catalog.hot_swap("fake", provider, reason="test")
        self.assertTrue(any(e.event_type == "runtime.tool_hot_swapped" for e in bus.history()))

    async def test_breaker_opens(self):
        breaker = CircuitBreaker("x", failure_threshold=2, recovery_timeout=20)
        async def fail():
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            await breaker.call(fail)
        with self.assertRaises(RuntimeError):
            await breaker.call(fail)
        self.assertEqual(breaker.state, "open")
        with self.assertRaises(CircuitOpenError):
            await breaker.call(lambda: 1)


if __name__ == "__main__":
    unittest.main()
