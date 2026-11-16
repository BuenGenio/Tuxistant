"""Settings app tests.

Logic runs in-process. HeadlessApp then drives the real window against the fake daemon, on a
private bus inside headless mutter with throwaway XDG folders and a memory GSettings backend, so
nothing reaches the user's session. Set TUX_SETTINGS_SHOTS=<folder> to save a PNG of each page.
"""
import io
import ctypes
import gc
import json
import os
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
INNER = "TUX_SETTINGS_INNER" in os.environ
FAKE = ROOT / "tests/shell_harness_settings_fake.py"
EXTENSION_SCHEMAS = ROOT / "extension/tuxistant@buengenio/schemas"

# Importing Gtk opens the display named in the environment. Outside the headless session that
# would be the user's own, so point it at nothing. Logic tests need no display.
with mock.patch.dict(os.environ):
    if not INNER:
        os.environ.pop("DISPLAY", None)
        os.environ["WAYLAND_DISPLAY"] = "tuxistant-test-no-display"
    from tuxistant import calendars, settings_app as sa
    from tuxistant import voices
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango


class Logic(unittest.TestCase):
    def test_clean_server(self):
        self.assertEqual(sa.clean_server(" sandpiper:8080 "), "sandpiper:8080")
        self.assertEqual(sa.clean_server("http://sandpiper:8080/"), "sandpiper:8080")
        self.assertEqual(sa.clean_server("ws://10.0.0.5"), "10.0.0.5")
        self.assertEqual(sa.clean_server("[::1]:8080"), "[::1]:8080")
        self.assertEqual(sa.clean_server("https://ai.example.com:8443/"),
                         "https://ai.example.com:8443")
        for bad in ("", "   ", "two words", "host:99999", "host:port", "http://host/v1/realtime",
                    "sandpiper..lan:8080", ".host", "a" * 64):
            self.assertIsNone(sa.clean_server(bad), bad)

    def test_voices_group_by_accent_in_order(self):
        catalog = voices.catalog()
        groups = sa.group_voices(catalog)
        self.assertEqual(list(groups), ["American", "British"])
        self.assertEqual(sum(map(len, groups.values())), len(catalog))
        self.assertEqual(groups["British"][0]["name"], "Alice")

    def test_voice_search(self):
        nicole = {"name": "Nicole", "accent": "American", "gender": "Female"}
        lewis = {"name": "Lewis", "accent": "British", "gender": "Male"}
        self.assertTrue(sa.voice_matches(nicole, ""))
        self.assertTrue(sa.voice_matches(nicole, "  NIC  "))
        self.assertTrue(sa.voice_matches(nicole, "american female"))
        self.assertFalse(sa.voice_matches(nicole, "british female"))
        self.assertFalse(sa.voice_matches(nicole, "male"), "male must not match Female")
        self.assertTrue(sa.voice_matches(lewis, "male brit"))

    def test_skill_subtitles_say_whose_it_is(self):
        recipe = {"description": "Tides", "permissions": {"network": ["api.example"]},
                  "source": "yours", "recipe": True}
        self.assertEqual(sa.skill_subtitle(recipe),
                         "A recipe you added. Tides\nCan use the network (api.example)")
        self.assertTrue(sa.skill_subtitle({**recipe, "recipe": False}).startswith(
            "A skill you added. "))
        self.assertTrue(sa.skill_subtitle({**recipe, "source": "built-in"}).startswith("Tides"))

    def test_skill_settings_name_skills_that_come_with_tuxistant(self):
        from tuxistant.skillhost import offline
        with tempfile.TemporaryDirectory() as folder:
            ids = set(offline(dict(sa.DEFAULTS), user=Path(folder) / "skills").current().skills)
        self.assertLessEqual(set(sa.SKILL_SETTINGS), ids)
        self.assertTrue(all(skill.startswith("org.tuxistant.") for skill in sa.SKILL_SETTINGS),
                        "ids only what comes with Tuxistant may have")
        for names in sa.SKILL_SETTINGS.values():
            for name in names:
                self.assertTrue(callable(getattr(sa.SettingsBox, f"{name}_rows", None)), name)

    def test_settings_whose_skill_isnt_listed_are_named(self):
        everything = [{"id": skill_id, "source": "built-in"} for skill_id in sa.SKILL_SETTINGS]
        self.assertEqual(sa.unreachable_settings(everything), [])
        self.assertEqual(set(sa.SETTINGS_TITLES), {name for names in sa.SKILL_SETTINGS.values()
                                                  for name in names})
        without = [s for s in everything if s["id"] != "org.tuxistant.lights"]
        self.assertEqual(sa.unreachable_settings(without), ["lights"])
        only_one = [s for s in everything if s["id"] != "org.tuxistant.home"]
        self.assertEqual(sa.unreachable_settings(only_one), [], "Home Assistant status has them")
        mine = [{**s, "source": "yours"} if s["id"] == "org.tuxistant.lights" else s
                for s in everything]
        self.assertEqual(sa.unreachable_settings(mine), ["lights"],
                         "a skill of yours can't take an id, but if one did it gets no account")

    def test_a_path_can_not_fake_a_line(self):
        self.assertEqual(sa.path_shown("moon\nComes with Tuxistant\tx"),
                         "moon\\nComes with Tuxistant\\tx")
        self.assertEqual(sa.skill_problems({"/s/a\nb.yaml": "Bad."}),
                         [("a\\nb.yaml", "Bad.", "/s/a\\nb.yaml")])

    def test_every_setting_has_one_home(self):
        """What General keeps and what skills' pages show, together, are all the settings."""
        from tuxistant.config import EDITABLE
        general = {"server", "api_key", "pipeline", "conversation_mode", "listen_timeout_s",
                   "fn_hold_enabled", "fn_hold_ms", "hide_on_release", "dictation_enabled",
                   "instructions", "voice", "theme"}
        on_skills = set().union(*EXPECTED_SETTINGS.values())
        self.assertEqual(general & on_skills, set())
        self.assertEqual(general | on_skills, set(EDITABLE))

    def test_permission_list(self):
        self.assertEqual(sa.permission_list({}), [])
        self.assertEqual(sa.permission_list({"network": True, "shell": False}),
                         [("Use the network", "Anywhere")])
        self.assertEqual(sa.permission_list({"network": ["home", "a.org"], "<b>x</b>": True}),
                         [("Use the network", "your own server, a.org"), ("Use <b>x</b>", "")])

    def test_risk_words(self):
        self.assertEqual(sa.risk_words("high", ["shell", "files_write"], {}),
                         "Because it can run shell commands and change files")
        self.assertEqual(sa.risk_words("low", [], {}), "It asks for nothing that raises the risk")
        self.assertEqual(sa.risk_words("medium", None, {"network": True}),
                         "Can use the network", "an older daemon gives no reason")

    def test_code_words_never_pretend_the_manifest_is_the_skill(self):
        built_in = {"source": "built-in", "actions": [
            {"module": "tuxistant.livedata"}, {"module": "tuxistant.skills"}, {}]}
        self.assertIn("tuxistant/livedata.py, tuxistant/skills.py", sa.code_words(built_in))
        self.assertIn("the code decides", sa.code_words(built_in))
        self.assertIn("tuxistant/recipes.py",
                      sa.code_words({"source": "yours", "recipe": True, "actions": []}))
        self.assertIn("sandbox", sa.code_words({"source": "yours", "actions": []}))
        self.assertIn("sandbox", sa.code_words({"source": "bundled", "actions": []}))

    def test_held_back_and_param_words(self):
        self.assertEqual(sa.held_back_words({"offered": True}), "The model is offered this")
        self.assertIn("switched on", sa.held_back_words(
            {"offered": False, "held_back": "switched_off"}))
        self.assertIn("filled in", sa.held_back_words(
            {"offered": False, "held_back": "not_set_up"}))
        self.assertEqual(sa.param_words("day", {"type": "string", "enum": ["a", "b"],
                                                "description": "Which day"}, ["day"]),
                         ("day (required)", 'Which day\ntype: string\nenum: ["a", "b"]'))
        self.assertEqual(sa.param_words("n", {"type": "integer"}, []), ("n", "type: integer"))
        self.assertEqual(sa.manifest_size("x" * 100), "100 bytes")
        self.assertEqual(sa.manifest_size("x" * 65536), "64.0 KB")

    def test_skill_problems_are_named_by_file(self):
        found = sa.skill_problems({"/x/skills/b.yaml": "Bad.", "/x/skills/a": "Worse."})
        self.assertEqual(found, [("a", "Worse.", "/x/skills/a"), ("b.yaml", "Bad.",
                                                                 "/x/skills/b.yaml")])
        self.assertEqual(sa.skill_problems(["not", "a", "mapping"]), [])

    def test_permission_words(self):
        words = sa.permission_words
        self.assertEqual(words({}), "Needs no special permissions")
        self.assertEqual(words({"shell": False, "network": []}), "Needs no special permissions")
        self.assertEqual(words({"shell": True}), "Can run shell commands")
        self.assertEqual(words({"network": ["a.org", "b.org"], "shell": True}),
                         "Can run shell commands and use the network (a.org, b.org)")
        self.assertEqual(words({"files_read": "~/Notes", "clipboard": True, "files_write": True}),
                         "Can change files, read files (~/Notes) and use the clipboard")
        self.assertEqual(words({"camera_feed": True}), "Can use camera feed")

    def test_a_home_network_permission_is_your_own_server(self):
        words = sa.permission_words
        self.assertEqual(words({"network": ["home"]}), "Can use the network (your own server)")
        self.assertEqual(words({"network": "home"}), "Can use the network (your own server)")
        self.assertEqual(words({"network": ["home", "a.org"], "clipboard": True}),
                         "Can use the network (your own server, a.org) and use the clipboard")
        self.assertEqual(words({"files_read": ["home"]}), "Can read files (home)",
                         "only the network names a server")

    def test_home_assistant_permissions_are_said_in_words(self):
        from tuxistant.skills import Skills
        said = {skill["name"]: sa.permission_words(skill["permissions"])
                for skill in Skills().listing() if skill["id"].startswith("org.tuxistant.home")}
        self.assertEqual(said, {
            "Home Assistant": "Can control devices through Home Assistant",
            "Home Assistant status": "Can read the state of your home"})

    def test_every_real_home_skill_says_so(self):
        from tuxistant.skills import Skills
        for skill in Skills().listing():
            network = skill["permissions"].get("network")   # true means anywhere at all
            if isinstance(network, list) and "home" in network:
                self.assertIn("your own server", sa.permission_words(skill["permissions"]))

    def test_risk_label(self):
        self.assertEqual(sa.risk_label("medium"), "Medium risk")
        self.assertEqual(set(sa.RISK_CLASSES), {"low", "medium", "high"})

    def test_format_time(self):
        now = datetime(2026, 11, 3, 15, 0)
        at = lambda when: sa.format_time(when.timestamp(), now.timestamp())  # noqa: E731
        self.assertEqual(at(now.replace(hour=9, minute=5)), "09:05")
        self.assertEqual(at(now - timedelta(days=1)), "Yesterday 15:00")
        self.assertEqual(at(datetime(2026, 11, 1, 8, 30)), "Sunday 08:30")
        self.assertEqual(at(datetime(2026, 3, 9, 8, 30)), "9 Mar 08:30")
        self.assertEqual(at(datetime(2025, 12, 31, 23, 59)), "31 Dec 2025 23:59")

    def test_action_words(self):
        self.assertEqual(sa.action_words([
            {"name": "time.now", "arguments": "{}", "result": {"time": "10:30"}},
            {"name": "web.search", "arguments": "{}", "result": {"error": "offline"}},
            {"name": "odd.result", "arguments": "{}", "result": "error"}]),
            "time.now, web.search (failed), odd.result")

    def test_history_details(self):
        self.assertEqual(sa.history_details({"reply": "Hi", "actions": [], "status": "ok"}), "Hi")
        action = {"name": "a", "arguments": "{}", "result": {}}
        self.assertEqual(
            sa.history_details({"reply": "", "actions": [action], "status": "cancelled"}),
            "No reply\nRan a\nCancelled")
        self.assertEqual(sa.history_details({"reply": "Hm", "actions": [], "status": "error"}),
                         "Hm\nEnded with an error")

    def test_actions_that_did_not_finish_or_run_are_not_called_ran(self):
        done = {"name": "time.now", "arguments": "{}", "result": {"time": "10:30"}}
        cut_off = {"name": "file.find", "arguments": "{}", "result": None}
        refused = {"name": "file.trash", "arguments": "{}",
                   "result": {"declined": True, "message": "no"}}
        self.assertEqual(sa.action_words([done, cut_off]), "time.now, file.find (no result)")
        text = sa.history_details({"reply": "Hm", "actions": [done, refused], "status": "ok"})
        self.assertEqual(text, "Hm\nRan time.now\nDid not run file.trash")

    def test_run_words(self):
        def words(result):
            return sa.run_words({"name": "shell.run", "result": result})

        self.assertEqual(words({"output": "", "exit_code": 0, "sandboxed": True}),
                         "Ran in the sandbox")
        self.assertEqual(words({"output": "", "exit_code": 0, "sandboxed": False}),
                         "Ran outside the sandbox")
        self.assertEqual(words({"output": "", "exit_code": 2, "sandboxed": True}),
                         "Ran in the sandbox, exit code 2", "a failed command must not look fine")
        self.assertEqual(words({"output": ""}), "Ran", "no word about the sandbox is no claim")
        self.assertEqual(words({"error": "That took too long, so it was stopped."}), "Failed",
                         "the shell's errors don't say where, so this doesn't either")
        self.assertEqual(words({"declined": True, "message": "no"}), "Did not run, not allowed")
        self.assertEqual(words(None), "No result, the request ended first")

    def test_answer_words(self):
        def words(allowed, level):
            return sa.answer_words({"allowed": allowed, "level": level})

        self.assertEqual(words(True, "click"), "Allowed")
        self.assertEqual(words(False, "click"), "Not allowed", "a timeout is a no, not a click")
        self.assertEqual(words(True, "admin"), "Allowed to run as root")
        self.assertEqual(words(False, "admin"), "Not allowed to run as root")

    def test_as_text_keeps_output_readable(self):
        self.assertEqual(sa.as_text("plain"), "plain")
        self.assertEqual(sa.as_text({"output": "one", "code": 0, "ok": True, "x": [1, "é"]}),
                         'output: one\ncode: 0\nok: true\nx: [1, "é"]')
        self.assertEqual(sa.as_text({"output": "total 8\n-rw 1 a\n-rw 2 b", "exit_code": 0}),
                         "output:\ntotal 8\n-rw 1 a\n-rw 2 b\nexit_code: 0",
                         "output starts on its own line, or its columns go out of step")
        self.assertEqual(sa.as_text([1]), "[1]")

    def test_exact_command(self):
        def command(arguments, result=None):
            return sa.exact_command({"name": "shell.run", "arguments": arguments, "result": result})

        self.assertEqual(command('{"command": "ls ~"}'), "ls ~")
        self.assertEqual(command('{"command": "ls ~"}', {"command": "ls /home/tux"}),
                         "ls /home/tux", "what ran beats what was asked for")
        self.assertEqual(command('{"command": "ls'), '{"command": "ls',
                         "broken arguments are shown as they came")
        self.assertEqual(command("ls -la"), "ls -la")
        self.assertEqual(command("[1, 2]"), "[1, 2]")
        self.assertEqual(command(None), "")
        self.assertEqual(command('{"command": ""}'), "")
        self.assertEqual(command('{"other": "x"}'), "")

    def test_action_fields_for_a_shell_command(self):
        action = {"name": "shell.run", "arguments": '{"command": "rm old.log"}',
                  "asked": [{"title": "Run this command?", "detail": "rm old.log",
                             "level": "click", "allowed": True}],
                  "result": {"output": "gone", "exit_code": 0, "sandboxed": False,
                             "file_contents": True}}
        self.assertEqual(sa.action_fields(action), [
            ("shell.run", "Ran outside the sandbox"), ("Command", "rm old.log"),
            ("Allowed: Run this command?", "rm old.log"),
            ("Result", "output: gone\nexit_code: 0\nsandboxed: false\nfile_contents: true")])

    def test_action_fields_of_other_actions(self):
        self.assertEqual(
            sa.action_fields({"name": "time.now", "arguments": "{}", "result": {"time": "10:30"}}),
            [("time.now", "Ran"), ("Result", "time: 10:30")])
        self.assertEqual(
            sa.action_fields({"name": "file.find", "arguments": '{"query": "tax"}',
                              "result": None}),
            [("file.find", "No result, the request ended first"), ("Arguments", "query: tax")])
        self.assertEqual(
            sa.action_fields({"name": "odd", "arguments": "not json", "result": "fine"}),
            [("odd", "Ran"), ("Arguments", "not json"), ("Result", "fine")])

    def test_action_fields_never_cut(self):
        output = "line\n" * 50000
        action = {"name": "shell.run", "arguments": json.dumps({"command": "x" * 9000}),
                  "result": {"sandboxed": True, "output": output}}
        fields = dict(sa.action_fields(action))
        self.assertEqual(fields["Command"], "x" * 9000)
        self.assertIn(output, fields["Result"])

    def test_history_details_for_a_shell_action(self):
        long_command = "echo " + "x" * 500
        entry = {"reply": "Done.", "status": "ok", "actions": [
            {"name": "shell.run", "arguments": '{"command": "ls"}',
             "result": {"sandboxed": True, "output": "a"}},
            {"name": "shell.run", "arguments": json.dumps({"command": "rm a"}),
             "asked": [{"title": "Run rm a?", "detail": "rm a", "level": "click",
                        "allowed": False}],
             "result": {"declined": True, "message": "no"}},
            {"name": "shell.run", "arguments": json.dumps({"command": long_command}),
             "result": {"sandboxed": True, "output": "x"}}]}
        lines = sa.history_details(entry).split("\n")
        self.assertEqual(lines[:4], ["Done.", "Ran in the sandbox: ls",
                                     "Did not run, not allowed: rm a", "Not allowed: Run rm a?"])
        self.assertTrue(lines[4].startswith("Ran in the sandbox: echo xxx"))
        self.assertIn("more characters", lines[4], "a cut in the summary is always marked")

    def test_history_details_for_a_command_that_never_was(self):
        action = {"name": "shell.run", "arguments": '{"command": ""}',
                  "result": {"error": "No command was given."}}
        self.assertEqual(sa.history_details({"reply": "Hm", "actions": [action], "status": "ok"}),
                         "Hm\nFailed")

    def test_history_details_for_a_planned_task(self):
        ask = {"title": "Send this to Claude?", "detail": "x", "level": "click", "allowed": True}
        root = {"title": "Install htop?", "detail": "apt install htop", "level": "admin",
                "allowed": True}
        text = sa.history_details({"reply": "Done.", "status": "ok", "actions": [
            {"name": "task.run", "arguments": "{}", "asked": [ask, root],
             "result": {"answer": "Done.", "steps": 2}}]})
        self.assertEqual(text, "Done.\nRan task.run\nAllowed: Send this to Claude?\n"
                               "Allowed to run as root: Install htop?")

    def test_a_task_shows_every_action_recorded_beside_it(self):
        ask = {"title": "Send this output to Claude?", "detail": "x", "level": "click",
               "allowed": True}
        actions = [{"name": "task.run", "arguments": '{"goal": "size it up"}', "asked": [ask],
                    "result": {"answer": "It's big.", "steps": 2}},
                   {"name": "shell.run", "arguments": '{"command": "du -sh ~"}',
                    "result": {"output": "12M", "exit_code": 0, "sandboxed": True}}]
        text = sa.history_details({"reply": "It's big.", "status": "ok", "actions": actions})
        self.assertEqual(text, "It's big.\nRan task.run\nAllowed: Send this output to Claude?\n"
                               "Ran in the sandbox: du -sh ~")
        fields = [field for action in actions for field in sa.action_fields(action)]
        self.assertIn(("Command", "du -sh ~"), fields)
        self.assertIn(("Result", "output: 12M\nexit_code: 0\nsandboxed: true"), fields)

    def test_shown_makes_hidden_characters_visible_and_cuts_nothing(self):
        self.assertEqual(sa.shown("a\x00b"), "a\\u0000b")
        self.assertEqual(sa.shown("rm \u202efdp.exe"), "rm \\u202efdp.exe")
        self.assertEqual(sa.shown(json.loads('"a\\ud800b"')), "a\\ud800b")
        self.assertEqual(sa.shown("two\nlines\tand <b>"), "two\nlines\tand <b>")
        self.assertEqual(sa.shown("x" * 100000), "x" * 100000)

    def test_phone_choices(self):
        devices = [{"id": "a1", "name": "Pixel", "type": "phone", "connected": True},
                   {"id": "b2", "name": "Tab", "type": "tablet", "connected": False}]
        pick = sa.phone_choices
        self.assertEqual(pick(devices, ""), {"": "Automatic", "Pixel": "Pixel", "Tab": "Tab"})
        self.assertEqual(pick([], ""), {"": "Automatic"})
        self.assertEqual(pick(None, ""), {"": "Automatic"})

    def test_calendar_listing_takes_the_daemons_reason(self):
        family = {"id": "c1", "name": "Family", "account": "me@example.org"}
        self.assertEqual(sa.calendar_listing([family]), ([family], ""))
        self.assertEqual(sa.calendar_listing([]), ([], ""))
        with mock.patch.object(calendars, "_backend", side_effect=ValueError(calendars.NO_EDS)):
            reply = json.loads(json.dumps(calendars.writable()))
        self.assertEqual(sa.calendar_listing(reply), (None, calendars.NO_EDS))
        self.assertEqual(sa.calendar_listing({}), (None, sa.NO_CALENDARS))
        self.assertEqual(sa.calendar_listing("nonsense"), ([], ""))

    def test_calendar_choices(self):
        family = {"id": "c1", "name": "Family", "account": "me@example.org"}
        local = {"id": "c2", "name": "Personal", "account": ""}
        pick = sa.calendar_choices
        self.assertEqual(pick([family, local], ""), {
            "": "Automatic", "c1": "Family (me@example.org)", "c2": "Personal"})
        self.assertEqual(pick([family], "c1"), {"": "Automatic", "c1": "Family (me@example.org)"})
        self.assertEqual(pick([family], "gone"), {
            "": "Automatic", "c1": "Family (me@example.org)", "gone": sa.GONE_CALENDAR},
            "a saved calendar that left is still a choice, so the next save doesn't lose it")
        self.assertEqual(pick(None, "c1"), {"": "Automatic", "c1": "…"},
                         "nothing is known to be missing before the daemon answers")
        self.assertEqual(pick([], ""), {"": "Automatic"})

    def test_calendar_permissions_read_as_words(self):
        self.assertEqual(sa.permission_words({"calendar_read": True}), "Can read your calendars")
        self.assertEqual(sa.permission_words({"mail_read": True}), "Can read your mail")
        self.assertEqual(sa.permission_words({"calendar_write": True}), "Can add events to a calendar")

    def test_a_saved_phone_is_kept_when_nothing_lists_it(self):
        pixel = {"id": "a1", "name": "Pixel", "type": "phone", "connected": True}
        pick = sa.phone_choices
        self.assertEqual(pick([pixel], "Old phone"), {
            "": "Automatic", "Pixel": "Pixel", "Old phone": "Not available: Old phone"})
        self.assertEqual(pick([], "Old phone"), {
            "": "Automatic", "Old phone": "Not available: Old phone"})
        self.assertEqual(pick(None, "Old phone"), {"": "Automatic", "Old phone": "Old phone"},
                         "nothing is known to be missing before the daemon answers")

    def test_a_saved_phone_means_what_the_daemon_takes_it_to_mean(self):
        pixel = {"id": "a1", "name": "Pixel", "type": "phone", "connected": True}
        twin = dict(pixel, id="a2")
        pick = sa.phone_choices
        self.assertEqual(pick([pixel], "PIXEL"), {"": "Automatic", "PIXEL": "Pixel"})
        self.assertEqual(pick([pixel], "a1"), {"": "Automatic", "a1": "Pixel"})
        self.assertEqual(pick([pixel, twin], ""), {"": "Automatic", "Pixel": "Pixel"})

    def test_cloud_note_says_where_requests_really_go(self):
        note = sa.cloud_note
        self.assertEqual(note({}), f"Nothing goes to Anthropic. {sa.RESTART_TO_AGREE}",
                         "a daemon from before Cloud")
        for config in ({**CLOUD_ON, "cloud_enabled": False}, {**CLOUD_ON, "cloud_enabled": "true"}):
            self.assertEqual(note(config), "Nothing goes to Anthropic")
        self.assertEqual(note(CLOUD_ON), "Requests with several steps go to Anthropic")
        self.assertEqual(note({**CLOUD_ON, "cloud_key_set": False}),
                         "No API key yet, so nothing goes to Anthropic")
        self.assertTrue(note({**CLOUD_ON, "cloud_consent": {}}).startswith(
            "On, but not agreed to, so nothing goes to Anthropic"), "an install from before")
        stale = {**CLOUD_ON["cloud_consent"], "version": CONSENT_VERSION - 1}
        self.assertTrue(note({**CLOUD_ON, "cloud_consent": stale}).startswith(
            "What is sent has changed since you agreed, so nothing goes"))
        self.assertEqual(note({**CLOUD_ON, "cloud_model": "<b>m</b>"}),
                         "You agreed for another model, so nothing goes to Anthropic. Turn it "
                         "on to agree for <b>m</b>")

    def test_the_switch_is_on_only_with_an_agreement(self):
        self.assertTrue(sa.cloud_on(CLOUD_ON))
        for change in ({"cloud_enabled": False}, {"cloud_consent": {}},
                       {"cloud_model": "claude-other-1"}):
            with self.subTest(change):
                self.assertFalse(sa.cloud_on({**CLOUD_ON, **change}))
        self.assertFalse(sa.cloud_on({}), "a daemon from before Cloud")
        self.assertTrue(sa.cloud_on({**CLOUD_ON, "cloud_key_set": False}),
                        "the note says the key is missing; the switch says what was chosen")

    def test_an_older_daemon_is_shown_as_it_really_is(self):
        """One that hasn't restarted since the update: no cloud_consent, and the cloud with the
        switch and a key alone. Saying nothing goes, or showing it off, would not be true."""
        old = {"cloud_enabled": True, "cloud_key_set": True, "cloud_model": "claude-opus-5-5"}
        self.assertTrue(sa.older_daemon(old))
        self.assertFalse(sa.older_daemon(CLOUD_ON))
        self.assertFalse(sa.older_daemon({**CLOUD_ON, "cloud_consent": {}}))
        self.assertTrue(sa.cloud_on(old), "on, so it can be switched off")
        self.assertEqual(sa.cloud_note(old), "Tuxistant must be restarted before it can ask you "
                         "to agree. Until then, requests with several steps go to Anthropic")
        self.assertEqual(sa.cloud_note({**old, "cloud_key_set": False}),
                         f"No API key yet, so nothing goes to Anthropic. {sa.RESTART_TO_AGREE}")
        self.assertFalse(sa.cloud_on({**old, "cloud_enabled": False}))
        self.assertEqual(sa.cloud_note({**old, "cloud_enabled": False}),
                         f"Nothing goes to Anthropic. {sa.RESTART_TO_AGREE}")
        self.assertIsNone(sa.consent_words(old))

    def test_the_agreement_is_shown_with_its_date(self):
        title, words = sa.consent_words(CLOUD_ON)
        self.assertEqual(title, "You agreed on 13 November 2026")
        self.assertEqual(words, f"For claude-opus-5-5 at Anthropic, version {CONSENT_VERSION} "
                                "of what is sent")
        title, words = sa.consent_words({**CLOUD_ON, "cloud_model": "other"})
        self.assertIn("doesn't cover what would be sent now", words)
        self.assertIsNone(sa.consent_words({**CLOUD_ON, "cloud_consent": {}}))
        self.assertIsNone(sa.consent_words({}))
        self.assertIsNone(sa.consent_words({"cloud_consent": "yes"}))
        self.assertEqual(sa.day_words("not a date"), "a date that can't be read")
        self.assertEqual(sa.day_words(None), "a date that can't be read")
        self.assertEqual(sa.day_words("2027-01-02"), "2 January 2027")

    def test_the_cloud_dialog_says_exactly_what_leaves(self):
        body = sa.cloud_body("claude-test-1") + sa.CLOUD_DETAILS
        for words in ("Anthropic's model claude-test-1", "with your API key",
                      "instead of to your own server", "While this is on",
                      "a request that needs several steps", "What leaves this computer",
                      "your request", "the names and descriptions of the actions",
                      "skills you added", "file and folder names, command output, calendar events "
                      "and search results", "never leaves without your click on that one item",
                      "what is inside a file", "what Home Assistant answers",
                      "your list of timers and reminders",
                      "the request itself, once the conversation has read something private",
                      "Your voice, dictation and single-step requests never go to Anthropic",
                      "privacy policy"):
            self.assertIn(words, body)
        self.assertEqual(sa.CLOUD_PRIVACY_URL, "https://www.anthropic.com/legal/privacy")

    def test_error_text(self):
        self.assertEqual(
            sa.error_text("GDBus.Error:org.tuxistant.Error.Invalid: voice must be str"),
            "voice must be str")
        self.assertEqual(sa.error_text("plain"), "plain")
        self.assertEqual(sa.error_text("GDBus.Error:org.x.Y:"), "The request failed")

    def test_not_answering(self):
        error = lambda quark, code: GLib.Error.new_literal(quark, "x", code)  # noqa: E731
        dbus, io_error = Gio.DBusError.quark(), Gio.io_error_quark()
        for code in (Gio.DBusError.SERVICE_UNKNOWN, Gio.DBusError.NAME_HAS_NO_OWNER,
                     Gio.DBusError.NO_REPLY):
            self.assertTrue(sa.not_answering(error(dbus, code)))
        self.assertTrue(sa.not_answering(error(io_error, Gio.IOErrorEnum.TIMED_OUT)))
        self.assertFalse(sa.not_answering(error(dbus, Gio.DBusError.FAILED)))
        self.assertFalse(sa.not_answering(error(dbus, Gio.DBusError.INVALID_ARGS)))
        self.assertFalse(sa.not_answering(GLib.Error("Unreadable reply")))

    def test_extension_schema_dirs(self):
        tail = "gnome-shell/extensions/tuxistant@buengenio/schemas"
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/h", "XDG_DATA_DIRS": "/a::/b"}):
            self.assertEqual([str(p) for p in sa.extension_schema_dirs()],
                             [f"/h/{tail}", f"/a/{tail}", f"/b/{tail}"])
        with mock.patch.dict(os.environ, {"HOME": "/home/t"}):
            os.environ.pop("XDG_DATA_HOME", None)
            os.environ.pop("XDG_DATA_DIRS", None)
            self.assertEqual(str(sa.extension_schema_dirs()[0]), f"/home/t/.local/share/{tail}")

    def test_load_shell_settings_without_extension(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": tmp, "XDG_DATA_DIRS": tmp}):
            self.assertIsNone(sa.load_shell_settings())

    def test_shortcut_from_key(self):
        mask = Gdk.ModifierType
        pick = sa.shortcut_from_key
        self.assertEqual(pick(Gdk.KEY_space, mask.CONTROL_MASK | mask.SUPER_MASK),
                         "<Control><Super>space")
        self.assertEqual(pick(Gdk.KEY_T, mask.ALT_MASK | mask.SHIFT_MASK), "<Shift><Alt>t")
        self.assertEqual(pick(Gdk.KEY_F9, mask(0)), "F9")
        self.assertIsNone(pick(Gdk.KEY_a, mask(0)))
        self.assertIsNone(pick(Gdk.KEY_a, mask.SHIFT_MASK))
        self.assertIsNone(pick(Gdk.KEY_Control_L, mask.CONTROL_MASK))

    def test_pipelines_warn_about_the_eou_model(self):
        notes = dict(sa.PIPELINES)
        self.assertEqual(list(notes), ["tux-realtime", "tux-realtime-eou"])
        self.assertIn("English only", notes["tux-realtime-eou"])
        self.assertIn("GPU memory", notes["tux-realtime-eou"])


class HeadlessApp(unittest.TestCase):
    """Starts the isolated session and has the Inner tests below run inside it."""

    def test_real_app(self):
        for tool in ("mutter", "dbus-run-session"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is not installed")
        # Under /tmp because a Wayland socket path is limited to 108 bytes.
        with tempfile.TemporaryDirectory(prefix="tuxs.", dir="/tmp") as tmp:
            home = Path(tmp)
            for name in ("run", "config", "data", "cache", "state", "home", "fake"):
                (home / name).mkdir(mode=0o700)
            result = home / "result.json"
            env = {
                "PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HOME": str(home / "home"),
                "XDG_RUNTIME_DIR": str(home / "run"), "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_STATE_HOME": str(home / "state"),
                "XDG_DATA_DIRS": "/usr/local/share:/usr/share", "XDG_SESSION_TYPE": "wayland",
                "GSETTINGS_BACKEND": "memory", "GIO_USE_VFS": "local", "GDK_BACKEND": "wayland",
                "GDK_DEBUG": "no-portals", "ADW_DISABLE_PORTAL": "1", "GTK_A11Y": "test",
                "GSK_RENDERER": "cairo", "TUX_SETTINGS_INNER": "1",
                # HOME moved, but the daemon's libraries may still live in the real user site.
                "PYTHONUSERBASE": site.getuserbase(),
                "TUX_SETTINGS_RESULT": str(result), "TUX_SETTINGS_FAKE_DIR": str(home / "fake"),
            }
            if shots := os.environ.get("TUX_SETTINGS_SHOTS"):
                env["TUX_SETTINGS_SHOTS"] = shots
            command = ["dbus-run-session", "--", "mutter", "--headless", "--wayland", "--no-x11",
                       f"--wayland-display=tuxset-{os.getpid()}", "--virtual-monitor", "1400x1600",
                       "--", sys.executable, str(Path(__file__).resolve()), "--inner"]
            process = subprocess.Popen(
                command, env=env, cwd=ROOT, text=True, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                output, _ = process.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)  # our own new session, nothing else
                output, _ = process.communicate()
            if not result.exists():
                self.fail("the headless session gave no result:\n" + output[-3000:])
            outcome = json.loads(result.read_text())
            self.assertTrue(outcome["ok"], outcome["log"])
            noisy = ("Gtk-", "Adwaita", "Gdk-", "Pango", "GLib-GObject")
            problems = [line for line in output.splitlines() if "CRITICAL" in line or (
                "WARNING" in line and any(source in line for source in noisy))]
            self.assertEqual(problems, [], "\n".join(problems))
            if shots:
                print(outcome["log"], file=sys.stderr)


# A separate process through app.run(), under another id: the Inner tests hold the real one.
LAST_SAVE_APP = """
import sys
from gi.repository import GLib
from tuxistant import settings_app as sa

app = sa.SettingsApp()
app.set_application_id("org.tuxistant.SettingsLastSaveTest")

def close_when_ready():
    window = app.get_active_window()
    if not window or window.state != "ready":
        return GLib.SOURCE_CONTINUE
    window.general.spins["listen_timeout_s"][0].set_value(int(sys.argv[1]))
    window.close()
    return GLib.SOURCE_REMOVE

GLib.timeout_add(20, close_when_ready)
sys.exit(app.run(sys.argv[:1]))
"""


def settle(ms=120):
    deadline = time.monotonic() + ms / 1000
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.003)


def wait(condition, what="condition", timeout=6):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        if not context.iteration(False):
            time.sleep(0.003)


def control(method, *args, signature=""):
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    params = GLib.Variant(f"({signature})", args) if signature else None
    reply = connection.call_sync(sa.BUS_NAME, sa.BUS_PATH, "org.tuxistant.Test", method, params,
                                 None, Gio.DBusCallFlags.NONE, 3000, None)
    return reply.unpack()


def calls(method=None):
    found = json.loads(control("Calls")[0])
    return [call for call in found if method is None or call[0] == method]


def saved():
    return [call[1:] for call in calls("SetConfig")]


def forget():
    control("Forget")


def start_fake():
    process = subprocess.Popen(
        [sys.executable, str(FAKE), "--dir", os.environ["TUX_SETTINGS_FAKE_DIR"]],
        stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"
    return process


def stop_fake(process):
    control("Quit")
    process.wait(timeout=5)
    process.stdout.close()


def colours(at):
    """The highlight tags at a place, leaving out the ones for the whole text and its wrapping."""
    return [tag.props.name for tag in at.get_tags() if tag.props.name in sa.highlight.KINDS]


def shot(window, name):
    folder = os.environ.get("TUX_SETTINGS_SHOTS")
    if not folder:
        return
    settle(350)
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    texture.save_to_png(str(Path(folder) / f"{name}.png"))


def is_chosen(row):
    return row.check.get_opacity() == 1


def selected_state(row) -> str:
    """What a screen reader is told, through GTK's own test API, which has no introspection.
    It compares with true, and when they differ it gives back what is really stored."""
    gtk = ctypes.CDLL("libgtk-4.so.1")
    check = gtk.gtk_test_accessible_check_state
    check.restype = ctypes.c_char_p
    capsule = ctypes.pythonapi.PyCapsule_GetPointer
    capsule.restype = ctypes.c_void_p
    capsule.argtypes = [ctypes.py_object, ctypes.c_char_p]
    stored = check(ctypes.c_void_p(capsule(row.__gpointer__, None)),
                   ctypes.c_int(int(Gtk.AccessibleState.SELECTED)), ctypes.c_int(1))
    return "true" if stored is None else stored.decode()


def chosen_voices(window):
    return [row.value for _g, rows in window.voices.groups for row, _v in rows if is_chosen(row)]


class Keyboard:
    """Real key events, through the compositor's remote desktop API on the private bus."""

    SERVICE = "org.gnome.Mutter.RemoteDesktop"

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = self.call("/org/gnome/Mutter/RemoteDesktop", self.SERVICE, "CreateSession",
                                 reply="(o)")[0]
        self.session_call("Start")

    def call(self, path, interface, method, params=None, reply=None):
        return self.bus.call_sync(
            self.SERVICE, path, interface, method, params,
            GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE, 3000, None).unpack()

    def session_call(self, method, params=None):
        self.call(self.session, f"{self.SERVICE}.Session", method, params)

    def press(self, *keys):
        for down, order in ((True, keys), (False, reversed(keys))):
            for key in order:
                self.session_call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (key, down)))
                settle(30)

    def close(self):
        self.session_call("Stop")


