"""tuxistant doctor and the command line, with no bus involved."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import FakeSpeech  # noqa: E402
from tuxistant import __main__ as cli  # noqa: E402
from tuxistant.config import Config  # noqa: E402


class Doctor(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.folder / "data"), "XDG_DATA_DIRS": str(self.folder / "none"),
            "TUXISTANT_FN_SOCKET": str(self.folder / "fn.sock"),
            "TUXISTANT_FN_HELPER": str(self.folder / "tuxistant-fn")})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real gsettings would read the session of whoever runs the tests.
        patcher = mock.patch.object(cli, "shell_setting", self.shell_setting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = {}

    def shell_setting(self, key):
        return self.settings.get(key, "")

    def config(self, port, key="good"):
        config = Config(self.folder / "config.json")
        config.values.update(server=f"127.0.0.1:{port}", api_key=key)
        return config

    def results(self, config) -> dict:
        return {what: passed for passed, what, _ in cli.checks(config)}

    def fake(self, **options):
        http = FakeSpeech(**options)
        self.addCleanup(http.close)
        return http

    def test_healthy_server_but_nothing_installed(self):
        results = self.results(self.config(self.fake().port))
        self.assertTrue(results["server reachable"])
        self.assertTrue(results["API key accepted"])
        self.assertTrue(results["pipeline present"])
        self.assertTrue(results["voice model present"])
        self.assertTrue(results["speech to text model present"])
        self.assertTrue(results["Fn helper running"], "holding Fn is optional")
        self.assertFalse(results["GNOME extension installed"])

    def test_bad_key_is_reachable_but_not_accepted(self):
        results = self.results(self.config(self.fake().port, key="wrong"))
        self.assertTrue(results["server reachable"])
        self.assertFalse(results["API key accepted"])
        self.assertFalse(results["pipeline present"])

    def test_server_down(self):
        results = self.results(self.config(9))
        self.assertFalse(results["server reachable"])
        self.assertFalse(results["API key accepted"])

    def test_missing_pipeline(self):
        results = self.results(self.config(self.fake(models=("other",)).port))
        self.assertFalse(results["pipeline present"])
        self.assertFalse(results["voice model present"])

    def test_extension_and_socket_found(self):
        extension = self.folder / "data/gnome-shell/extensions" / cli.EXTENSION
        extension.mkdir(parents=True)
        (extension / "metadata.json").write_text("{}")
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(str(self.folder / "fn.sock"))
            results = self.results(self.config(self.fake().port))
        self.assertTrue(results["GNOME extension installed"])
        self.assertTrue(results["Fn helper running"])

    def test_an_installed_extension_is_not_an_enabled_one(self):
        self.settings["enabled-extensions"] = "['other@example']"
        self.assertFalse(self.results(self.config(9))["GNOME extension enabled"])
        self.settings["enabled-extensions"] = f"['other@example', '{cli.EXTENSION}']"
        self.settings["disable-user-extensions"] = "false"
        self.assertTrue(self.results(self.config(9))["GNOME extension enabled"])
        self.settings["disable-user-extensions"] = "true"
        self.assertFalse(self.results(self.config(9))["GNOME extension enabled"])

    def test_no_gsettings_counts_as_not_enabled(self):
        mock.patch.stopall()
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(cli.extension_enabled())

    def test_the_daemon_has_to_be_activatable(self):
        self.assertFalse(self.results(self.config(9))["daemon can be started"])
        services = self.folder / "data/dbus-1/services"
        services.mkdir(parents=True)
        (services / "org.tuxistant.Daemon.service").write_text("")
        self.assertTrue(self.results(self.config(9))["daemon can be started"])

    def test_an_installed_helper_has_to_be_running(self):
        (self.folder / "tuxistant-fn").write_text("")
        self.assertFalse(self.results(self.config(9))["Fn helper running"])
        (self.folder / "fn.sock").write_text("")     # a plain file is not the socket
        self.assertFalse(self.results(self.config(9))["Fn helper running"])

    def test_missing_modules_are_named_and_nothing_else_is_tried(self):
        with mock.patch.dict(sys.modules, {"dbus_fast": None, "websockets": None}):
            results = cli.checks(self.config(9))
        self.assertEqual(results, [(False, "Python modules present", "on Ubuntu or Debian: "
                                    "sudo apt install python3-dbus-fast python3-websockets")])

    def test_a_command_that_needs_a_missing_module_says_so(self):
        err = io.StringIO()
        with mock.patch.object(cli, "call", side_effect=ModuleNotFoundError(name="dbus_fast")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["toggle"]), 1)
        self.assertIn("dbus_fast is missing", err.getvalue())

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertRegex(out.getvalue(), r"^tuxistant \d+\.\d+\.\d+(\.dev\d+)?\n$")

    def test_missing_pipewire_tools(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.folder)}):
            self.assertFalse(self.results(self.config(9))["PipeWire tools present"])

    def test_doctor_prints_and_sets_the_exit_code(self):
        out = io.StringIO()
        ok = [(True, "a", ""), (True, "b", "detail")]
        with mock.patch.object(cli, "checks", return_value=ok), contextlib.redirect_stdout(out):
            self.assertEqual(cli.doctor(), 0)
        self.assertIn("ok    b  detail", out.getvalue())
        with mock.patch.object(cli, "checks", return_value=ok + [(False, "c", "")]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(), 1)

    def test_the_key_is_never_printed(self):
        out = io.StringIO()
        with mock.patch.object(cli, "Config", return_value=self.config(self.fake().port, "hunter2")), \
                contextlib.redirect_stdout(out):
            cli.doctor()
        self.assertNotIn("hunter2", out.getvalue())

    def test_the_skill_sandbox_is_checked(self):
        from tuxistant import skillbox
        config = self.config(9)
        config.values.update(tapo_email="me@example.com", tapo_password="pw")
        for available in (True, False):
            with self.subTest(available), \
                    mock.patch.object(skillbox, "available", return_value=available), \
                    mock.patch.object(skillbox, "landlock_abi", return_value=skillbox.LANDLOCK_SCOPE_ABI):
                self.assertEqual(self.results(config)["skill sandbox ready"], available)

    def test_no_sandbox_is_no_failure_when_nothing_needs_it(self):
        from tuxistant import skillbox
        with mock.patch.object(skillbox, "available", return_value=False):
            passed, detail = cli.sandbox_ready(self.config(9))
            self.assertTrue(passed)
            self.assertIn("can't run", detail)
            for ignored in (".git", "__pycache__"):
                (self.folder / "data/tuxistant/skills" / ignored).mkdir(parents=True)
            self.assertTrue(cli.sandbox_ready(self.config(9))[0])
            (self.folder / "data/tuxistant/skills/mine").mkdir(parents=True)
            passed, detail = cli.sandbox_ready(self.config(9))
            self.assertFalse(passed)
            self.assertIn("your skills in", detail)
            self.assertNotIn("TP-Link", detail)

    def test_half_a_tapo_account_needs_no_sandbox(self):
        for email, password in (("me@example.com", ""), ("", "pw"), ("  ", "pw")):
            config = self.config(9)
            config.values.update(tapo_email=email, tapo_password=password)
            with self.subTest(email=email, password=password):
                self.assertEqual(cli.sandbox_needs(config), [])

    def test_an_old_kernel_fails_when_lights_need_the_sandbox(self):
        from tuxistant import skillbox
        config = self.config(9)
        config.values.update(tapo_email="me@example.com", tapo_password="pw")
        with mock.patch.object(skillbox, "available", return_value=True), \
                mock.patch.object(skillbox, "landlock_abi", return_value=4):
            passed, detail = cli.sandbox_ready(config)
        self.assertFalse(passed)
        self.assertIn("6.12", detail)
        self.assertIn("TP-Link", detail)

    def test_skill_problems_are_listed(self):
        config = self.config(9)
        self.assertEqual(cli.skill_problems(config), {})
        self.assertTrue(self.results(config)[cli.SKILLS_LOAD])
        broken = self.folder / "data/tuxistant/skills/broken"
        broken.mkdir(parents=True)
        (broken / "skill.yaml").write_text("id: org.example.broken\nname: Broken\n"
                                           "description: d\nrun: [python3, main.py]\n"
                                           "permissions: { shell: true }\n"
                                           "actions: { broken.go: { description: d } }\n")
        problems = cli.skill_problems(config)
        self.assertEqual(list(problems), [str(broken)])
        self.assertIn("shell", problems[str(broken)])
        self.assertFalse(self.results(config)[cli.SKILLS_LOAD])
        out = io.StringIO()
        failing = [(False, cli.SKILLS_LOAD, "1 with a problem")]
        with mock.patch.object(cli, "checks", return_value=failing), \
                mock.patch.object(cli, "Config", return_value=config), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.doctor(), 1)
        self.assertIn(f"{broken}: It asks for shell", out.getvalue())


class Commands(unittest.TestCase):
    def test_say_joins_its_words(self):
        with mock.patch.object(cli, "call", mock.AsyncMock(return_value=[])) as call:
            self.assertEqual(cli.main(["say", "hello", "there"]), 0)
        call.assert_awaited_once_with("Say", "s", ("hello there",))

    def test_toggle_and_cancel_call_the_daemon(self):
        for word, method in (("toggle", "Toggle"), ("cancel", "Cancel")):
            with mock.patch.object(cli, "call", mock.AsyncMock(return_value=[])) as call:
                self.assertEqual(cli.main([word]), 0)
            call.assert_awaited_once_with(method)

    def test_settings_runs_the_settings_app_lazily(self):
        fake = mock.MagicMock()
        fake.main.return_value = 7
        with mock.patch.dict(sys.modules, {"tuxistant.settings_app": fake}):
            self.assertEqual(cli.main(["settings"]), 7)

    def test_unknown_command(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["dance"])

    def test_history_prints_what_ran(self):
        rows = '[{"ts": 0, "heard": "time?", "reply": "Noon.", "status": "ok", ' \
               '"actions": [{"name": "time.now"}]}]'
        out = io.StringIO()
        with mock.patch.object(cli, "call", mock.AsyncMock(return_value=[rows])), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["history"]), 0)
        for text in ("time?", "time.now", "Noon."):
            self.assertIn(text, out.getvalue())


if __name__ == "__main__":
    unittest.main()
