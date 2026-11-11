"""Evaluation setup must fail before a request can carry a key to a fallback address."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.eval_config import evaluation_server
from tuxistant.config import Config
from tests.test_realtime_live import CountingSkills
from tuxistant.gate import Session


class EvaluationServer(unittest.TestCase):
    def test_missing_server_refuses_and_explicit_override_is_memory_only(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"TUXISTANT_SERVER": ""}):
            path = Path(folder) / "config.json"
            config = Config(path)
            with self.assertRaises(SystemExit):
                evaluation_server(config)
            with patch.dict("os.environ", {"TUXISTANT_SERVER": "model.example.test:8080"}):
                evaluation_server(config)
            self.assertEqual(config["server"], "model.example.test:8080")
            self.assertFalse(path.exists())

    def test_explicit_local_server_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"TUXISTANT_SERVER": ""}):
            path = Path(folder) / "config.json"
            path.write_text('{"server": "localhost:8080"}')
            evaluation_server(Config(path))


class LiveCutoff(unittest.IsolatedAsyncioTestCase):
    async def test_every_unexpected_tool_is_blocked_before_a_handler_runs(self):
        skills = CountingSkills()
        for tool in skills.tools():
            if tool["name"] == "time_now":
                continue
            with patch("tuxistant.skills.Skills.execute",
                       side_effect=AssertionError("handler reached")) as execute:
                _, result = await skills.execute(tool["name"], "{}", Session())
            execute.assert_not_awaited()
            self.assertIn("error", result)
