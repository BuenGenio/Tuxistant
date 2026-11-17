"""The whole D-Bus interface, on a private bus from dbus-run-session.

The test class below re-runs this file under dbus-run-session. The service tests only run there,
and check that the bus really is not the user's before touching anything.
"""
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from dbus_fast import DBusError, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from dbus_fast.service import signal as dbus_signal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.test_daemon_fakes import CHUNK, FakeServer, FakeSpeech, FakeSpeaker, FakeMic, tone  # noqa: E402
from tests.test_daemon_fakes import CountingSkills  # noqa: E402
from tuxistant import BUS_NAME, BUS_PATH, controller, service, themes, timers  # noqa: E402
from tuxistant.config import CLOUD_CONSENT_VERSION, NOT_THE_CONSENT, Config, cloud_in_use  # noqa: E402
from tuxistant.gate import Ask  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.service import Daemon, serve  # noqa: E402
from tuxistant.browse import NO_REGISTRY, Browse  # noqa: E402
from tuxistant.registry import INDEX_NS, NOT_LISTED, RECIPE_NS, REVOCATIONS_NS  # noqa: E402
from tuxistant.skillhost import SkillHost  # noqa: E402
from tuxistant.skills import BUILTIN  # noqa: E402

PRIVATE = "TUXISTANT_TEST_PRIVATE_BUS"
REAL_BUS = "TUXISTANT_TEST_REAL_BUS"

METHODS = {
    "Toggle": ("", ""), "HoldStart": ("", ""), "HoldEnd": ("", ""), "Cancel": ("", ""),
    "DictateStart": ("", ""), "DictateEnd": ("", ""), "DictateToggle": ("", ""),
    "CanType": ("", "b"), "Typed": ("ss", ""),
    "GetState": ("", "s"), "GetConfig": ("", "s"), "SetConfig": ("ss", ""),
    "ListVoices": ("", "s"), "PreviewVoice": ("s", ""), "Say": ("s", ""),
    "ListThemes": ("", "s"), "PreviewTheme": ("s", ""), "ListSkills": ("", "s"),
    "ReloadSkills": ("", "s"), "SkillProblems": ("", "s"), "SkillDetails": ("s", "s"),
    "GetHistory": ("i", "s"), "ClearHistory": ("", ""), "OpenSettings": ("", ""),
    "ListPhones": ("", "s"), "ListCalendars": ("", "s"),
    "Hello": ("i", ""), "CanAsk": ("", "b"), "Answer": ("sb", ""),
    "SelectionText": ("ss", ""),
    "BrowseSkills": ("", "s"), "BrowseSkill": ("s", "s"), "FetchSkill": ("s", "s"),
    "InstallSkill": ("s", "s"), "UpdateSkill": ("s", "s"), "RemoveSkill": ("s", "s"),
    "CheckRegistry": ("", "s"),
}
SIGNALS = {
    "StateChanged": "s", "Presence": "b", "Level": "d", "Transcript": "sb", "Reply": "s",
    "Error": "s", "ConfigChanged": "s", "ThemePreview": "s", "HistoryChanged": "",
    "SkillsChanged": "",
    "Approval": "ssssi", "ApprovalClosed": "s", "SelectionWanted": "s",
    "DictationStarted": "s", "DictationMode": "ss", "TypeText": "ssb", "FollowUp": "ssi",
    "RegistryChanged": "",
}


class RunsOnPrivateBus(unittest.TestCase):
    @unittest.skipIf(os.environ.get(PRIVATE), "already on the private bus")
    def test_service(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, PRIVATE: "1", REAL_BUS: os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
                   "XDG_CONFIG_HOME": f"{folder}/config", "XDG_DATA_HOME": f"{folder}/data",
                   "XDG_CACHE_HOME": f"{folder}/cache", "XDG_STATE_HOME": f"{folder}/state",
                   "TUXISTANT_FN_SOCKET": f"{folder}/no-helper.sock"}
            done = subprocess.run(
                ["dbus-run-session", "--", sys.executable, "-m", "unittest",
                 "tests.test_daemon_dbus.Service", "tests.test_daemon_dbus.Process",
                 "tests.test_daemon_dbus.Lock", "tests.test_daemon_dbus.OnlyTheShellClicks",
                 "tests.test_daemon_dbus.OnlyTheShellTypes", "tests.test_daemon_dbus.Timers",
                 "tests.test_daemon_dbus.OnlyTheShellInstalls"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])
        self.assertIn("OK", done.stderr)
        self.assertNotIn("skipped", done.stderr.splitlines()[-1])


class SettingsStandIn(unittest.TestCase):
    """Settings is tested against a stand-in daemon, so a signature that differs there from the
    daemon's would pass Settings' tests and fail on the real bus."""

    def test_it_serves_the_daemons_signatures(self):
        from tests.shell_harness_settings_fake import Daemon as StandIn
        with tempfile.TemporaryDirectory() as folder:
            stand_in = StandIn(Path(folder))
            stand_in.history.db.close()
        interface = stand_in.introspect()
        methods = {m.name: (m.in_signature, m.out_signature) for m in interface.methods}
        signals = {s.name: s.signature for s in interface.signals}
        self.assertEqual({name: METHODS.get(name) for name in methods}, methods)
        self.assertEqual({name: SIGNALS.get(name) for name in signals}, signals)
        browse = {"BrowseSkills", "BrowseSkill", "FetchSkill", "InstallSkill", "UpdateSkill",
                  "RemoveSkill", "CheckRegistry"}
        self.assertLessEqual(browse, set(methods))
        self.assertIn("RegistryChanged", signals)


class BlockedMic:
    """A microphone that opens and has nothing to say."""

    async def start(self):
        pass

    async def stop(self):
        pass

    async def read(self):
        await asyncio.Event().wait()


