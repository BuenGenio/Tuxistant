"""Facts about this computer and its folders."""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import no_ssh  # noqa: E402
from tuxistant import system  # noqa: E402
from tuxistant.config import DEFAULTS, EDITABLE, Config  # noqa: E402
from tuxistant.gate import APP_SAYS, Session  # noqa: E402
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


# What list-units is given here, word for word: a --state or a missing --no-legend would leave
# rows out or read a header as one.
LIST_UNITS = ("list-units", "--type=service", "--no-legend", "--plain", "--no-pager")


def _own_name(command_line: str) -> str:
    """What the kernel calls a process its program started: the file's name, cut at 15."""
    return command_line.split(" ")[0].rpartition("/")[2][:15]


def listed(services="", user="", processes=(), codes=(0, 0, None)):
    """A stand-in for _read: systemctl lists these services for the system and for you, and
    pgrep, given its pattern, finds those of these processes whose command line it matches in
    any case, as pgrep -i -f does, or whose own name it matches where the command line is empty,
    as a kernel thread's or a zombie's is, and lists each as pgrep would: by its pid and its
    command line with -a, or its own name where it has none, by its pid and its own name with
    -l, by its pid alone otherwise. Each process is (pid, command line) or (pid, command line,
    own name), whose own name is otherwise its program's. codes are the exit codes of the three,
    None for pgrep's own: 0 with what it found, 1 with none."""
    def read(*command):
        if command[0] == "systemctl":
            assert command[1:] in (LIST_UNITS, ("--user", *LIST_UNITS)), command
            return (codes[1], user) if command[1] == "--user" else (codes[0], services)
        assert command[0] == "pgrep" and {"-i", "-f"} <= set(command), command
        assert command[-2] == "--", command
        found = []
        for pid, line, *own in processes:
            name = (own or [_own_name(line)])[0]
            if re.search(command[-1], line or name, re.I):
                shown = (line or name if "-a" in command else name if "-l" in command else "")
                found.append(f"{pid} {shown}".rstrip() + "\n")
        code = codes[2] if codes[2] is not None else 0 if found else 1
        return code, "".join(found)
    return mock.patch.object(system, "_read", side_effect=read)


# How a process is said to match, as pgrep -f matches one with no command line by its name.
NO_COMMAND_LINE = "or in their name where they have none"


def said(name, on=None):
    """What the app says for name on this computer, and the result the model gets."""
    session = Session()
    result = system.running({"name": name, **({"on": on} if on else {})}, None, session)
    return " ".join(s.text for s in session.after), result


