"""Facts about this computer and its folders."""
import asyncio
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import system  # noqa: E402
from tuxistant.config import DEFAULTS, EDITABLE, Config  # noqa: E402
from tuxistant.gate import Session  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402


class Facts(unittest.TestCase):
    def test_every_fact_is_there_and_can_be_sent_to_the_model(self):
        with mock.patch.object(system, "_tool", return_value=""):
            json.dumps(system.info({}, None))
            self.assertEqual(set(system.info({}, None)), set(system.FACTS))

    def test_one_fact_when_asked_for_one(self):
        self.assertEqual(set(system.info({"about": "uptime"}, None)), {"uptime"})

    def test_an_invented_word_gets_everything_rather_than_nothing(self):
        with mock.patch.object(system, "_tool", return_value=""):
            self.assertEqual(set(system.info({"about": "vibes"}, None)), set(system.FACTS))

    def test_spans_read_aloud(self):
        for seconds, words in ((20, "less than a minute"), (60, "1 minute"), (3720, "1 hour 2 minutes"),
                               (90000, "1 day 1 hour"), (2 * 86400 + 180, "2 days 3 minutes")):
            with self.subTest(seconds):
                self.assertEqual(system._span(seconds), words)

    def test_the_address_is_the_one_in_use_not_a_docker_bridge(self):
        route = json.dumps([{"prefsrc": "192.168.1.7", "dev": "wlan0"}])
        with mock.patch.object(system, "_tool", side_effect=[route, ""]):
            self.assertEqual(system._ip(), {"address": "192.168.1.7", "connection": "wlan0"})

    def test_no_network_is_said_plainly(self):
        with mock.patch.object(system, "_tool", return_value=""):
            self.assertEqual(system._ip(), {"address": "not connected"})

    def test_a_network_name_cannot_fake_a_line(self):
        with mock.patch.object(system, "_tool", return_value="yes:evil‮name\n"):
            self.assertNotIn("‮", system._wifi())

    def test_a_missing_tool_is_empty_not_a_crash(self):
        self.assertEqual(system._tool("/no/such/tool"), "")


ROUTE = json.dumps([{"prefsrc": "192.168.1.7", "dev": "wlan0"}])
TAILNET = json.dumps([{"ifname": "tailscale0", "addr_info": [{"local": "100.101.102.103"}]}])


def fake_ip(*command):
    return TAILNET if "tailscale0" in command else ROUTE if "route" in command else ""


