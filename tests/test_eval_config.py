"""Evaluations and the live test: the key goes only to a server chosen for the run, and every
real handler they run meets a stand-in in place of the desktop, the phone, the home and the mail.
Nothing here reaches a network: the guard is a check of how an address is written, and the
interface addresses it compares with are made up."""
import ast
import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import yaml

from tests import no_ssh
from tools import eval_cutoffs
from tools import eval_config
from tools.eval_config import Refused, evaluation_server, on_this_computer, run_server
from tools.eval_cutoffs import ACTIONS, install, owner, stand_ins
from tests.test_realtime_live import CountingSkills
from tuxistant import (apps, calendars, goamail, homeassistant, mail, mcp, media, phone,
                       serverstatus, system, timers, webfetch)
from tuxistant.config import DEFAULTS, Config
from tuxistant.gate import APP_SAYS, Session
from tuxistant.skillhost import BUNDLED, Offline
from tuxistant.skills import HANDLERS

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-" + "e" * 40     # made up
HELD = {"192.168.1.20", "172.17.0.1", "fd00::5"}     # made-up interface addresses
FIB_TRIE = """Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        |-- 127.0.0.1
           /32 host LOCAL
     |-- 172.17.0.1
        /32 host LOCAL
     |-- 172.17.255.255
        /32 link BROADCAST
     |-- 192.168.1.0
        /24 link UNICAST
"""
IF_INET6 = """00000000000000000000000000000001 01 80 10 80       lo
fd000000000000000000000000000005 02 40 00 00     wlan0
"""


