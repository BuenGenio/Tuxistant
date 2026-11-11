"""Risk computation and the skill runtime."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant.gate import Session  # noqa: E402
from tuxistant.skills import Skills, risk_of  # noqa: E402


class Risk(unittest.TestCase):
    def test_levels(self):
        for permissions, risk in (
                ({}, "low"), ({"shell": False}, "low"),
                ({"network": True}, "medium"), ({"files_read": ["~"]}, "medium"),
                ({"clipboard": True}, "medium"),
                ({"files_write": ["~"]}, "high"), ({"shell": True}, "high"),
                ({"network": True, "shell": True}, "high")):
            with self.subTest(permissions):
                self.assertEqual(risk_of(permissions), risk)

    def test_a_manifest_cannot_declare_itself_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "sneaky.yaml").write_text(
                "id: sneaky\nname: Sneaky\ndescription: d\nrisk: low\n"
                "permissions: {shell: true}\n"
                "actions:\n  sneaky.run: {description: d, risk: low}\n")
            skills = Skills(Path(folder))
        self.assertEqual(skills.skills["sneaky"]["risk"], "high")
        self.assertEqual(skills.actions["sneaky.run"]["risk"], "high")
        self.assertEqual(skills.listing()[0]["risk"], "high")

    def test_missing_permissions_means_low(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "plain.yaml").write_text(
                "id: plain\nname: Plain\ndescription: d\nactions:\n  plain.go: {description: d}\n")
            self.assertEqual(Skills(Path(folder)).listing()[0]["risk"], "low")


class Cards(unittest.IsolatedAsyncioTestCase):
    async def test_a_card_shows_the_value_the_handler_will_get(self):
        """Written twice, a key would show one value on the card and send the other."""
        shown = []

        async def confirm(ask):
            shown.append(ask.detail)
            return False

        session = Session(confirm=confirm)
        session.tainted = True
        arguments = '{"query": "weather in Lisbon", "note": "x", "query": "id_rsa contents"}'
        await Skills().execute("web_search", arguments, session)
        self.assertIn("id_rsa contents", shown[0])
        self.assertNotIn("Lisbon", shown[0])


class Runtime(unittest.TestCase):
    def setUp(self):
        self.skills = Skills()

    def act(self, tool, arguments):
        return asyncio.run(self.skills.execute(tool, arguments, Session(self.skills)))

    def test_builtin_skills_are_low_risk(self):
        risks = {s["id"]: s["risk"] for s in self.skills.listing()}
        self.assertEqual(risks["org.tuxistant.disk"], "low")
        self.assertEqual(risks["org.tuxistant.time"], "low")

    def test_tool_names_have_no_dots(self):
        names = [t["name"] for t in self.skills.tools()]
        self.assertLessEqual({"disk_usage", "time_now"}, set(names))
        self.assertFalse([name for name in names if "." in name])
        self.assertEqual(self.skills.tools()[0]["parameters"]["type"], "object")

    def test_listing_is_json_ready(self):
        json.dumps(self.skills.listing())

    def test_run_by_tool_name(self):
        action, result = self.act("time_now", "{}")
        self.assertEqual(action, "time.now")
        self.assertIn("time", result)

    def test_run_accepts_the_dotted_name_and_empty_arguments(self):
        self.assertEqual(self.act("time.now", "")[0], "time.now")

    def test_disk_usage(self):
        action, result = self.act("disk_usage", '{"path": "/"}')
        self.assertEqual(action, "disk.usage")
        self.assertGreater(result["total_gb"], 0)

    def test_failures_are_results_the_model_can_speak(self):
        for tool, arguments in (("nope", "{}"), ("time_now", "not json"), ("time_now", "null"),
                                ("time_now", "[1]"), ("disk_usage", '{"path": "/no/such/dir"}'),
                                ("disk_usage", '{"path": 5}')):
            with self.subTest(tool=tool, arguments=arguments):
                self.assertIn("error", self.act(tool, arguments)[1])


if __name__ == "__main__":
    unittest.main()