class Tailscale(unittest.TestCase):
    def ask(self, config):
        with mock.patch.object(system, "_tool", side_effect=fake_ip) as tool:
            found = system.info({"about": "ip"}, config)["ip"]
        return found, [call.args for call in tool.call_args_list]

    def test_off_as_installed(self):
        self.assertIs(DEFAULTS["say_tailscale_address"], False)
        self.assertIs(EDITABLE["say_tailscale_address"], bool)

    def test_off_it_is_never_looked_up_or_said(self):
        cases = {"no config": None, "installed": DEFAULTS, "an older config": {},
                 "written by hand": {**DEFAULTS, "say_tailscale_address": "true"}}
        for case, config in cases.items():
            with self.subTest(case):
                found, calls = self.ask(config)
                self.assertEqual(found, {"address": "192.168.1.7", "connection": "wlan0"})
                self.assertNotIn("tailscale", json.dumps(found).casefold())
                self.assertFalse([c for c in calls if "tailscale0" in c], "ip was run for it")

    def test_every_fact_at_once_leaves_it_out_too(self):
        with mock.patch.object(system, "_tool", side_effect=fake_ip) as tool:
            self.assertNotIn("tailscale", json.dumps(system.info({}, DEFAULTS)).casefold())
        self.assertFalse([c for c in tool.call_args_list if "tailscale0" in c.args])

    def test_on_it_is_said(self):
        found, calls = self.ask({**DEFAULTS, "say_tailscale_address": True})
        self.assertEqual(found, {"address": "192.168.1.7", "connection": "wlan0",
                                 "tailscale": "100.101.102.103"})
        self.assertIn(("ip", "-j", "-4", "addr", "show", "tailscale0"), calls)

    def test_on_without_tailscale_says_nothing_about_it(self):
        with mock.patch.object(system, "_tool", side_effect=[ROUTE, ""]):
            found = system.info({"about": "ip"}, {**DEFAULTS, "say_tailscale_address": True})["ip"]
        self.assertEqual(found, {"address": "192.168.1.7", "connection": "wlan0"})

    def test_the_tool_the_model_calls_follows_the_switch(self):
        for on in (False, True):
            with self.subTest(on=on), mock.patch.object(system, "_tool", side_effect=fake_ip):
                skills = Skills(config={**DEFAULTS, "say_tailscale_address": on})
                _, result = asyncio.run(skills.execute("system_info", '{"about": "ip"}', Session()))
                self.assertIs("tailscale" in result["ip"], on)

    def test_the_switch_reaches_the_tool_through_the_saved_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            config = Config(path)
            self.assertIs(config["say_tailscale_address"], False)
            config.set("say_tailscale_address", True)
            self.assertIs(Config(path)["say_tailscale_address"], True)
            found, _ = self.ask(Config(path))
            self.assertEqual(found["tailscale"], "100.101.102.103")
            config.set("say_tailscale_address", False)
            self.assertNotIn("say_tailscale_address", json.loads(path.read_text()),
                             "the default is not written down")
            with self.assertRaises(TypeError):
                config.set("say_tailscale_address", "yes")


class Running(unittest.TestCase):
    def outputs(self, service="", user="", process="", command_line=""):
        return mock.patch.object(system, "_tool",
                                 side_effect=[service, user, process, command_line])

    def test_a_service(self):
        with mock.patch.object(system, "_tool", return_value="active\n"):
            self.assertEqual(system.running({"name": "Docker"}, None),
                             {"name": "docker", "running": True})

    def test_a_user_service(self):
        with self.outputs("inactive\n", "active\n"):
            self.assertTrue(system.running({"name": "pipewire"}, None)["running"])

    def test_a_plain_program(self):
        with self.outputs("inactive\n", "inactive\n", "1234\n"):
            self.assertTrue(system.running({"name": "firefox"}, None)["running"])

    def test_not_running(self):
        with self.outputs("inactive\n", "inactive\n", "", ""):
            self.assertFalse(system.running({"name": "firefox"}, None)["running"])

    def test_a_name_longer_than_linux_keeps_is_found_by_its_command_line(self):
        """Linux keeps 15 characters of a process's own name, all that pgrep -x sees: at-spi2-
        registryd is at-spi2-registr there. Its command line has it whole."""
        command_lines = ["/usr/libexec/at-spi2-registryd --use-gnome-session",
                         "/usr/bin/python3 /opt/evolution-addressbook-factory-helper.py",
                         "gnome-text-editor"]
        comms = [line.split()[0].rpartition("/")[2][:15] for line in command_lines]

        def tool(*command):
            if command[0] == "systemctl":
                return "inactive\n"
            how, pattern = command[2], command[-1]
            found = comms if how == "-x" else command_lines
            match = re.fullmatch if how == "-x" else re.search
            return "".join("1\n" for line in found if match(pattern, line, re.I))
        for name, here in (("at-spi2-registryd", True), ("gnome-text-editor", True),
                           ("gnometexteditor", True), ("at-spi2-registry", False),
                           ("evolution-addressbook-factory", False), ("libexec", False)):
            with self.subTest(name), mock.patch.object(system, "_tool", side_effect=tool):
                self.assertEqual(system.running({"name": name}, None)["running"], here)
        # Asked about another computer, what is said of this one is so too.
        with mock.patch.object(system, "_tool", side_effect=tool), \
                self.assertRaises(ValueError) as raised:
            system.running({"name": "at-spi2-registryd", "on": "my work laptop"}, None)
        self.assertIn("On this computer, at-spi2-registryd is running.", str(raised.exception))

    def test_a_name_cannot_become_an_option_or_a_command(self):
        for name in ("", "-h", "--user", "a b", "a;rm", "$(x)", "a/b", "x" * 80, "../x"):
            with self.subTest(name), mock.patch.object(system, "_tool") as tool:
                with self.assertRaises(ValueError):
                    system.running({"name": name}, None)
                tool.assert_not_called()