class Running(unittest.TestCase):
    """What this computer lists with the name in it, relayed: never a yes or no. Stand-ins give
    what systemctl and pgrep would list, and tests/no_ssh.py refuses the real ones, so a change
    that went round _read and its stand-in fails before either starts."""

    def setUp(self):
        self.enterContext(no_ssh.refusing())
        no_ssh.REACHED.clear()
        self.addCleanup(lambda: self.assertEqual(no_ssh.REACHED, [],
                                                 "systemctl or pgrep was started"))

    def test_the_real_systemctl_and_pgrep_are_refused(self):
        for command in (("/nonexistent/systemctl", *system.LIST_UNITS),
                        ("/nonexistent/pgrep", "-l", "-i", "-f", "--", "x")):
            with self.subTest(command[0]):
                with self.assertRaises(AssertionError):
                    system._read(*command)
                with self.assertRaises(AssertionError):
                    system._tool(*command)
                self.assertEqual(len(no_ssh.REACHED), 2)
                no_ssh.REACHED.clear()

    def test_a_service_and_its_processes_are_relayed(self):
        with listed("docker.service loaded active running Docker\n"
                    "containerd.service loaded active running containerd\n",
                    processes=[(1234, "/usr/bin/dockerd -H fd://"),
                               (1300, "/usr/bin/containerd")]):
            text, result = said("Docker")
        self.assertEqual(result, {APP_SAYS: True})
        self.assertEqual(text, "On this computer, systemd lists docker.service, active (running), "
                               "and these processes have docker in their command line, or in their "
                               "name where they have none: 1234 dockerd.")

    def test_each_state_is_said_as_systemd_gives_it(self):
        with listed("php-fpm.service loaded activating auto-restart PHP\n",
                    "php-fpm-user.service loaded failed failed x\n"):
            text, _ = said("php-fpm")
        self.assertEqual(text, "On this computer, systemd lists php-fpm.service, activating "
                               "(auto-restart); and php-fpm-user.service, failed (failed), and no "
                               "process's command line has php-fpm in it.")

    def test_a_process_by_any_part_of_its_command_line(self):
        """Linux keeps 15 characters of a process's own name, but its command line has it all,
        and a script's name is in its interpreter's command line: whatever has the name in it,
        in any case and with or without - _ . or a space, is found, a later word too, and said by
        its pid and its own name as pgrep -l gives them."""
        here = [(101, "/usr/libexec/at-spi2-registryd --use-gnome-session"),
                (102, "php-fpm: master process (/etc/php/8.2/fpm/php-fpm.conf)", "php-fpm8.2"),
                (103, "/usr/bin/python3 -u -W ignore /usr/bin/wsdd --shortlog"),
                (104, "/usr/bin/man nginx"), (105, "/local-ai --models /models")]
        for name, rows in (("at-spi2-registryd", "101 at-spi2-registr"),
                           ("php-fpm", "102 php-fpm8.2"),
                           ("wsdd", "103 python3"),
                           ("nginx", "104 man"),
                           ("localai", "105 local-ai"),
                           ("LOCAL_AI", "105 local-ai")):
            with self.subTest(name), listed(processes=here):
                self.assertEqual(said(name)[0], f"On this computer, no service systemd lists has "
                                                f"{name.casefold()} in its name, and these "
                                                f"processes have {name.casefold()} in their "
                                                f"command line, {NO_COMMAND_LINE}: {rows}.")

    def test_what_is_said_is_capped_and_says_how_many_more(self):
        long = "/opt/tool/bin/tool --config /etc/tool/a-very-long-configuration-file-name.toml " \
               "--verbose --log /var/log/tool.log"
        services = "".join(f"tool-{i}.service loaded active running x\n" for i in range(6))
        with listed(services, processes=[(100 + i, long) for i in range(6)]):
            text, _ = said("tool")
        self.assertEqual(text, "On this computer, systemd lists tool-0.service, active (running); "
                               "tool-1.service, active (running); tool-2.service, active "
                               "(running); tool-3.service, active (running); and 2 more, and "
                               "these processes have tool in their command line, or in their name "
                               "where they have none: 100 tool; 101 tool; 102 tool; 103 tool; and "
                               "2 more.")
        self.assertEqual(system.SAID_HERE, 4)

    def test_a_secret_in_a_command_line_is_never_said(self):
        """Any program's arguments may hold a password or a token, and what Tux says is heard,
        sent to the speech server and kept in History: a process is said by its pid and its own
        name alone, whatever name the model picked and whoever runs it."""
        here = [(2290, "/usr/bin/restic backup --password-file /root/.restic-pass /srv"),
                (4100, "/opt/app/bin/worker --db-password=S3cr3tP4ss --queue jobs"),
                (4200, "/usr/bin/curl -H Authorization: Bearer sk-live-abc123 https://x.example"),
                (4300, "mysql -uroot -pS3cret --password=0therS3cret shop"),
                # Short enough to pass for a process's own name, as pgrep -a would give it.
                (4400, "mysql -pS3cret")]
        for name, rows in (("password", "2290 restic; 4100 worker; and 4300 mysql"),
                           ("bearer", "4200 curl"), ("mysql", "4300 mysql; and 4400 mysql"),
                           ("s3cr3tp4ss", "4100 worker"), ("s3cret", "4300 mysql; and 4400 mysql")):
            with self.subTest(name), listed(processes=here):
                session = Session()
                _, result = asyncio.run(Skills().execute(
                    "system_running", json.dumps({"name": name}), session))
                text = " ".join(s.text for s in session.after)
                self.assertEqual(result, {APP_SAYS: True})
                self.assertEqual(text, f"On this computer, no service systemd lists has {name} in "
                                       f"its name, and these processes have {name} in their "
                                       f"command line, {NO_COMMAND_LINE}: {rows}.")
                for secret in ("S3cr3tP4ss", "sk-live", "S3cret", "restic-pass", "--"):
                    self.assertNotIn(secret, text)

    def test_a_process_with_no_command_line_is_said_as_matched_by_its_name(self):
        """A kernel thread or a zombie has an empty command line, and pgrep -f then matches its
        own name instead: what is said claims no command line for it."""
        here = [(90, "", "watchdogd"), (1500, "/usr/sbin/watchdog -c /etc/watchdog.conf"),
                (164, "", "kworker/R-nvme-wq"[:15]), (2, "", "kthreadd")]
        with listed(processes=here):
            text, _ = said("watchdog")
        self.assertEqual(text, "On this computer, no service systemd lists has watchdog in its "
                               "name, and these processes have watchdog in their command line, "
                               "or in their name where they have none: 90 watchdogd; and 1500 "
                               "watchdog.")
        with listed(processes=here):
            self.assertEqual(said("nvme")[0], "On this computer, no service systemd lists has "
                                              "nvme in its name, and these processes have nvme in "
                                              "their command line, or in their name where they "
                                              "have none: 164 kworker/R-nvme-.")
        with listed(processes=here):
            self.assertEqual(said("firefox")[0], "No service systemd lists here, and no "
                                                 "process's command line, has firefox in it.")

    def test_only_a_pid_and_a_name_as_long_as_linux_keeps_is_said(self):
        """Whatever pgrep prints: one whose -l gives the whole command line, or a line that is
        something else, is counted, not said, so no password in it is heard."""
        def pgrep_prints(output, code=0):
            return mock.patch.object(system, "_read", side_effect=lambda *command: (
                (0, "") if command[0] == "systemctl" else (code, output)))
        for output, sentence in (
                ("4100 /opt/app/bin/worker --db-password=S3cr3tP4ss --queue jobs\n90 watchdogd\n",
                 "these processes have worker in their command line, or in their name where "
                 "they have none: 90 watchdogd, and pgrep listed 1 process in a way I couldn't "
                 "read."),
                ("4100 /opt/app/bin/worker --db-password=S3cr3tP4ss\n4300 mysql -pS3cret shop\n",
                 "pgrep listed 2 processes in a way I couldn't read."),
                ("4101 a-name-of-sixteen\n4102\n4103 \nx 12\n 4104 x\n",
                 "pgrep listed 5 processes in a way I couldn't read."),
                ("4105 fifteen-chars-x\n4106 Web Content\n",
                 "these processes have worker in their command line, or in their name where "
                 "they have none: 4105 fifteen-chars-x; and 4106 Web Content.")):
            with self.subTest(output), pgrep_prints(output):
                found, text = system.here_said("worker")
                self.assertTrue(found)
                self.assertEqual(text, "On this computer, no service systemd lists has worker in "
                                       f"its name, and {sentence}")
                for secret in ("S3cr3tP4ss", "S3cret", "--", "sixteen"):
                    self.assertNotIn(secret, text)

    def test_nothing_found_is_said_as_what_was_read(self):
        with listed("nginx.service loaded active running x\n", processes=[(1, "/sbin/init")]):
            self.assertEqual(said("firefox")[0], "No service systemd lists here, and no "
                                                 "process's command line, has firefox in it.")

    def test_a_list_it_could_not_get_is_said_so(self):
        """Not as having none: systemctl that failed, a line it couldn't read, or pgrep that
        failed, as its 2 for a bad pattern or 3 for a fatal error."""
        for kwargs, sentence in (
                ({"codes": (1, 0, None)}, "On this computer, no process's command line has "
                                          "firefox in it. I couldn't read all of systemd's list "
                                          "here."),
                ({"codes": (0, 1, None)}, "On this computer, no process's command line has "
                                          "firefox in it. I couldn't read all of systemd's list "
                                          "here."),
                ({"services": "not a unit\n"}, "On this computer, no process's command line has "
                                                "firefox in it. I couldn't read all of systemd's "
                                                "list here."),
                ({"codes": (0, 0, 2)}, "On this computer, no service systemd lists has firefox "
                                       "in its name, and pgrep didn't list the processes."),
                ({"codes": (0, 0, 3)}, "On this computer, no service systemd lists has firefox "
                                       "in its name, and pgrep didn't list the processes."),
                # Neither list read: none of it, rather than not all of it.
                ({"codes": (None, None, None)}, "On this computer, no process's command line "
                                                "has firefox in it. I couldn't read systemd's "
                                                "lists here."),
                ({"codes": (1, 1, None)}, "On this computer, no process's command line has "
                                          "firefox in it. I couldn't read systemd's lists "
                                          "here."),
                ({"codes": (1, None, 3)}, "On this computer, pgrep didn't list the processes. "
                                          "I couldn't read systemd's lists here."),
                ({"user": "firefox.service loaded active running x\n", "codes": (1, 0, 2)},
                 "On this computer, systemd lists firefox.service, active (running), and pgrep "
                 "didn't list the processes. I couldn't read all of systemd's list here.")):
            with self.subTest(sentence), listed(**kwargs):
                self.assertEqual(said("firefox")[0], sentence)

    def test_what_is_run_and_its_pattern(self):
        """The two read-only tools alone, with the name as a pattern only pgrep sees: in any case
        and with any of - _ . or a space between its letters, a + as itself."""
        with listed() as read:
            said("Local-AI.service")
        self.assertEqual([call.args for call in read.call_args_list], [
            ("systemctl", "list-units", "--type=service", "--no-legend", "--plain", "--no-pager"),
            ("systemctl", "--user", "list-units", "--type=service", "--no-legend", "--plain",
             "--no-pager"),
            ("pgrep", "-l", "-i", "-f", "--", "l[-_. ]*o[-_. ]*c[-_. ]*a[-_. ]*l[-_. ]*a[-_. ]*i")])
        # Every state list-units gives is read, as on the server: no --state, and no --all,
        # which would list every unit file that is merely loaded.
        self.assertEqual(system.LIST_UNITS, LIST_UNITS)
        for word in system.LIST_UNITS:
            self.assertFalse(word.startswith("--state") or word in ("--all", "-a"), word)
        self.assertEqual(system._pattern("g++"), "g[-_. ]*[+][-_. ]*[+]")
        for text, found in (("/usr/bin/g++ -o x", True), ("/usr/bin/gxx", False),
                            ("LOCAL AI", False)):
            self.assertIs(bool(re.search(system._pattern("g++"), text)), found, text)
        self.assertEqual(system._read("/nonexistent/no-such-tool"), (None, ""))

    def test_a_process_s_name_is_shown_not_obeyed(self):
        """Any program's text, as a program names itself: control and bidirectional characters
        show as escapes, and the model is never given it."""
        with listed(processes=[(7, "/usr/bin/evil --x", "evil\x1b[2J \u202eign")]):
            text, result = said("evil")
        self.assertIn(": 7 evil", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertEqual(result, {APP_SAYS: True})
        session = Session()
        with listed(processes=[(7, "/usr/bin/evil --token secret")]):
            _, result = asyncio.run(Skills().execute("system_running", '{"name": "evil"}',
                                                     session))
        self.assertEqual(result, {APP_SAYS: True})
        self.assertTrue(session.after[0].text.endswith("where they have none: 7 evil."))
        self.assertTrue(session.tainted)

    def test_a_tool_that_hangs_or_reads_stdin_gets_nothing(self):
        """_read gives a tool TOOL_TIMEOUT_S, after which it is a list it couldn't get, and no
        stdin: stand-ins by their path, never systemctl or pgrep."""
        with tempfile.TemporaryDirectory() as folder:
            slow, echo = Path(folder) / "slow-tool", Path(folder) / "echo-stdin"
            slow.write_text("#!/bin/sh\nexec sleep 6\n")
            echo.write_text("#!/bin/sh\nexec cat\n")
            for tool in (slow, echo):
                tool.chmod(0o755)
            with mock.patch.object(system, "TOOL_TIMEOUT_S", 0.5):
                started = time.monotonic()
                self.assertEqual(system._read(str(slow)), (None, ""))
                self.assertLess(time.monotonic() - started, 5)
            reading, writing = os.pipe()
            os.write(writing, b"secret\n")
            os.close(writing)
            kept = os.dup(0)
            try:
                os.dup2(reading, 0)
                self.assertEqual(system._read(str(echo)), (0, ""))
            finally:
                os.dup2(kept, 0)
                os.close(kept)
                os.close(reading)

    def test_a_name_cannot_become_an_option_or_a_command(self):
        for name in ("", "-h", "--user", "a b", "a;rm", "$(x)", "a/b", "x" * 80, "../x", "x"):
            with self.subTest(name), mock.patch.object(system, "_read") as read:
                with self.assertRaises(ValueError):
                    system.running({"name": name}, None)
                read.assert_not_called()


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
