from __future__ import annotations

import asyncio
import unittest

from c_o_r_t_e_x.bus import BlackboardConflict, InMemoryEventBus, SharedBlackboard
from c_o_r_t_e_x.signals import Event


class BusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_wildcard_and_history(self):
        bus = InMemoryEventBus()
        subscription = bus.subscribe("tool.*")
        await bus.publish(Event.create("task.created", {}, source="test"))
        await bus.publish(Event.create("tool.call.completed", {"name": "x"}, source="test"))
        event = await asyncio.wait_for(subscription.get(), timeout=0.2)
        self.assertEqual(event.event_type, "tool.call.completed")
        self.assertEqual(len(bus.history(pattern="tool.*")), 1)
        await subscription.close()

    async def test_blackboard_cas(self):
        bus = InMemoryEventBus()
        board = SharedBlackboard(bus)
        entry = await board.write("project/plan", {"step": 1})
        self.assertEqual(entry.version, 1)
        updated = await board.write("project/plan", {"step": 2}, expected_version=1)
        self.assertEqual(updated.version, 2)
        with self.assertRaises(BlackboardConflict):
            await board.write("project/plan", {}, expected_version=1)
        self.assertEqual(board.read("project/plan"), {"step": 2})
        self.assertTrue(any(e.event_type == "blackboard.updated" for e in bus.history()))


if __name__ == "__main__":
    unittest.main()