def dispose(window):
    """The real app has one window and exits with it. Here others must stop listening too."""
    GObject.signal_handlers_destroy(window.daemon.proxy)
    window.destroy()


def open_skill(window, skill_id):
    """A skill's page, as a click on its row opens it, once its details have come."""
    dialog = window.skills.dialog
    if dialog and dialog.skill and dialog.skill["id"] == skill_id:   # not a settings-only page
        return dialog
    close_skill(window)
    window.views.set_visible_child_name("skills")
    wait(lambda: any(row.skill["id"] == skill_id for row in window.skills.rows), "its row")
    row = next(row for row in window.skills.rows if row.skill["id"] == skill_id)
    row.activate()
    dialog = window.skills.dialog
    assert dialog is window.get_visible_dialog(), "a click opens the skill's page"
    wait(lambda: dialog.details is not None or dialog.problem, "the skill's details")
    return dialog


def close_skill(window):
    if window.skills.dialog:
        window.skills.dialog.force_close()
        settle(30)


def ready(window):
    return (window.state == "ready" and window.voices.groups and window.appearance.rows
            and window.skills.rows and window.history.rows)


def open_window(app, width=860, height=760):
    window = sa.SettingsWindow(app)
    window.set_default_size(width, height)
    window.present()
    return window


@unittest.skipUnless(INNER, "runs inside the session HeadlessApp starts")
class Inner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        target = sa.extension_schema_dirs()[0]
        shutil.copytree(EXTENSION_SCHEMAS, target)
        # git ignores the compiled schema, so a fresh clone has only the XML.
        subprocess.run(["glib-compile-schemas", str(target)], check=True)
        cls.fake = start_fake()
        cls.app = sa.SettingsApp()
        cls.app.register(None)
        cls.activated = []
        cls.app.connect("activate", lambda app: cls.activated.append(app))
        cls.app.activate()
        cls.win = cls.app.get_active_window()
        wait(lambda: ready(cls.win), "the first load")

    def setUp(self):
        close_skill(self.win)
        self.win.views.set_visible_child_name("general")
        forget()

    def skill_page(self, skill_id, window=None):
        """The settings on a skill's page, opening it unless it is open already."""
        return open_skill(window or self.win, skill_id).settings

    def test_01_a_second_launch_presents_the_same_window(self):
        before = len(self.activated)
        second = subprocess.Popen([sys.executable, "-m", "tuxistant.settings_app"], cwd=ROOT)
        wait(lambda: second.poll() is not None, "the second launch to exit", 15)
        self.assertEqual(second.returncode, 0)
        wait(lambda: len(self.activated) > before, "activation of the first instance")
        self.assertEqual(len(self.app.get_windows()), 1)
        self.assertIs(self.app.get_active_window(), self.win)

    def test_02_general_shows_what_the_daemon_holds(self):
        page = self.win.general
        self.assertEqual(page.server.get_text(), "localhost:8080")
        self.assertEqual(page.key_status.get_label(), "No key set")
        self.assertFalse(page.key_remove.get_visible())
        self.assertTrue(page.pipelines["tux-realtime"].get_active())
        self.assertFalse(page.switches["conversation_mode"].get_active())
        self.assertTrue(page.switches["fn_hold_enabled"].get_active())
        self.assertEqual(page.spins["listen_timeout_s"][0].get_value(), 8)
        self.assertEqual(page.spins["fn_hold_ms"][0].get_value(), 1.5)
        self.assertIn("You are Tux", page.instructions_text())
        self.assertFalse(page.save_instructions.get_sensitive())
        settle()
        self.assertEqual(saved(), [], "loading must not write anything back")

    def test_03_server_address(self):
        row = self.win.general.server
        apply = apply_button(row)
        settle(30)
        self.assertFalse(apply.get_mapped(), "text from the daemon is not an edit")
        with mock.patch.object(self.win, "toast") as toast:
            row.grab_focus()  # Adw.EntryRow only counts edits made while focused
            row.set_text("not a server")
            wait(apply.get_mapped, "the apply button")
            apply.emit("clicked")
            settle()
            self.assertTrue(row.has_css_class("error"))
            toast.assert_called_once()
            self.assertEqual(saved(), [])
        row.set_text("http://tuxbox:9090/")
        self.assertFalse(row.has_css_class("error"))
        apply.emit("clicked")
        wait(lambda: saved() == [["server", '"tuxbox:9090"']], "the server to save")
        self.assertEqual(row.get_text(), "tuxbox:9090")
        settle(30)
        self.assertFalse(apply.get_mapped(), "the cleaned-up text is not an edit either")
        wait(lambda: self.win.config["server"] == "tuxbox:9090", "the change to come back")
        row.grab_focus()
        row.set_text("HTTPS://tuxbox:9443/")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: saved()[-1] == ["server", '"https://tuxbox:9443"'], "https to survive")
        self.assertEqual(row.get_text(), "https://tuxbox:9443")
        wait(lambda: self.win.config["server"] == "https://tuxbox:9443", "the change to come back")

    def test_03_z_a_new_server_asks_for_its_key(self):
        page = self.win.general
        with mock.patch.dict(self.win.config, {"api_key_set": True}), \
                mock.patch.object(self.win, "toast") as toast:
            page.set_row_text(page.server, "othertux:9090")
            page.on_server_apply(page.server)
            toast.assert_called_once()
            self.assertIn("API key", toast.call_args.args[0])
            toast.reset_mock()
            page.on_server_apply(page.server)
            toast.assert_not_called()
        wait(lambda: self.win.config["server"] == "othertux:9090", "the change to come back")
        page.set_row_text(page.server, "tuxbox:9090")
        page.on_server_apply(page.server)
        wait(lambda: self.win.config["server"] == "tuxbox:9090", "the change to come back")

    def test_04_api_key_is_write_only(self):
        page = self.win.general
        apply = apply_button(page.api_key)
        page.api_key.set_text("  sekret  ")
        apply.emit("clicked")
        wait(lambda: saved() == [["api_key", '"sekret"']], "the key to save")
        self.assertEqual(page.api_key.get_text(), "")
        settle(30)
        self.assertFalse(apply.get_mapped())
        self.assertEqual(page.key_status.get_label(), "Key is set")
        wait(lambda: self.win.config.get("api_key_set") is True, "api_key_set")
        self.assertTrue(page.key_remove.get_visible())
        self.assertNotIn("api_key", self.win.config)
        self.assertNotIn("sekret", json.dumps(self.win.config))
        self.assertEqual(json.loads(control("Stored", "api_key", signature="s")[0]), "sekret")
        forget()
        page.api_key.set_text("   ")
        apply.emit("clicked")
        settle()
        self.assertEqual(saved(), [], "a blank entry must not wipe the key")
        page.key_remove.emit("clicked")
        wait(lambda: saved() == [["api_key", '""']], "the key to be removed")
        wait(lambda: self.win.config.get("api_key_set") is False, "api_key_set to clear")
        self.assertEqual(page.key_status.get_label(), "No key set")
        self.assertFalse(page.key_remove.get_visible())

    def test_05_pipeline(self):
        checks = self.win.general.pipelines
        checks["tux-realtime-eou"].set_active(True)
        wait(lambda: saved() == [["pipeline", '"tux-realtime-eou"']], "the pipeline to save")
        wait(lambda: self.win.config["pipeline"] == "tux-realtime-eou", "the change to come back")
        self.assertFalse(checks["tux-realtime"].get_active())
        checks["tux-realtime"].set_active(True)
        wait(lambda: self.win.config["pipeline"] == "tux-realtime", "the pipeline to go back")

    def test_06_switches_and_spins_save_the_right_types(self):
        page = self.win.general
        page.switches["conversation_mode"].set_active(True)
        wait(lambda: saved() == [["conversation_mode", "true"]], "the switch to save")
        forget()
        timeout = page.spins["listen_timeout_s"][0]
        for value in (9, 10, 11):
            timeout.set_value(value)
        hold = page.spins["fn_hold_ms"][0]
        hold.set_value(2.5)
        wait(lambda: len(saved()) == 2, "the spin rows to save")
        self.assertCountEqual(saved(), [["listen_timeout_s", "11"], ["fn_hold_ms", "2500"]])
        wait(lambda: self.win.config["fn_hold_ms"] == 2500, "the values to come back")
        self.assertEqual(timeout.get_value(), 11)
        self.assertEqual(hold.get_value(), 2.5)

    def test_06_z_fact_lookup_switch(self):
        row = self.skill_page("org.tuxistant.knowledge").switches["fact_lookup"]
        self.assertTrue(row.get_active())
        row.set_active(False)
        wait(lambda: saved() == [["fact_lookup", "false"]], "the switch to save")
        wait(lambda: self.win.config["fact_lookup"] is False, "the change to come back")
        self.assertFalse(row.get_active())
        row.set_active(True)
        wait(lambda: self.win.config["fact_lookup"] is True, "the switch to come back")

    def test_06_zz_mail_search_is_off_until_switched_on(self):
        row = self.skill_page("org.tuxistant.mail").switches["mail_search"]
        self.assertFalse(row.get_active())
        row.set_active(True)
        wait(lambda: saved() == [["mail_search", "true"]], "the switch to save")
        wait(lambda: self.win.config["mail_search"] is True, "the change to come back")
        row.set_active(False)
        wait(lambda: self.win.config["mail_search"] is False, "the switch to come back")

    def test_06_zzz_the_timer_chime_is_off_until_switched_on(self):
        row = self.skill_page("org.tuxistant.timers").switches["timer_chime"]
        self.assertFalse(row.get_active(), "nothing sounds on a locked screen unless asked for")
        row.set_active(True)
        wait(lambda: saved() == [["timer_chime", "true"]], "the switch to save")
        wait(lambda: self.win.config["timer_chime"] is True, "the change to come back")
        row.set_active(False)
        wait(lambda: self.win.config["timer_chime"] is False, "the switch to come back")

    def test_06_zzzz_the_tailscale_address_is_off_until_switched_on(self):
        row = self.skill_page("org.tuxistant.system").switches["say_tailscale_address"]
        self.assertFalse(row.get_active(), "an address on your tailnet is said only when asked for")
        self.assertEqual(row.get_title(), "Say my Tailscale address")
        row.set_active(True)
        wait(lambda: saved() == [["say_tailscale_address", "true"]], "the switch to save")
        wait(lambda: self.win.config["say_tailscale_address"] is True, "the change to come back")
        row.set_active(False)
        wait(lambda: self.win.config["say_tailscale_address"] is False, "the switch to come back")

    def test_07_hold_rows_follow_the_switch(self):
        page = self.win.general
        hold, duration = page.switches["fn_hold_enabled"], page.spins["fn_hold_ms"][0]
        hide = page.switches["hide_on_release"]
        self.assertTrue(duration.get_sensitive())
        hold.set_active(False)
        self.assertFalse(duration.get_sensitive())
        self.assertFalse(hide.get_sensitive())
        wait(lambda: saved() == [["fn_hold_enabled", "false"]], "the switch to save")
        wait(lambda: self.win.config["fn_hold_enabled"] is False, "the change to come back")
        hold.set_active(True)
        wait(lambda: self.win.config["fn_hold_enabled"] is True, "the switch to come back")

    def test_07_z_home_town(self):
        page = self.skill_page("org.tuxistant.weather")
        row = page.location
        apply = apply_button(row)
        settle(30)
        self.assertFalse(apply.get_mapped(), "text from the daemon is not an edit")
        row.grab_focus()  # Adw.EntryRow only counts edits made while focused
        row.set_text("  Lisbon,   Portugal ")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: ["location", '"Lisbon, Portugal"'] in saved(), "the town to be saved")
        self.assertEqual(row.get_text(), "Lisbon, Portugal")
        control("Change", "location", '"Oslo"', signature="ss")
        wait(lambda: self.win.config["location"] == "Oslo", "the config to reload")
        self.assertEqual(row.get_text(), "Oslo")
        settle(30)
        self.assertFalse(apply_button(row).get_mapped())

    def test_08_changes_from_elsewhere_show_up_and_are_not_echoed(self):
        page = self.win.general
        control("Change", "listen_timeout_s", "20", signature="ss")
        control("Change", "voice", '"bm_george"', signature="ss")
        control("Change", "theme", '"waves"', signature="ss")
        control("Change", "server", '"elsewhere:1234"', signature="ss")
        control("Change", "instructions", '"Be brief."', signature="ss")
        wait(lambda: self.win.config["instructions"] == "Be brief.", "the config to reload")
        self.assertEqual(page.spins["listen_timeout_s"][0].get_value(), 20)
        self.assertEqual(page.server.get_text(), "elsewhere:1234")
        settle(30)
        self.assertFalse(apply_button(page.server).get_mapped())
        self.assertEqual(page.instructions_text(), "Be brief.")
        self.assertEqual(chosen_voices(self.win), ["bm_george"])
        self.assertEqual([r.value for r in self.win.appearance.rows if is_chosen(r)], ["waves"])
        settle()
        self.assertEqual(saved(), [])

    def test_09_unsaved_edits_survive_a_reload(self):
        page = self.win.general
        page.server.set_text("half-typed")
        page.instructions.get_buffer().set_text("Half a thought")
        control("Change", "conversation_mode", "false", signature="ss")
        control("Change", "server", '"moved:1"', signature="ss")
        wait(lambda: self.win.config["server"] == "moved:1", "the config to reload")
        self.assertEqual(page.server.get_text(), "half-typed")
        self.assertEqual(page.instructions_text(), "Half a thought")
        self.assertTrue(page.save_instructions.get_sensitive())
        page.server.set_text("moved:1")
        page.instructions.get_buffer().set_text("Be brief.")
        self.assertFalse(page.save_instructions.get_sensitive())

    def test_10_instructions(self):
        page = self.win.general
        page.instructions.get_buffer().set_text("Speak like a <pirate> & be kind")
        self.assertTrue(page.save_instructions.get_sensitive())
        page.save_instructions.emit("clicked")
        self.assertFalse(page.save_instructions.get_sensitive())
        wait(lambda: self.win.config["instructions"].startswith("Speak"), "the text to save")
        self.assertEqual(saved(), [["instructions", '"Speak like a <pirate> & be kind"']])

    def test_11_a_refused_save_is_reported_and_undone(self):
        voices_page = self.win.voices
        control("Change", "voice", '"af_nicole"', signature="ss")
        wait(lambda: self.win.config["voice"] == "af_nicole", "the voice to reset")
        control("FailNext", "SetConfig", "disk <full> & sad", signature="ss")
        with mock.patch.object(self.win, "toast", wraps=self.win.toast) as toast:
            voices_page.choose("am_adam")
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("disk <full> & sad")
        wait(lambda: self.win.config["voice"] == "af_nicole", "the voice to roll back")
        self.assertEqual(chosen_voices(self.win), ["af_nicole"])
        settle()
        self.assertEqual(self.win.state, "ready", "a refusal must not look like an outage")

    def test_12_shortcut(self):
        page = self.win.general
        self.assertTrue(page.shortcut.get_sensitive())
        self.assertEqual(page.shortcut_label.get_accelerator(), "<Control><Super>space")
        page.set_shortcut("<Alt>F9")
        self.assertEqual(page.shell.get_strv(sa.SHORTCUT_KEY), ["<Alt>F9"])
        wait(lambda: page.shortcut_label.get_accelerator() == "<Alt>F9", "the label")
        page.shell.set_strv(sa.SHORTCUT_KEY, ["<Super>F2"])
        wait(lambda: page.shortcut_label.get_accelerator() == "<Super>F2", "the outside change")
        page.set_shortcut("")
        self.assertEqual(page.shell.get_strv(sa.SHORTCUT_KEY), [])
        wait(lambda: page.shortcut_label.get_accelerator() == "", "the shortcut to go off")
        page.shell.reset(sa.SHORTCUT_KEY)
        settle(30)

    def test_13_shortcut_dialog_keys(self):
        mask = Gdk.ModifierType
        picked = []
        dialog = sa.ShortcutDialog(picked.append)
        dialog.present(self.win)
        wait(lambda: self.win.get_visible_dialog() is dialog, "the dialog")
        shot(self.win, "shortcut-dialog")
        dialog.on_key(None, Gdk.KEY_Control_L, 0, mask.CONTROL_MASK)
        dialog.on_key(None, Gdk.KEY_a, 0, mask(0))
        self.assertEqual(picked, [])
        self.assertIn("Add Ctrl", dialog.page.get_description())
        dialog.on_key(None, Gdk.KEY_k, 0, mask.CONTROL_MASK | mask.SUPER_MASK)
        self.assertEqual(picked, ["<Control><Super>k"])
        wait(lambda: self.win.get_visible_dialog() is None, "the dialog to close")

        picked.clear()
        for key, expected in ((Gdk.KEY_Escape, []), (Gdk.KEY_BackSpace, [""])):
            dialog = sa.ShortcutDialog(picked.append)
            dialog.present(self.win)
            wait(lambda: self.win.get_visible_dialog() is dialog, "the dialog")
            dialog.on_key(None, key, 0, mask(0))
            wait(lambda: self.win.get_visible_dialog() is None, "the dialog to close")
            self.assertEqual(picked, expected)

    def test_13_real_keys(self):
        """Through the compositor and the real event path, not by calling the handler."""
        page = self.win.general
        keyboard = Keyboard()
        self.addCleanup(keyboard.close)

        def open_dialog():
            page.shortcut.activate()
            wait(lambda: isinstance(self.win.get_visible_dialog(), sa.ShortcutDialog), "the dialog")
            settle(200)

        def closed():
            return self.win.get_visible_dialog() is None

        open_dialog()
        keyboard.press(Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_k)
        wait(closed, "the dialog to take the shortcut")
        self.assertEqual(page.shell.get_strv(sa.SHORTCUT_KEY), ["<Control><Alt>k"])
        open_dialog()
        keyboard.press(Gdk.KEY_a)
        settle()
        self.assertIn("Add Ctrl", self.win.get_visible_dialog().page.get_description())
        keyboard.press(Gdk.KEY_Escape)
        wait(closed, "Esc to close the dialog")
        self.assertEqual(page.shell.get_strv(sa.SHORTCUT_KEY), ["<Control><Alt>k"])
        open_dialog()
        keyboard.press(Gdk.KEY_BackSpace)
        wait(closed, "Backspace to close the dialog")
        self.assertEqual(page.shell.get_strv(sa.SHORTCUT_KEY), [])
        page.shell.reset(sa.SHORTCUT_KEY)

        page.server.grab_focus()
        settle()
        keyboard.press(Gdk.KEY_Control_L, Gdk.KEY_a)
        keyboard.press(Gdk.KEY_BackSpace)
        for character in "box:1":
            keyboard.press(ord(character))
        wait(lambda: page.server.get_text() == "box:1", "the typing")
        keyboard.press(Gdk.KEY_Return)
        wait(lambda: saved() == [["server", '"box:1"']], "Enter to save the server")
        wait(lambda: self.win.config["server"] == "box:1", "the change to come back")

    def test_14_shortcut_row_without_the_extension(self):
        with mock.patch.object(sa, "extension_schema_dirs", return_value=[]):
            window = open_window(self.app, 860, 1000)
        page = window.general
        self.assertIsNone(page.shell)
        self.assertFalse(page.shortcut.get_sensitive())
        self.assertIn("Install the Tuxistant GNOME Shell extension", page.shortcut.get_subtitle())
        wait(lambda: window.state == "ready", "the window")
        shot(window, "general-no-extension")
        dispose(window)

    def test_15_voices(self):
        page = self.win.voices
        self.win.views.set_visible_child_name("voice")
        self.assertEqual([g.get_title() for g, _rows in page.groups], ["American", "British"])
        self.assertEqual(sum(len(rows) for _g, rows in page.groups), len(voices.catalog()))
        row = page.groups[1][1][0][0]
        self.assertEqual((row.get_title(), row.get_subtitle()), ("Alice", "Female"))
        row.activate()
        wait(lambda: saved() == [["voice", '"bf_alice"']], "the voice to save")
        self.assertTrue(is_chosen(row))
        wait(lambda: self.win.config["voice"] == "bf_alice", "the change to come back")

    def test_16_voice_preview(self):
        page = self.win.voices
        row = page.groups[0][1][0][0]
        button = next(c for c in iter_children(row) if isinstance(c, Gtk.Button))
        control("DelayNext", "PreviewVoice", 300, signature="si")
        button.emit("clicked")
        self.assertFalse(button.get_sensitive(), "busy while the sample plays")
        wait(lambda: calls("PreviewVoice") == [["PreviewVoice", "af_alloy"]], "the preview")
        wait(button.get_sensitive, "the button to come back")
        self.assertEqual(button.get_icon_name(), "media-playback-start-symbolic")
        self.assertEqual(saved(), [], "previewing must not change the voice")
        control("FailNext", "PreviewVoice", "no speech server", signature="ss")
        with mock.patch.object(self.win, "toast") as toast:
            button.emit("clicked")
            wait(button.get_sensitive, "the button to come back")
        toast.assert_called_once_with("no speech server")

    def test_17_voice_search(self):
        page = self.win.voices
        self.win.views.set_visible_child_name("voice")

        def shown():
            return [r.get_title() for _g, rows in page.groups for r, _v in rows if r.get_visible()]

        page.search.set_text("british female")
        wait(lambda: shown() == ["Alice", "Emma", "Isabella", "Lily"], "the filter")
        self.assertFalse(page.groups[0][0].get_visible())
        self.assertEqual(page.stack.get_visible_child_name(), "list")
        page.search.set_text("nobody at all")
        wait(lambda: shown() == [], "the filter")
        self.assertEqual(page.stack.get_visible_child_name(), "empty")
        shot(self.win, "voice-no-match")
        page.search.set_text("")
        wait(lambda: len(shown()) == len(voices.catalog()), "the filter to clear")
        self.assertEqual(page.stack.get_visible_child_name(), "list")

    def test_18_appearance(self):
        page = self.win.appearance
        self.win.views.set_visible_child_name("appearance")
        self.assertEqual([r.get_title() for r in page.rows], ["Orb", "Rock & <Roll", "Waves"])
        self.assertEqual(page.rows[1].get_subtitle(), "By A <b>bold</b> & loud author")
        self.assertTrue(page.problems.get_visible())
        self.assertEqual([r.get_title() for r in page.problem_rows], ["broken", "bad&<name"])
        self.assertIn("<wobble>", page.problem_rows[1].get_subtitle())
        button = next(c for c in iter_children(page.rows[0]) if isinstance(c, Gtk.Button))
        button.emit("clicked")
        wait(lambda: calls("PreviewTheme") == [["PreviewTheme", "orb"]], "the preview")
        self.assertEqual(saved(), [], "trying must not change the theme")
        page.rows[1].activate()
        wait(lambda: saved() == [["theme", '"rock"']], "the theme to save")
        self.assertTrue(is_chosen(page.rows[1]))
        self.assertEqual([selected_state(row) for row in page.rows], ["false", "true", "false"])
        self.assertFalse(is_chosen(page.rows[0]))

    def test_19_open_themes_folder(self):
        page = self.win.appearance
        with mock.patch.object(sa.Gtk.FileLauncher, "new") as launcher:
            page.on_open_folder(None)
        self.assertTrue(sa.themes.USER.is_dir())
        launched = launcher.call_args.args[0].get_path()
        self.assertEqual(launched, str(sa.themes.USER))
        launcher.return_value.launch.assert_called_once()

        def failing(error):
            stub = mock.Mock()
            stub.launch_finish.side_effect = error
            return stub

        dismissed = GLib.Error.new_literal(Gtk.DialogError.quark(), "x", Gtk.DialogError.DISMISSED)
        broken = GLib.Error.new_literal(
            Gtk.DialogError.quark(), "no handler", Gtk.DialogError.FAILED)
        with mock.patch.object(self.win, "toast") as toast:
            page.on_folder_opened(failing(dismissed), None)
            toast.assert_not_called()
            page.on_folder_opened(failing(broken), None)
            toast.assert_called_once()

    def test_20_coming_back_to_the_window_rereads_themes(self):
        rows = list(self.win.appearance.rows)
        self.skill_page("org.tuxistant.phone")      # the phones are read while its page is open
        forget()
        with mock.patch.object(self.win, "is_active", return_value=True):
            self.win.on_active()
        wait(lambda: calls("ListThemes"), "the themes to reload")
        wait(lambda: calls("ListPhones"), "the phones to reload")
        settle()
        self.assertEqual(self.win.appearance.rows, rows, "an unchanged list keeps its rows")

    def test_20_b_a_changed_theme_list_keeps_focus_on_your_theme(self):
        page = self.win.appearance
        self.win.views.set_visible_child_name("appearance")
        listing = page.listed
        self.addCleanup(page.fill, listing)
        page.rows[2].grab_focus()
        settle()
        first = {"id": "aardvark", "name": "Aardvark", "author": "Tux"}
        page.fill({**listing, "themes": [first, *listing["themes"]]})
        settle()
        self.assertEqual(holder(self.win, page.rows).value, listing["themes"][2]["id"])
        page.fill({**listing, "themes": listing["themes"][:1]})
        settle()
        self.assertIs(holder(self.win, page.rows), page.rows[0], "or the nearest one, if it went")

    def test_21_skills(self):
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        rows = {row.get_title(): row for row in page.rows}
        self.assertLessEqual({"Disk space", "Run commands", "Translate", "Web lookup"}, set(rows))

        def badge(row):
            return next(c for c in iter_children(row) if isinstance(c, Gtk.Label)
                        and c.has_css_class("risk-badge"))

        self.assertEqual(badge(rows["Disk space"]).get_label(), "Low risk")
        self.assertTrue(badge(rows["Disk space"]).has_css_class("success"))
        self.assertEqual(badge(rows["Web lookup"]).get_label(), "Medium risk")
        self.assertTrue(badge(rows["Web lookup"]).has_css_class("warning"))
        self.assertEqual(badge(rows["Run commands"]).get_label(), "High risk")
        self.assertTrue(badge(rows["Run commands"]).has_css_class("error"))
        self.assertIn("Needs no special permissions", rows["Disk space"].get_subtitle())
        self.assertIn("Can read files and use the network (api.example.org)",
                      rows["Web lookup"].get_subtitle())
        self.assertIn("use camera", rows["Run commands"].get_subtitle())
        self.assertIn("(your own server)", rows["Translate"].get_subtitle())
        self.assertNotIn("(home)", rows["Translate"].get_subtitle())
        self.assertTrue(rows["Run commands"].get_activatable(), "each opens to be read")
        self.assertTrue(rows["Tides"].get_subtitle().startswith("A recipe you added. "))
        self.assertFalse(rows["Disk space"].get_subtitle().startswith("A "))
        wait(lambda: page.problem_rows, "the skills with problems")
        self.assertTrue(page.problems.get_visible())
        self.assertEqual([r.get_title() for r in page.problem_rows], ["broken.yaml"])
        self.assertIn("may only name the hosts", page.problem_rows[0].get_subtitle())
        self.assertTrue(page.problem_rows[0].get_tooltip_text().endswith("/skills/broken.yaml"))
        before = {row.get_tooltip_text(): row.get_subtitle() for row in page.problem_rows}
        self.addCleanup(control, "SetSkillProblems", json.dumps(before), signature="s")
        control("SetSkillProblems", json.dumps({"/x/skills/coins.yml": "Has to end in .yaml."}),
                signature="s")
        wait(lambda: [r.get_title() for r in page.problem_rows] == ["coins.yml"],
             "the problems a reload found, with the window open")

    def test_21_a_a_built_in_skill_shows_what_the_model_is_given(self):
        from tuxistant.skills import Skills
        dialog = open_skill(self.win, "org.tuxistant.weather")
        self.assertEqual(dialog.get_title(), "Weather")
        about = texts_of(dialog.about)
        self.assertIn("Built into Tuxistant", about)
        code = next(text for text in about if text.startswith("Built in: Python code"))
        self.assertIn("tuxistant/livedata.py", code)
        self.assertIn("Use the network", texts_of(dialog.permissions))
        self.assertIn("geocoding-api.open-meteo.com, api.open-meteo.com",
                      texts_of(dialog.permissions))
        self.assertIn("Because it can use the network", texts_of(dialog.permissions))
        tool = Skills().tool("weather.forecast")
        [row] = [r for r in iter_children(dialog.actions) if isinstance(r, sa.SkillActionRow)]
        self.assertEqual(row.get_title(), "weather_forecast")
        self.assertIn("The model is offered this", row.get_subtitle())
        row.set_expanded(True)
        settle()
        shown = texts_of(row)
        self.assertIn(tool["description"], shown, "the description, word for word")
        self.assertIn(json.dumps(tool, indent=2, ensure_ascii=False), shown,
                      "and the whole tool as it is sent")
        self.assertIn("place", shown)
        self.assertIn(tool["parameters"]["properties"]["place"]["description"]
                      + "\ntype: string", shown)
        self.assertIn("tuxistant/livedata.py", shown)
        row.grab_focus()
        shot(self.win, "skill-action")
        self.assertEqual(dialog.settings.location.get_title(), "Home town")
        manifest = (Path(sa.__file__).parent / "skills_builtin/weather.yaml").read_text()
        self.assertEqual(dialog.details["manifest"], manifest)
        self.assertFalse([r for r in iter_children(dialog.manifest)
                          if isinstance(r, Adw.ButtonRow)], "nothing to open: it is built in")
        dialog.manifest_row.activate()
        settle()
        text = next(w for w in iter_children(dialog) if isinstance(w, Gtk.TextView))
        buffer = text.get_buffer()
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), False), manifest)
        self.assertFalse(text.get_editable())
        self.assertTrue(text.get_monospace())
        self.assertEqual(colours(buffer.get_start_iter()), ["key"],
                         "the manifest is coloured: id is a key")
        self.assertFalse(buffer.get_tag_table().lookup("whole").props.insert_hyphens,
                         "a wrap never shows a hyphen the file doesn't have")
        self.assertEqual(text.get_wrap_mode(), Gtk.WrapMode.WORD_CHAR)
        key = buffer.get_tag_table().lookup("key")
        style = Adw.StyleManager.get_default()
        self.assertTrue(key.props.foreground_set)
        colour = Gdk.RGBA()
        colour.parse(sa.SYNTAX["key"][style.get_dark()])
        self.assertTrue(key.props.foreground_rgba.equal(colour), "in the colour for this theme")
        shot(self.win, "skill-manifest")
        scheme = style.get_color_scheme()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        settle()
        colour.parse(sa.SYNTAX["key"][True])
        self.assertTrue(key.props.foreground_rgba.equal(colour), "dark follows at once")
        shot(self.win, "skill-manifest-dark")
        style.set_color_scheme(scheme)
        settle()
        page = text.get_ancestor(sa.ManifestPage)
        page.colour(Contrast())
        self.assertFalse(key.props.foreground_set, "high contrast keeps the theme's colour")
        self.assertEqual(key.props.weight, Pango.Weight.BOLD, "and keys go bold instead")
        page.colour(style)
        self.assertTrue(key.props.foreground_set)
        self.assertEqual(key.props.weight, Pango.Weight.NORMAL)
        del page
        dialog.copy_manifest()
        self.assertEqual(clipboard_text(dialog), manifest)

    def test_21_a_b_a_manifest_page_is_freed_once_popped(self):
        style = Adw.StyleManager.get_default()
        dialog = open_skill(self.win, "org.tuxistant.weather")
        for _ in range(2):
            dialog.manifest_row.activate()
            settle()
            page = next(w for w in iter_children(dialog) if isinstance(w, Gtk.TextView)
                        ).get_ancestor(sa.ManifestPage)
            handlers = list(page.watching)
            self.assertEqual(len(handlers), 2, "it follows dark and high contrast while shown")
            freed = []
            page.weak_ref(lambda: freed.append(True))
            del page
            dialog.pop_subpage()
            wait(lambda: gc.collect() is not None and freed, "the page to be freed")
            self.assertFalse([handler for handler in handlers
                              if GObject.signal_handler_is_connected(style, handler)])

    def test_21_a_c_a_manifest_with_a_long_line_opens_at_once(self):
        """A code skill's manifest may be 64 KB on one line of a list. Each tag on a line made
        GTK slower at the next, and this took 96 s; wrapping it at words took 1.7 s more."""
        head = ("id: org.example.big\nname: Big\ndescription: Says hi\nrun: [python3, main.py]\n"
                "actions:\n  big.hi:\n    description: Say hi\npermissions:\n  network: [")
        long_line = head + ",".join(["a"] * ((64 * 1024 - len(head)) // 2)) + "]\n"
        many_lines = "".join(f"k{n}: [x, 'y', 3, null]  # {n}\n" for n in range(2400))
        dialog = open_skill(self.win, "org.tuxistant.weather")
        for name, manifest in (("long line", long_line), ("many lines", many_lines)):
            with self.subTest(name):
                began = time.monotonic()
                page = sa.ManifestPage(dialog, {"manifest": manifest, "file": "skill.yaml"})
                dialog.push_subpage(page)
                settle(30)
                took = time.monotonic() - began
                self.assertLess(took, 1.0, f"{len(manifest)} characters took {took:.2f} s")
                buffer = page.text.get_buffer()
                self.assertEqual(buffer.get_text(*buffer.get_bounds(), False), manifest)
                self.assertEqual(colours(buffer.get_start_iter()), ["key"])
                self.assertEqual(page.text.get_wrap_mode(), Gtk.WrapMode.WORD_CHAR,
                                 "the rest of the manifest still wraps between words")
                long_tag = buffer.get_tag_table().lookup("long")
                self.assertFalse(buffer.get_iter_at_line(0)[1].has_tag(long_tag))
                if name == "long line":
                    _found, at = buffer.get_iter_at_line(8)
                    self.assertTrue(at.has_tag(long_tag), "only the long line wraps anywhere")
                    toggles = 0
                    while at.forward_to_tag_toggle(None) and at.get_line() == 8:
                        toggles += 1
                    self.assertLessEqual(toggles, 2 * sa.highlight.MAX_PER_LINE + 2)
                else:
                    _found, last = buffer.get_iter_at_line(2399)
                    last.forward_chars(len("k2399: [x, 'y', 3, null]  "))
                    self.assertEqual(colours(last), ["comment"])
                dialog.pop_subpage()
                settle(30)
                del page

    def test_21_b_a_recipe_you_added_shows_its_file_whole(self):
        from tests.shell_harness_settings_fake import RECIPE
        dialog = open_skill(self.win, "org.example.tides")
        self.assertEqual(dialog.details["manifest"], RECIPE)
        about = texts_of(dialog.about)
        where = next(text for text in about if text.startswith("A recipe you added"))
        self.assertTrue(where.endswith("/skills/tides.yaml"), where)
        self.assertIn("tuxistant/recipes.py", "\n".join(about))
        self.assertEqual(dialog.settings, None, "a skill you add has no settings here")
        [row] = [r for r in iter_children(dialog.actions) if isinstance(r, sa.SkillActionRow)]
        row.set_expanded(True)
        settle()
        self.assertIn("town (required)", texts_of(row))
        self.assertIn("The town, like Brighton\ntype: string\nmaxLength: 40", texts_of(row))
        self.assertTrue(dialog.manifest_row.get_subtitle().startswith("60.1 KB\n"))
        dialog.manifest_row.activate()
        settle()
        text = next(w for w in iter_children(dialog) if isinstance(w, Gtk.TextView))
        buffer = text.get_buffer()
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), False), RECIPE,
                         "all of it, markup in its comments shown as written")
        [open_file] = [r for r in iter_children(dialog.manifest) if isinstance(r, Adw.ButtonRow)]
        self.assertEqual(open_file.get_title(), "Open file")
        with mock.patch.object(sa.Gtk.FileLauncher, "new") as launcher:
            open_file.emit("activated")
        self.assertEqual(launcher.call_args.args[0].get_path(), dialog.details["file"])
        launcher.return_value.launch.assert_called_once()

    def test_21_b_b_windows_line_endings_show_as_line_breaks(self):
        """The loader reads CR LF as a line break, so it isn't a hidden character to show. A lone
        CR still shows: the loader reads it as a line break, but grep and wc don't."""
        text = "id: org.example.crlf\r\nname: Crlf  # a note\r\nversion: '1'\r\nlast: x\r"
        page = sa.ManifestPage(mock.Mock(), {"manifest": text, "file": "/x/crlf.yaml"})
        buffer = page.text.get_buffer()
        shown_text = buffer.get_text(*buffer.get_bounds(), False)
        self.assertEqual(shown_text,
                         "id: org.example.crlf\nname: Crlf  # a note\nversion: '1'\nlast: x\\u000d")

        def at(word):
            return colours(buffer.get_iter_at_offset(shown_text.index(word)))

        self.assertEqual(at("# a note"), [sa.highlight.COMMENT])
        self.assertEqual(at("last"), [sa.highlight.KEY], "the colours stay in step with the text")
        self.assertEqual(at("'1'"), [sa.highlight.STRING])

    def test_21_c_a_code_skill_you_added_lists_its_files(self):
        dialog = open_skill(self.win, "org.example.moon")
        self.assertIn("A code skill you added", "\n".join(texts_of(dialog.about)))
        self.assertTrue(dialog.files.get_visible())
        self.assertLessEqual({"lib/phase.py", "main.py", "skill.yaml"},
                             set(texts_of(dialog.files)))
        [open_folder] = [r for r in iter_children(dialog.files) if isinstance(r, Adw.ButtonRow)]
        self.assertEqual(open_folder.get_title(), "Open folder")
        with mock.patch.object(sa.Gtk.FileLauncher, "new") as launcher:
            open_folder.emit("activated")
        self.assertEqual(launcher.call_args.args[0].get_path(), dialog.details["folder"])
        self.assertTrue(dialog.details["folder"].endswith("/skills/moon"))
        lights = open_skill(self.win, "org.tuxistant.lights")
        self.assertIn("Comes with Tuxistant: a code skill", texts_of(lights.about))
        self.assertTrue(lights.files.get_visible())
        self.assertFalse([r for r in iter_children(lights.files) if isinstance(r, Adw.ButtonRow)],
                         "what comes with Tuxistant isn't opened to be changed")

    def test_21_d_what_a_skill_says_is_never_read_as_markup(self):
        from tests.shell_harness_settings_fake import DETAILS, MARKUP_NAME, MARKUP_TEXT
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        row = next(r for r in page.rows if r.skill["id"] == "org.example.markup")
        self.assertEqual(row.get_title(), MARKUP_NAME)
        started = time.monotonic()
        dialog = open_skill(self.win, "org.example.markup")
        painted = []    # the first frame after the rows went in, so their layout is counted
        clock = self.win.get_frame_clock()
        handler = clock.connect("after-paint", lambda _clock: painted.append(time.monotonic()))
        self.win.queue_draw()
        wait(lambda: painted, "the page to be drawn", 30)
        clock.disconnect(handler)
        opening = painted[0] - started
        self.assertLess(opening, 2, "2,000 files with long names must not hold the page")
        for action in iter_children(dialog.actions):
            if isinstance(action, sa.SkillActionRow):
                action.set_expanded(True)
        settle()
        dialog.manifest_row.activate()
        settle()
        texts = [label.get_text() for label in labels_of(dialog)]
        for wanted in (MARKUP_NAME, MARKUP_TEXT, "<i>1</i>", "<b>main</b>.py", "<i>q</i> (required)",
                       '<span foreground="red">q</span>\ntype: string', "Use <b>odd</b>"):
            self.assertIn(wanted, texts, "shown as written")
        self.assertIn("README\\nskill.yaml", texts, "one file, not two")
        self.assertFalse([text for text in texts if "\nComes with Tuxistant" in text],
                         "a folder's name can't fake a line saying where a skill comes from")
        self.assertIn(f"And {2000 - sa.MAX_FILES_SHOWN} more files", texts)
        marked = [label.get_text() for label in labels_of(dialog) if label.get_use_markup()
                  and ("<" in label.get_label() or "&" in label.get_label())]
        self.assertEqual(marked, [], "no label holding a skill's text reads it as markup")
        text = next(w for w in iter_children(dialog) if isinstance(w, Gtk.TextView))
        buffer = text.get_buffer()
        manifest = DETAILS["org.example.markup"]["manifest"]
        self.assertGreater(len(manifest), 64000)
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), False), manifest)
        shot(self.win, "skill-markup")

    def test_21_e_a_daemon_older_than_this_app_shows_what_it_can(self):
        control("FailNextAs", "SkillDetails", "org.freedesktop.DBus.Error.UnknownMethod",
                "No such method", signature="sss")
        with mock.patch.object(self.win, "toast") as toast:
            dialog = open_skill(self.win, "org.tuxistant.weather")
        toast.assert_not_called()
        self.assertEqual(self.win.state, "ready")
        self.assertIn("older than these Settings", dialog.problem)
        self.assertEqual(dialog.actions.get_description(), dialog.problem, "no markup in it")
        self.assertIn("weather.forecast", texts_of(dialog.actions), "ListSkills named it")
        self.assertFalse(dialog.manifest.get_visible())
        self.assertIn("Can use the network (geocoding-api.open-meteo.com, api.open-meteo.com)",
                      texts_of(dialog.permissions), "its permissions came with ListSkills")
        self.assertEqual(dialog.settings.location.get_title(), "Home town",
                         "its settings don't need the new method")
        close_skill(self.win)
        control("FailNext", "SkillDetails", "disk <full>", signature="ss")
        dialog = open_skill(self.win, "org.tuxistant.weather")
        self.assertIn("disk <full>", dialog.problem)
        self.assertIn("disk <full>", "\n".join(texts_of(dialog.page)), "shown as written")
        close_skill(self.win)
        dialog = open_skill(self.win, "org.example.shell")
        self.assertIn("isn't loaded any more", dialog.problem)

    def test_21_f_a_skills_settings_are_on_its_page_and_nowhere_else(self):
        general = self.win.general
        for name in sa.SettingsBox.ROWS:
            self.assertIsNone(getattr(general, name), name)
        self.assertEqual(set(general.switches), {"conversation_mode", "fn_hold_enabled",
                                                 "hide_on_release", "dictation_enabled"})
        self.assertEqual(set(general.keys), {"api_key"})
        dialog = open_skill(self.win, "org.tuxistant.time")
        self.assertIsNone(dialog.settings, "a skill with nothing to set has no Settings group")

    def test_21_f_z_every_setting_has_one_home(self):
        """Every row on every page is used as a person would, and what reaches the daemon says
        where each setting really is: once each, and none missing."""
        from tuxistant.config import EDITABLE, SECRETS
        stored = {key: control("Stored", key, signature="s")[0] for key in SECRETS}
        before = dict(self.win.config)
        found = {}
        for skill_id in sa.SKILL_SETTINGS:
            dialog = open_skill(self.win, skill_id)
            box = dialog.settings
            if box.phone:
                wait(lambda: box.devices is not None, "the phones")
            if box.calendar:
                wait(lambda: isinstance(box.calendars, list), "the calendars")
            found[skill_id] = self.sent_by(dialog.page)
        close_skill(self.win)
        self.win.views.set_visible_child_name("general")
        general = self.sent_by(self.win.general)
        forget()
        self.win.voices.choose(before["voice"])
        self.win.appearance.choose(before["theme"])
        wait(lambda: len(saved()) == 2, "the voice and theme to save")
        elsewhere = {key for key, _value in saved()}
        for key, value in stored.items():
            control("Change", key, value, signature="ss")
        self.assertEqual(found, EXPECTED_SETTINGS)
        on_skills = set().union(*found.values())
        self.assertEqual(general & on_skills, set(), "one home each")
        self.assertEqual(general | on_skills | elsewhere, set(EDITABLE),
                         "every setting has a home: General, a skill's page, Voice or Appearance")
        wait(lambda: {k: self.win.config.get(k) for k in before} == before, "all put back")

    def sent_by(self, root) -> set:
        """The keys SetConfig was sent while each setting row under root was changed and put
        back."""
        forget()
        for widget in list(iter_children(root)):
            if isinstance(widget, Adw.SwitchRow):
                was = widget.get_active()
                widget.set_active(not was)
                alert = self.win.get_visible_dialog()
                agreed = isinstance(alert, Adw.AlertDialog)
                if agreed:      # Cloud asks first, and Turn On waits for the tick
                    alert.agree.set_active(True)
                    alert.emit("response", "enable")
                    alert.force_close()
                widget.set_active(was)
                if agreed:      # as it was before: never agreed to
                    next(b for b in iter_children(root) if isinstance(b, Gtk.Button)
                         and b.get_label() == "Withdraw").emit("clicked")
            elif isinstance(widget, Adw.SpinRow):
                was = widget.get_value()
                widget.set_value(was + widget.get_adjustment().get_step_increment())
                widget.set_value(was)
            elif isinstance(widget, Adw.PasswordEntryRow):
                widget.set_text("one-home-test")
                widget.emit("apply")
                remove = next(b for b in iter_children(widget) if isinstance(b, Gtk.Button)
                              and b.get_icon_name() == "user-trash-symbolic")
                remove.emit("clicked")
            elif isinstance(widget, Adw.EntryRow):
                widget.emit("apply")    # what it shows now, saved again
            elif isinstance(widget, Adw.ComboRow):
                was, count = widget.get_selected(), widget.get_model().get_n_items()
                self.assertGreater(count, 1, widget.get_title())
                widget.set_selected((was + 1) % count)
                widget.set_selected(was)
            elif isinstance(widget, Gtk.CheckButton) and not widget.get_active():
                chosen = next(c for c in iter_children(root) if isinstance(c, Gtk.CheckButton)
                              and c.get_active())
                widget.set_active(True)
                chosen.set_active(True)
            elif isinstance(widget, Gtk.TextView) and widget.get_editable():
                buffer = widget.get_buffer()
                was = buffer.get_text(*buffer.get_bounds(), False)
                save = next(b for b in iter_children(root) if isinstance(b, Gtk.Button)
                            and b.get_label() == "Save")
                for text in (was + " ", was):
                    buffer.set_text(text)
                    save.emit("clicked")
        settle(sa.SAVE_DELAY_MS + 300)      # a spin saves once you stop
        return {key for key, _value in saved()}

    def test_21_g_a_setting_two_skills_use_agrees_on_both(self):
        page = self.skill_page("org.tuxistant.phone")
        wait(lambda: page.devices is not None, "the device list")
        page.phone.set_selected(2)
        wait(lambda: self.win.config["phone_device"] == "Galaxy Tab", "the choice to come back")
        other = self.skill_page("org.tuxistant.phone_send")
        wait(lambda: other.devices is not None, "the device list")
        self.assertEqual(other.phone_labels[other.phone.get_selected()], "Galaxy Tab")
        other.phone.set_selected(0)
        wait(lambda: self.win.config["phone_device"] == "", "the reset")

    def test_21_h_settings_whose_skill_isnt_loaded_still_have_a_place(self):
        """Without bubblewrap Lights is only a problem, and a stored password must still go."""
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        self.assertFalse(page.orphans.get_visible())

        def lights_listed():
            return any(row.skill["id"] == "org.tuxistant.lights" for row in page.rows)

        def back():
            control("SetSandbox", True, signature="b")
            wait(lights_listed, "Lights to come back")

        self.addCleanup(back)
        control("Change", "tapo_password", '"stored secret"', signature="ss")
        wait(lambda: self.win.config.get("tapo_password_set") is True, "a stored password")
        control("SetSandbox", False, signature="b")
        wait(lambda: not lights_listed(), "Lights to go")
        wait(lambda: any("bubblewrap" in row.get_subtitle() for row in page.problem_rows),
             "the problem to say why")
        self.assertTrue(page.orphans.get_visible())
        self.assertEqual([row.get_title() for row in page.orphan_rows], ["Lights"])
        page.orphan_rows[0].activate()
        dialog = page.dialog
        self.assertIs(self.win.get_visible_dialog(), dialog)
        self.assertEqual(dialog.get_title(), "Lights")
        box = dialog.settings
        self.assertIn("isn't loaded now", box.group.get_description())
        self.assertEqual(box.tapo_password_status.get_label(), "Password is set")
        shot(self.win, "skill-not-loaded")
        forget()
        box.tapo_password_remove.emit("clicked")
        wait(lambda: saved() == [["tapo_password", '""']], "the password to be removed")
        wait(lambda: self.win.config.get("tapo_password_set") is False, "tapo_password_set")
        self.assertEqual(json.loads(control("Stored", "tapo_password", signature="s")[0]), "")
        box.tapo_email.set_text("tux@example.org")
        apply_button(box.tapo_email).emit("clicked")
        wait(lambda: self.win.config.get("tapo_email") == "tux@example.org", "the email to save")
        control("Change", "tapo_email", '""', signature="ss")
        wait(lambda: box.tapo_email.get_text() == "", "the outside change")

    def test_21_i_a_skill_pages_messages_show_on_it(self):
        """The window's own toasts would be hidden behind the page."""
        page = self.skill_page("org.tuxistant.home")
        dialog = self.win.skills.dialog
        with mock.patch.object(dialog, "add_toast", wraps=dialog.add_toast) as on_page, \
                mock.patch.object(self.win.toasts, "add_toast") as behind:
            page.home.set_text("two words")
            apply_button(page.home).emit("clicked")
            self.assertEqual([call.args[0].get_title() for call in on_page.call_args_list],
                             ["Enter an address like homeassistant.local:8123"])
            control("FailNext", "SetConfig", "That is not a <valid> token", signature="ss")
            page.home_token.set_text("bad token")
            apply_button(page.home_token).emit("clicked")
            wait(lambda: on_page.call_count == 2, "the daemon's refusal")
            self.assertEqual(on_page.call_args.args[0].get_title(), "That is not a <valid> token")
            self.assertFalse(on_page.call_args.args[0].get_use_markup())
            shot(self.win, "skill-toast")
            behind.assert_not_called()
        wait(lambda: page.home_token_status.get_label() == "No token set", "the state to roll back")
        close_skill(self.win)
        with mock.patch.object(self.win.toasts, "add_toast") as window:
            self.win.toast("No page open")
        window.assert_called_once()

    def test_21_j_coming_back_to_the_window_keeps_your_place_in_the_skills(self):
        """The skills are read again each time the window becomes active. An unchanged list must
        not be rebuilt, or a keyboard or screen reader user is sent back to the first skill."""
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        wait(lambda: page.problem_rows, "the skills with problems")
        settle()
        rows, problems, orphans = list(page.rows), list(page.problem_rows), list(page.orphan_rows)
        rows[-3].grab_focus()
        scroll = scrolled(page).get_vadjustment()
        before = still(scroll)
        self.assertGreater(before, 0, "far enough down that the page has scrolled")
        forget()
        with mock.patch.object(self.win, "is_active", return_value=True):
            self.win.on_active()
        wait(lambda: calls("ListSkills") and calls("SkillProblems"), "the skills to reload")
        settle()
        self.assertEqual(page.rows, rows, "an unchanged list keeps its rows")
        self.assertEqual(page.problem_rows, problems)
        self.assertEqual(page.orphan_rows, orphans)
        self.assertIs(holder(self.win, page.rows), rows[-3])
        self.assertEqual(still(scroll), before)

    def test_21_k_a_changed_skill_list_keeps_focus_on_your_skill(self):
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        settle()

        def ids():
            return [row.skill["id"] for row in page.rows]

        def back():
            control("SetSandbox", True, signature="b")
            wait(lambda: "org.tuxistant.lights" in ids(), "Lights to come back")

        self.addCleanup(back)
        lights = ids().index("org.tuxistant.lights")
        self.assertLess(lights, len(page.rows) - 3)
        target = page.rows[-3]
        target_id = target.skill["id"]
        target.grab_focus()
        scroll = scrolled(page).get_vadjustment()
        before = still(scroll)
        control("SetSandbox", False, signature="b")
        wait(lambda: "org.tuxistant.lights" not in ids(), "Lights to go")
        settle()
        focus = holder(self.win, page.rows)
        self.assertIsNotNone(focus, "the focus stays in the list")
        self.assertEqual(focus.skill["id"], target_id)
        self.assertLessEqual(abs(still(scroll) - before), focus.get_height(),
                             "the page stays where it was, give or take the row that went")
        back()
        lights_row = page.rows[ids().index("org.tuxistant.lights")]
        lights_row.grab_focus()
        settle()
        control("SetSandbox", False, signature="b")
        wait(lambda: "org.tuxistant.lights" not in ids(), "Lights to go")
        settle()
        self.assertIs(holder(self.win, page.rows), page.rows[lights],
                      "the skill that went leaves the focus on the one now in its place")

    def test_21_l_a_skill_page_closed_after_the_list_changed_goes_back_to_its_row(self):
        page = self.win.skills
        self.win.views.set_visible_child_name("skills")
        settle()

        def lights_listed():
            return any(row.skill["id"] == "org.tuxistant.lights" for row in page.rows)

        def back():
            control("SetSandbox", True, signature="b")
            wait(lights_listed, "Lights to come back")

        self.addCleanup(back)
        row = next(row for row in page.rows if row.skill["id"] == "org.tuxistant.weather")
        row.grab_focus()
        settle()
        row.activate()
        dialog = page.dialog
        wait(lambda: dialog.details is not None, "the skill's details")
        control("SetSandbox", False, signature="b")
        wait(lambda: not lights_listed(), "Lights to go")
        self.assertIs(page.dialog, dialog, "a reload leaves the open page alone")
        dialog.close()
        wait(lambda: self.win.get_visible_dialog() is None, "the page to close")
        settle()
        focus = holder(self.win, page.rows)
        self.assertIsNotNone(focus, "the focus goes back to the list")
        self.assertEqual(focus.skill["id"], "org.tuxistant.weather")

    def test_22_history_lists_and_refreshes_live(self):
        page = self.win.history
        self.win.views.set_visible_child_name("history")
        titles = [row.get_title() for row in page.rows]
        self.assertEqual(titles, ["Check a folder that doesn't exist", "Nothing heard",
                                  "How much disk is free?", "What's the <time> & date?"])
        self.assertIn("Ran disk.usage (failed)", page.rows[0].get_subtitle())
        self.assertIn("Ended with an error", page.rows[1].get_subtitle())
        self.assertEqual(calls("GetHistory"), [])
        control("AddRequest", "Play <music> & dance", "Done.", signature="ss")
        wait(lambda: len(page.rows) == 5, "the live refresh")
        self.assertEqual(page.rows[0].get_title(), "Play <music> & dance")
        self.assertEqual(calls("GetHistory"), [["GetHistory", sa.HISTORY_ROWS]])

    def test_23_a_slow_stale_reply_does_not_win(self):
        page = self.win.history
        control("DelayNext", "GetHistory", 300, signature="si")
        page.load()
        wait(lambda: len(calls("GetHistory")) == 1, "the slow request to reach the daemon")
        control("AddRequest", "newer", "1", signature="ss")
        wait(lambda: page.rows[0].get_title() == "newer", "the newer reply")
        settle(450)
        self.assertEqual(len(calls("GetHistory")), 2)
        self.assertEqual(page.rows[0].get_title(), "newer", "the older reply must be dropped")

    def test_24_a_history_shows_commands_and_what_was_asked(self):
        page = self.win.history
        self.win.views.set_visible_child_name("history")
        command = 'du -sh ~/<Downloads> && echo "a & b"'
        add_record("How big is <Downloads>?", "It's <b>12</b> megabytes & change.", [{
            "name": "shell.run", "arguments": json.dumps({"command": command}),
            "asked": [{"title": "Run <this> command?", "detail": f"{command}\nas you & me",
                       "level": "click", "allowed": True}],
            "result": {"output": "12M\t~/Downloads\n<i>done</i> & dusted", "exit_code": 0,
                       "sandboxed": True}}])
        row = page.rows[0]
        self.assertIsInstance(row, sa.HistoryRow)
        self.assertEqual(row.get_title(), "How big is <Downloads>?")
        self.assertEqual(row.get_subtitle(), "It's <b>12</b> megabytes & change.\n"
                                             f"Ran in the sandbox: {command}\n"
                                             "Allowed: Run <this> command?")
        self.assertEqual(texts_of(row)[:2], ["How big is <Downloads>?", row.get_subtitle()])
        self.assertFalse(row.built, "nothing is built for a row nobody opened")
        row.set_expanded(True)
        settle()
        shown = texts_of(row)
        for text in ("shell.run", "Ran in the sandbox", "Command", command,
                     "Allowed: Run <this> command?", f"{command}\nas you & me",
                     "output:\n12M\t~/Downloads\n<i>done</i> & dusted\nexit_code: 0\n"
                     "sandboxed: true"):
            self.assertIn(text, shown)
        self.assertEqual([label for label in labels_of(row) if label.get_use_markup()], [],
                         "no label here may read its text as markup")

    def test_24_b_history_tells_refusals_and_root_apart(self):
        page = self.win.history
        add_record("Clear it out", "You said no.", [{
            "name": "shell.run", "arguments": '{"command": "rm -rf ~/old"}',
            "asked": [{"title": "Delete ~/old?", "detail": "rm -rf ~/old", "level": "click",
                       "allowed": False}],
            "result": {"declined": True, "message": "The user said no."}}])
        self.assertIn("Did not run, not allowed: rm -rf ~/old", page.rows[0].get_subtitle())
        self.assertIn("Not allowed: Delete ~/old?", page.rows[0].get_subtitle())
        self.assertNotIn("Ran", page.rows[0].get_subtitle())
        add_record("Install htop", "Done.", [{
            "name": "shell.run", "arguments": '{"command": "apt install htop"}',
            "asked": [{"title": "Install htop?", "detail": "apt install htop", "level": "admin",
                       "allowed": True}],
            "result": {"output": "ok", "exit_code": 0, "sandboxed": False}}])
        self.assertIn("Ran outside the sandbox: apt install htop", page.rows[0].get_subtitle())
        self.assertIn("Allowed to run as root: Install htop?", page.rows[0].get_subtitle())

    def test_24_c_hostile_strings_cannot_hide_or_break_anything(self):
        page = self.win.history
        before = len(page.rows)
        bidi = "rm \u202efdp.exe"
        broken = "a\ud800b"    # a lone surrogate: fine in JSON, fatal in a label
        add_record("Odd output", "Hm.", [{
            "name": "shell.run", "arguments": json.dumps({"command": f"cat x\x00; {bidi}"}),
            "result": {"output": broken, "exit_code": 0, "sandboxed": True}},
            {"name": "<made.up>", "arguments": '{"oops', "result": {"error": broken}}])
        wait(lambda: len(page.rows) == before + 1, "the row to appear")
        row = page.rows[0]
        self.assertIn("cat x\\u0000; rm \\u202efdp.exe", row.get_subtitle())
        row.set_expanded(True)
        settle()
        shown = texts_of(row)
        self.assertIn("cat x\\u0000; rm \\u202efdp.exe", shown)
        self.assertIn("output: a\\ud800b\nexit_code: 0\nsandboxed: true", shown)
        self.assertIn("<made.up>", shown)
        self.assertIn('{"oops', shown)

    def test_24_d_a_long_result_is_all_there(self):
        page = self.win.history
        output = "\n".join(f"line {number}" for number in range(400))  # past the shell's cap
        add_record("List it all", "That's a lot.", [{
            "name": "shell.run", "arguments": '{"command": "seq 400"}',
            "result": {"output": output, "exit_code": 0, "sandboxed": True, "truncated": True}}])
        row = page.rows[0]
        row.set_expanded(True)
        settle()
        self.assertIn(f"output:\n{output}\nexit_code: 0\nsandboxed: true\ntruncated: true",
                      texts_of(row))
        row.set_expanded(False)

    def test_24_e_a_long_unbroken_command_wraps_in_a_narrow_window(self):
        add_record("Fetch it", "Done.", [{
            "name": "shell.run",
            "arguments": json.dumps({"command": "curl https://example.org/" + "x" * 150}),
            "asked": [{"title": "Run this command?", "detail": "curl", "level": "click",
                       "allowed": True}],
            "result": {"output": "y" * 300, "exit_code": 0, "sandboxed": False}}])
        window = open_window(self.app, 360, 720)
        wait(lambda: ready(window) and window.split.get_collapsed(), "a collapsed window")
        window.views.set_visible_child_name("history")
        window.split.set_show_content(True)
        window.history.rows[0].set_expanded(True)
        settle(300)
        self.assertEqual(window.get_width(), 360, "long text must not widen the window")
        shot(window, "history-long-command-narrow")
        dispose(window)

    def test_24_f_a_new_request_keeps_open_entries_open(self):
        page = self.win.history
        opened = page.rows[1]
        opened.set_expanded(True)
        settle(30)
        heard = opened.get_title()
        control("AddRequest", "Something else", "Hello.", signature="ss")
        wait(lambda: page.rows[0].get_title() == "Something else", "the refresh")
        self.assertEqual(page.rows[2].get_title(), heard)
        self.assertTrue(page.rows[2].get_expanded())
        self.assertTrue(page.rows[2].built)
        page.rows[2].set_expanded(False)

    def test_24_g_a_new_request_keeps_focus_on_your_entry(self):
        page = self.win.history
        self.win.views.set_visible_child_name("history")
        settle()
        row = next(row for row in page.rows[1:] if isinstance(row, sa.HistoryRow))
        at = page.rows.index(row)
        row.grab_focus()
        settle()
        self.assertIs(holder(self.win, page.rows), row)
        control("AddRequest", "One more thing", "Hello.", signature="ss")
        wait(lambda: page.rows[0].get_title() == "One more thing", "the refresh")
        settle()
        self.assertIs(holder(self.win, page.rows), page.rows[at + 1],
                      "the same request, a place further down")
        self.assertEqual(page.rows[at + 1].entry["id"], row.entry["id"])

    def test_25_cloud_asks_before_turning_on(self):
        page = self.skill_page("org.tuxistant.task")
        self.assertFalse(page.cloud.get_active())
        self.assertEqual(page.cloud.get_subtitle(), "Nothing goes to Anthropic")
        self.assertFalse(page.cloud_consent.get_visible(), "nothing agreed to yet")
        page.cloud.set_active(True)
        dialog = self.win.get_visible_dialog()
        self.assertIsInstance(dialog, Adw.AlertDialog)
        self.assertEqual(dialog.get_body(), sa.cloud_body("claude-opus-5-5"))
        self.assertFalse(dialog.get_body_use_markup())
        self.assertEqual(dialog.details.get_label(), sa.CLOUD_DETAILS)
        self.assertFalse(dialog.details.get_use_markup())
        self.assertEqual(dialog.get_close_response(), "cancel")
        self.assertEqual(dialog.get_default_response(), "cancel", "Enter must not agree")
        self.assertEqual(page.cloud.get_subtitle(), "Nothing goes to Anthropic",
                         "nothing has changed yet")
        shot(self.win, "cloud-dialog")
        settle()
        self.assertEqual(saved(), [], "nothing is saved before the answer")
        dialog.emit("response", "cancel")
        dialog.force_close()
        settle()
        self.assertFalse(page.cloud.get_active())
        self.assertEqual(saved(), [])
        self.assertIs(json.loads(control("Stored", "cloud_enabled", signature="s")[0]), False)

        page.cloud.set_active(True)
        agree(self.win)
        self.assertTrue(page.cloud.get_active())
        wait(lambda: saved() == [["cloud_consent", json.dumps(AGREED)]], "the agreement to save")
        wait(lambda: self.win.config["cloud_enabled"] is True, "the change to come back")
        self.assertTrue(page.cloud.get_active())
        self.assertIn("nothing goes to Anthropic", page.cloud.get_subtitle(), "there is no key yet")
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog, "a reload must not ask again")

        forget()
        page.cloud.set_active(False)
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog, "turning it off needs no dialog")
        wait(lambda: saved() == [["cloud_enabled", "false"]], "the switch to save")
        wait(lambda: self.win.config["cloud_enabled"] is False, "the change to come back")
        self.assertTrue(page.cloud_consent.get_visible(), "off is not withdrawn")
        forget()
        page.cloud.set_active(True)
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog,
                      "an agreement that still covers it is not asked for again")
        wait(lambda: saved() == [["cloud_enabled", "true"]], "the switch to save")
        withdraw(self.win, page)

    def test_25_cloud_turn_on_waits_for_the_tick(self):
        page = self.skill_page("org.tuxistant.task")
        page.cloud.set_active(True)
        dialog = self.win.get_visible_dialog()
        self.assertFalse(dialog.get_response_enabled("enable"))
        self.assertFalse(dialog.agree.get_active())
        self.assertEqual(dialog.agree.get_label(), "I understand and agree")
        self.assertEqual(dialog.policy.get_uri(), "https://www.anthropic.com/legal/privacy")
        self.assertIn("privacy policy", dialog.policy.get_label())
        dialog.agree.set_active(True)
        self.assertTrue(dialog.get_response_enabled("enable"))
        dialog.agree.set_active(False)
        self.assertFalse(dialog.get_response_enabled("enable"), "untick, and it can't be pressed")
        dialog.emit("response", "enable")   # as if the button were pressed anyway
        dialog.force_close()
        settle()
        self.assertEqual(saved(), [], "no tick, no agreement")
        self.assertFalse(page.cloud.get_active())
        self.assertEqual(json.loads(control("Stored", "cloud_consent", signature="s")[0]), {})

    def test_25_cloud_the_agreement_is_recorded_shown_and_withdrawn(self):
        page = self.skill_page("org.tuxistant.task")
        page.cloud.set_active(True)
        agree(self.win)
        wait(lambda: stored_value("cloud_consent"), "the record to be kept")
        stored = stored_value("cloud_consent")
        today = datetime.now().date()
        self.assertEqual(stored, {**AGREED, "date": today.isoformat()})
        self.assertTrue(page.cloud_consent.get_visible())
        self.assertEqual(page.cloud_consent.get_title(), f"You agreed on {sa.day_words(str(today))}")
        self.assertIn("For claude-opus-5-5 at Anthropic", page.cloud_consent.get_subtitle())
        self.assertTrue(page.withdraw.get_visible())
        shot(self.win, "cloud-agreed")
        withdraw(self.win, page)
        self.assertFalse(page.cloud.get_active())
        self.assertFalse(page.cloud_consent.get_visible())
        self.assertEqual(json.loads(control("Stored", "cloud_consent", signature="s")[0]), {})
        self.assertIs(json.loads(control("Stored", "cloud_enabled", signature="s")[0]), False)
        page.cloud.set_active(True)
        self.assertIsInstance(self.win.get_visible_dialog(), Adw.AlertDialog,
                              "after withdrawing it asks again")
        cancel(self.win)

    def test_25_cloud_a_reload_behind_the_dialog_does_not_lose_the_answer(self):
        page = self.skill_page("org.tuxistant.task")
        page.cloud.set_active(True)
        dialog = self.win.get_visible_dialog()
        dialog.agree.set_active(True)
        control("Change", "listen_timeout_s", "21", signature="ss")
        wait(lambda: self.win.config["listen_timeout_s"] == 21, "the config to reload")
        self.assertIs(self.win.get_visible_dialog(), dialog, "a reload must not close or reopen it")
        self.assertTrue(dialog.agree.get_active())
        agree(self.win)
        self.assertTrue(page.cloud.get_active())
        wait(lambda: saved() == [["cloud_consent", json.dumps(AGREED)]], "the answer to save")
        wait(lambda: self.win.config["cloud_enabled"] is True, "the change to come back")
        self.assertTrue(page.cloud.get_active())
        withdraw(self.win, page)

    def test_25_cloud_real_keys_cannot_agree_by_accident(self):
        page = self.skill_page("org.tuxistant.task")
        keyboard = Keyboard()
        self.addCleanup(keyboard.close)

        def closed():
            return self.win.get_visible_dialog() is self.win.skills.dialog

        keyboard.press(Gdk.KEY_Shift_L)     # a new virtual keyboard loses its first key
        for key, ticked in ((Gdk.KEY_Escape, False), (Gdk.KEY_Return, False),
                            (Gdk.KEY_Escape, True)):
            page.cloud.activate()
            wait(lambda: isinstance(self.win.get_visible_dialog(), Adw.AlertDialog), "the dialog")
            self.win.get_visible_dialog().agree.set_active(ticked)
            settle(200)
            keyboard.press(key)
            wait(closed, "the key to close the dialog")
            self.assertFalse(page.cloud.get_active())
        settle()
        self.assertEqual(saved(), [])

    def test_25_cloud_on_from_elsewhere_is_not_agreed_and_says_so(self):
        """As SetConfig from anyone on the bus, or a config from before there was an agreement."""
        page = self.skill_page("org.tuxistant.task")
        control("Change", "cloud_enabled", "true", signature="ss")
        wait(lambda: self.win.config["cloud_enabled"] is True, "the change to come back")
        self.assertFalse(page.cloud.get_active(), "on without an agreement is not on")
        self.assertTrue(page.cloud.get_subtitle().startswith(
            "On, but not agreed to, so nothing goes to Anthropic"))
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog)
        shot(self.win, "cloud-not-agreed")
        page.cloud.set_active(True)
        self.assertIsInstance(self.win.get_visible_dialog(), Adw.AlertDialog, "it asks")
        cancel(self.win)
        self.assertFalse(page.cloud.get_active())
        control("Change", "cloud_enabled", "false", signature="ss")
        wait(lambda: self.win.config["cloud_enabled"] is False, "the change to come back")
        settle()
        self.assertEqual(saved(), [])

    def test_25_cloud_an_agreement_from_elsewhere_asks_nothing_and_is_not_echoed(self):
        page = self.skill_page("org.tuxistant.task")
        control("Change", "cloud_consent", json.dumps(AGREED), signature="ss")
        wait(page.cloud.get_active, "the switch to follow")
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog)
        control("Change", "cloud_consent", "{}", signature="ss")
        wait(lambda: not page.cloud.get_active(), "the switch to follow")
        self.assertFalse(page.cloud_consent.get_visible())
        settle()
        self.assertEqual(saved(), [])

    def test_25_cloud_a_new_version_of_what_is_sent_asks_again(self):
        page = self.skill_page("org.tuxistant.task")
        control("Change", "cloud_consent", json.dumps(AGREED), signature="ss")
        wait(page.cloud.get_active, "the switch to follow")
        with mock.patch.object(sa.cloud, "CLOUD_CONSENT_VERSION", CONSENT_VERSION + 1):
            page.sync(self.win.config)
            self.assertFalse(page.cloud.get_active())
            self.assertTrue(page.cloud.get_subtitle().startswith(
                "What is sent has changed since you agreed, so nothing goes to Anthropic"))
            self.assertIn("doesn't cover what would be sent now", page.cloud_consent.get_subtitle())
            page.cloud.set_active(True)
            self.assertIsInstance(self.win.get_visible_dialog(), Adw.AlertDialog, "it asks")
            cancel(self.win)
        page.sync(self.win.config)
        self.assertTrue(page.cloud.get_active())
        settle()
        self.assertEqual(saved(), [])
        withdraw(self.win, page)

    def test_25_cloud_another_model_asks_again(self):
        page = self.skill_page("org.tuxistant.task")
        control("Change", "cloud_consent", json.dumps(AGREED), signature="ss")
        wait(page.cloud.get_active, "the switch to follow")
        row, apply = page.cloud_model, apply_button(page.cloud_model)
        row.set_text("claude-other-1")
        apply.emit("clicked")
        dialog = self.win.get_visible_dialog()
        self.assertIsInstance(dialog, Adw.AlertDialog)
        self.assertIn("model claude-other-1,", dialog.get_body())
        self.assertTrue(page.cloud.get_active(), "still on, for the model agreed to")
        cancel(self.win)
        self.assertEqual(row.get_text(), "claude-opus-5-5", "Cancel puts the agreed model back")
        settle()
        self.assertEqual(saved(), [])

        row.set_text("claude-other-1")
        apply.emit("clicked")
        agree(self.win)
        other = {**AGREED, "model": "claude-other-1"}
        wait(lambda: saved() == [["cloud_consent", json.dumps(other)]], "one save, with the model")
        wait(lambda: stored_value("cloud_model") == "claude-other-1", "the model to be kept")
        self.assertEqual(stored_value("cloud_consent")["model"], "claude-other-1")
        self.assertTrue(page.cloud.get_active())
        self.assertEqual(row.get_text(), "claude-other-1")

        control("Change", "cloud_model", '"claude-<b>third</b>"', signature="ss")
        wait(lambda: not page.cloud.get_active(), "another model from elsewhere turns it off")
        self.assertEqual(page.cloud.get_subtitle(), "You agreed for another model, so nothing "
                         "goes to Anthropic. Turn it on to agree for claude-<b>third</b>")
        self.assertFalse(page.cloud.get_use_markup())
        self.assertFalse(page.cloud_consent.get_use_markup())
        withdraw(self.win, page)
        control("Change", "cloud_model", '"claude-opus-5-5"', signature="ss")
        wait(lambda: self.win.config["cloud_model"] == "claude-opus-5-5", "the reset")

    def test_25_cloud_a_refused_save_is_undone(self):
        page = self.skill_page("org.tuxistant.task")
        control("FailNext", "SetConfig", "disk <full>", signature="ss")
        with mock.patch.object(self.win, "toast", wraps=self.win.toast) as toast:
            page.cloud.set_active(True)
            agree(self.win)
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("disk <full>")
        wait(lambda: not page.cloud.get_active(), "the switch to roll back")
        self.assertEqual(page.cloud.get_subtitle(), "Nothing goes to Anthropic")
        self.assertFalse(page.cloud_consent.get_visible())
        settle()
        self.assertEqual(len(saved()), 1, "the rollback is not a save")

    def as_from_an_older_daemon(self, **values):
        """What GetConfig gives before the daemon restarts after an update: no cloud_consent."""
        config = {key: value for key, value in self.win.config.items() if key != "cloud_consent"}
        self.addCleanup(self.win.load_config)
        self.win.apply_config({**config, **values})

    def test_25_cloud_an_older_daemon_using_it_can_be_switched_off(self):
        """Its planner uses the cloud with the switch and a key alone, so say so, and let the
        user turn it off. Shown off, it could only be turned on, which it would refuse."""
        page = self.skill_page("org.tuxistant.task")
        self.as_from_an_older_daemon(cloud_enabled=True, cloud_key_set=True)
        self.assertTrue(page.cloud.get_active())
        self.assertEqual(page.cloud.get_subtitle(), sa.cloud_note(
            {"cloud_enabled": True, "cloud_key_set": True}))
        self.assertIn("go to Anthropic", page.cloud.get_subtitle())
        self.assertFalse(page.cloud_consent.get_visible())
        settle()
        self.assertEqual(saved(), [], "loading must not write anything back")
        page.cloud.set_active(False)
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog)
        wait(lambda: saved() == [["cloud_enabled", "false"]], "the switch to save")

    def test_25_cloud_an_older_daemon_is_not_asked_to_agree(self):
        """It would refuse cloud_consent, so the dialog would end in a refusal."""
        page = self.skill_page("org.tuxistant.task")
        self.as_from_an_older_daemon(cloud_enabled=False)
        self.assertEqual(page.cloud.get_subtitle(),
                         f"Nothing goes to Anthropic. {sa.RESTART_TO_AGREE}")
        with mock.patch.object(self.win, "toast") as toast:
            page.cloud.set_active(True)
            settle(30)
        toast.assert_called_once_with(sa.RESTART_TO_AGREE)
        self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog, "no agreement")
        wait(lambda: not page.cloud.get_active(), "the switch to go back")
        settle()
        self.assertEqual(saved(), [])

    def test_25_cloud_a_model_no_agreement_could_name_is_refused_at_once(self):
        page = self.skill_page("org.tuxistant.task")
        row, apply = page.cloud_model, apply_button(page.cloud_model)
        for cloud_on in (False, True):
            if cloud_on:
                control("Change", "cloud_consent", json.dumps(AGREED), signature="ss")
                wait(page.cloud.get_active, "the switch to follow")
            with self.subTest(cloud_on=cloud_on), \
                    mock.patch.object(self.win, "toast") as toast:
                row.set_text("m" * 101)
                apply.emit("clicked")
                settle(30)
                toast.assert_called_once_with("That is not a model name")
                self.assertIs(self.win.get_visible_dialog(), self.win.skills.dialog,
                              "no agreement for it")
                self.assertEqual(row.get_text(), "claude-opus-5-5")
        settle()
        self.assertEqual(saved(), [])
        withdraw(self.win, page)

    def test_25_cloud_key_is_write_only(self):
        page = self.skill_page("org.tuxistant.task")
        apply = apply_button(page.cloud_key)
        control("Change", "cloud_consent", json.dumps(AGREED), signature="ss")
        wait(page.cloud.get_active, "the switch to follow")
        self.assertIn("nothing goes to Anthropic", page.cloud.get_subtitle())
        self.assertEqual(page.cloud_key_status.get_label(), "No key set")
        page.cloud_key.set_text("  sk-cloud-sekret \n")
        apply.emit("clicked")
        wait(lambda: saved() == [["cloud_api_key", '"sk-cloud-sekret"']], "the key to save")
        self.assertEqual(page.cloud_key.get_text(), "")
        self.assertEqual(page.cloud_key_status.get_label(), "Key is set")
        wait(lambda: self.win.config.get("cloud_key_set") is True, "cloud_key_set")
        self.assertTrue(page.cloud_key_remove.get_visible())
        self.assertIn("go to Anthropic", page.cloud.get_subtitle())
        self.assertNotIn("cloud_api_key", self.win.config)
        self.assertNotIn("sk-cloud-sekret", json.dumps(self.win.config))
        self.assertEqual(page.cloud_key.get_text(), "", "a reload must not bring it back")
        self.assertEqual(
            json.loads(control("Stored", "cloud_api_key", signature="s")[0]), "sk-cloud-sekret")
        self.assertEqual(json.loads(control("Stored", "api_key", signature="s")[0]), "",
                         "the two keys are not the same setting")
        forget()
        page.cloud_key.set_text("   ")
        apply.emit("clicked")
        settle()
        self.assertEqual(saved(), [], "a blank entry must not wipe the key")
        page.cloud_key_remove.emit("clicked")
        wait(lambda: saved() == [["cloud_api_key", '""']], "the key to be removed")
        wait(lambda: self.win.config.get("cloud_key_set") is False, "cloud_key_set to clear")
        self.assertEqual(page.cloud_key_status.get_label(), "No key set")
        self.assertIn("nothing goes to Anthropic", page.cloud.get_subtitle())
        control("Change", "cloud_consent", "{}", signature="ss")
        wait(lambda: not page.cloud.get_active(), "the switch to follow")

    def test_25_cloud_key_refused_says_so_without_repeating_it(self):
        page = self.skill_page("org.tuxistant.task")
        with mock.patch.object(self.win, "toast", wraps=self.win.toast) as toast:
            page.cloud_key.set_text("has a space inside")
            apply_button(page.cloud_key).emit("clicked")
            wait(lambda: toast.called, "the toast")
        self.assertNotIn("space", toast.call_args.args[0])
        wait(lambda: page.cloud_key_status.get_label() == "No key set", "the state to roll back")
        self.assertFalse(page.cloud_key_remove.get_visible())

    def test_25_cloud_model(self):
        row = self.skill_page("org.tuxistant.task").cloud_model
        apply = apply_button(row)
        self.assertEqual(row.get_text(), "claude-opus-5-5")
        settle(30)
        self.assertFalse(apply.get_mapped(), "text from the daemon is not an edit")
        row.grab_focus()
        row.set_text("  claude-other-1 ")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: saved() == [["cloud_model", '"claude-other-1"']], "the model to save")
        wait(lambda: self.win.config["cloud_model"] == "claude-other-1", "the change to come back")
        row.grab_focus()
        row.set_text("")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: self.win.config["cloud_model"] == "claude-opus-5-5", "the default to return")
        self.assertEqual(row.get_text(), "claude-opus-5-5")
        control("Change", "cloud_model", '""', signature="ss")
        wait(lambda: self.win.config["cloud_model"] == "", "the empty value")
        self.assertEqual(row.get_text(), "claude-opus-5-5", "empty means the default")
        control("Change", "cloud_model", '"claude-opus-5-5"', signature="ss")
        wait(lambda: self.win.config["cloud_model"] == "claude-opus-5-5", "the reset")

    def test_25_phone_lists_devices_and_saves_the_choice(self):
        page = self.skill_page("org.tuxistant.phone")
        wait(lambda: page.devices is not None, "the device list")
        self.assertEqual(page.phone_labels, ["Automatic", "Pixel <8> & co", "Galaxy Tab"])
        self.assertEqual(page.phone.get_selected(), 0)
        self.assertEqual(page.phone.get_subtitle(), sa.PHONE_NOTE)
        settle()
        self.assertEqual(saved(), [], "listing devices must not write anything back")
        page.phone.set_selected(1)
        wait(lambda: saved() == [["phone_device", '"Pixel <8> & co"']], "the device to save")
        wait(lambda: self.win.config["phone_device"] == "Pixel <8> & co", "the change to come back")
        self.assertEqual(page.phone.get_selected(), 1)
        self.assertIn("Pixel <8> & co", texts_of(page.phone), "the name as it is, not as markup")
        self.assertEqual([label for label in labels_of(page.phone) if label.get_use_markup()], [])
        forget()
        page.phone.set_selected(0)
        wait(lambda: saved() == [["phone_device", '""']], "Automatic to save as empty")
        wait(lambda: self.win.config["phone_device"] == "", "the change to come back")

    def test_25_calendar_lists_what_can_be_changed_and_saves_the_choice(self):
        page = self.skill_page("org.tuxistant.calendar_add")
        self.real_calendars()
        self.assertEqual(page.calendar_labels,
                         ["Automatic", "Family (me@example.org)", "Personal (On This Computer)"])
        self.assertEqual(page.calendar.get_selected(), 0)
        self.assertEqual(page.calendar.get_subtitle(), sa.CALENDAR_NOTE)
        forget()
        settle()
        self.assertEqual(saved(), [], "listing calendars must not write anything back")
        page.calendar.set_selected(1)
        wait(lambda: saved() == [["calendar_add_to", '"cal-family"']], "the calendar to save")
        wait(lambda: self.win.config["calendar_add_to"] == "cal-family", "the change to come back")
        self.assertEqual(page.calendar.get_selected(), 1)
        forget()
        control("Change", "calendar_add_to", '"a-calendar-that-left"', signature="ss")
        wait(lambda: page.calendar_labels[-1] == sa.GONE_CALENDAR, "the missing calendar to show")
        self.assertEqual(page.calendar.get_selected(), 3)
        settle()
        self.assertEqual(saved(), [], "a change from the daemon is not sent back to it")
        control("Change", "calendar_add_to", '""', signature="ss")
        wait(lambda: page.calendar.get_selected() == 0, "the reset")

    def test_25_calendar_without_accounts_points_to_online_accounts(self):
        page = self.skill_page("org.tuxistant.calendar_add")
        self.list_calendars_as([], sa.NO_CALENDARS)
        self.assertIn("Online Accounts", page.calendar.get_subtitle())
        self.assertEqual(page.calendar_labels, ["Automatic"])

    def test_25_calendar_without_evolution_data_server_names_the_packages(self):
        page = self.skill_page("org.tuxistant.calendar_add")
        self.list_calendars_as({"problem": calendars.NO_EDS}, calendars.NO_EDS)
        self.assertIn("gir1.2-ecal-2.0", page.calendar.get_subtitle())
        self.assertNotIn("Online Accounts", page.calendar.get_subtitle())
        self.assertEqual(page.calendar_labels, ["Automatic"])
        settle()
        self.assertEqual(saved(), [], "a missing calendar service is not a choice")
        shot(self.win, "calendar-no-eds")

    def test_25_calendar_a_failed_listing_gives_no_account_advice(self):
        # A timeout or a daemon older than this app: the user may well have accounts already.
        page = self.skill_page("org.tuxistant.calendar_add")
        self.addCleanup(self.load_calendars, self.real_calendars(), sa.CALENDAR_NOTE)
        self.fail_calendar_listing()
        self.assertNotIn("Online Accounts", page.calendar.get_subtitle())
        self.assertEqual(page.calendar_labels, ["Automatic"])
        settle()
        self.assertEqual(saved(), [], "a failed listing is not a choice")

    def test_25_calendar_a_failed_listing_does_not_call_the_saved_one_gone(self):
        """We only know the list didn't come, not that the saved calendar left."""
        page = self.skill_page("org.tuxistant.calendar_add")
        self.addCleanup(self.load_calendars, self.real_calendars(), sa.CALENDAR_NOTE)
        self.addCleanup(control, "Change", "calendar_add_to", '""', signature="ss")
        control("Change", "calendar_add_to", '"cal-family"', signature="ss")
        wait(lambda: page.calendar.get_selected() == 1, "the saved calendar")
        self.fail_calendar_listing()
        self.assertEqual(page.calendar_labels, ["Automatic", "…"])
        self.assertEqual(page.calendar.get_selected(), 1)
        self.load_calendars({"problem": calendars.NO_EDS}, calendars.NO_EDS)
        self.assertEqual(page.calendar_labels, ["Automatic", "…"])
        settle()
        self.assertEqual(saved(), [], "the saved calendar is kept as it is")

    def fail_calendar_listing(self):
        page = self.skill_page("org.tuxistant.calendar_add")
        control("FailNext", "ListCalendars", "timed out", signature="ss")
        forget()
        page.load()
        wait(lambda: calls("ListCalendars"), "a fresh calendar list")
        wait(lambda: page.calendar.get_subtitle() == sa.CALENDARS_FAILED, "the calendar note")

    def real_calendars(self) -> list:
        """The list the fake sends, taken before a test replaces it: a problem leaves None."""
        page = self.skill_page("org.tuxistant.calendar_add")
        wait(lambda: isinstance(page.calendars, list) and not page.calendar_problem,
             "the calendar list")
        return list(page.calendars)

    def list_calendars_as(self, reply, subtitle):
        """Have the fake daemon send this reply and wait for the row to show it. Afterwards the
        real list comes back, on the fake and on the page."""
        self.addCleanup(self.load_calendars, self.real_calendars(), sa.CALENDAR_NOTE)
        self.load_calendars(reply, subtitle)

    def load_calendars(self, reply, subtitle):
        page = self.skill_page("org.tuxistant.calendar_add")
        control("SetCalendars", json.dumps(reply), signature="s")
        forget()
        page.load()
        wait(lambda: calls("ListCalendars"), "a fresh calendar list")
        wait(lambda: page.calendar.get_subtitle() == subtitle, "the calendar note")

    def test_25_news_lists_the_sources_and_saves_the_choice(self):
        page = self.skill_page("org.tuxistant.news")
        sources = list(sa.SOURCES)
        model = page.news.get_model()
        self.assertEqual([model.get_string(i) for i in range(model.get_n_items())],
                         [name for name, _base, _feeds in sa.SOURCES.values()])
        self.assertEqual(page.news.get_selected(), sources.index("bbc"))
        forget()
        settle()
        self.assertEqual(saved(), [], "showing the sources must not write anything back")
        page.news.set_selected(sources.index("npr"))
        wait(lambda: saved() == [["news_source", '"npr"']], "the source to save")
        wait(lambda: self.win.config["news_source"] == "npr", "the change to come back")
        forget()
        control("Change", "news_source", '"dw"', signature="ss")
        wait(lambda: page.news.get_selected() == sources.index("dw"), "another client's choice")
        settle()
        self.assertEqual(saved(), [], "a change from the daemon is not sent back to it")
        control("Change", "news_source", '"bbc"', signature="ss")
        wait(lambda: page.news.get_selected() == sources.index("bbc"), "the reset")

    def test_25_phone_a_saved_device_survives_not_being_listed(self):
        page = self.skill_page("org.tuxistant.phone")
        control("Change", "phone_device", '"Old <phone>"', signature="ss")
        wait(lambda: self.win.config["phone_device"] == "Old <phone>", "the config to reload")
        self.assertEqual(page.phone_labels[-1], "Not available: Old <phone>")
        self.assertEqual(page.phone.get_selected(), 3)
        page.load()
        wait(lambda: calls("ListPhones"), "a fresh list")
        settle()
        self.assertEqual(page.phone.get_selected(), 3, "a fresh list keeps the choice")
        self.assertEqual(saved(), [], "a new model selecting its first row is not a choice")
        self.assertEqual(json.loads(control("Stored", "phone_device", signature="s")[0]),
                         "Old <phone>")
        page.phone.set_selected(1)
        wait(lambda: len(page.phone_labels) == 3, "the old choice to go once it is replaced")
        self.assertEqual(page.phone_labels, ["Automatic", "Pixel <8> & co", "Galaxy Tab"])
        self.assertEqual(page.phone.get_selected(), 1)
        page.phone.set_selected(0)
        wait(lambda: self.win.config["phone_device"] == "", "the reset")

    def test_25_phone_without_gsconnect(self):
        page = self.skill_page("org.tuxistant.phone_send")
        control("Change", "phone_device", '"Pixel <8> & co"', signature="ss")
        wait(lambda: self.win.config["phone_device"] == "Pixel <8> & co", "the config to reload")
        listed = json.dumps(page.devices)
        control("SetPhones", "[]", signature="s")
        self.addCleanup(control, "SetPhones", listed, signature="s")
        page.load()
        wait(lambda: page.devices == [], "the empty list")
        self.assertEqual(page.phone.get_subtitle(), sa.NO_PHONES)
        self.assertIn("GSConnect", page.phone.get_subtitle())
        self.assertEqual(page.phone_labels, ["Automatic", "Not available: Pixel <8> & co"])
        self.assertEqual(page.phone.get_selected(), 1)
        settle()
        self.assertEqual(saved(), [])
        shot(self.win, "phone-no-gsconnect")
        page.phone.set_selected(0)
        wait(lambda: page.phone_labels == ["Automatic"], "going back to Automatic")
        self.assertEqual(saved(), [["phone_device", '""']])
        self.assertEqual(self.win.state, "ready")

    def test_25_phone_a_failed_or_slow_list_is_not_an_outage(self):
        page = self.skill_page("org.tuxistant.phone")
        control("FailNext", "ListPhones", "boom", signature="ss")
        with mock.patch.object(self.win, "toast") as toast:
            page.load()
            wait(lambda: page.devices == [], "the failure to count as empty")
            toast.assert_not_called()
        self.assertEqual(self.win.state, "ready")
        control("DelayNext", "ListPhones", sa.READ_TIMEOUT_MS + 500, signature="si")
        page.load()
        wait(lambda: len(page.phone_labels) == 3, "the slow list", 8)
        self.assertEqual(self.win.state, "ready", "GSConnect can take longer than a plain read")

    def test_25_phone_before_the_list_arrives_nothing_is_called_missing(self):
        control("Change", "phone_device", '"Pixel <8> & co"', signature="ss")
        wait(lambda: self.win.config["phone_device"] == "Pixel <8> & co", "the config to reload")
        control("DelayNext", "ListPhones", 1000, signature="si")
        window = open_window(self.app)
        wait(lambda: ready(window), "the window")
        page = self.skill_page("org.tuxistant.phone", window)
        self.assertIsNone(page.devices)
        self.assertEqual(page.phone_labels, ["Automatic", "Pixel <8> & co"])
        self.assertEqual(page.phone.get_selected(), 1)
        self.assertEqual(page.phone.get_subtitle(), sa.PHONE_NOTE)
        wait(lambda: page.devices is not None, "the list")
        self.assertEqual(page.phone.get_selected(), 1)
        settle()
        self.assertEqual(saved(), [])
        close_skill(window)
        dispose(window)
        control("Change", "phone_device", '""', signature="ss")
        wait(lambda: self.win.config["phone_device"] == "", "the reset")

    def test_25_a_daemon_older_than_this_app_is_no_crash(self):
        """Settings updates on disk at once. The daemon keeps its old code until it restarts."""
        new = ("phone_device", "translate_server", "search_server", "fact_lookup", "cloud_enabled",
               "cloud_model", "cloud_key_set", "cloud_consent", "tapo_email", "tapo_password_set", "mail_search",
               "timer_chime", "ha_server", "ha_token_set", "say_tailscale_address")
        current = dict(self.win.config)
        self.addCleanup(self.win.apply_config, current)
        self.win.apply_config({key: value for key, value in current.items() if key not in new})
        for skill, check in (
                ("org.tuxistant.task", lambda page: (
                    self.assertFalse(page.cloud.get_active()),
                    self.assertEqual(page.cloud_model.get_text(), "claude-opus-5-5"))),
                ("org.tuxistant.phone", lambda page: self.assertEqual(
                    page.phone.get_selected(), 0)),
                ("org.tuxistant.lights", lambda page: self.assertEqual(
                    page.tapo_password_status.get_label(), "No password set")),
                ("org.tuxistant.home", lambda page: self.assertEqual(
                    (page.home.get_text(), page.home_token_status.get_label()),
                    ("", "No token set"))),
                ("org.tuxistant.knowledge", lambda page: self.assertEqual(
                    page.search.get_text(), ""))):
            with self.subTest(skill):
                check(self.skill_page(skill))
        settle()
        self.assertEqual(saved(), [])

    def test_25_a_key_the_daemon_lacks_leaves_its_row_alone(self):
        """Falling back to empty or off would show what isn't so, and a save would write it."""
        page = self.win.general
        weather = self.skill_page("org.tuxistant.weather")
        current = dict(self.win.config)
        held = {"server": "kept:1234", "location": "Oslo", "pipeline": "tux-realtime-eou",
                "instructions": "Be brief.", "dictation_enabled": False,
                "conversation_mode": True, "listen_timeout_s": 33, "fn_hold_ms": 2500}
        self.win.apply_config({**current, **held})
        self.win.apply_config({key: value for key, value in current.items() if key not in held})
        self.assertEqual(page.server.get_text(), "kept:1234")
        self.assertEqual(weather.location.get_text(), "Oslo")
        self.assertTrue(page.pipelines["tux-realtime-eou"].get_active())
        self.assertFalse(page.switches["dictation_enabled"].get_active())
        self.assertTrue(page.switches["conversation_mode"].get_active())
        self.assertEqual(page.spins["listen_timeout_s"][0].get_value(), 33)
        self.assertEqual(page.spins["fn_hold_ms"][0].get_value(), 2.5)
        self.assertEqual(page.instructions_text(), "Be brief.")
        self.assertFalse(page.save_instructions.get_sensitive(), "no edit was made")
        settle()
        self.assertEqual(saved(), [])
        self.win.apply_config(current)

    def test_25_search_server(self):
        page = self.skill_page("org.tuxistant.websearch")
        row = page.search
        self.assertEqual(row.get_text(), "", "empty means the same host, port 8888")
        apply = apply_button(row)
        settle(30)
        self.assertFalse(apply.get_mapped(), "text from the daemon is not an edit")
        with mock.patch.object(self.win, "toast") as toast:
            row.grab_focus()
            row.set_text("not a server")
            wait(apply.get_mapped, "the apply button")
            apply.emit("clicked")
            settle()
            self.assertTrue(row.has_css_class("error"))
            toast.assert_called_once_with("Enter a host name and port, like myserver:8888")
            self.assertEqual(saved(), [])
        row.set_text("http://tuxbox:8888/")
        self.assertFalse(row.has_css_class("error"))
        apply.emit("clicked")
        wait(lambda: saved() == [["search_server", '"tuxbox:8888"']], "the server to save")
        self.assertEqual(row.get_text(), "tuxbox:8888")
        wait(lambda: self.win.config["search_server"] == "tuxbox:8888", "the save to come back")
        settle(30)
        self.assertFalse(apply.get_mapped(), "the cleaned-up text is not an edit either")

        control("FailNext", "SetConfig", "That is not a server address", signature="ss")
        with mock.patch.object(self.win, "toast") as toast:
            row.grab_focus()
            row.set_text("other:8888")
            wait(apply.get_mapped, "the apply button")
            apply.emit("clicked")
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("That is not a server address")
        wait(lambda: row.get_text() == "tuxbox:8888", "the row to show what the daemon holds")

        forget()
        row.grab_focus()
        row.set_text("   ")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: saved() == [["search_server", '""']], "empty to save")
        self.assertEqual(row.get_text(), "")
        self.assertFalse(row.has_css_class("error"))
        wait(lambda: self.win.config["search_server"] == "", "the change to come back")
        control("Change", "search_server", '"elsewhere:8888"', signature="ss")
        wait(lambda: row.get_text() == "elsewhere:8888", "the outside change")
        control("Change", "search_server", '""', signature="ss")
        wait(lambda: row.get_text() == "", "the reset")

    def test_25_translation_server(self):
        page = self.skill_page("org.tuxistant.translate")
        row = page.translate
        self.assertEqual(row.get_text(), "", "empty means the same host, port 5000")
        apply = apply_button(row)
        settle(30)
        self.assertFalse(apply.get_mapped(), "text from the daemon is not an edit")
        with mock.patch.object(self.win, "toast") as toast:
            row.grab_focus()
            row.set_text("not a server")
            wait(apply.get_mapped, "the apply button")
            apply.emit("clicked")
            settle()
            self.assertTrue(row.has_css_class("error"))
            toast.assert_called_once()
            self.assertEqual(saved(), [])
        row.set_text("http://tuxbox:5000/")
        self.assertFalse(row.has_css_class("error"))
        apply.emit("clicked")
        wait(lambda: saved() == [["translate_server", '"tuxbox:5000"']], "the server to save")
        self.assertEqual(row.get_text(), "tuxbox:5000")
        wait(lambda: self.win.config["translate_server"] == "tuxbox:5000", "the save to come back")
        settle(30)
        self.assertFalse(apply.get_mapped(), "the cleaned-up text is not an edit either")

        control("FailNext", "SetConfig", "That is not a server address", signature="ss")
        with mock.patch.object(self.win, "toast") as toast:
            row.grab_focus()
            row.set_text("other:5000")
            wait(apply.get_mapped, "the apply button")
            apply.emit("clicked")
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("That is not a server address")
        wait(lambda: row.get_text() == "tuxbox:5000", "the row to show what the daemon holds")

        forget()
        row.grab_focus()
        row.set_text("   ")
        wait(apply.get_mapped, "the apply button")
        apply.emit("clicked")
        wait(lambda: saved() == [["translate_server", '""']], "empty to save")
        self.assertEqual(row.get_text(), "")
        self.assertFalse(row.has_css_class("error"))
        wait(lambda: self.win.config["translate_server"] == "", "the change to come back")
        control("Change", "translate_server", '"elsewhere:5000"', signature="ss")
        wait(lambda: row.get_text() == "elsewhere:5000", "the outside change")
        control("Change", "translate_server", '""', signature="ss")
        wait(lambda: row.get_text() == "", "the reset")

    def test_25_lights_account(self):
        page = self.skill_page("org.tuxistant.lights")
        apply = apply_button(page.tapo_email)
        self.assertEqual(page.tapo_password_status.get_label(), "No password set")
        page.tapo_email.set_text("  tux@example.org ")
        apply.emit("clicked")
        wait(lambda: saved() == [["tapo_email", '"tux@example.org"']], "the email to save")
        self.assertEqual(page.tapo_email.get_text(), "tux@example.org")
        wait(lambda: self.win.config.get("tapo_email") == "tux@example.org", "the save to come back")
        forget()
        page.tapo_password.set_text(" pëngüin pass ")
        apply_button(page.tapo_password).emit("clicked")
        wait(lambda: saved() == [["tapo_password", '" p\\u00ebng\\u00fcin pass "']],
             "the password to save as typed, spaces and all")
        self.assertEqual(page.tapo_password.get_text(), "")
        wait(lambda: self.win.config.get("tapo_password_set") is True, "tapo_password_set")
        self.assertEqual(page.tapo_password_status.get_label(), "Password is set")
        self.assertNotIn("tapo_password", self.win.config)
        self.assertNotIn("ngüin", json.dumps(self.win.config, ensure_ascii=False))
        self.assertNotIn("ng\\u00fcin", json.dumps(self.win.config))
        self.assertEqual(json.loads(control("Stored", "tapo_password", signature="s")[0]),
                         " pëngüin pass ")
        forget()
        page.tapo_password.set_text("   ")
        apply_button(page.tapo_password).emit("clicked")
        settle()
        self.assertEqual(saved(), [], "a blank entry must not wipe the password")
        page.tapo_password_remove.emit("clicked")
        wait(lambda: saved() == [["tapo_password", '""']], "the password to be removed")
        wait(lambda: self.win.config.get("tapo_password_set") is False, "tapo_password_set")
        control("Change", "tapo_email", '""', signature="ss")
        wait(lambda: page.tapo_email.get_text() == "", "the outside change")

    def test_25_z_home_assistant_address_and_token(self):
        page = self.skill_page("org.tuxistant.home")
        apply, token = apply_button(page.home), "eyJ.long-lived.token"
        self.assertEqual(page.home_token_status.get_label(), "No token set")
        self.assertFalse(page.home_token_remove.get_visible())
        with mock.patch.object(self.win, "toast") as toast:
            page.home.set_text("ws://ha.lan:8123")
            apply.emit("clicked")
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("Enter an address like homeassistant.local:8123")
        self.assertTrue(page.home.has_css_class("error"))
        settle()
        self.assertEqual(saved(), [], "what is not an address is not sent")
        page.home.set_text(" http://ha.lan/ ")
        self.assertFalse(page.home.has_css_class("error"))
        apply.emit("clicked")
        wait(lambda: saved() == [["ha_server", '"ha.lan:8123"']], "the address to save")
        self.assertEqual(page.home.get_text(), "ha.lan:8123")
        wait(lambda: self.win.config.get("ha_server") == "ha.lan:8123", "the save to come back")
        forget()
        page.home_token.set_text(f"  {token} ")
        apply_button(page.home_token).emit("clicked")
        wait(lambda: saved() == [["ha_token", json.dumps(token)]], "the token to save")
        self.assertEqual(page.home_token.get_text(), "")
        wait(lambda: self.win.config.get("ha_token_set") is True, "ha_token_set")
        self.assertEqual(page.home_token_status.get_label(), "Token is set")
        self.assertTrue(page.home_token_remove.get_visible())
        self.assertNotIn("ha_token", self.win.config)
        self.assertNotIn("long-lived", json.dumps(self.win.config))
        self.assertEqual(json.loads(control("Stored", "ha_token", signature="s")[0]), token)
        forget()
        with mock.patch.object(self.win, "toast") as toast:
            page.home.set_text("other.lan:8123")
            apply.emit("clicked")
            wait(lambda: toast.called, "the toast")
        toast.assert_called_once_with("A new address needs its own token: enter it below")
        self.assertEqual(page.home_token_status.get_label(), "No token set", "shown at once")
        wait(lambda: saved() == [["ha_server", '"other.lan:8123"']], "the new address to save")
        wait(lambda: self.win.config.get("ha_server") == "other.lan:8123", "the save to come back")
        self.assertIs(self.win.config.get("ha_token_set"), False)
        self.assertEqual(json.loads(control("Stored", "ha_token", signature="s")[0]), "",
                         "the daemon dropped the token with the address it was given for")
        forget()
        page.home_token.set_text(token)
        apply_button(page.home_token).emit("clicked")
        wait(lambda: saved() == [["ha_token", json.dumps(token)]], "the token to save again")
        forget()
        page.home_token.set_text("   ")
        apply_button(page.home_token).emit("clicked")
        settle()
        self.assertEqual(saved(), [], "a blank entry must not wipe the token")
        page.home_token_remove.emit("clicked")
        wait(lambda: saved() == [["ha_token", '""']], "the token to be removed")
        wait(lambda: self.win.config.get("ha_token_set") is False, "ha_token_set")
        control("Change", "ha_server", '""', signature="ss")
        wait(lambda: page.home.get_text() == "", "the outside change")

    def test_27_clearing_history_asks_first(self):
        page = self.win.history
        self.win.views.set_visible_child_name("history")
        page.clear_button.emit("clicked")
        dialog = self.win.get_visible_dialog()
        self.assertIsInstance(dialog, Adw.AlertDialog)
        self.assertEqual(
            dialog.get_response_appearance("clear"), Adw.ResponseAppearance.DESTRUCTIVE)
        self.assertEqual(dialog.get_close_response(), "cancel")
        self.assertEqual(dialog.get_default_response(), "cancel")
        shot(self.win, "history-clear-confirm")
        dialog.emit("response", "cancel")
        dialog.force_close()
        settle()
        self.assertEqual(calls("ClearHistory"), [])
        self.assertTrue(page.rows)

        page.clear_button.emit("clicked")
        dialog = self.win.get_visible_dialog()
        dialog.emit("response", "clear")
        dialog.force_close()
        wait(lambda: not page.rows, "the history to empty")
        self.assertEqual(calls("ClearHistory"), [["ClearHistory"]])
        self.assertEqual(page.stack.get_visible_child_name(), "empty")
        shot(self.win, "history-empty")
        control("AddRequest", "back again", "Hello.", signature="ss")
        wait(lambda: page.stack.get_visible_child_name() == "list", "the list to return")

    def test_29_narrow_window_navigates_from_the_list(self):
        window = open_window(self.app, 360, 720)
        wait(lambda: ready(window) and window.split.get_collapsed(), "a collapsed window")
        self.assertFalse(window.split.get_show_content(), "it opens on the list of pages")
        self.assertEqual(window.sidebar.get_mode(), Adw.SidebarMode.PAGE)
        window.views.set_visible_child_name("history")
        window.sidebar.emit("activated")
        self.assertTrue(window.split.get_show_content())
        self.assertEqual(window.content.get_title(), "History")
        dispose(window)

    def test_29_b_a_skills_page_fits_a_narrow_window(self):
        window = open_window(self.app, 360, 720)
        wait(lambda: ready(window) and window.split.get_collapsed(), "a collapsed window")
        window.split.set_show_content(True)
        for skill_id in ("org.example.markup", "org.tuxistant.home", "org.tuxistant.weather"):
            dialog = open_skill(window, skill_id)
            for row in iter_children(dialog.actions):
                if isinstance(row, sa.SkillActionRow):
                    row.set_expanded(True)
            settle(300)
            self.assertEqual(window.get_width(), 360, "long text must not widen the window")
            self.assertLessEqual(dialog.get_width(), 360)
            self.assertLessEqual(dialog.get_height(), 720)
            shot(window, f"skill-narrow-{skill_id.rsplit('.', 1)[-1]}")
        dialog.manifest_row.activate()
        shot(window, "skill-narrow-manifest")
        close_skill(window)
        dispose(window)

    def test_28_closing_right_after_a_change_saves_at_once(self):
        window = open_window(self.app)
        wait(lambda: window.state == "ready", "the second window")
        settle()
        forget()
        window.general.spins["listen_timeout_s"][0].set_value(33)
        self.assertIn("listen_timeout_s", window.timers)
        self.assertIn("<Control>w", self.app.get_accels_for_action("window.close"))
        self.assertTrue(window.activate_action("window.close", None))
        self.assertEqual(window.timers, {}, "the save must go now, not when a timer fires")
        wait(lambda: saved() == [["listen_timeout_s", "33"]], "the pending save")
        dispose(window)

    def test_28_the_process_delivers_a_last_save_and_then_exits(self):
        # Several rounds: closing this early used to leave the process spinning, but not always.
        for value in (41, 42, 43, 44):
            process = subprocess.Popen([sys.executable, "-c", LAST_SAVE_APP, str(value)], cwd=ROOT)
            wait(lambda: process.poll() is not None, "the app to exit by itself", 15)
            self.assertEqual(process.returncode, 0)
            stored = control("Stored", "listen_timeout_s", signature="s")[0]
            self.assertEqual(json.loads(stored), value)

    def test_26_screenshots(self):
        if not os.environ.get("TUX_SETTINGS_SHOTS"):
            self.skipTest("TUX_SETTINGS_SHOTS is not set")
        for key, value in (("server", '"ai-box.local:8080"'), ("voice", '"af_nicole"'),
                           ("listen_timeout_s", "8"), ("theme", '"orb"')):
            control("Change", key, value, signature="ss")
        pages = ("general", "voice", "appearance", "skills", "history")
        styles = Adw.StyleManager.get_default()
        control("Change", "api_key", '"x"', signature="ss")
        for scheme, tag, width, height in ((Adw.ColorScheme.FORCE_LIGHT, "light", 860, 1500),
                                           (Adw.ColorScheme.FORCE_DARK, "dark", 860, 1100),
                                           (Adw.ColorScheme.FORCE_LIGHT, "medium", 640, 760),
                                           (Adw.ColorScheme.FORCE_LIGHT, "narrow", 360, 720)):
            styles.set_color_scheme(scheme)
            window = open_window(self.app, width, height)
            wait(lambda: ready(window), "a screenshot window")
            shot(window, f"start-{tag}")
            for name in pages:
                window.views.set_visible_child_name(name)
                window.split.set_show_content(True)
                shot(window, f"{name}-{tag}")
            window.views.set_visible_child_name("general")
            window.general.api_key.grab_focus()
            shot(window, f"general-key-{tag}")
            window.views.set_visible_child_name("history")
            for row in window.history.rows:
                if isinstance(row, sa.HistoryRow) and "seq" not in row.get_subtitle():
                    row.set_expanded(True)
            shot(window, f"history-open-{tag}")
            for skill_id in ("org.tuxistant.weather", "org.tuxistant.home", "org.example.tides"):
                dialog = open_skill(window, skill_id)
                next(row for row in iter_children(dialog.actions)
                     if isinstance(row, sa.SkillActionRow)).set_expanded(True)
                shot(window, f"skill-{skill_id.rsplit('.', 1)[-1]}-{tag}")
            close_skill(window)
            dispose(window)
        styles.set_color_scheme(Adw.ColorScheme.DEFAULT)
        control("Change", "api_key", '""', signature="ss")

    def test_30_losing_the_daemon_shows_the_status_page(self):
        stop_fake(self.fake)
        try:
            wait(lambda: self.win.state == "offline", "the offline page")
            self.assertEqual(self.win.root.get_visible_child_name(), "offline")
            shot(self.win, "offline")
            self.win.retry_button.emit("clicked")
            wait(lambda: self.win.state == "offline", "Retry to fail again")
        finally:
            self.__class__.fake = start_fake()
        wait(lambda: self.win.state == "ready", "the window to recover by itself")
        self.assertTrue(self.win.voices.groups)
        self.assertEqual(self.win.root.get_visible_child_name(), "ready")

    def test_31_starting_without_a_daemon_then_retrying(self):
        stop_fake(self.fake)
        window = open_window(self.app)
        wait(lambda: window.state == "offline", "the offline page")
        self.__class__.fake = start_fake()
        wait(lambda: window.state == "ready", "the name to appear")
        dispose(window)

    def test_32_retry_after_a_failed_start(self):
        window = open_window(self.app)
        wait(lambda: window.state == "ready", "a window")
        control("FailNext", "GetConfig", "still starting", signature="ss")
        window.reload()
        wait(lambda: window.state == "offline", "the offline page")
        window.retry_button.emit("clicked")
        wait(lambda: window.state == "ready", "Retry to work")
        dispose(window)

    def test_33_retry_starts_an_activatable_daemon(self):
        services = Path(os.environ["XDG_DATA_HOME"]) / "dbus-1/services"
        services.mkdir(parents=True)
        (services / "org.tuxistant.Daemon.service").write_text(
            f"[D-BUS Service]\nName={sa.BUS_NAME}\n"
            f"Exec={sys.executable} {FAKE} --dir {os.environ['TUX_SETTINGS_FAKE_DIR']}\n")
        window = open_window(self.app)
        wait(lambda: window.state == "ready", "a window")
        stop_fake(self.fake)
        self.__class__.fake = None
        wait(lambda: window.state == "offline", "the offline page")
        settle(300)  # dbus-daemon notices the new service file by itself
        window.retry_button.emit("clicked")
        wait(lambda: window.state == "ready", "the bus to start the daemon", 10)
        self.assertTrue(calls("GetConfig"))
        dispose(window)

    @classmethod
    def tearDownClass(cls):
        if cls.fake:
            stop_fake(cls.fake)
        else:
            control("Quit")


