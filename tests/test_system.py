"""Facts about this computer and its folders."""
import asyncio
import contextlib
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


def _argv(command_line) -> list[str]:
    """A command line given as its words, or as one string of words split at its spaces."""
    return command_line.split(" ") if isinstance(command_line, str) else list(command_line)


def pgrep(processes: list[tuple]):
    """A stand-in for _tool with these processes, each (its own name, its command line), and
    every service inactive: pgrep -x matches the whole of a process's own name, pgrep -f any of
    its command line, its words joined by spaces, and each prints the PIDs it found, one a
    line, from 100 on."""
    def tool(*command):
        if command[0] == "systemctl":
            return "inactive\n"
        assert command[:2] == ("pgrep", "-i") and command[3] == "--", command
        how, pattern = command[2], command[-1]
        match = re.fullmatch if how == "-x" else re.search
        return "".join(f"{pid}\n" for pid, (own, line) in enumerate(processes, 100)
                       if match(pattern, own if how == "-x" else " ".join(_argv(line)), re.I))
    return tool


@contextlib.contextmanager
def processes_here(processes: list[tuple]):
    """These processes alone, as pgrep finds them and as /proc gives their own names and their
    command lines word by word, by the PIDs pgrep printed."""
    def process(pid: str):
        own, line = processes[int(pid) - 100]
        return own, _argv(line)
    with mock.patch.object(system, "_tool", side_effect=pgrep(processes)), \
            mock.patch.object(system, "_process", side_effect=process):
        yield