class Folders(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = mock.patch.dict("os.environ", {"HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, size in (("big/a.bin", 5000), ("big/deep/b.bin", 5000), ("small/c.txt", 10),
                           ("loose.txt", 1)):
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)

    def test_the_biggest_comes_first_and_everything_is_counted(self):
        result = system.folders({}, None)
        self.assertEqual(result["files"], 4)
        self.assertEqual([f["name"] for f in result["biggest"]], ["big", "small"])
        self.assertEqual(result["biggest"][0]["files"], 2)
        self.assertNotIn("note", result)

    def test_one_folder(self):
        result = system.folders({"folder": "~/big"}, None, Session())
        self.assertEqual((result["folder"], result["files"]), ("~/big", 2))

    def test_links_are_not_followed_out(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "x").write_bytes(b"y" * 9000)
        (self.home / "small/link").symlink_to(outside.name)
        counted = next(f for f in system.folders({}, None)["biggest"] if f["name"] == "small")
        self.assertEqual(counted["files"], 2)    # the link itself, not what is behind it

    def test_a_file_is_not_a_folder(self):
        with self.assertRaisesRegex(ValueError, "isn't a folder"):
            system.folders({"folder": "~/loose.txt"}, None, Session())

    def test_outside_home_is_refused(self):
        with self.assertRaises(ValueError):
            system.folders({"folder": "/etc"}, None, Session())

    def test_running_out_of_time_says_so(self):
        with mock.patch.object(system, "SCAN_LIMIT_S", -1):
            result = system.folders({}, None)
        self.assertIn("note", result)
        self.assertIn("biggest_so_far", result)
        self.assertNotIn("biggest", result)

    def test_a_slow_folder_early_in_the_alphabet_cannot_hide_the_big_one(self):
        (self.home / "aaa").mkdir()
        for i in range(system.SCAN_BATCH * 4):
            (self.home / f"aaa/{i}").mkdir()
        (self.home / "zzz").mkdir()
        (self.home / "zzz/huge.bin").write_bytes(b"x" * 100000)
        clock = iter(range(1000))
        with mock.patch.object(system.time, "monotonic", lambda: next(clock)), \
                mock.patch.object(system, "SCAN_LIMIT_S", 5):
            result = system.folders({}, None)
        self.assertEqual(result["biggest_so_far"][0]["name"], "zzz")


class Manifests(unittest.TestCase):
    def test_measuring_a_folder_counts_as_reading_files(self):
        actions = Skills().actions
        self.assertEqual(actions["disk.folders"]["risk"], "medium")

    def test_your_address_and_wifi_name_are_private(self):
        self.assertEqual(Skills().actions["system.info"]["risk"], "medium")

    def test_a_search_after_them_needs_a_click(self):
        session = Session()
        skills = Skills()
        with mock.patch.dict("tuxistant.skills.HANDLERS",
                             {"system.info": lambda _a, _c, _s: {"address": "192.168.1.7"}}):
            asyncio.run(skills.execute("system_info", '{"about": "ip"}', session))
        self.assertTrue(session.tainted)
        called = mock.Mock(return_value={"found": 0})
        with mock.patch.dict("tuxistant.skills.HANDLERS", {"web.search": called}):
            _, result = asyncio.run(skills.execute("web_search", '{"query": "192.168.1.7"}', session))
        self.assertTrue(result["declined"])
        called.assert_not_called()


if __name__ == "__main__":
    unittest.main()