class EvaluationServer(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        for stand_in in (patch.dict("os.environ", {"TUXISTANT_SERVER": ""}),
                         patch.object(eval_config, "this_computer", return_value=HELD)):
            stand_in.start()
            self.addCleanup(stand_in.stop)

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

    def test_this_computers_name_on_the_local_network_is_refused(self):
        """mDNS answers <name>.local with this computer's own addresses."""
        with patch("socket.gethostname", return_value="tuxbox"):
            for saved in ("tuxbox.local:8080", "TUXBOX.LOCAL.:8080"):
                with self.subTest(saved):
                    self.refused(self.config(saved))
            self.assertFalse(on_this_computer("otherbox.local:8080"))

    def test_an_address_this_computer_holds_is_refused(self):
        """Docker publishes a port on all of them, so a container there answers on each."""
        for saved in ("192.168.1.20:8080", "172.17.0.1:8080", "3232235796:8080",
                      "[::ffff:172.17.0.1]:8080", "[fd00::5]:8080", "[FD00:0::5]:8080"):
            with self.subTest(saved):
                self.refused(self.config(saved))

    def test_the_interface_addresses_are_read_from_the_kernel_lists(self):
        files = {"/proc/net/fib_trie": FIB_TRIE, "/proc/net/if_inet6": IF_INET6}
        with patch.object(webfetch, "_read_proc", side_effect=files.get):
            found = webfetch.this_computer()
        self.assertEqual(found, {"127.0.0.1", "172.17.0.1", "::1", "fd00::5"})

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

    def test_a_run_with_its_own_key_needs_its_server_named(self):
        """Settings are never read, so neither a saved server nor the default is taken."""
        with patch.object(eval_config, "evaluation_server", wraps=evaluation_server) as asked:
            with self.assertRaises(Refused) as stopped:
                run_server(["e2e"])
            self.assertIn("TUXISTANT_SERVER", str(stopped.exception))
            self.assertNotIn("--server", str(stopped.exception))
            self.assertIn(eval_config.NOTHING_SENT, str(stopped.exception))
            self.assertEqual(asked.call_args.args[0]["server"], DEFAULTS["server"])
        self.assertEqual(run_server(["e2e", "--server", "localhost:8080"]), "localhost:8080")
        with patch.dict("os.environ", {"TUXISTANT_SERVER": "http://model.example.test:8080/"}):
            self.assertEqual(run_server([]), "model.example.test:8080")

    def test_the_shell_gets_the_named_server_or_the_reason(self):
        env = {"PATH": os.environ["PATH"], "TUXISTANT_SERVER": "model.example.test:8080"}
        command = [sys.executable, "-B", "-m", "tools.eval_config"]
        named = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual((named.returncode, named.stdout), (0, "model.example.test:8080\n"))
        env["TUXISTANT_SERVER"] = ""
        refused = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn(eval_config.NOTHING_SENT, refused.stderr)

    def test_the_end_to_end_run_asks_before_it_writes_the_key(self):
        """Read and never run: it starts a whole shell."""
        harness = ROOT / "tests" / "shell_harness"
        script = (harness / "e2e.sh").read_text()
        guard = script.index("server=$(cd \"$root\" && python3 -B -m tools.eval_config) || exit 1")
        self.assertLess(guard, script.index("mktemp"))
        self.assertLess(guard, script.index('os.environ["TUXISTANT_API_KEY"]'))
        self.assertIn('config["server"] = sys.argv[2]', script)
        self.assertNotIn('os.environ.get("TUXISTANT_SERVER")', script)
        live = (harness / "test_e2e_live.py").read_text()
        self.assertLess(live.index("run_server([])"), live.index("subprocess.run("))

    def test_the_unreachable_server_step_never_has_the_key(self):
        """Port 9 on this computer was not chosen for the run."""
        source = (ROOT / "tests" / "shell_harness" / "e2e_scenario.py").read_text()
        step = source[source.index("async def step_errors"):source.index("async def step_cli(")]
        self.assertLess(step.index('set_config("api_key", "not-the-key")'),
                        step.index('set_config("server", "127.0.0.1:9")'))
        self.assertLess(step.index('set_config("server", server)'),
                        step.index('set_config("api_key", key)'))

    def test_the_rejected_key_step_really_sends_a_wrong_key(self):
        """The daemon drops the key when the server changes, so it goes in again after."""
        source = (ROOT / "tests" / "shell_harness" / "e2e_scenario.py").read_text()
        step = source[source.index("async def step_errors"):source.index("async def step_cli(")]
        restored = step.index('set_config("server", server)')
        wrong = step.rindex('set_config("api_key", "not-the-key")')
        self.assertLess(restored, wrong)
        self.assertLess(wrong, step.rindex('call("Toggle")'))
        self.assertLess(step.rindex('call("Toggle")'), step.index("errors: rejected key shown"))

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
                tree = ast.parse(path.read_text())
                self.assertEqual([node for node in ast.walk(tree) if _reads_settings(node)], [])
                self.assertEqual(_renamed_configs(tree), [])

    def test_the_check_finds_every_way_of_writing_config(self):
        guarded = "cfg = Config()\nevaluation_server(cfg)\n"
        for source in ("if Config()['server']:\n    pass\n",
                       "with open(Config()['x']) as c:\n    evaluation_server(c)\n",
                       "for c in [Config()]:\n    evaluation_server(c)\n",
                       "c = config.Config()\nprint(c)\n",
                       "c = tuxistant.config.Config()\n",
                       "c = Config()\nprint(c)\nevaluation_server(c)\n"):
            with self.subTest(source), self.assertRaises(AssertionError):
                _guarded_configs(ast.parse(guarded + source))
        self.assertEqual(_guarded_configs(ast.parse(
            "c = config.Config()\nevaluation_server(c)\n"
            "def f():\n    d = Config()\n    evaluation_server(d)\n")), 2)
        self.assertTrue(_reads_settings(ast.parse("tuxistant.config.Config()").body[0].value))
        self.assertEqual(len(_renamed_configs(ast.parse(
            "from tuxistant.config import Config as Settings\n"))), 1)


def _reads_settings(node) -> bool:
    """A call of Config however it is reached: config.Config() holds the key just the same."""
    return isinstance(node, ast.Call) and ast.unparse(node.func).rpartition(".")[2] == "Config"


def _renamed_configs(tree: ast.Module) -> list:
    """An import that renames Config would hide it from the checks."""
    return [alias for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
            if alias.name.rpartition(".")[2] == "Config" and alias.asname not in (None, "Config")]


def _statement_lists(tree: ast.Module):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                yield body


def _guarded_configs(tree: ast.Module) -> int:
    """How many Config() calls there are, after failing on any that is not in a plain statement
    with the guard as the very next one. One in an if, with or for header fails, unclaimed."""
    claimed = set()
    for body in _statement_lists(tree):
        for statement, after in zip(body, body[1:]):
            plain = isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr))
            guard = isinstance(after, ast.Expr) and _calls(after.value, "evaluation_server")
            if plain and guard:
                claimed |= {id(part) for part in ast.walk(statement) if _reads_settings(part)}
    for node in ast.walk(tree):
        if _reads_settings(node) and id(node) not in claimed:
            raise AssertionError(f"line {node.lineno}: Config() with no guard after it")
    if _renamed_configs(tree):
        raise AssertionError("Config is imported under another name")
    return len(claimed)


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
            # With the made-up index missing or empty, mail search would go on to the real
            # Online Accounts, get a real token and read real mail.
            self.assertIn("goamail._connect", ACTIONS["mail.search"])
            with self.assertRaises(RuntimeError):
                goamail._connect()
            self.assertTrue(str(timers._attached.path).startswith(temp))
            listed = apps._installed()
            self.assertTrue(listed and all(app.path.startswith("/eval/") for app in listed))
            self.assertEqual(apps._default_for("https"), "firefox.desktop")
            self.assertIs(serverstatus._run, eval_cutoffs._made_up_ssh)
            self.assertIs(system._read, eval_cutoffs._made_up_read)
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertTrue(apps._launch("/eval/applications/firefox.desktop"))
            self.assertIn("(would open firefox.desktop)", printed.getvalue())

    def test_a_folder_app_open_names_is_not_opened(self):
        """app.open opens your Downloads folder with file.open's opener and no card."""
        self.assertIn("files._launch", ACTIONS["app.open"])
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "Downloads").mkdir()
            paths = {"HOME": home, "DOWNLOAD": f"{home}/Downloads"}
            session, printed = Session(), io.StringIO()
            with install(), contextlib.redirect_stdout(printed), \
                    mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(apps, "_folder_paths", lambda: paths), \
                    mock.patch.object(subprocess, "run",
                                      side_effect=AssertionError("something was started")):
                result = apps.open_app({"name": "my Downloads folder"}, DEFAULTS, session)
        self.assertEqual(result, {APP_SAYS: True})
        self.assertEqual([said.text for said in session.after],
                         ["Opening your Downloads folder."])
        self.assertIn("(would open Downloads)", printed.getvalue())

    def test_no_evaluation_reaches_your_server(self):
        """system.running on the server has no card, so it would really run: ssh is never
        started, whatever server the evaluation has, by a Popen imported by name too."""
        config = {**DEFAULTS, "server": "real.example:8080"}
        session = Session()
        no_ssh.REACHED.clear()
        with install(), contextlib.redirect_stdout(io.StringIO()), no_ssh.refusing():
            result = system.running({"name": "localai", "on": "server"}, config, session)
        self.assertEqual(no_ssh.REACHED, [], "ssh was started")
        self.assertEqual(result, {APP_SAYS: True})
        self.assertEqual([said.text for said in session.after],
                         ["On your server, docker lists localai, Up 3 days. None of the 2 "
                          "services systemd lists has localai in its name."])

    def test_no_evaluation_prints_this_computer_s_own_lists(self):
        """eval_flows prints what the app says, and "Is docker running?" here would say the
        owner's own services and processes: made-up ones answer, and systemctl and pgrep are
        never started."""
        session = Session()
        no_ssh.REACHED.clear()
        with install(), contextlib.redirect_stdout(io.StringIO()), no_ssh.refusing(), \
                mock.patch.object(system.subprocess, "run",
                                  side_effect=AssertionError("a tool was started")):
            result = system.running({"name": "docker"}, DEFAULTS, session)
        self.assertEqual(no_ssh.REACHED, [], "systemctl was started")
        self.assertEqual(result, {APP_SAYS: True})
        self.assertEqual([said.text for said in session.after],
                         ["On this computer, systemd lists docker.service, active (running), and "
                          "these processes have docker in their command line, or in their name "
                          "where they have none: 1187 dockerd."])
        self.assertIn("made-up services and processes in place of `systemctl` and `pgrep` here, "
                      "so no evaluation prints your own",
                      " ".join((ROOT / "docs" / "SKILLS.md").read_text().split()))

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
        self.assertIn("skills = offline(cfg, user=eval_skills()).current()", source)
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