class Running(unittest.TestCase):
    def outputs(self, service="", user="", process="", command_line="", script=""):
        return mock.patch.object(system, "_tool",
                                 side_effect=[service, user, process, command_line, script])

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
        registryd is at-spi2-registr there. Its command line has it whole. A process title of
        "name: ...", as php-fpm's master sets, is its name too, and so is a script run by an
        interpreter, by name or by its #! line, whose own name is cut at 15 characters."""
        here = [("at-spi2-registr", "/usr/libexec/at-spi2-registryd --use-gnome-session"),
                ("python3", "/usr/bin/python3 /opt/evolution-addressbook-factory-helper.py"),
                ("gnome-text-edit", "gnome-text-editor"),
                # Debian's and Ubuntu's php8.2-fpm, whose binary is php-fpm8.2
                ("php-fpm8.2", "php-fpm: master process (/etc/php/8.2/fpm/php-fpm.conf)"),
                ("systemd-userwor", "systemd-userwork: waiting..."),
                # Run by an interpreter: its own name is the interpreter's.
                ("python3", "python3 /usr/bin/wsdd --shortlog"),
                ("python3", "/usr/bin/python3 /usr/share/unattended-upgrades/"
                            "unattended-upgrade-shutdown --wait-for-signal"),
                ("python3", "/usr/bin/python3 -sP /usr/sbin/firewalld --nofork"),
                ("bash", "bash /home/me/bin/charge-pulse.sh"),
                # Started by its #! line: its own name is its first 15 characters.
                ("tux-probe-long-", "/usr/bin/python3 /usr/local/bin/tux-probe-long-daemon"),
                ("tux-probe-shell", "/bin/sh /usr/local/bin/tux-probe-shell-loop"),
                ("tux-probe-lua-s", "/usr/bin/lua5.4 /usr/local/bin/tux-probe-lua-script"),
                ("jupyter-noteboo", "/usr/bin/python3 /usr/bin/jupyter-notebook --no-browser")]
        # Only a later word of a command line, which is no program of that name.
        later = [("man", "/usr/bin/man at-spi2-registryd"), ("tail", "tail -f /var/log/nginx"),
                 ("vim", "vim gnome-text-editor"), ("sh", "sh -c nginx -g 'daemon off;'"),
                 ("python3", "python3 -c wsdd"), ("python3", "python3 -m firewalld"),
                 ("less", "less /usr/local/bin/tux-probe-lua-script"),
                 ("vi", "vi /home/me/bin/charge-pulse.sh")]
        for processes, name, running in (
                (here, "at-spi2-registryd", True), (here, "gnome-text-editor", True),
                (here, "gnometexteditor", True), (here, "php-fpm", True),
                (here, "systemd-userwork", True), (here, "wsdd", True),
                (here, "unattended-upgrade-shutdown", True), (here, "firewalld", True),
                (here, "charge-pulse", True), (here, "tux-probe-long-daemon", True),
                (here, "tux-probe-shell-loop", True), (here, "tux-probe-lua-script", True),
                (here, "jupyter-notebook", True),
                (here, "at-spi2-registry", False), (here, "tux-probe-lua-scripts", False),
                (here, "evolution-addressbook-factory", False), (here, "libexec", False),
                (here, "php", False), (here, "fpm", False), (here, "lua5.4", True),
                (later, "at-spi2-registryd", False), (later, "nginx", False),
                (later, "gnome-text-editor", False), (later, "wsdd", False),
                (later, "firewalld", False), (later, "tux-probe-lua-script", False),
                (later, "charge-pulse", False)):
            with self.subTest(name=name, processes=processes[0][0]), processes_here(processes):
                self.assertEqual(system.running({"name": name}, None)["running"], running)
        # Asked about another computer, what is said of this one is so too.
        for name in ("at-spi2-registryd", "php-fpm", "wsdd", "tux-probe-long-daemon"):
            with self.subTest(name), processes_here(here), \
                    self.assertRaises(ValueError) as raised:
                system.running({"name": name, "on": "my work laptop"}, None)
            self.assertIn(f"On this computer, {name} is running.", str(raised.exception))
        with processes_here(later), self.assertRaises(ValueError) as raised:
            system.running({"name": "nginx", "on": "my work laptop"}, None)
        self.assertIn("No service or process called nginx is running on this computer.",
                      str(raised.exception))

    def test_a_script_is_read_from_its_command_line_word_by_word(self):
        """Each alone, as only the command line tells them apart. The interpreter's options
        come before the script, some with an argument of their own, which is no script, and
        some with code or a module to run instead. A script whose #! line runs its interpreter
        through env has the interpreter's own name, so it is found under the interpreters
        listed; one whose #! line names its interpreter by its path keeps the script's own name,
        so it is found under any, and gets what follows the interpreter on the #! line as one
        word, as "-w -T" for "#!/usr/bin/perl -w -T"."""
        for own, line, name, running in (
                ("python3", "/usr/bin/python3 -u -W ignore /usr/bin/wsdd", "wsdd", True),
                ("python3", "python3 -u -B /usr/bin/wsdd", "wsdd", True),
                ("python3", "python3 -X utf8 /home/me/bot.py", "bot", True),
                ("python3", "/usr/bin/python3 -Es /usr/sbin/foo-daemon", "foo-daemon", True),
                ("python3", "python3 -uWignore /usr/local/bin/feeds", "feeds", True),
                ("node", "node --inspect /opt/app/bridge.js", "bridge", True),
                ("node", "node /usr/local/bin/n8n start", "n8n", True),
                ("node", "node --require dotenv/config /opt/app/worker.js", "worker", True),
                ("perl", "/usr/bin/perl /usr/sbin/foo-daemon", "foo-daemon", True),
                ("perl", "perl -Mstrict -I /opt/lib /usr/local/bin/feed-fetcher", "feed-fetcher",
                 True),
                ("ruby", "ruby /usr/local/bin/foo-worker", "foo-worker", True),
                # Through env: #!/usr/bin/env php, lua5.4 or tclsh.
                ("php", "php /usr/local/bin/mytool", "mytool", True),
                ("php8.2", "php8.2 /usr/local/bin/queue-worker --sleep=3", "queue-worker", True),
                ("php", "php /usr/local/bin/my-long-php-daemon-script",
                 "my-long-php-daemon-script", True),
                ("lua5.4", "lua5.4 /usr/local/bin/tux-probe-lua-script", "tux-probe-lua-script",
                 True),
                ("tclsh", "tclsh /usr/local/bin/tcltool", "tcltool", True),
                # By its #! line, whose options are one word.
                ("long-script-nam", ["/usr/bin/perl", "-w -T",
                                     "/usr/local/bin/long-script-name-here"],
                 "long-script-name-here", True),
                ("long-python-nam", "/usr/bin/python3 -u /usr/local/bin/long-python-name-here",
                 "long-python-name-here", True),
                ("foo.py", "/usr/bin/python3 ./foo.py", "foo", True),
                # An option's argument, code, a module, or a program that is no interpreter.
                ("perl", "perl -I /usr/lib/nginx x", "nginx", False),
                ("python3", "python3 -W ignore wsdd", "ignore", False),
                ("node", "node -e 1 /opt/app/bridge.js", "bridge", False),
                ("perl", "perl -E say /usr/sbin/foo-daemon", "foo-daemon", False),
                ("python3", "celery worker --loglevel=info", "worker", False),
                ("tux-probe-long-", "/bin/sh /usr/local/bin/tux-probe-long-daemon-wrapper "
                                    "tux-probe-long-daemon", "tux-probe-long-daemon", False)):
            with self.subTest(name=name, line=line), processes_here([(own, line)]):
                self.assertEqual(system.running({"name": name}, None)["running"], running)
        with processes_here([("php", "php /usr/local/bin/mytool")]), \
                self.assertRaises(ValueError) as raised:
            system.running({"name": "mytool", "on": "my work laptop"}, None)
        self.assertIn("On this computer, mytool is running.", str(raised.exception))

    def test_what_proc_gives_is_read_word_by_word(self):
        """Its own name without the line break, and its command line split at the NULs."""
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder) / "123"
            proc.mkdir()
            (proc / "comm").write_text("foo\n")
            (proc / "cmdline").write_bytes(b"/usr/bin/perl\0-w -T\0/usr/sbin/foo\0")
            real = Path

            def paths(path):     # this computer's /proc is never read
                assert str(path).startswith("/proc/"), path
                return real(folder, str(path).removeprefix("/proc/"))
            with mock.patch.object(system, "Path", side_effect=paths):
                self.assertEqual(system._process("123"),
                                 ("foo", ["/usr/bin/perl", "-w -T", "/usr/sbin/foo"]))
                self.assertEqual(system._process("124"), ("", []))     # gone

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
