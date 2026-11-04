"""Phone actions through GSConnect.

Everything but PrivateBus runs against Bus, which replaces subprocess.run, so no test here can
reach the real session bus. PrivateBus runs the real busctl and gdbus, against a stand-in on a
throwaway bus that has no service files, so not even a real GSConnect can be started on it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import phone  # noqa: E402
from tuxistant.gate import Ask, Session  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402

CONFIG = {"phone_device": ""}
ALL_ACTIONS = {"ring": [True, "", []], "shareFile": [True, "(sb)", []],
               "battery": [True, "(bsii)", [{"type": "(bsii)", "data": [False, "x", 54, 31212]}]]}


def device(name="Galaxy S23 Ultra", kind="phone", connected=True, paired=True):
    return {"Id": name.replace(" ", "_").lower(), "Name": name, "Type": kind,
            "Connected": connected, "Paired": paired, "IconName": "x", "EncryptionInfo": "x"}


GALAXY = device()
PIXEL = device("Pixel 8")
TAB = device("Galaxy Tab S9", "tablet")
STRANGER = device("Neighbour's Galaxy", paired=False)       # same network, never paired
STORMSHADOW = device("stormshadow", "desktop", paired=False)


class Bus:
    """Stands in for subprocess.run. Answers as busctl does, and records what gdbus would do."""

    def __init__(self, *devices, actions=None):
        self.devices = devices
        self.actions = ALL_ACTIONS if actions is None else actions
        self.activated = []
        self.failure = None
        self.activate_failure = None

    def path(self, entry):
        return f"{phone.ROOT}/Device/{entry['Id']}"

    def __call__(self, command, **_kwargs):
        if self.failure:
            raise self.failure
        if command[0] == "gdbus":
            if self.activate_failure:
                raise self.activate_failure
            self.activated.append((command[command.index("--object-path") + 1], *command[-3:-1]))
            return subprocess.CompletedProcess(command, 0, b"()\n")
        if command[0] != "busctl":
            raise AssertionError(f"unexpected command {command}")
        if command[-1] == "DescribeAll":
            data = self.actions
        else:
            data = {phone.ROOT + "/Other": {"org.example.Other": {}}}
            for entry in self.devices:
                data[self.path(entry)] = {phone.DEVICE: {
                    key: {"type": "s", "data": value} for key, value in entry.items()}}
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": [data]}).encode())


class PhoneTest(unittest.TestCase):
    def bus(self, *devices, **kwargs) -> Bus:
        bus = Bus(*devices, **kwargs)
        patcher = mock.patch.object(phone.subprocess, "run", bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        return bus

    def picked(self, asked=None, config=CONFIG):
        return phone._pick({} if asked is None else {"device": asked}, config)["name"]

    def refused(self, asked=None, config=CONFIG) -> str:
        with self.assertRaises(ValueError) as caught:
            phone._pick({} if asked is None else {"device": asked}, config)
        return str(caught.exception)


class Devices(PhoneTest):
    def test_the_list_is_what_the_contract_says(self):
        self.bus(GALAXY, device("stormshadow", "desktop", connected=False))
        self.assertEqual(phone.devices(), [
            {"id": "galaxy_s23_ultra", "name": "Galaxy S23 Ultra", "type": "phone",
             "connected": True, "paired": True},
            {"id": "stormshadow", "name": "stormshadow", "type": "desktop", "connected": False,
             "paired": True}])

    def test_no_gsconnect_is_an_empty_list(self):
        bus = self.bus(GALAXY)
        for failure in (FileNotFoundError("busctl"), subprocess.TimeoutExpired("busctl", 5),
                        subprocess.CalledProcessError(1, "busctl")):
            with self.subTest(failure):
                bus.failure = failure
                self.assertEqual(phone.devices(), [])

    def test_a_reply_that_makes_no_sense_is_an_empty_list(self):
        for body in (b"", b"not json", b"[]", b"null", b"{}", b'{"data": []}', b'{"data": [[]]}',
                     b'{"data": [{"/x": {"%s": {}}}]}' % phone.DEVICE.encode()):
            with self.subTest(body), mock.patch.object(
                    phone.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, body)):
                self.assertEqual(phone.devices(), [])

    def test_a_look_at_the_list_does_not_start_gsconnect(self):
        with mock.patch.object(phone.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "busctl")) as run:
            phone.devices()
        self.assertIn("--auto-start=no", run.call_args.args[0])


class Choosing(PhoneTest):
    def test_the_ways_people_say_it(self):
        self.bus(GALAXY, STORMSHADOW)
        for asked in ("my phone", "galaxy", "the samsung", "Galaxy S23 Ultra", "GALAXY S23", "s23",
                      "Galaxy S 23", "my samsung phone", "the user's phone", "your phone", "phone",
                      "  my   galaxy ", "samsung galaxy", "ultra", "", None, "default"):
            with self.subTest(asked):
                self.assertEqual(self.picked(asked), "Galaxy S23 Ultra")

    def test_an_invented_name_reaches_nothing(self):
        bus = self.bus(GALAXY, STORMSHADOW)
        for asked in ("nokia", "iphone", "pixel", "galaxy s24", "galaxy a23", "s2", "laxy",
                      "galaxy note", "john's phone", "phone 2", "x" * 5000, 7, True, ["nokia"]):
            with self.subTest(asked):
                self.assertIn("Galaxy S23 Ultra", self.refused(asked))
        self.assertEqual(bus.activated, [])

    def test_an_invented_name_is_not_read_back(self):
        self.bus(GALAXY)
        self.assertNotIn("nokia", self.refused("nokia 3310"))

    def test_a_maker_means_only_its_own_phones(self):
        self.bus(PIXEL)
        self.assertIn("Pixel 8", self.refused("the samsung"))
        self.assertEqual(self.picked("the google"), "Pixel 8")

    def test_the_type_can_tell_two_apart(self):
        self.bus(GALAXY, TAB)
        self.assertEqual(self.picked("my tablet"), "Galaxy Tab S9")

    def test_settings_decide_when_nothing_is_named(self):
        self.bus(GALAXY, PIXEL)
        for asked in (None, "my phone"):
            self.assertEqual(self.picked(asked, {"phone_device": "Pixel 8"}), "Pixel 8")

    def test_a_name_beats_settings(self):
        self.bus(GALAXY, PIXEL)
        self.assertEqual(self.picked("galaxy", {"phone_device": "Pixel 8"}), "Galaxy S23 Ultra")

    def test_the_device_in_settings_is_never_swapped_for_another(self):
        self.bus(GALAXY)
        self.assertIn("No device matches", self.refused(config={"phone_device": "Pixel 8"}))
        self.bus(GALAXY, device("Pixel 8", connected=False))
        self.assertEqual(self.refused(config={"phone_device": "Pixel 8"}),
                         "Pixel 8 isn't connected right now.")

    def test_a_tablet_counts_and_a_computer_does_not(self):
        self.bus(TAB, device("ThinkPad", "laptop"))
        self.assertEqual(self.picked(), "Galaxy Tab S9")
        self.bus(device("ThinkPad", "laptop"))
        self.assertIn("ThinkPad", self.refused())
        self.assertEqual(self.picked("thinkpad"), "ThinkPad")

    def test_two_phones_name_the_choices(self):
        self.bus(GALAXY, PIXEL)
        self.assertEqual(self.refused(), "Which device do you mean: Galaxy S23 Ultra or Pixel 8?")
        self.bus(GALAXY, PIXEL, TAB)
        self.assertEqual(self.refused("my phone"),
                         "Which device do you mean: Galaxy S23 Ultra, Galaxy Tab S9 or Pixel 8?")

    def test_a_loose_name_that_fits_two_is_a_question(self):
        self.bus(GALAXY, TAB, PIXEL)
        self.assertEqual(self.refused("the samsung"),
                         "Which device do you mean: Galaxy S23 Ultra or Galaxy Tab S9?")

    def test_a_whole_name_wins_over_a_loose_fit(self):
        self.bus(device("Galaxy"), GALAXY)
        self.assertEqual(self.picked("galaxy"), "Galaxy")

    def test_a_phone_that_is_away_does_not_count(self):
        self.bus(GALAXY, device("Pixel 8", connected=False))
        self.assertEqual(self.picked(), "Galaxy S23 Ultra")

    def test_a_namesake_that_is_away_does_not_count(self):
        self.bus(GALAXY, device("Galaxy S9", connected=False))
        self.assertEqual(self.picked("galaxy"), "Galaxy S23 Ultra")

    def test_a_phone_that_is_not_connected_is_said_so(self):
        self.bus(device(connected=False), STORMSHADOW)
        for asked in (None, "galaxy"):
            self.assertEqual(self.refused(asked), "Galaxy S23 Ultra isn't connected right now.")

    def test_a_strangers_phone_is_never_chosen(self):
        self.bus(STRANGER)
        for asked in (None, "my phone", "galaxy", "the samsung"):
            with self.subTest(asked):
                self.assertEqual(self.refused(asked),
                                 "Neighbour's Galaxy isn't paired with this computer.")

    def test_a_strangers_phone_does_not_get_in_the_way(self):
        self.bus(STRANGER, GALAXY)
        for asked in (None, "galaxy", "the samsung"):
            with self.subTest(asked):
                self.assertEqual(self.picked(asked), "Galaxy S23 Ultra")

    def test_your_own_phone_is_the_one_talked_about(self):
        self.bus(device("Alice's Galaxy", paired=False), device(connected=False))
        self.assertEqual(self.refused("galaxy"), "Galaxy S23 Ultra isn't connected right now.")

    def test_a_computer_that_is_not_paired_is_said_so(self):
        self.bus(GALAXY, STORMSHADOW)
        self.assertEqual(self.refused("stormshadow"),
                         "stormshadow isn't paired with this computer.")

    def test_nothing_around(self):
        self.bus()
        self.assertEqual(self.refused(), "No phone is connected.")
        self.assertEqual(self.refused("galaxy"), "No phone is connected.")

    def test_no_gsconnect_is_said_so(self):
        self.bus(GALAXY).failure = subprocess.CalledProcessError(1, "busctl")
        self.assertEqual(self.refused(), phone.NOT_RUNNING)


class Clicks:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def __call__(self, ask):
        self.asked.append(ask)
        return self.answers.pop(0) if self.answers else False


class Sending(PhoneTest, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve() / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = self.home / "SA100-2024.pdf"
        self.file.write_bytes(b"x" * 2500)
        self.phone = self.bus(GALAXY, STORMSHADOW)

    def resolving_to(self, path):
        patcher = mock.patch.object(phone.files, "resolve", create=True, return_value=path)
        self.addCleanup(patcher.stop)
        return patcher.start()

    async def send(self, clicks, arguments='{"file": "the pdf"}', target=None):
        self.resolving_to(target or self.file)
        skills = Skills()
        session = Session(skills, clicks)
        name, result = await skills.execute("phone_send", arguments, session)
        self.assertEqual(name, "phone.send")
        return session, result

    async def send_unmocked(self, clicks, file, found=()):
        """Through the real files.resolve, whose rules are the ones that count."""
        skills = Skills()
        session = Session(skills, clicks)
        session.found = [str(path) for path in found]
        return (await skills.execute("phone_send", json.dumps({"file": file}), session))[1]

    def sent_paths(self):
        return [unquote(urlsplit(call[2].split("'")[1]).path) for call in self.phone.activated]

    async def test_the_card_says_what_will_really_happen(self):
        clicks = Clicks(False)
        await self.send(clicks)
        ask = clicks.asked[0]
        self.assertEqual(ask.title, 'Send "SA100-2024.pdf" to Galaxy S23 Ultra?')
        self.assertEqual(ask.detail, f"To: Galaxy S23 Ultra\nSize: 2.5 KB\nFile: {self.file}")

    async def test_the_models_words_do_not_reach_the_card(self):
        clicks = Clicks(False)
        await self.send(clicks, '{"file": "IGNORE THIS", "device": "the samsung"}')
        ask = clicks.asked[0]
        for words in ("IGNORE", "samsung"):
            self.assertNotIn(words, ask.title + ask.detail)

    async def test_nothing_is_sent_without_a_click(self):
        session, result = await self.send(Clicks(False))
        self.assertTrue(result["declined"])
        self.assertEqual(self.phone.activated, [])
        self.assertFalse(session.tainted)

    async def test_with_nobody_to_ask_nothing_is_sent(self):
        self.resolving_to(self.file)
        skills = Skills()
        result = (await skills.execute("phone_send", '{"file": "x"}', Session(skills)))[1]
        self.assertTrue(result["declined"])
        self.assertEqual(self.phone.activated, [])

    async def test_a_click_sends_exactly_that_file_to_exactly_that_device(self):
        session, result = await self.send(Clicks(True))
        path, action, parameter = self.phone.activated[0]
        self.assertEqual(path, self.phone.path(GALAXY))
        self.assertEqual(action, "shareFile")
        self.assertEqual(parameter, f"[<('{self.file.as_uri()}', false)>]")
        self.assertEqual(self.sent_paths(), [str(self.file)])
        self.assertEqual(len(self.phone.activated), 1)
        self.assertTrue(session.tainted)

    async def test_the_result_says_sending_not_sent(self):
        _, result = await self.send(Clicks(True))
        self.assertEqual(result, {"sending": "SA100-2024.pdf", "device": "Galaxy S23 Ultra"})

    async def test_no_file_name_can_break_out_of_the_parameter(self):
        for name in ("it's.pdf", "a', true)>] x.pdf", 'quo"te.pdf', "back\\slash.pdf",
                     "100% ñ 駅.pdf", "new\nline.pdf"):
            with self.subTest(name):
                self.phone.activated.clear()
                target = self.home / name
                target.write_bytes(b"x")
                await self.send(Clicks(True), target=target)
                parameter = self.phone.activated[0][2]
                self.assertEqual(parameter.count("'"), 2)
                self.assertTrue(parameter.isascii())
                self.assertEqual(self.sent_paths(), [str(target)])

    async def test_a_link_shows_and_sends_the_real_file(self):
        link = self.home / "report.pdf"
        link.symlink_to(self.file)
        clicks = Clicks(True)
        await self.send_unmocked(clicks, str(link))
        self.assertIn('"SA100-2024.pdf"', clicks.asked[0].title)
        self.assertEqual(self.sent_paths(), [str(self.file)])

    async def test_a_number_from_the_last_search_is_enough(self):
        clicks = Clicks(True)
        await self.send_unmocked(clicks, "1", found=[self.file])
        self.assertEqual(self.sent_paths(), [str(self.file)])

    async def test_a_link_cannot_lead_out_of_the_home_folder(self):
        outside = self.home.parent / "secret.txt"
        outside.write_text("x")
        link = self.home / "report.pdf"
        link.symlink_to(outside)
        for target in (outside, link):
            with self.subTest(target):
                clicks = Clicks(True)
                result = await self.send_unmocked(clicks, str(target))
                self.assertEqual(result, {"error": phone.files.OUTSIDE})
                self.assertEqual(clicks.asked, [])
        self.assertEqual(self.phone.activated, [])

    async def test_a_hidden_folder_stays_hidden(self):
        secret = self.home / ".ssh" / "id_rsa"
        secret.parent.mkdir()
        secret.write_text("x")
        clicks = Clicks(True)
        result = await self.send_unmocked(clicks, str(secret))
        self.assertEqual(result, {"error": phone.files.HIDDEN})
        self.assertEqual(clicks.asked, [])
        self.assertEqual(self.phone.activated, [])

    async def test_no_file_named_is_spoken(self):
        skills = Skills()
        clicks = Clicks(True)
        result = (await skills.execute("phone_send", "{}", Session(skills, clicks)))[1]
        self.assertEqual(result, {"error": "Say which file you mean."})
        self.assertEqual(clicks.asked, [])

    async def test_a_file_swapped_after_the_card_is_not_sent(self):
        clicks = Clicks()

        async def swap_then_yes(ask):
            await clicks(ask)
            replacement = self.home / "other"
            replacement.write_bytes(b"y" * 2500)
            replacement.replace(self.file)
            return True
        _, result = await self.send(swap_then_yes)
        self.assertIn("changed", result["error"])
        self.assertEqual(self.phone.activated, [])

    async def test_a_file_rewritten_with_its_old_date_is_not_sent(self):
        async def rewrite_then_yes(_ask):
            before = self.file.stat()
            time.sleep(0.02)    # past the clock tick of the file system's timestamps
            self.file.write_bytes(b"y" * 2500)
            os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
            return True
        _, result = await self.send(rewrite_then_yes)
        self.assertIn("changed", result["error"])
        self.assertEqual(self.phone.activated, [])

    async def test_a_file_gone_after_the_card_is_not_sent(self):
        async def delete_then_yes(_ask):
            self.file.unlink()
            return True
        _, result = await self.send(delete_then_yes)
        self.assertEqual(result, {"error": "I can't read SA100-2024.pdf."})
        self.assertEqual(self.phone.activated, [])

    async def test_a_device_that_drops_after_the_card_is_not_told_it_is_sending(self):
        async def drop_then_yes(_ask):
            self.phone.actions = {**ALL_ACTIONS, "shareFile": [False, "(sb)", []]}
            return True
        _, result = await self.send(drop_then_yes)
        self.assertEqual(result, {"error": "Galaxy S23 Ultra can't receive files right now."})
        self.assertEqual(self.phone.activated, [])

    async def test_a_failed_activate_is_not_called_sending(self):
        self.phone.activate_failure = subprocess.CalledProcessError(1, "gdbus")
        _, result = await self.send(Clicks(True))
        self.assertIn("can't say if that reached Galaxy S23 Ultra", result["error"])

    async def test_a_folder_is_not_a_file(self):
        clicks = Clicks(True)
        _, result = await self.send(clicks, target=self.home)
        self.assertIn("isn't one", result["error"])
        self.assertEqual(clicks.asked, [])

    async def test_an_unreadable_file_gets_no_card(self):
        clicks = Clicks(True)
        with mock.patch.object(phone.os, "access", return_value=False):
            _, result = await self.send(clicks)
        self.assertEqual(result, {"error": "I can't read SA100-2024.pdf."})
        self.assertEqual(clicks.asked, [])

    async def test_a_file_that_cannot_be_found_is_spoken(self):
        patcher = mock.patch.object(phone.files, "resolve", create=True,
                                    side_effect=ValueError("I can't find that file."))
        patcher.start()
        self.addCleanup(patcher.stop)
        skills = Skills()
        result = (await skills.execute("phone_send", '{"file": "x"}', Session(skills, Clicks())))[1]
        self.assertEqual(result, {"error": "I can't find that file."})

    async def test_a_device_that_cannot_receive_gets_no_card(self):
        self.bus(GALAXY, actions={})
        clicks = Clicks(True)
        _, result = await self.send(clicks)
        self.assertEqual(result, {"error": "Galaxy S23 Ultra can't receive files right now."})
        self.assertEqual(clicks.asked, [])

    async def test_a_handler_never_sends_on_its_own(self):
        self.resolving_to(self.file)
        answer = phone.send({"file": "x"}, CONFIG, Session())
        self.assertIsInstance(answer, Ask)
        self.assertEqual(self.phone.activated, [])


class Ringing(PhoneTest, unittest.IsolatedAsyncioTestCase):
    async def ring(self, arguments="{}"):
        clicks = Clicks()
        skills = Skills()
        result = (await skills.execute("phone_ring", arguments, Session(skills, clicks)))[1]
        self.assertEqual(clicks.asked, [])
        return result

    async def test_it_rings_without_a_card(self):
        bus = self.bus(GALAXY, STORMSHADOW)
        self.assertEqual(await self.ring(), {"ringing": "Galaxy S23 Ultra"})
        self.assertEqual(bus.activated, [(bus.path(GALAXY), "ring", "[]")])

    async def test_an_invented_device_rings_nothing(self):
        bus = self.bus(GALAXY)
        self.assertIn("error", await self.ring('{"device": "nokia"}'))
        self.assertEqual(bus.activated, [])

    async def test_a_device_that_cannot_ring_is_said_so(self):
        bus = self.bus(GALAXY, actions={"ring": [False, "", []]})
        self.assertEqual(await self.ring(), {"error": "Galaxy S23 Ultra can't ring right now."})
        self.assertEqual(bus.activated, [])


class Battery(PhoneTest):
    def state(self, charging, level):
        return {"battery": [True, "(bsii)",
                            [{"type": "(bsii)", "data": [charging, "icon", level, 0]}]]}

    def test_a_phrase_to_read_out(self):
        self.bus(GALAXY, actions=self.state(False, 54))
        self.assertEqual(phone.battery({}, CONFIG),
                         {"device": "Galaxy S23 Ultra", "battery": "54 percent, not charging"})
        self.bus(GALAXY, actions=self.state(True, 100))
        self.assertEqual(phone.battery({}, CONFIG)["battery"], "100 percent, charging")

    def test_reading_it_activates_nothing(self):
        bus = self.bus(GALAXY)
        phone.battery({}, CONFIG)
        self.assertEqual(bus.activated, [])

    def test_a_level_not_yet_reported_is_said_so(self):
        for actions in (self.state(False, -1), {"battery": [True, "(bsii)", []]}):
            self.bus(GALAXY, actions=actions)
            with self.assertRaisesRegex(ValueError, "hasn't reported its battery"):
                phone.battery({}, CONFIG)

    def test_a_device_without_the_plugin_is_said_so(self):
        self.bus(GALAXY, actions={})
        with self.assertRaisesRegex(ValueError, "doesn't share its battery level"):
            phone.battery({}, CONFIG)


class Sizes(unittest.TestCase):
    def test_sizes(self):
        for count, shown in ((0, "0 bytes"), (1, "1 byte"), (999, "999 bytes"), (2500, "2.5 KB"),
                             (3_400_000, "3.4 MB"), (5_000_000_000, "5.0 GB"),
                             (7_000_000_000_000, "7.0 TB")):
            self.assertEqual(phone._size(count), shown)


class Manifests(unittest.TestCase):
    def setUp(self):
        self.skills = Skills()

    def test_every_action_has_a_handler(self):
        self.assertEqual({name for name in self.skills.actions if name.startswith("phone.")},
                         set(phone.HANDLERS))

    def test_sending_reads_files_and_the_rest_ask_for_nothing(self):
        self.assertEqual(self.skills.actions["phone.send"]["permissions"], {"files_read": ["~"]})
        self.assertEqual(self.skills.actions["phone.send"]["risk"], "medium")
        for name in ("phone.ring", "phone.battery"):
            self.assertEqual(self.skills.actions[name]["permissions"], {})
            self.assertEqual(self.skills.actions[name]["risk"], "low")

    def test_the_model_sees_few_parameters(self):
        tools = {tool["name"]: tool["parameters"] for tool in self.skills.tools()}
        self.assertEqual(set(tools["phone_send"]["properties"]), {"file", "device"})
        self.assertEqual(tools["phone_send"]["required"], ["file"])
        for name in ("phone_ring", "phone_battery"):
            self.assertEqual(set(tools[name]["properties"]), {"device"})
            self.assertEqual(tools[name]["required"], [])


BUS_CONFIG = """<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""
FAKE_ID = "fakephone0000"
AWKWARD_NAME = "it's \"a\" 100% \\ né.pdf"
OBJECT_MANAGER = """<node><interface name="org.freedesktop.DBus.ObjectManager">
  <method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
</interface></node>"""


