"""The approval gate: what Skills.execute lets through, and how the controller asks."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import skills as skills_module  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.controller import Controller  # noqa: E402
from tuxistant.gate import Ask, Session, printable, shown  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.skills import Skills, leaves_home, reads_private  # noqa: E402

MANIFEST = """
id: org.test.gate
name: Gate
description: d
permissions: {permissions}
actions:
  gate.go: {{description: d}}
"""


def skills_with(permissions: str, handler) -> Skills:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "gate.yaml").write_text(MANIFEST.format(permissions=permissions))
        built = Skills(Path(folder))
    patcher = mock.patch.dict(skills_module.HANDLERS, {"gate.go": handler})
    patcher.start()
    built.stop = patcher.stop
    return built


class Clicks:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def __call__(self, ask):
        self.asked.append(ask)
        return self.answers.pop(0) if self.answers else False


class Execute(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, permissions, handler, clicks, session=None):
        built = skills_with(permissions, handler)
        self.addCleanup(built.stop)
        session = session or Session(built, clicks)
        return session, (await built.execute("gate_go", "{}", session))[1]

    async def test_an_ask_runs_only_after_a_yes(self):
        ran = []
        ask = Ask("Do it?", "the detail", lambda: ran.append(1) or {"done": True})
        clicks = Clicks(True)
        _, result = await self.run_with("{}", lambda *_: ask, clicks)
        self.assertEqual(result, {"done": True})
        self.assertEqual(ran, [1])
        self.assertEqual(clicks.asked, [ask])

    async def test_a_no_runs_nothing_and_tells_the_model(self):
        ran = []
        _, result = await self.run_with(
            "{}", lambda *_: Ask("Do it?", "d", lambda: ran.append(1)), Clicks(False))
        self.assertTrue(result["declined"])
        self.assertEqual(ran, [])

    async def test_with_nobody_to_ask_the_answer_is_no(self):
        ran = []
        built = skills_with("{}", lambda *_: Ask("Do it?", "d", lambda: ran.append(1)))
        self.addCleanup(built.stop)
        result = (await built.execute("gate_go", "{}", Session(built)))[1]
        self.assertTrue(result["declined"])
        self.assertEqual(ran, [])

    async def test_reading_something_private_taints_the_conversation(self):
        for permissions, tainted in (("{}", False), ("{files_read: true}", True),
                                     ("{shell: true}", True), ("{clipboard: true}", True),
                                     ("{network: [api.example.com]}", False)):
            with self.subTest(permissions):
                session, _ = await self.run_with(permissions, lambda *_: {"ok": 1}, Clicks())
                self.assertEqual(session.tainted, tainted)

    async def test_a_failure_does_not_taint(self):
        def boom(*_):
            raise ValueError("no such file")
        session, result = await self.run_with("{files_read: true}", boom, Clicks())
        self.assertEqual(result, {"error": "no such file"})
        self.assertFalse(session.tainted)

    async def test_after_private_data_nothing_leaves_without_a_click(self):
        ran = []
        clicks = Clicks(False)
        session = Session(None, clicks)
        session.tainted = True
        built = skills_with("{network: [evil.example.com]}", lambda *_: ran.append(1) or {})
        self.addCleanup(built.stop)
        session.skills = built
        result = (await built.execute("gate_go", "{}", session))[1]
        self.assertTrue(result["declined"])
        self.assertEqual(ran, [])
        self.assertIn("evil.example.com", clicks.asked[0].title)

    async def test_the_users_own_server_is_still_home(self):
        ran = []
        session = Session(None, Clicks())
        session.tainted = True
        built = skills_with("{network: [home]}", lambda *_: ran.append(1) or {"ok": 1})
        self.addCleanup(built.stop)
        session.skills = built
        self.assertEqual((await built.execute("gate_go", "{}", session))[1], {"ok": 1})
        self.assertEqual(ran, [1])

    async def test_an_async_handler_runs_on_the_loop_and_can_ask(self):
        clicks = Clicks(True)

        async def handler(_args, _config, session):
            return {"answer": await session.confirm(Ask("Sure?", "d", dict))}
        _, result = await self.run_with("{}", handler, clicks)
        self.assertEqual(result, {"answer": True})

    async def test_arguments_that_are_not_an_object_are_an_error_the_model_can_read(self):
        built = Skills()
        for arguments in ("[1]", "{nope"):
            with self.subTest(arguments):
                result = (await built.execute("time_now", arguments, Session(built)))[1]
                self.assertIn("error", result)


class Permissions(unittest.TestCase):
    def test_leaves_home(self):
        for network, leaves in ((None, False), ([], False), (["home"], False), (True, True),
                                (["home", "api.example.com"], True)):
            with self.subTest(network):
                self.assertEqual(leaves_home({"network": network}), leaves)

    def test_reads_private(self):
        self.assertFalse(reads_private({"network": True}))
        self.assertTrue(reads_private({"files_read": ["~"]}))


class Printable(unittest.TestCase):
    def test_nothing_can_hide_in_the_text(self):
        for text in ("rm -rf ~\rls", "a\x1b[2Kb", "safe‮exe.fdp", "x⁦y", "zero​width"):
            with self.subTest(text):
                shown = printable(text, 100)
                self.assertTrue(shown.isprintable() or "\n" in shown)
                self.assertIn("\\u", shown)

    def test_unicode_line_separators_cannot_fake_a_line(self):
        for char in "\u2028\u2029":
            self.assertNotIn(char, printable(f"a{char}b", 100))

    def test_a_name_cannot_fake_a_line_of_the_card(self):
        self.assertNotIn("\n", shown("old.log\n\nAll other files are kept."))
        self.assertIn("\\n", shown("a\nb"))

    def test_lines_and_ordinary_text_stay(self):
        self.assertEqual(printable("ls -l\n¿Dónde? 駅", 100), "ls -l\n¿Dónde? 駅")

    def test_a_cut_says_so(self):
        self.assertIn("5 more characters", printable("x" * 15, 10))


class Asking(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        self.signals = []
        self.ctl = Controller(Config(path / "c.json"), History(path / "h.db"), Skills(),
                              emit=lambda *signal: self.signals.append(signal))
        self.ctl.face = True

    def card(self):
        return next(s for s in self.signals if s[0] == "Approval")

    async def asking(self, **kwargs):
        task = asyncio.create_task(self.ctl.ask(Ask("Do it?", "the detail", dict, **kwargs)))
        await asyncio.sleep(0)
        return task

    async def test_a_click_is_the_answer(self):
        for allow in (True, False):
            with self.subTest(allow):
                self.signals.clear()
                task = await self.asking()
                self.ctl.answer(self.card()[1], allow)
                self.assertEqual(await task, allow)
                self.assertIn(("ApprovalClosed", self.card()[1]), self.signals)

    async def test_the_card_shows_the_blob_and_then_puts_it_back(self):
        task = await self.asking()
        self.assertTrue(self.ctl.presence)
        self.ctl.answer(self.card()[1], True)
        await task
        self.assertFalse(self.ctl.presence)

    async def test_the_card_says_what_the_handler_said(self):
        task = await self.asking(level="admin")
        _, _, title, detail, level, seconds = self.card()
        self.assertEqual((title, detail, level), ("Do it?", "the detail", "admin"))
        self.assertGreater(seconds, 0)
        self.ctl.answer(self.card()[1], False)
        await task

    async def test_a_wrong_id_changes_nothing(self):
        task = await self.asking()
        self.ctl.answer("0" * 32, True)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.ctl.answer(self.card()[1], False)
        self.assertFalse(await task)

    async def test_a_late_click_cannot_approve_the_next_card(self):
        task = await self.asking()
        first = self.card()[1]
        self.ctl.answer(first, False)
        await task
        self.signals.clear()
        task = await self.asking()
        self.ctl.answer(first, True)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.ctl.answer(self.card()[1], False)
        self.assertFalse(await task)

    async def test_no_answer_is_no(self):
        with mock.patch("tuxistant.controller.APPROVAL_WAIT_S", 0.01):
            self.assertFalse(await (await self.asking()))

    async def test_cancelling_takes_the_card_down(self):
        task = await self.asking()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any(s[0] == "ApprovalClosed" for s in self.signals))
        self.assertIsNone(self.ctl.approval)

    async def test_a_locked_screen_is_a_no_without_a_card(self):
        self.ctl.locked = True
        self.assertFalse(await (await self.asking()))
        self.assertFalse(any(s[0] == "Approval" for s in self.signals))

    async def test_one_card_at_a_time(self):
        first = await self.asking()
        self.assertFalse(await (await self.asking()))
        self.ctl.answer(self.card()[1], False)
        await first

    async def test_hostile_text_is_escaped_on_the_way_to_the_card(self):
        task = asyncio.create_task(self.ctl.ask(Ask("ok‮?", "ls\rrm -rf ~", dict)))
        await asyncio.sleep(0)
        _, request, title, detail, *_ = self.card()
        self.assertNotIn("‮", title)
        self.assertNotIn("\r", detail)
        self.ctl.answer(request, False)
        await task

    async def test_with_nothing_to_draw_a_card_the_user_hears_why_at_once(self):
        self.ctl.face = False
        with self.assertRaisesRegex(RuntimeError, "Log out"):
            await self.ctl.ask(Ask("Do it?", "d", dict))
        self.assertFalse(any(s[0] == "Approval" for s in self.signals))

    async def test_the_selection_comes_back_from_the_shell(self):
        task = asyncio.create_task(self.ctl.selection())
        await asyncio.sleep(0)
        request = next(s for s in self.signals if s[0] == "SelectionWanted")[1]
        self.ctl.selection_text("wrong", "nope")
        self.ctl.selection_text(request, "hello")
        self.assertEqual(await task, "hello")

    async def test_no_shell_means_no_selection(self):
        with mock.patch("tuxistant.controller.SELECTION_WAIT_S", 0.01):
            self.assertEqual(await self.ctl.selection(), "")


class Secrets(unittest.TestCase):
    def test_public_config_holds_neither_key(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(Path(folder) / "c.json")
            config.set("cloud_api_key", "sk-secret")
            config.values["api_key"] = "other-secret"
            shown = json.dumps(config.public())
        self.assertNotIn("secret", shown)
        self.assertTrue(config.public()["cloud_key_set"])

    def test_the_cloud_is_off_until_asked_for(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(Config(Path(folder) / "c.json")["cloud_enabled"])


if __name__ == "__main__":
    unittest.main()