class OnPrivateBus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        address = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        if not address or address == os.environ[REAL_BUS]:
            raise RuntimeError("refusing to run on the real session bus")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class Service(OnPrivateBus):
    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.http = FakeSpeech(key="secret-key")
        self.addCleanup(self.http.close)
        self.config.values.update(server=f"127.0.0.1:{self.http.port}", api_key="secret-key",
                                  listen_timeout_s=30)
        self.history = History(self.folder / "history.db")
        self.addCleanup(self.history.db.close)
        self.journal = []
        self.speakers = []
        self.daemon = Daemon(self.config, self.history, CountingSkills(),
                             mic_factory=lambda: FakeMic(self.journal, tone(1.0)),
                             speaker_factory=self.new_speaker)
        self.bus = await MessageBus().connect()
        self.assertTrue(await serve(self.bus, self.daemon))
        self.addCleanup(self.bus.disconnect)
        self.addAsyncCleanup(self.daemon.controller.cancel)
        self.client = await MessageBus().connect()
        self.addCleanup(self.client.disconnect)
        self.introspection = await self.client.introspect(BUS_NAME, BUS_PATH)
        proxy = self.client.get_proxy_object(BUS_NAME, BUS_PATH, self.introspection)
        self.iface = proxy.get_interface(BUS_NAME)
        self.received = []
        for name, handler in (("state_changed", "StateChanged"), ("presence", "Presence"),
                              ("level", "Level"), ("transcript", "Transcript"),
                              ("reply", "Reply"), ("error", "Error"),
                              ("config_changed", "ConfigChanged"),
                              ("theme_preview", "ThemePreview"),
                              ("history_changed", "HistoryChanged")):
            getattr(self.iface, f"on_{name}")(
                lambda *args, signal=handler: self.received.append((signal, *args)))

    def new_speaker(self, on_level):
        speaker = FakeSpeaker(self.journal, on_level)
        self.speakers.append(speaker)
        return speaker

    def got(self, name: str) -> list:
        return [s[1:] for s in self.received if s[0] == name]

    async def signal(self, *wanted):
        async with asyncio.timeout(5):
            while wanted not in self.received:
                await asyncio.sleep(0.002)

    async def test_interface_matches_the_contract(self):
        interface = next(i for i in self.introspection.interfaces if i.name == BUS_NAME)
        methods = {m.name: (m.in_signature, m.out_signature) for m in interface.methods}
        self.assertEqual(methods, METHODS)
        self.assertEqual({s.name: s.signature for s in interface.signals}, SIGNALS)

    async def test_get_state(self):
        self.assertEqual(await self.iface.call_get_state(), "idle")

    async def test_get_config_never_has_the_key(self):
        text = await self.iface.call_get_config()
        self.assertNotIn("secret-key", text)
        values = json.loads(text)
        self.assertNotIn("api_key", values)
        self.assertIs(values["api_key_set"], True)

    async def test_get_config_never_has_the_home_assistant_token(self):
        token = "eyJ-a-long-lived-token"
        await self.iface.call_set_config("ha_server", '"ha.lan:8123"')
        await self.iface.call_set_config("ha_token", json.dumps(token))
        text = await self.iface.call_get_config()
        self.assertNotIn(token, text)
        self.assertNotIn("ha_token", {key for key in json.loads(text) if key != "ha_token_set"})
        self.assertIs(json.loads(text)["ha_token_set"], True)
        for bad in ("two words", "a\nb"):
            with self.subTest(bad), self.assertRaises(DBusError) as refused:
                await self.iface.call_set_config("ha_token", json.dumps(bad))
            self.assertNotIn(bad, refused.exception.text)
        await self.iface.call_set_config("ha_server", '"127.0.0.1:4444"')
        self.assertEqual(self.config["ha_token"], "", "a new address does not inherit it")
        self.assertIs(json.loads(await self.iface.call_get_config())["ha_token_set"], False)

    async def test_set_config(self):
        await self.iface.call_set_config("voice", '"bm_lewis"')
        self.assertEqual(self.config["voice"], "bm_lewis")
        await self.signal("ConfigChanged", "voice")
        self.assertEqual(json.loads((self.folder / "config.json").read_text())["voice"], "bm_lewis")

    async def test_set_config_rejects_bad_values(self):
        for key, value in (("voice", "5"), ("nonsense", "1"), ("voice", "{not json"),
                           ("conversation_mode", '"yes"'), ("listen_timeout_s", "true"),
                           ("fn_hold_ms", "-1")):
            with self.subTest(key=key, value=value), self.assertRaises(DBusError):
                await self.iface.call_set_config(key, value)
        self.assertEqual(self.got("ConfigChanged"), [])

    async def test_cloud_is_used_only_with_an_agreement_made_over_set_config(self):
        await self.iface.call_set_config("cloud_api_key", '"sk-cloud"')
        await self.iface.call_set_config("cloud_enabled", "true")
        self.assertFalse(cloud_in_use(self.config), "the switch alone does nothing")
        asked = {"version": CLOUD_CONSENT_VERSION, "provider": "Anthropic",
                 "model": "claude-opus-5-5"}
        for bad in ({**asked, "version": CLOUD_CONSENT_VERSION + 1}, {**asked, "date": "x"}):
            with self.subTest(bad), self.assertRaises(DBusError) as refused:
                await self.iface.call_set_config("cloud_consent", json.dumps(bad))
            self.assertEqual(refused.exception.text, NOT_THE_CONSENT)
        self.assertFalse(cloud_in_use(self.config))
        await self.iface.call_set_config("cloud_consent", json.dumps(asked))
        await self.signal("ConfigChanged", "cloud_consent")
        self.assertTrue(cloud_in_use(self.config))
        shown = json.loads(await self.iface.call_get_config())
        self.assertEqual(shown["cloud_consent"], {**asked, "date": date.today().isoformat()})
        self.assertNotIn("sk-cloud", json.dumps(shown))
        await self.iface.call_set_config("cloud_consent", "{}")
        self.assertFalse(cloud_in_use(self.config))
        self.assertIs(json.loads(await self.iface.call_get_config())["cloud_enabled"], False)

    async def test_list_voices(self):
        voices = json.loads(await self.iface.call_list_voices())
        self.assertIn("af_nicole", [v["id"] for v in voices])

    async def test_list_themes(self):
        answer = json.loads(await self.iface.call_list_themes())
        self.assertEqual(set(answer), {"themes", "problems"})

    async def test_preview_theme(self):
        await self.iface.call_preview_theme("waves")
        await self.signal("ThemePreview", "waves")

    async def test_list_skills(self):
        skills = json.loads(await self.iface.call_list_skills())
        self.assertIn("org.tuxistant.time", [s["id"] for s in skills])

    async def test_skill_details(self):
        found = json.loads(await self.iface.call_skill_details("org.tuxistant.time"))
        self.assertEqual(found["actions"][0]["tool"]["name"], "time_now")
        self.assertIn("time.now", found["manifest"])
        self.assertIsNone(json.loads(await self.iface.call_skill_details("org.example.none")))

    async def test_history(self):
        self.history.add("heard", "reply", [{"name": "time.now"}], "ok")
        rows = json.loads(await self.iface.call_get_history(10))
        self.assertEqual(rows[0]["heard"], "heard")
        await self.iface.call_clear_history()
        await self.signal("HistoryChanged")
        self.assertEqual(json.loads(await self.iface.call_get_history(10)), [])

    async def test_say(self):
        await self.iface.call_say("One. Two.")
        await self.signal("StateChanged", "idle")
        await self.signal("Presence", False)    # sent just after idle, so it can still be on its way
        self.assertEqual([r["input"] for r in self.http.requests], ["One.", "Two."])
        self.assertEqual(self.got("Presence"), [(True,), (False,)])
        self.assertTrue(self.speakers[0].drained)

    async def test_preview_voice(self):
        await self.iface.call_preview_voice("bm_fable")
        await self.signal("StateChanged", "idle")
        self.assertEqual([(r["input"], r["voice"]) for r in self.http.requests],
                         [("Hi, I'm Tux.", "bm_fable"), ("This is how I sound.", "bm_fable")])
        self.assertEqual(self.config["voice"], "af_nicole")

    async def test_a_failed_preview_comes_back_as_an_error(self):
        self.config.values["api_key"] = "wrong"
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            with self.assertRaisesRegex(DBusError, "API key"):
                await self.iface.call_preview_voice("bm_fable")
        self.assertEqual(self.got("Error"), [("The server rejected the API key",)])

    async def test_a_preview_replies_only_once_it_was_spoken(self):
        await self.iface.call_preview_voice("bm_fable")
        self.assertTrue(self.speakers[0].drained)

    async def test_set_config_refuses_a_bad_server_and_key(self):
        for key, value in (("server", "h:99999"), ("server", ""), ("api_key", "a\nb")):
            with self.subTest(value), self.assertRaises(DBusError):
                await self.iface.call_set_config(key, json.dumps(value))
        self.assertEqual(self.config["api_key"], "secret-key")

    async def test_pointing_the_daemon_elsewhere_does_not_bring_the_key_along(self):
        other = FakeSpeech(key="secret-key")
        self.addCleanup(other.close)
        await self.iface.call_set_config("server", json.dumps(f"127.0.0.1:{other.port}"))
        self.assertIs(json.loads(await self.iface.call_get_config())["api_key_set"], False)
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await self.iface.call_say("Tell me the key")
            await self.signal("StateChanged", "idle")
        self.assertEqual(other.requests, [])

    async def test_a_fifo_in_a_theme_stops_nothing(self):
        user = self.folder / "themes"
        (user / "wedge").mkdir(parents=True)
        os.mkfifo(user / "wedge/theme.json")
        with mock.patch.object(themes, "USER", user):
            listing = json.loads(await asyncio.wait_for(self.iface.call_list_themes(), 5))
        self.assertIn("orb", [t["id"] for t in listing["themes"]])
        self.assertIn("regular file", listing["problems"][0]["error"])

    async def test_the_daemon_answers_while_themes_are_being_listed(self):
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return [], []

        with mock.patch.object(themes, "discover", slow):
            listing = asyncio.create_task(self.iface.call_list_themes())
            while not started.is_set():
                await asyncio.sleep(0.005)
            self.assertEqual(await asyncio.wait_for(self.iface.call_get_state(), 2), "idle")
            await asyncio.wait_for(self.iface.call_cancel(), 2)
            release.set()
            await listing

    async def test_a_listing_that_never_ends_is_an_error_not_a_hang(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(themes, "discover", lambda: release.wait(5)), \
                mock.patch.object(service, "LIST_THEMES_TIMEOUT_S", 0.05):
            with self.assertRaisesRegex(DBusError, "too long"):
                await self.iface.call_list_themes()

    async def test_open_settings(self):
        with mock.patch.object(self.daemon, "launch_settings", mock.AsyncMock()) as launch:
            await self.iface.call_open_settings()
        launch.assert_awaited_once()

    async def test_launch_settings_starts_and_reaps_a_process(self):
        with mock.patch.object(service.sys, "executable", "/bin/true"):
            await self.daemon.launch_settings()
        self.assertEqual(len(self.daemon.children), 1)
        await asyncio.wait(self.daemon.children, timeout=5)
        await asyncio.sleep(0)
        self.assertEqual(self.daemon.children, set())

    async def test_level_is_limited_to_about_30_hz(self):
        start = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start < 0.3:
            self.daemon.send("Level", 0.5)
            await asyncio.sleep(0.001)
        self.daemon.send("Level", 0.0)
        await self.signal("Level", 0.0)
        count = len(self.got("Level"))
        self.assertGreater(count, 5)
        self.assertLessEqual(count, 12)

    async def test_voice_turn_over_the_bus(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.send("input_audio_buffer.speech_started", audio_start_ms=1, item_id="")
            await c.committed("what time is it")
            await c.tool_call()
            await c.done()
            await c.expect("response.create")
            await c.reply("It is noon.")

        async with FakeServer(script, key="secret-key") as server:
            self.config.values["server"] = f"127.0.0.1:{server.port}"
            await self.iface.call_toggle()
            await self.signal("StateChanged", "idle")
        self.assertEqual([s[0] for s in self.got("StateChanged")],
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        self.assertEqual(self.got("Transcript"), [("what time is it", True)])
        self.assertEqual(self.got("Reply"), [("It is noon.",)])
        self.assertEqual(self.got("Presence"), [(True,), (False,)])
        self.assertGreater(len(self.got("Level")), 0)
        await self.signal("HistoryChanged")
        rows = json.loads(await self.iface.call_get_history(5))
        self.assertEqual([a["name"] for a in rows[0]["actions"]], ["time.now"])

    async def test_hold_and_cancel_over_the_bus(self):
        async def script(c):
            await c.expect("response.create")
            await c.reply("Hi.")

        self.daemon.controller.make_mic = lambda: FakeMic(self.journal, tone(1.0), block=True)
        with mock.patch.object(controller, "transcribe", mock.AsyncMock(return_value="hello")), \
                mock.patch.object(controller.knowledge, "is_fact",
                                  mock.AsyncMock(return_value=False)):
            await self.hold_and_cancel(script)

    async def hold_and_cancel(self, script):
        async with FakeServer(script, key="secret-key") as server:
            self.config.values["server"] = f"127.0.0.1:{server.port}"
            await self.iface.call_hold_start()
            await self.signal("StateChanged", "listening")
            await asyncio.sleep(0.15)
            await self.iface.call_hold_end()
            await self.signal("StateChanged", "idle")
            self.assertEqual(self.got("Presence"), [(True,), (False,)])

            await self.iface.call_toggle()
            await self.signal("StateChanged", "listening")
            await self.iface.call_cancel()
            self.assertEqual(await self.iface.call_get_state(), "idle")

    async def test_failed_turn_reports_an_error_and_keeps_serving(self):
        self.config.values["server"] = "127.0.0.1:9"
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await self.iface.call_toggle()
            await self.signal("StateChanged", "error")
            await self.signal("StateChanged", "idle")
        self.assertIn("Cannot reach", self.got("Error")[0][0])
        self.assertEqual(await self.iface.call_get_state(), "idle")

    async def test_second_owner_is_refused(self):
        other_bus = await MessageBus().connect()
        self.addCleanup(other_bus.disconnect)
        other = Daemon(self.config, self.history, CountingSkills())
        self.assertFalse(await serve(other_bus, other))


class FakeShield(ServiceInterface):
    def __init__(self, active=False):
        super().__init__(service.SHIELD_INTERFACE)
        self.active = active

    @method()
    def GetActive(self) -> "b":
        return self.active

    @dbus_signal()
    def ActiveChanged(self, active) -> "b":
        return active


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class Lock(OnPrivateBus):
    """watch_lock against a stand-in for what GNOME Shell owns."""

    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.history = History(self.folder / "history.db")
        self.addCleanup(self.history.db.close)
        self.controller = controller.Controller(self.config, self.history, CountingSkills())
        self.bus = await MessageBus().connect()
        self.addCleanup(self.bus.disconnect)

    async def shield(self, active=False) -> FakeShield:
        shield = FakeShield(active)
        bus = await MessageBus().connect()
        self.addCleanup(bus.disconnect)
        bus.export(service.SHIELD_PATH, shield)
        await bus.request_name(service.SHIELD_NAME)
        return shield

    async def locked(self, wanted: bool):
        async with asyncio.timeout(5):
            while self.controller.locked != wanted:
                await asyncio.sleep(0.002)

    async def test_follows_the_shield(self):
        shield = await self.shield()
        await service.watch_lock(self.bus, self.controller)
        self.assertFalse(self.controller.locked)
        shield.ActiveChanged(True)
        await self.locked(True)
        shield.ActiveChanged(False)
        await self.locked(False)

    async def test_a_daemon_started_on_a_locked_screen_knows(self):
        await self.shield(active=True)
        await service.watch_lock(self.bus, self.controller)
        self.assertTrue(self.controller.locked)

    async def test_no_shell_on_the_bus_is_not_a_lock(self):
        await service.watch_lock(self.bus, self.controller)
        self.assertFalse(self.controller.locked)

    async def test_a_shell_that_turns_up_later_is_followed(self):
        await service.watch_lock(self.bus, self.controller)
        shield = await self.shield()
        await asyncio.sleep(0.1)
        shield.ActiveChanged(True)
        await self.locked(True)

    async def test_another_client_cannot_unlock_it(self):
        shield = await self.shield()
        await service.watch_lock(self.bus, self.controller)
        shield.ActiveChanged(True)
        await self.locked(True)
        other = await MessageBus().connect()
        self.addCleanup(other.disconnect)
        other.send(Message.new_signal(service.SHIELD_PATH, service.SHIELD_INTERFACE,
                                      "ActiveChanged", "b", [False]))
        await asyncio.sleep(0.2)
        self.assertTrue(self.controller.locked)


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class Process(OnPrivateBus):
    """The real entry points, as separate processes on the private bus."""

    def cli(self, *args):
        return asyncio.create_subprocess_exec(
            sys.executable, "-m", "tuxistant", *args, cwd=ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def run_cli(self, *args) -> tuple[int, str, str]:
        process = await self.cli(*args)
        out, err = await asyncio.wait_for(process.communicate(), 20)
        return process.returncode, out.decode(), err.decode()

    async def daemon(self):
        process = await self.cli("daemon")
        self.addCleanup(lambda: process.returncode is None and process.kill())
        bus = await MessageBus().connect()
        self.addCleanup(bus.disconnect)
        async with asyncio.timeout(15):
            while True:
                try:
                    await bus.introspect(BUS_NAME, BUS_PATH)
                    return process
                except DBusError:
                    await asyncio.sleep(0.05)

    async def test_daemon_runs_answers_the_cli_and_stops_on_sigterm(self):
        process = await self.daemon()
        code, out, err = await self.run_cli("history")
        self.assertEqual((code, out), (0, ""), err)
        code, _, err = await self.run_cli("cancel")
        self.assertEqual(code, 0, err)

        code, _, err = await self.run_cli("daemon")
        self.assertEqual(code, 1)
        self.assertIn("already running", err)

        process.send_signal(signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(process.wait(), 10), 0)

    async def test_cli_without_a_daemon_says_so(self):
        code, _, err = await self.run_cli("toggle")
        self.assertNotEqual(code, 0)
        self.assertIn("tuxistant:", err)
        self.assertNotIn("Traceback", err)

    async def test_config_and_history_live_in_the_xdg_overrides(self):
        process = await self.daemon()
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 10)
        self.assertTrue(Path(os.environ["XDG_DATA_HOME"], "tuxistant/history.db").exists())


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class OnlyTheShellTypes(OnPrivateBus):
    """Anyone can ask to dictate. Only the shell can say the words went in, or that it can type."""

    async def asyncSetUp(self):
        history = History(self.folder / "history.db")
        self.addCleanup(history.db.close)
        self.daemon = Daemon(Config(self.folder / "config.json"), history, CountingSkills())
        self.bus = await MessageBus().connect()
        self.assertTrue(await serve(self.bus, self.daemon))
        self.addCleanup(self.bus.disconnect)
        self.client = await MessageBus().connect()
        self.addCleanup(self.client.disconnect)

    async def call(self, bus, member, signature="", body=()):
        return await bus.call(Message(destination=BUS_NAME, path=BUS_PATH, interface=BUS_NAME,
                                      member=member, signature=signature, body=list(body)))

    async def the_shell(self):
        shell = await MessageBus().connect()
        self.addCleanup(shell.disconnect)
        await shell.request_name(service.SHELL_NAME)
        while self.daemon.shell_owner != shell.unique_name:
            await asyncio.sleep(0.01)
        return shell

    async def test_a_stranger_cannot_report_words_as_typed(self):
        done = asyncio.get_running_loop().create_future()
        self.daemon.controller.typing = ("words", done)
        reply = await self.call(self.client, "Typed", "ss", ["words", "typed"])
        self.assertEqual(reply.error_name, service.DENIED)
        self.assertFalse(done.done())

    async def test_a_stranger_cannot_claim_to_type(self):
        reply = await self.call(self.client, "Hello", "i", [2])
        self.assertEqual(reply.error_name, service.DENIED)
        self.assertEqual((await self.call(self.client, "CanType")).body, [False])

    async def test_a_stranger_cannot_start_or_stop_dictation(self):
        self.daemon.controller.typist = True
        for member in ("DictateStart", "DictateEnd", "DictateToggle"):
            reply = await self.call(self.client, member)
            self.assertEqual(reply.error_name, service.DENIED, member)
        self.assertIsNone(self.daemon.controller.dictation)

    async def test_the_shell_can_start_dictation(self):
        shell = await self.the_shell()
        await self.call(shell, "Hello", "i", [2])
        self.daemon.controller.make_mic = BlockedMic
        await self.call(shell, "DictateStart")
        self.assertIsNotNone(self.daemon.controller.dictation)
        await self.daemon.controller.cancel()

    async def test_the_shortcut_says_it_is_stopped_by_the_shortcut(self):
        shell = await self.the_shell()
        heard = []
        shell.add_message_handler(
            lambda m: heard.append(m.body) if m.member == "DictationMode" else None)
        await shell.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',interface='{BUS_NAME}',member='DictationMode'"]))
        await self.call(shell, "Hello", "i", [2])
        self.daemon.controller.make_mic = BlockedMic
        for member in ("DictateToggle", "DictateStart"):
            await self.call(shell, member)
            go = self.daemon.controller.dictation
            async with asyncio.timeout(2):
                while [go.id, "shortcut"] not in heard:
                    await asyncio.sleep(0.01)
            await self.daemon.controller.cancel()

    async def test_the_words_go_to_the_shell_and_nobody_else_hears_them(self):
        shell = await self.the_shell()
        heard, overheard = [], []
        shell.add_message_handler(lambda m: heard.append(m.body) if m.member == "TypeText" else None)
        self.client.add_message_handler(
            lambda m: overheard.append(m.body) if m.member == "TypeText" else None)
        await self.client.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',interface='{BUS_NAME}'"]))
        self.daemon.send("TypeText", "words", "private words", False)
        async with asyncio.timeout(2):
            while not heard:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(heard, [["words", "private words", False]])
        self.assertEqual(overheard, [])

    async def test_saying_goodbye_stops_dictation(self):
        shell = await self.the_shell()
        await self.call(shell, "Hello", "i", [2])
        self.daemon.controller.make_mic = BlockedMic
        await self.call(shell, "DictateStart")
        await self.call(shell, "Hello", "i", [0])
        self.assertEqual((await self.call(self.client, "CanType")).body, [False])
        async with asyncio.timeout(2):
            while self.daemon.controller.active:
                await asyncio.sleep(0.01)

    async def test_the_shell_going_away_stops_dictation(self):
        shell = await self.the_shell()
        await self.call(shell, "Hello", "i", [2])
        self.daemon.controller.make_mic = BlockedMic
        await self.call(shell, "DictateStart")
        shell.disconnect()
        async with asyncio.timeout(2):
            while self.daemon.controller.active:
                await asyncio.sleep(0.01)

    async def test_level_one_is_cards_alone_and_level_two_is_both(self):
        shell = await self.the_shell()
        await self.call(shell, "Hello", "i", [1])
        self.assertEqual((await self.call(self.client, "CanAsk")).body, [True])
        self.assertEqual((await self.call(self.client, "CanType")).body, [False])
        await self.call(shell, "Hello", "i", [2])
        self.assertEqual((await self.call(self.client, "CanType")).body, [True])

    async def test_a_new_shell_has_to_say_so_again(self):
        shell = await self.the_shell()
        await self.call(shell, "Hello", "i", [2])
        shell.disconnect()
        while self.daemon.controller.typist:
            await asyncio.sleep(0.01)

    async def test_the_shell_can_report_words_as_typed(self):
        shell = await self.the_shell()
        done = asyncio.get_running_loop().create_future()
        self.daemon.controller.typing = ("words", done)
        await self.call(shell, "Typed", "ss", ["words", "pasted"])
        self.assertEqual(await done, "pasted")


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class OnlyTheShellClicks(OnPrivateBus):
    """Any program on the session bus can see a card's id. Only the shell may answer it."""

    async def asyncSetUp(self):
        history = History(self.folder / "history.db")
        self.addCleanup(history.db.close)
        self.daemon = Daemon(Config(self.folder / "config.json"), history, CountingSkills())
        self.bus = await MessageBus().connect()
        self.assertTrue(await serve(self.bus, self.daemon))
        self.addCleanup(self.bus.disconnect)
        self.client = await MessageBus().connect()
        self.addCleanup(self.client.disconnect)
        self.daemon.controller.face = True
        self.card = None
        self.daemon.controller.emit = self.spy

    def spy(self, name, *args):
        if name == "Approval":
            self.card = args[0]
        self.daemon.send(name, *args)

    async def asking(self):
        task = asyncio.create_task(self.daemon.controller.ask(Ask("Do it?", "d", dict)))
        while not self.card:
            await asyncio.sleep(0.01)
        return task

    async def stranger(self, member, signature, body, interface=BUS_NAME, bus=None):
        return await (bus or self.client).call(Message(
            destination=BUS_NAME, path=BUS_PATH, interface=interface, member=member,
            signature=signature, body=body))

    async def become_the_shell(self):
        shell = await MessageBus().connect()
        self.addCleanup(shell.disconnect)
        await shell.request_name(service.SHELL_NAME)
        while self.daemon.shell_owner != shell.unique_name:
            await asyncio.sleep(0.01)
        return shell

    async def assert_refused(self, task):
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "a stranger answered the card")
        self.daemon.controller.answer(self.card, False)
        self.assertFalse(await task)

    async def test_a_stranger_cannot_answer_a_card(self):
        task = await self.asking()
        reply = await self.stranger("Answer", "sb", [self.card, True])
        self.assertEqual(reply.message_type, MessageType.ERROR)
        self.assertEqual(reply.error_name, service.DENIED)
        await self.assert_refused(task)

    async def test_leaving_out_the_interface_is_no_way_round(self):
        task = await self.asking()
        reply = await self.stranger("Answer", "sb", [self.card, True], interface=None)
        self.assertEqual(reply.message_type, MessageType.ERROR)
        await self.assert_refused(task)

    async def test_a_stranger_cannot_claim_to_draw_cards_or_feed_the_selection(self):
        self.daemon.controller.face = False
        self.assertEqual((await self.stranger("Hello", "i", [1])).message_type, MessageType.ERROR)
        self.assertFalse(self.daemon.controller.face)
        reply = await self.stranger("SelectionText", "ss", ["x", "text"])
        self.assertEqual(reply.message_type, MessageType.ERROR)

    async def test_the_shell_can_answer(self):
        shell = await self.become_the_shell()
        self.daemon.controller.face = True
        task = await self.asking()
        reply = await self.stranger("Answer", "sb", [self.card, True], bus=shell)
        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN)
        self.assertTrue(await task)

    async def test_a_new_shell_must_say_hello_again(self):
        shell = await self.become_the_shell()
        self.assertFalse(self.daemon.controller.face)
        await self.stranger("Hello", "i", [1], bus=shell)
        self.assertTrue(self.daemon.controller.face)
        await shell.release_name(service.SHELL_NAME)
        while self.daemon.shell_owner:
            await asyncio.sleep(0.01)
        self.assertFalse(self.daemon.controller.face)


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen isn't installed")
class OnlyTheShellInstalls(OnPrivateBus):
    """Any program on the bus can ask Browse to install a skill, and so put up a card, as a
    model can. Only a click from the shell installs it."""

    async def asyncSetUp(self):
        from tests.test_registry import BLUETOOTH, NOW, dumped, fingerprint, make_key, sign
        keys = self.folder / "keys"
        keys.mkdir()
        ada, registry_key = make_key(keys, "ada"), make_key(keys, "registry")
        self.registry_key = registry_key    # moderation's too, here
        data = BLUETOOTH.format(version="1.0.0").encode()
        digest = hashlib.sha256(data).hexdigest()
        version = {"version": "1.0.0", "date": "2026-11-14T10:00:00Z", "sha256": digest,
                   "size": len(data), "key": fingerprint(ada[1]),
                   "sig": sign(ada[0], RECIPE_NS, data).decode(), "permissions": {},
                   "risk": "low"}
        index = dumped({
            "format": 1, "seq": 1, "date": "2026-11-20T09:00:00Z", "commit": "a" * 40,
            "moderation_keys": [registry_key[1]],
            "categories": [{"id": "desktop", "name": "Desktop", "tags": ["settings"]}],
            "authors": {"ada": {"status": "active", "keys": [
                {"key": ada[1], "fingerprint": fingerprint(ada[1]), "added": "2026-11-14",
                 "removed": None}]}},
            "skills": [{"id": "io.github.ada.bluetooth", "author": "ada", "name": "Bluetooth",
                        "summary": "Opens Bluetooth", "tags": ["settings"], "licence": "MIT",
                        "links": [], "versions": [version]}],
            "revocations": [], "lifted": []})
        self.served = {"index.json": index, f"recipes/{digest}.yaml": data,
                       "index.json.sig": sign(registry_key[0], INDEX_NS, index)}
        signers = self.folder / "signers"
        signers.write_text(f'tuxistant-registry namespaces="{INDEX_NS}" {registry_key[1]}\n')
        builtin, self.skills = self.folder / "builtin", self.folder / "data" / "skills"
        builtin.mkdir()
        self.skills.mkdir(parents=True)
        shutil.copy(BUILTIN / "time.yaml", builtin / "time.yaml")
        config = Config(self.folder / "config.json")
        config.values["registry_url"] = "https://registry.example/skills/"
        host = SkillHost(config, builtin, self.folder / "bundled", self.skills,
                         launcher=lambda *_: None)
        host.reload()
        history = History(self.folder / "history.db")
        self.addCleanup(history.db.close)
        self.daemon = Daemon(config, history, host)
        self.daemon.browse = Browse(config, host, self.daemon.controller, emit=self.daemon.send,
                                    state=self.folder / "state", signers=signers,
                                    fetch=self.fetch, clock=lambda: NOW)
        self.bus = await MessageBus().connect()
        self.assertTrue(await serve(self.bus, self.daemon))
        self.addCleanup(self.bus.disconnect)
        self.client = await MessageBus().connect()
        self.addCleanup(self.client.disconnect)
        self.cards = []
        self.daemon.controller.emit = self.spy
        await self.daemon.browse.check()

    def fetch(self, path, _limit):
        if path not in self.served:
            raise OSError("not found")
        return self.served[path]

    def spy(self, name, *args):
        if name == "Approval":
            self.cards.append(args[0])
        self.daemon.send(name, *args)

    async def call(self, member, signature="", body=(), bus=None):
        return await (bus or self.client).call(Message(
            destination=BUS_NAME, path=BUS_PATH, interface=BUS_NAME, member=member,
            signature=signature, body=list(body)))

    async def the_shell(self):
        shell = await MessageBus().connect()
        self.addCleanup(shell.disconnect)
        await shell.request_name(service.SHELL_NAME)
        while self.daemon.shell_owner != shell.unique_name:
            await asyncio.sleep(0.01)
        self.daemon.controller.face = True
        return shell

    async def card_for(self, task):
        async with asyncio.timeout(10):
            while not self.cards and not task.done():
                await asyncio.sleep(0.01)
        self.assertFalse(task.done(), "no card came up")
        return self.cards[-1]

    async def test_only_the_shells_click_installs(self):
        shell = await self.the_shell()
        changed = []
        await self.client.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',interface='{BUS_NAME}',member='RegistryChanged'"]))
        self.client.add_message_handler(
            lambda message: changed.append(message.member)
            if message.member == "RegistryChanged" else None)
        task = asyncio.create_task(self.call("InstallSkill", "s", ["io.github.ada.bluetooth"]))
        card = await self.card_for(task)
        refused = await self.call("Answer", "sb", [card, True])
        self.assertEqual(refused.error_name, service.DENIED)
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "a stranger answered the card")
        self.assertFalse((self.skills / "io.github.ada.bluetooth.yaml").exists())
        await self.call("Answer", "sb", [card, True], bus=shell)
        reply = await task
        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN, reply.body)
        self.assertEqual(json.loads(reply.body[0])["version"], "1.0.0")
        self.assertTrue((self.skills / "io.github.ada.bluetooth.yaml").exists())
        async with asyncio.timeout(5):
            while not changed:
                await asyncio.sleep(0.01)
        listing = json.loads((await self.call("BrowseSkills")).body[0])
        self.assertEqual(listing["skills"][0]["installed"]["version"], "1.0.0")

    async def test_a_deny_and_an_unknown_skill_come_back_as_sentences(self):
        shell = await self.the_shell()
        task = asyncio.create_task(self.call("InstallSkill", "s", ["io.github.ada.bluetooth"]))
        await self.call("Answer", "sb", [await self.card_for(task), False], bus=shell)
        reply = await task
        self.assertEqual((reply.error_name, reply.body),
                         (service.FAILED, ["You didn't allow it, so nothing was installed."]))
        reply = await self.call("BrowseSkill", "s", ["io.github.ada.other"])
        self.assertEqual((reply.error_name, reply.body), (service.FAILED, [NOT_LISTED]))

    async def test_a_switch_off_at_the_daily_check_is_a_notification_on_the_desktop(self):
        from tests.test_registry import dumped, sign
        browse = self.daemon.browse
        browse.registry.install(browse.registry.check("io.github.ada.bluetooth", "1.0.0"))
        revocations = dumped({"format": 1, "seq": 1, "date": "2026-11-20T10:00:00Z",
                              "revocations": [{
                                  "id": 1, "kind": "suspend",
                                  "scope": {"skill": "io.github.ada.bluetooth"},
                                  "reason": "Its host & owner changed.",
                                  "date": "2026-11-19T16:05:00Z", "actor": "mod",
                                  "report": None}]})
        self.served["revocations.json"] = revocations
        self.served["revocations.json.sig"] = sign(self.registry_key[0], REVOCATIONS_NS,
                                                   revocations)
        desktop = FakeDesktop()
        bus = await MessageBus().connect()
        self.addCleanup(bus.disconnect)
        bus.export("/org/freedesktop/Notifications", desktop)
        await bus.request_name("org.freedesktop.Notifications")

        async def once():
            await browse.check()
        browse.run = once       # the daily check, once and now
        await service._check_registry(browse, self.bus)
        self.assertEqual(desktop.notices, [(
            "Tuxistant switched off io.github.ada.bluetooth",
            "The skills registry has switched it off: Its host &amp; owner changed.")])

    async def test_a_failure_in_browse_is_a_sentence_and_the_daemon_carries_on(self):
        async def fails(*_args):
            raise RuntimeError("something unforeseen")
        self.daemon.browse.fetch = fails
        with self.assertLogs("tuxistant", "ERROR"):
            reply = await self.call("FetchSkill", "s", ["io.github.ada.bluetooth"])
        self.assertEqual((reply.error_name, reply.body), (service.FAILED, [service.BROWSE_FAILED]))
        self.daemon.browse.run = fails
        with self.assertLogs("tuxistant", "ERROR"):
            await service._check_registry(self.daemon.browse, self.bus)
        self.daemon.browse = None       # as for a fixed set of skills, which has no folder
        await service._check_registry(None, self.bus)
        for member in ("BrowseSkills", "CheckRegistry"):
            reply = await self.call(member)
            self.assertEqual(json.loads(reply.body[0]), service.NOTHING_HELD, member)
        for member in ("BrowseSkill", "InstallSkill"):
            reply = await self.call(member, "s", ["io.github.ada.bluetooth"])
            self.assertEqual((reply.error_name, reply.body), (service.FAILED, [NO_REGISTRY]))


