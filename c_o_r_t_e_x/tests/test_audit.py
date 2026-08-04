from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_o_r_t_e_x.signals import ToolDescriptor
from c_o_r_t_e_x.workflows.tool_audit import ToolAuditWorkflow


class FakeAuditProvider:
    name = "fake-provider"

    def list_tools(self):
        return [
            ToolDescriptor(name="safe.echo", description="read only", input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, attributes={"read_only": True}),
            ToolDescriptor(name="danger.exec", description="dangerous", dangerous=True),
        ]

    def call_tool(self, name, arguments):
        return arguments["text"]


class AuditTests(unittest.TestCase):
    def test_report_distinguishes_policy_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = ToolAuditWorkflow(FakeAuditProvider(), workspace=Path(directory), native_diagnostics=False, allow_network=False, allow_side_effects=False)
            report = audit.run()
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.coverage_percent, 50.0)
        self.assertTrue(report.recommendations)


if __name__ == "__main__":
    unittest.main()
