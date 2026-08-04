from __future__ import annotations

import unittest
from datetime import datetime

from c_o_r_t_e_x.signals import Event, Task, TaskStatus, ToolDescriptor


class SignalsTests(unittest.TestCase):
    def test_event_roundtrip_and_context(self):
        event = Event.create("code_written", {"path": "main.py"}, source="coder", correlation_id="corr-1")
        payload = event.to_dict()
        restored = Event.from_dict(payload)
        self.assertEqual(restored.event_type, "code_written")
        self.assertEqual(restored.correlation_id, "corr-1")
        self.assertIsInstance(restored.occurred_at, datetime)

    def test_task_transition(self):
        task = Task(title="audit")
        task.transition(TaskStatus.RUNNING)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(task.version, 1)
        self.assertEqual(task.to_dict()["status"], "running")

    def test_mcp_schema(self):
        descriptor = ToolDescriptor.from_mcp({"name": "demo", "inputSchema": {"type": "object"}, "metadata": {"dangerous": True}})
        self.assertTrue(descriptor.dangerous)
        self.assertEqual(descriptor.input_schema["type"], "object")


if __name__ == "__main__":
    unittest.main()