@unittest.skipUnless(shutil.which("dbus-run-session") and shutil.which("busctl")
                     and shutil.which("gdbus"), "needs dbus-run-session, busctl and gdbus")
class PrivateBus(unittest.TestCase):
    """The parts a fake can't prove: that real busctl output parses, and that real gdbus gets a
    parameter past GLib's own action export, which is what GSConnect uses."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "bus.conf"
            config.write_text(BUS_CONFIG)
            # A runtime dir of its own, so even a busctl that ignored the address finds no bus.
            env = {**os.environ, "XDG_RUNTIME_DIR": folder,
                   "REAL_BUS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")}
            done = subprocess.run(
                ["dbus-run-session", f"--config-file={config}", "--", sys.executable, __file__,
                 "private-bus"], env=env, capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise AssertionError(f"private bus run failed: {done.stderr[-2000:]}")
        cls.out = json.loads(done.stdout)

    def test_no_gsconnect_is_an_empty_list(self):
        self.assertEqual(self.out["before"], [])

    def test_real_busctl_output_parses(self):
        self.assertEqual(self.out["devices"], [{"id": FAKE_ID, "name": "Fake Phone", "type": "phone",
                                                "connected": True, "paired": True}])

    def test_battery(self):
        self.assertEqual(self.out["battery"], {"device": "Fake Phone",
                                               "battery": "71 percent, charging"})

    def test_ring_reaches_the_action(self):
        self.assertEqual(self.out["ring"], {"ringing": "Fake Phone"})
        self.assertEqual(self.out["received"][0], ["ring", None])

    def test_the_path_survives_the_trip(self):
        self.assertEqual(self.out["result"], {"sending": AWKWARD_NAME, "device": "Fake Phone"})
        action, (path, opened) = self.out["received"][1]
        self.assertEqual((action, Path(path).name, opened), ("shareFile", AWKWARD_NAME, False))
        self.assertEqual(path, self.out["sent"])

    def test_a_disabled_action_is_refused_and_nothing_arrives(self):
        self.assertEqual(self.out["disabled"], "Fake Phone can't receive files right now.")
        self.assertEqual(len(self.out["received"]), 2)


def private_bus() -> dict:
    """Runs inside dbus-run-session, from PrivateBus. Every check here that exits comes before
    anything is activated, so a mistake in the setup can only end the test."""
    import threading

    from gi.repository import Gio, GLib

    address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    if "REAL_BUS" not in os.environ or address == os.environ["REAL_BUS"]:
        sys.exit("refusing to run outside a private bus")
    out = {"before": phone.devices()}
    if out["before"]:
        sys.exit("this bus already has GSConnect on it")
    os.environ["HOME"] = os.environ["XDG_RUNTIME_DIR"]  # set late: imports need the real one

    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    path = f"{phone.ROOT}/Device/{FAKE_ID}"
    received = []

    def activated(action, parameter):
        value = parameter.unpack() if parameter else None
        if value:   # read it the way GSConnect does
            value = (Gio.File.new_for_uri(value[0]).get_path(), value[1])
        received.append([action.get_name(), value])

    group = Gio.SimpleActionGroup()
    share = Gio.SimpleAction.new("shareFile", GLib.VariantType("(sb)"))
    ring = Gio.SimpleAction.new("ring", None)
    for action in (share, ring):
        action.connect("activate", activated)
        group.add_action(action)
    group.add_action(Gio.SimpleAction.new_stateful(
        "battery", GLib.VariantType("(bsii)"), GLib.Variant("(bsii)", (True, "icon", 71, 900))))
    bus.export_action_group(path, group)

    variant = GLib.Variant
    properties = {"Id": variant("s", FAKE_ID), "Name": variant("s", "Fake Phone"),
                  "Type": variant("s", "phone"), "Connected": variant("b", True),
                  "Paired": variant("b", True)}

    def managed(_bus, _sender, _path, _interface, _method, _parameters, invocation):
        invocation.return_value(variant("(a{oa{sa{sv}}})", ({path: {phone.DEVICE: properties}},)))

    interface = Gio.DBusNodeInfo.new_for_xml(OBJECT_MANAGER).interfaces[0]
    bus.register_object(phone.ROOT, interface, managed, None, None)
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                  "RequestName", variant("(su)", (phone.NAME, 4)), None,
                  Gio.DBusCallFlags.NONE, -1, None)
    loop = GLib.MainLoop()
    thread = threading.Thread(target=loop.run)
    thread.start()
    try:
        out["devices"] = phone.devices()
        if [entry["id"] for entry in out["devices"]] != [FAKE_ID]:
            sys.exit("that is not the stand-in")
        out["battery"] = phone.battery({}, CONFIG)
        out["ring"] = phone.ring({"device": "my phone"}, CONFIG)
        target = Path.home() / AWKWARD_NAME
        target.write_bytes(b"x" * 2048)
        ask = phone.send({"file": str(target)}, CONFIG, Session())
        out["sent"] = str(target.resolve())
        out["result"] = ask.run()
        share.set_enabled(False)
        try:
            ask.run()
        except ValueError as e:
            out["disabled"] = str(e)
        out["received"] = received
    finally:
        loop.quit()
        thread.join()
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["private-bus"]:
        print(json.dumps(private_bus()))
    else:
        unittest.main()