def stored_value(key):
    return json.loads(control("Stored", key, signature="s")[0])


def agree(window):
    """Tick the box and press Turn On, on the agreement that is open."""
    dialog = window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog), "the agreement is open"
    dialog.agree.set_active(True)
    dialog.emit("response", "enable")
    dialog.force_close()
    settle(30)


def cancel(window):
    dialog = window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog), "the agreement is open"
    dialog.emit("response", "cancel")
    dialog.force_close()
    settle(30)


def withdraw(window, page):
    page.withdraw.emit("clicked")
    wait(lambda: stored_value("cloud_consent") == {} and not stored_value("cloud_enabled"),
         "the agreement to be withdrawn")
    wait(lambda: window.config.get("cloud_consent") == {} and not window.config["cloud_enabled"],
         "the change to come back")


def add_record(heard, reply, actions, status="ok"):
    """Through the real History and the same JSON and D-Bus trip a record really takes. The new
    row is built by the time this returns."""
    page = Gtk.Application.get_default().get_active_window().history
    first = page.rows[0] if page.rows else None
    control("AddRecord", heard, reply, json.dumps(actions), status, signature="ssss")
    wait(lambda: page.rows and page.rows[0] is not first, "the history to refresh")


CONSENT_VERSION = sa.cloud.CLOUD_CONSENT_VERSION
AGREED = {"version": CONSENT_VERSION, "provider": "Anthropic", "model": "claude-opus-5-5"}
CLOUD_ON = {"cloud_enabled": True, "cloud_key_set": True, "cloud_model": "claude-opus-5-5",
            "cloud_consent": {**AGREED, "date": "2026-11-13"}}


