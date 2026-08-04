from __future__ import annotations

import json
import unittest

from c_o_r_t_e_x.gateway.app import FallbackApp, create_app, create_services


class APITests(unittest.TestCase):
    def test_health_and_mcp_over_fallback(self):
        services = create_services()
        app = create_app(services)
        if isinstance(app, FallbackApp):
            status, headers, body = app.handle("GET", "/health")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "ok")
            status, _, body = app.handle("POST", "/sse", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["result"]["tools"])
        else:
            self.assertTrue(app.title.startswith("C.O.R.T.E.X"))


if __name__ == "__main__":
    unittest.main()