class FakeDesktop(ServiceInterface):
    """What takes a notification, in place of GNOME Shell."""

    def __init__(self):
        super().__init__("org.freedesktop.Notifications")
        self.shown, self.notices = [], []

    @method()
    def Notify(self, app: "s", replaces: "u", icon: "s", summary: "s", body: "s",
               actions: "as", hints: "a{sv}", expires: "i") -> "u":
        self.shown.append(summary)
        self.notices.append((summary, body))
        return 1


@unittest.skipUnless(os.environ.get(PRIVATE), "needs dbus-run-session")
class Timers(OnPrivateBus):
    """The real run(), in this process so that its speaker can be a fake: a daemon that starts
    on a locked screen with a reminder that came due while it was away."""

    async def asyncSetUp(self):
        config, self.file = Config(), service.DATA_DIR / "timers.json"
        real = (Path.home() / ".config", Path.home() / ".local/share")
        for path, home in ((config.path, "XDG_CONFIG_HOME"), (self.file, "XDG_DATA_HOME")):
            given = Path(os.environ[home])
            if path.parent != given / "tuxistant" or given.resolve() in real or path.exists():
                raise RuntimeError("refusing to run on the real config or the real timers")
        self.http = FakeSpeech()
        self.addCleanup(self.http.close)
        # Both are taken away again: the daemons of the Process tests share these folders, and
        # theirs is the real speaker.
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path in (
            config.path, self.file, *self.file.parent.glob("timers.json.*"))])
        config.values.update(server=f"127.0.0.1:{self.http.port}", api_key="good")
        config.save()
        self.config = config
        now = time.time()
        timers.Store(self.file).add("reminder", "call mum", now - 7200, now - 9000)
        self.shield, self.desktop = FakeShield(active=True), FakeDesktop()
        for name, path, thing in (
                (service.SHIELD_NAME, service.SHIELD_PATH, self.shield),
                ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", self.desktop)):
            bus = await MessageBus().connect()
            self.addCleanup(bus.disconnect)
            bus.export(path, thing)
            await bus.request_name(name)
        self.speakers, self.said = [], []
        for patcher in (mock.patch.object(service, "START_GRACE_S", 0.05),
                        mock.patch.object(timers, "RETRY_S", 0.02)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def new_speaker(self, on_level):
        self.speakers.append(FakeSpeaker([], on_level))
        return self.speakers[-1]

    async def start(self):
        self.running = asyncio.create_task(service.run(speaker_factory=self.new_speaker))
        self.addAsyncCleanup(self.stop)
        client = await MessageBus().connect()
        self.addCleanup(client.disconnect)
        async with asyncio.timeout(10):
            while True:
                try:
                    introspection = await client.introspect(BUS_NAME, BUS_PATH)
                    break
                except DBusError:
                    await asyncio.sleep(0.02)
        iface = client.get_proxy_object(BUS_NAME, BUS_PATH, introspection).get_interface(BUS_NAME)
        iface.on_reply(self.said.append)
        await client.introspect(BUS_NAME, BUS_PATH)     # by its answer the match rule is in place

    async def stop(self):
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:     # run() never got that far
            self.running.cancel()
        elif not self.running.done():
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait({self.running}, timeout=10)

    async def until(self, condition, timeout=10):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    def saved(self) -> list:
        return [(row["label"], row["state"]) for row in json.loads(self.file.read_text())["items"]]

    async def test_as_installed_it_says_nothing_until_the_unlock(self):
        await self.start()
        await self.until(lambda: self.desktop.shown)
        await asyncio.sleep(0.3)        # a dozen retries' worth
        self.assertEqual(self.desktop.shown, ["Reminder: call mum"], "the trace, at once")
        self.assertEqual((self.said, self.speakers, self.http.requests), ([], [], []),
                         "nothing is said, played or sent to be spoken on a locked screen")
        self.assertEqual(self.saved(), [("call mum", "pending")])
        self.shield.ActiveChanged(False)
        await self.until(lambda: self.saved() == [("call mum", "done")])
        await self.until(lambda: self.said)
        self.assertRegex(self.said[0], r"^You missed a reminder .* at .*: call mum\.$")
        self.assertEqual(len(self.speakers), 1)
        self.assertEqual(self.desktop.shown, ["Reminder: call mum"], "one notification, not two")
        await self.stop()
        self.assertEqual(self.running.result(), 0)
        self.assertIsNone(timers._attached, "the model's three actions lose the store again")

    async def test_with_the_chime_on_the_locked_screen_hears_that_and_no_words(self):
        self.config.values["timer_chime"] = True
        self.config.save()
        await self.start()
        await self.until(lambda: self.speakers and self.speakers[0].drained)
        await asyncio.sleep(0.3)
        self.assertEqual([bytes(s.written) for s in self.speakers], [timers.chime()], "once")
        self.assertEqual((self.said, self.http.requests), ([], []))
        self.shield.ActiveChanged(False)
        await self.until(lambda: self.said)
        await self.until(lambda: self.saved() == [("call mum", "done")])
        self.assertNotEqual(bytes(self.speakers[1].written)[:len(timers.chime())], timers.chime(),
                            "said late, it has no chime")


if __name__ == "__main__":
    unittest.main()
