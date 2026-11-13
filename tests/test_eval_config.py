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
from tools.eval_config import evaluation_server, on_this_computer
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
