"""Evaluations and the live test: the key goes only to a server chosen for the run, and every
real handler they run meets a stand-in in place of the desktop, the phone, the home and the mail.
Nothing here reaches a network: the guard is a check of how an address is written."""
import ast
import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tools import eval_cutoffs
from tools.eval_config import Refused, evaluation_server, on_this_computer
from tools.eval_cutoffs import ACTIONS, install, owner, stand_ins
from tests.test_realtime_live import CountingSkills
from tuxistant import calendars, homeassistant, mail, mcp, media, phone, timers
from tuxistant.config import DEFAULTS, Config
from tuxistant.gate import Session
from tuxistant.skillhost import BUNDLED, Offline
from tuxistant.skills import HANDLERS

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-" + "e" * 40     # made up


class EvaluationServer(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        env = patch.dict("os.environ", {"TUXISTANT_SERVER": ""})
        env.start()
        self.addCleanup(env.stop)

    def config(self, saved: str | None = None) -> Config:
        if saved is not None:
            self.path.write_text(f'{{"server": "{saved}", "api_key": "{KEY}"}}')
        config = Config(self.path)
        config.values["api_key"] = KEY
        return config

    def refused(self, config, argv=None) -> str:
        with self.assertRaises(SystemExit) as stopped:
            evaluation_server(config, ["tool"] if argv is None else argv)
        self.assertEqual(config["api_key"], "", "nothing is left to send")
        self.assertNotIn(KEY, str(stopped.exception))
        return str(stopped.exception)

    def test_nothing_saved_and_nothing_named_is_refused(self):
        self.assertIn("TUXISTANT_SERVER", self.refused(self.config()))

    def test_the_default_is_refused_even_when_written_down(self):
        self.refused(self.config("localhost:8080"))
        self.refused(self.config("http://localhost:8080/"))

    def test_a_saved_address_on_this_computer_is_refused(self):
        for saved in ("127.0.0.1:8080", "localhost:9000", "[::1]:8080", "0.0.0.0:8080",
                      "box.localhost:8080", "https://127.0.0.2"):
            with self.subTest(saved):
                self.refused(self.config(saved))

    def test_every_form_the_resolver_reads_as_this_computer_is_refused(self):
        """getaddrinfo reads all of these as 127.0.0.1 or 0.0.0.0; ipaddress rejects most."""
        for saved in ("127.1:8080", "2130706433:8080", "0x7f000001:8080", "0177.0.0.1:8080",
                      "0x7f.1:8080", "0:8080", "[::ffff:127.0.0.1]:8080", "[::]:8080",
                      "ip6-localhost:8080", "ip6-loopback:8080", "localhost.localdomain:8080",
                      "LOCALHOST.:8080", "http://localhost6:8080"):
            with self.subTest(saved):
                self.refused(self.config(saved))

    def test_this_computers_own_name_is_refused(self):
        """Debian and Ubuntu map it to 127.0.1.1 in /etc/hosts."""
        with patch("socket.gethostname", return_value="tuxbox.lan"):
            for saved in ("tuxbox:8080", "tuxbox.lan:8080", "TuxBox:8080"):
                with self.subTest(saved):
                    self.refused(self.config(saved))
            self.assertFalse(on_this_computer("other.lan:8080"))

    def test_addresses_elsewhere_are_not_taken_for_this_computer(self):
        for server in ("192.168.1.5:8080", "10.0.0.2:8080", "3232235781:8080",
                       "[2001:db8::1]:8080", "[::ffff:192.168.1.5]:8080", "model.example.test",
                       "localhost.example.test:8080", "127.example.test:8080"):
            with self.subTest(server):
                self.assertFalse(on_this_computer(server))

    def test_a_refusal_still_ends_a_tool_that_does_not_catch_it(self):
        with self.assertRaises(SystemExit) as stopped:
            evaluation_server(self.config(), ["tool"])
        self.assertIsInstance(stopped.exception, Refused)

    def test_a_saved_server_elsewhere_is_used(self):
        config = self.config("model.example.test:8080")
        self.assertEqual(evaluation_server(config, ["tool"]), "model.example.test:8080")
        self.assertEqual(config["api_key"], KEY)

    def test_a_server_named_for_the_run_is_used_even_here_and_never_saved(self):
        for env, argv in (("localhost:8080", ["tool"]), ("", ["tool", "--server", "127.0.0.1:8080"]),
                          ("", ["tool", "--server=localhost:8080"])):
            with self.subTest(env=env, argv=argv), patch.dict("os.environ",
                                                              {"TUXISTANT_SERVER": env}):
                config = self.config()
                server = evaluation_server(config, argv)
                self.assertTrue(on_this_computer(server))
                self.assertEqual(config["server"], server)
                self.assertEqual(config["api_key"], KEY)
        self.assertFalse(self.path.exists())

    def test_the_flag_wins_and_is_taken_out_of_the_arguments(self):
        argv = ["tool", "3", "--server", "model.example.test:8080", "pause"]
        with patch.dict("os.environ", {"TUXISTANT_SERVER": "other.example.test:8080"}):
            self.assertEqual(evaluation_server(self.config(), argv), "model.example.test:8080")
        self.assertEqual(argv, ["tool", "3", "pause"])

    def test_a_bad_name_is_refused(self):
        self.refused(self.config(), ["tool", "--server"])
        self.refused(self.config(), ["tool", "--server", "not a server"])
        with patch.dict("os.environ", {"TUXISTANT_SERVER": "ftp://x"}):
            self.refused(self.config("model.example.test:8080"))

    def test_each_tool_asks_before_it_reads_the_key(self):
        """Read and never imported: importing one would run it."""
        for name in ("eval_routing", "eval_flows", "eval_facts"):
            with self.subTest(name):
                source = (ROOT / "tools" / f"{name}.py").read_text()
                self.assertEqual(source.count("evaluation_server("), 1)
                guard = source.index("evaluation_server(")
                for use in ('["api_key"]', "parse_args(", "websockets.connect(", "sys.argv["):
                    if use in source:
                        self.assertLess(guard, source.index(use), use)
                self.assertNotIn("localhost", source)

    def test_every_tool_that_reads_the_settings_asks_first(self):
        """Config holds the key, so the guard is the very next statement after every Config()."""
        tools = ("eval_routing", "eval_flows", "eval_facts", "check_voices", "demo_video")
        for name in tools:
            with self.subTest(name):
                source = (ROOT / "tools" / f"{name}.py").read_text()
                self.assertGreater(_guarded_configs(ast.parse(source)), 0)
                self.assertNotIn("localhost", source)

    def test_no_other_tool_reads_the_settings(self):
        """A new tool that reads the key is added to the list above, with the guard."""
        listed = {"eval_routing", "eval_flows", "eval_facts", "check_voices", "demo_video"}
        for path in (ROOT / "tools").glob("*.py"):
            if path.stem in listed:
                continue
            with self.subTest(path.name):
                calls = [node for node in ast.walk(ast.parse(path.read_text()))
                         if _calls(node, "Config")]
                self.assertEqual(calls, [])


def _guarded_configs(tree: ast.Module) -> int:
    """How many Config() calls there are, after failing if one is not followed by the guard."""
    found = 0
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, statement in enumerate(body):
            if hasattr(statement, "body"):      # looked at from inside, by the walk
                continue
            if not any(_calls(part, "Config") for part in ast.walk(statement)):
                continue
            after = body[i + 1] if i + 1 < len(body) else None
            if after is None or not any(_calls(part, "evaluation_server")
                                        for part in ast.walk(after)):
                raise AssertionError(f"line {statement.lineno}: Config() with no guard after it")
            found += 1
    return found


class DemoVideo(unittest.TestCase):
    """Imported for real: it runs nothing until main(), and nothing here films or speaks."""
    def setUp(self):
        import sys
        sys.path.insert(0, str(ROOT / "tests" / "shell_harness"))
        self.addCleanup(sys.path.remove, str(ROOT / "tests" / "shell_harness"))
        try:
            from tools import demo_video
        except ImportError as e:     # Pillow is needed only to cut the film
            self.skipTest(str(e))
        self.demo = demo_video
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)
        saved = self.work / "config.json"
        saved.write_text(f'{{"api_key": "{KEY}"}}')
        made = patch.object(demo_video, "Config", lambda: Config(saved))
        made.start()
        self.addCleanup(made.stop)

    def test_the_default_server_makes_the_film_silent_and_sends_nothing(self):
        with patch.dict("os.environ", {"TUXISTANT_SERVER": ""}), \
                patch.object(self.demo, "speech", side_effect=AssertionError("spoke")), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            self.demo.voices(self.work, silent=False)
        self.assertIn("silent", said.getvalue())
        self.assertNotIn(KEY, said.getvalue())
        self.assertFalse((self.work / "voice").exists())
        self.assertTrue((self.work / "voice.json").exists())

    def test_a_server_named_for_the_run_is_used(self):
        self.assertEqual(self.demo.chosen("model.example.test:8080")["server"],
                         "model.example.test:8080")
        self.assertEqual(self.demo.chosen("localhost:8080")["api_key"], KEY)