EXPECTED_SETTINGS = {
    "org.tuxistant.weather": {"location", "fahrenheit"},
    "org.tuxistant.news": {"news_source"},
    "org.tuxistant.calendar_add": {"calendar_add_to"},
    "org.tuxistant.mail": {"mail_search"},
    "org.tuxistant.timers": {"timer_chime"},
    "org.tuxistant.phone": {"phone_device"},
    "org.tuxistant.phone_send": {"phone_device"},
    "org.tuxistant.system": {"say_tailscale_address"},
    "org.tuxistant.translate": {"translate_server"},
    "org.tuxistant.translate_selection": {"translate_server"},
    "org.tuxistant.knowledge": {"fact_lookup", "search_server"},
    "org.tuxistant.websearch": {"search_server"},
    "org.tuxistant.research": {"search_server"},
    "org.tuxistant.lights": {"tapo_email", "tapo_password"},
    "org.tuxistant.home": {"ha_server", "ha_token"},
    "org.tuxistant.home_status": {"ha_server", "ha_token"},
    "org.tuxistant.task": {"cloud_enabled", "cloud_consent", "cloud_api_key", "cloud_model"},
}


class Contrast:
    """Adw.StyleManager as it is under high contrast, which a test can't turn on."""

    def get_high_contrast(self):
        return True

    def get_dark(self):
        return False