class YourSkills(unittest.TestCase):
    """An evaluation offers the same tools on every computer: never the owner's own skills."""

    def test_the_folder_is_empty_unless_the_fixtures_are_asked_for(self):
        with patch.dict("os.environ", {"TUX_EVAL_RECIPES": ""}):
            folder = eval_config.eval_skills()
        self.assertFalse(folder.exists())
        self.assertEqual(list(folder.parent.iterdir()), [])
        with patch.dict("os.environ", {"TUX_EVAL_RECIPES": "1"}):
            self.assertEqual(eval_config.eval_skills(), ROOT / "tests" / "recipes")

    def test_both_evaluations_use_it(self):
        for tool in ("eval_routing.py", "eval_flows.py"):
            source = (ROOT / "tools" / tool).read_text()
            self.assertIn("user=eval_skills()", source, tool)
            self.assertEqual(source.count("offline("), 1, tool)


class RoutingCases(unittest.TestCase):
    """Read from the source: importing eval_routing would start a run."""

    def cases(self, name: str) -> dict:
        tree = ast.parse((ROOT / "tools" / "eval_routing.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and node.targets[0].id == name:
                return dict(ast.literal_eval(node.value))
        raise AssertionError(name)

    def test_your_server_by_the_words_people_use_and_a_local_server_by_name(self):
        cases = self.cases("CASES")
        self.assertEqual(cases["Is LocalAI running on my home server?"], {"system_running@server"})
        self.assertEqual(cases["Is the Plex server running?"], {"system_running"})
        self.assertEqual(cases["Is LocalAI running on my {label}?"], {"system_running@server"})

    def test_the_cases_are_asked_with_the_server_filled_in_and_judged_by_the_handler(self):
        tree = ast.parse((ROOT / "tools" / "eval_routing.py").read_text())
        source = ast.unparse(tree)
        self.assertIn("name, args = await first_tool(text.replace('{server}', SERVER_HOST)"
                      ".replace('{label}', SERVER_LABEL))", source)
        self.assertIn("name = judged(name, args) if name not in (None, 'ERROR') else name",
                      source)
        self.assertIn("SERVER_LABEL = SERVER_HOST if is_address(SERVER_HOST) else "
                      "SERVER_HOST.partition('.')[0]", source)
        judged = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "judged")
        space = {"json": json, "about_server": system.about_server,
                 "cfg": {**DEFAULTS, "server": "nas.lan:8080"}}
        exec(compile(ast.Module([judged], []), "eval_routing.py", "exec"), space)
        for args, name in (('{"name": "localai", "on": "my nas"}', "system_running@server"),
                           ('{"name": "localai", "on": "my server"}', "system_running@server"),
                           ('{"name": "firefox"}', "system_running"),
                           ('{"name": "plex", "on": "office pc"}', "system_running")):
            with self.subTest(args):
                self.assertEqual(space["judged"]("system_running", args), name)
        self.assertEqual(space["judged"]("shell_run", '{"command": "ssh nas"}'), "shell_run")

    def test_what_is_redacted(self):
        """The model may name zephyr.lan as zephyr, and the output goes into TESTING.md."""
        for host, said, shown in (
                ("zephyr.lan", '{"name": "localai", "on": "Zephyr"}',
                 '{"name": "localai", "on": "{server}"}'),
                ("zephyr.lan", '{"name": "zephyr"} ssh ZEPHYR.LAN docker ps',
                 '{"name": "{server}"} ssh {server} docker ps'),
                ("box.tail1234.ts.net", '{"on": "box"} box.tail1234 tail1234.ts.net',
                 '{"on": "{server}"} {server}.{server} {server}.ts.net'),
                ("pi.lan", "the pi server, pin and spice on pi.lan.",
                 "the {server} server, pin and spice on {server}."),
                ("10.0.0.5", '{"name": "10.0.0.5"} on 10 servers',
                 '{"name": "{server}"} on 10 servers'),
                ("nas.home.arpa", "my home nas", "my home {server}"),
                # A label of more than one word, as the model may write it.
                ("jeff-nas.lan", '{"on": "jeff nas"} Jeff-NAS JeffNAS jeff_nas, my nas',
                 '{"on": "{server}"} {server} {server} {server}, my nas'),
                ("zephyr-box.lan", '{"on": "ZephyrBox"} zephyr box, Zephyr',
                 '{"on": "{server}"} {server}, {server}'),
                # A long word split by the model, but not a short or generic one, and inside
                # another word.
                ("zephyrbox.lan", "my zephyr box, Zephyr-Box, zephyr_box. zephyrboxes",
                 "my {server}, {server}, {server}. {server}es"),
                ("zephyr.lan", "myzephyr zephyrs Zephyr2, zephyr", "my{server} {server}s {server}2, "
                                                                 "{server}"),
                ("box.tail1234.ts.net", "mytail1234 boxes", "my{server} boxes"),
                # A word that is in the placeholder is left alone there.
                ("serve.lan", "{server} serves", "{server} {server}s"),
                # An address in another spelling, but not another address.
                ("fd00::12", "fd00:0:0::12 [FD00::12] fd00::1:2 fd00::12.",
                 "{server} [{server}] fd00::1:2 {server}."),
                ("10.0.0.5", "10.0.0.05, 10.0.0.50 and ::ffff:10.0.0.5",
                 "{server}, 10.0.0.50 and ::ffff:{server}"),
                ("zephyrbox-kitchen.lan", "zephyr box kitchen, kit chen, Zephyrbox.Kitchen",
                 "{server}, {server}, {server}"),
                ("pi.lan", "p i on pi", "p i on {server}"),
                ("alice-nas.lan", '"on": "alice nas" Alice NAS alicenas alice jeff',
                 '"on": "{server}" {server} {server} {server} jeff'),
                # Every label of three letters or more, but no generic one.
                ("pi.bob.lan", "pi.bob bob bobby on the lan", "{server}.{server} {server} bobby "
                                                             "on the lan"),
                ("pi.jeff.lan", "jeff, jeffrey", "{server}, jeffrey"),
                ("home-server.lan", "my home server, home-server", "my home server, {server}")):
            with self.subTest(host):
                self.assertEqual(eval_config.redacted(said, host), shown)

    def test_what_is_printed_names_no_server(self):
        """eval_routing itself, with a stand-in for the model that names the server in every
        spelling it might, the case text filled in, and no network."""
        calls = []

        async def first_tool(text):
            calls.append(text)
            spelled = SPELLINGS[len(calls) % len(SPELLINGS)]
            if len(calls) % 3 == 0:
                return None, (SAID_ACROSS_THE_CUT if len(calls) % 2 else
                              f"I think {spelled} is fine. {text}")
            if len(calls) % 9 == 1:
                return "system_running", ACROSS_THE_CUT
            return "system_running", json.dumps({"name": "localai", "on": f"my {spelled}",
                                                 "text": text})
        # Cut before it is redacted, the host would be zephyr or zephyrb, which no pattern takes:
        # a call's arguments are cut at 90 and a fact's answer at 70.
        for across, cut in ((ACROSS_THE_CUT, 90), (SAID_ACROSS_THE_CUT, 70)):
            at = across.index(ROUTED_HOST)
            self.assertLessEqual(at + len("zephyr"), cut, across)
            self.assertLess(cut, at + len("zephyrbox"), across)
        printed, space = _run_routing(first_tool)
        self.assertIn('"on": "{serve', printed, "no call's host was across the cut")
        self.assertIn("y {serve", printed, "no answer's host was across the cut")
        self.assertGreater(len(calls), 150)
        self.assertTrue(any(ROUTED_HOST in text for text in calls), "the cases were filled in")
        self.assertGreater(printed.count("{server}"), 100)
        self.assertIsNone(re.search(r"zephyr|kitchen", printed, re.I),
                          [line for line in printed.splitlines()
                           if re.search(r"zephyr|kitchen", line, re.I)][:5])
        self.assertFalse(space["SERVER_IS_HERE"])

    def test_a_tool_name_the_model_makes_up_is_not_printed(self):
        """Only the arguments go through redacted(), and a name no tool has may hold the host."""
        async def first_tool(text):
            return "system_running_zephyrboxkitchen", json.dumps({"name": "localai"})
        printed, _ = _run_routing(first_tool)
        self.assertIn("{'(other)': 1}", printed)
        self.assertIsNone(re.search(r"zephyr|kitchen", printed, re.I))

    def test_a_connection_that_fails_names_no_server_either(self):
        async def first_tool(text):
            raise ConnectionRefusedError(f"Connect call failed ('{ROUTED_HOST}', 8080)")
        with self.assertRaises(SystemExit) as raised:
            _run_routing(first_tool)
        self.assertEqual(str(raised.exception.code),
                         "The model server didn't answer (ConnectionRefusedError).")

    def test_a_run_whose_server_is_this_computer_leaves_the_server_cases_out(self):
        """_server_host drops a local host, so they could never be judged right."""
        source = ast.unparse(ast.parse((ROOT / "tools" / "eval_routing.py").read_text()))
        self.assertIn("SERVER_IS_HERE = is_local(SERVER_HOST)", source)
        self.assertIn("if SERVER_IS_HERE and wanted and ('system_running@server' in wanted):\n"
                      "            skipped += 1\n            continue", source)

    def test_sending_a_photo_is_asked_in_every_setup_and_never_trashes(self):
        """It went to file_trash with or without recipes, so it can't wait for TUX_EVAL_RECIPES."""
        text = "Send the photo from yesterday to my phone"
        self.assertNotIn(text, self.cases("RECIPE_CASES"))
        self.assertEqual(self.cases("CASES")[text], {"file_find", "phone_send", "task_run"})


ROUTED_HOST = "zephyrbox-kitchen.lan"      # made up
SPELLINGS = (ROUTED_HOST, ROUTED_HOST.upper(), "zephyrbox-kitchen", "zephyrbox kitchen",
             "ZephyrboxKitchen", "zephyrbox", "Zephyrbox.Kitchen", "zephyrbox_kitchen", "kitchen",
             f"{ROUTED_HOST}:8080", f"ssh://{ROUTED_HOST}", "zephyr box", "Zephyr-Box kitchen",
             "zephyr box kitchen", "myzephyrbox", "zephyrbox2", "zephyrboxes", "kitchens",
             "the-zephyrbox-kitchen2")
# The host across where eval_routing cuts what it prints: 90 for a call, 70 for an answer.
ACROSS_THE_CUT = json.dumps({"name": "x" * 63, "on": ROUTED_HOST})
SAID_ACROSS_THE_CUT = "y" * 63 + " " + ROUTED_HOST


class _Settings:
    """Settings that are never read from a file: the server comes with the run."""
    def __init__(self):
        self.values = dict(DEFAULTS)

    def __getitem__(self, key):
        return self.values[key]


def _run_routing(first_tool) -> tuple[str, dict]:
    """tools/eval_routing.py, run once a case with first_tool as the model, and what it printed.
    websockets is a stand-in that can't connect, and Settings are never read."""
    path = ROOT / "tools" / "eval_routing.py"
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if ast.unparse(node) != "run()"]
    offline_socket = types.SimpleNamespace(
        connect=mock.Mock(side_effect=AssertionError("the network was reached")),
        WebSocketException=type("WebSocketException", (Exception,), {}))
    space = {"__name__": "eval_routing", "__file__": str(path)}
    printed = io.StringIO()
    with patch.dict("os.environ", {"TUXISTANT_SERVER": f"{ROUTED_HOST}:8080",
                                   "TUX_EVAL_HOME": "", "TUX_EVAL_RECIPES": ""}), \
            patch.dict(sys.modules, {"websockets": offline_socket}), \
            patch.object(sys, "argv", ["eval_routing.py", "1"]), \
            patch.object(sys, "path", list(sys.path)), \
            patch("tuxistant.config.Config", _Settings), contextlib.redirect_stdout(printed):
        exec(compile(tree, str(path), "exec"), space)
        space["first_tool"] = first_tool
        space["run"]()
    return printed.getvalue(), space


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