def _top_level(tree: ast.Module, test) -> int:
    return next(i for i, node in enumerate(tree.body)
                if any(test(part) for part in ast.walk(node)))


def _calls(node, name: str) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) == name


class EveryActionIsCutOff(unittest.TestCase):
    def test_every_action_there_is_is_named(self):
        """A new action fails here until tools/eval_cutoffs.py says what it meets in an
        evaluation, so neither eval_flows nor the live test can reach something new by surprise."""
        code = {name for file in BUNDLED.glob("*/skill.yaml")
                for name in yaml.safe_load(file.read_text())["actions"]}
        self.assertTrue(code)
        self.assertEqual(set(ACTIONS), set(HANDLERS) | code)

    def test_every_cut_named_has_a_stand_in(self):
        with tempfile.TemporaryDirectory() as folder:
            named = stand_ins(Path(folder))
        for action, cuts in ACTIONS.items():
            for cut in cuts:
                self.assertIn(cut, named, action)

    def test_each_stand_in_is_in_place_and_put_back_after(self):
        with tempfile.TemporaryDirectory() as folder:
            names = list(stand_ins(Path(folder)))
        real = {name: getattr(*owner(name)) for name in names}
        with install():
            for name in names:
                with self.subTest(name):
                    self.assertNotEqual(getattr(*owner(name)), real[name])
        for name in names:      # a bound method is made anew on each look, so not "is"
            self.assertEqual(getattr(*owner(name)), real[name], name)

    def test_what_has_no_card_is_blocked_or_made_up(self):
        with install(), contextlib.redirect_stdout(io.StringIO()):
            for stand_in in (phone._activate, calendars.Eds, timers.notify,
                             homeassistant._opener.open, calendars._backend().add):
                with self.assertRaises(RuntimeError):
                    stand_in("anything")
            self.assertIsInstance(asyncio.run(media._connect()), eval_cutoffs.MadeUpPlayer)
            self.assertIsInstance(media._wpctl, eval_cutoffs.MadeUpVolume)
            self.assertIsInstance(mcp.SkillProcess("Lights", None, {}), Offline)
            real_home = {**DEFAULTS, "ha_server": "192.168.1.5:8123", "ha_token": "x" * 40}
            self.assertEqual(homeassistant._connect(real_home).url, "http://eval.invalid")
            self.assertIsInstance(homeassistant._call.__self__, eval_cutoffs.NoHome)
            temp = tempfile.gettempdir()
            self.assertTrue(all(root.startswith(temp) for root in mail.ROOTS), mail.ROOTS)
            self.assertTrue(str(timers._attached.path).startswith(temp))

    def test_eval_flows_installs_them_first_and_leaves_them_alone(self):
        """Read and never imported, as importing it would run it."""
        tree = ast.parse((ROOT / "tools" / "eval_flows.py").read_text())
        installed = _top_level(tree, lambda node: _calls(node, "install"))
        self.assertEqual(ast.unparse(tree.body[installed]), "CUT_OFF = install()")
        for first in ("Config", "evaluation_server", "offline", "asyncio.run"):
            self.assertLess(installed, _top_level(tree, lambda node: _calls(node, first)), first)
        with tempfile.TemporaryDirectory() as folder:
            cut_modules = {name.partition(".")[0] for name in stand_ins(Path(folder))}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module.startswith("tuxistant"):
                self.assertFalse({alias.name for alias in node.names} & cut_modules,
                                 "a module with a stand-in is left to tools/eval_cutoffs.py")
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, cut_modules)
            if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "CUT_OFF":
                self.fail("the stand-ins stay until the evaluation ends")
        source = ast.unparse(tree)
        self.assertIn("skills = offline(cfg).current()", source)
        self.assertIn("cfg.values['ha_server'], cfg.values['ha_token'] = "
                      "('eval.invalid:8123', 'eval')", source)

    def test_the_live_test_installs_them_before_anything_runs(self):
        tree = ast.parse((ROOT / "tests" / "test_realtime_live.py").read_text())
        setup = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.AsyncFunctionDef) and node.name == "asyncSetUp")
        lines = [ast.unparse(node) for node in setup.body]
        installed = lines.index("self.addCleanup(install().close)")
        for later in ("Config(", "evaluation_server(", "speech(", "Controller("):
            self.assertLess(installed, next(i for i, line in enumerate(lines) if later in line))


class LiveCutoff(unittest.IsolatedAsyncioTestCase):
    async def test_every_unexpected_action_is_blocked_before_a_handler_runs(self):
        """Every action, offered or not: one left out of the model's list can still be named."""
        skills = CountingSkills()
        for tool in skills.tool_names:
            if tool == "time_now":
                continue
            with patch("tuxistant.skills.Skills.execute",
                       side_effect=AssertionError("handler reached")) as execute:
                _, result = await skills.execute(tool, "{}", Session())
            execute.assert_not_awaited()
            self.assertIn("error", result)
