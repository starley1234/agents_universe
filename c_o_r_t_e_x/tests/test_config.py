from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from c_o_r_t_e_x.config import Settings, load_dotenv


class ConfigTests(unittest.TestCase):
    def test_dotenv_comments_quotes_and_environment_precedence(self):
        names = ["APP_PORT", "ENVIRONMENT", "CORTEX_EVENT_BUS", "CUSTOM_REMOTE_URL"]
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".env"
                path.write_text(
                    "APP_PORT=9123 # inline comment\n"
                    "ENVIRONMENT=\"staging\"\n"
                    "CORTEX_EVENT_BUS=redis\n"
                    "CUSTOM_REMOTE_URL=\"https://example.test/v1#fragment\" # URL fragment\n",
                    encoding="utf-8",
                )
                self.assertEqual(load_dotenv(path), path.resolve())
                settings = Settings.from_env()
                self.assertEqual(settings.app_port, 9123)
                self.assertEqual(settings.environment, "staging")
                self.assertEqual(settings.event_bus_backend, "redis")
                self.assertEqual(settings.custom_remote_url, "https://example.test/v1#fragment")

                os.environ["APP_PORT"] = "9777"
                load_dotenv(path)
                self.assertEqual(Settings.from_env().app_port, 9777)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