def clipboard_text(widget) -> str:
    found = []
    clipboard = widget.get_clipboard()
    clipboard.read_text_async(None, lambda c, result: found.append(c.read_text_finish(result)))
    wait(lambda: found, "the clipboard")
    return found[0]


def labels_of(widget):
    return [child for child in iter_children(widget) if isinstance(child, Gtk.Label)]


def texts_of(widget):
    return [label.get_text() for label in labels_of(widget)]


def holder(window, rows):
    """The row that holds the window's keyboard focus, itself or in one of its children."""
    focus = window.get_focus()
    return next((row for row in rows if focus is not None
                 and (focus == row or focus.is_ancestor(row))), None)


def still(adjustment):
    """Where a scroll ends up, once an animated one has stopped."""
    value = None
    while value != adjustment.get_value():
        value = adjustment.get_value()
        settle(150)
    return value


def scrolled(widget):
    return next(child for child in iter_children(widget) if isinstance(child, Gtk.ScrolledWindow))


def apply_button(row):
    return next(child for child in iter_children(row) if isinstance(child, Gtk.Button)
                and child.get_icon_name() == "adw-entry-apply-symbolic")


def iter_children(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from iter_children(child)
        child = child.get_next_sibling()


def run_inner():
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Inner)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    Path(os.environ["TUX_SETTINGS_RESULT"]).write_text(
        json.dumps({"ok": result.wasSuccessful(), "log": stream.getvalue()}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--inner" in sys.argv:
        sys.exit(run_inner())
    unittest.main()
