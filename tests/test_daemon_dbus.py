"""The whole D-Bus interface, on a private bus from dbus-run-session.

The test class below re-runs this file under dbus-run-session. The service tests only run there,
and check that the bus really is not the user's before touching anything.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
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
from tuxistant import BUS_NAME, BUS_PATH, controller, service, themes  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import Ask  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.service import Daemon, serve  # noqa: E402

PRIVATE = "TUXISTANT_TEST_PRIVATE_BUS"
REAL_BUS = "TUXISTANT_TEST_REAL_BUS"

METHODS = {
    "Toggle": ("", ""), "HoldStart": ("", ""), "HoldEnd": ("", ""), "Cancel": ("", ""),
    "DictateStart": ("", ""), "DictateEnd": ("", ""), "DictateToggle": ("", ""),
    "CanType": ("", "b"), "Typed": ("ss", ""),
    "GetState": ("", "s"), "GetConfig": ("", "s"), "SetConfig": ("ss", ""),
    "ListVoices": ("", "s"), "PreviewVoice": ("s", ""), "Say": ("s", ""),
    "ListThemes": ("", "s"), "PreviewTheme": ("s", ""), "ListSkills": ("", "s"),
    "GetHistory": ("i", "s"), "ClearHistory": ("", ""), "OpenSettings": ("", ""),
    "ListPhones": ("", "s"), "Hello": ("i", ""), "CanAsk": ("", "b"), "Answer": ("sb", ""),
    "SelectionText": ("ss", ""),
}
SIGNALS = {
    "StateChanged": "s", "Presence": "b", "Level": "d", "Transcript": "sb", "Reply": "s",
    "Error": "s", "ConfigChanged": "s", "ThemePreview": "s", "HistoryChanged": "",
    "Approval": "ssssi", "ApprovalClosed": "s", "SelectionWanted": "s",
    "DictationStarted": "s", "TypeText": "ssb",
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
                 "tests.test_daemon_dbus.OnlyTheShellTypes"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])
        self.assertIn("OK", done.stderr)
        self.assertNotIn("skipped", done.stderr.splitlines()[-1])


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
            await c.committed("hello")
            await c.reply("Hi.")

        self.daemon.controller.make_mic = lambda: FakeMic(self.journal, tone(1.0), block=True)
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


if __name__ == "__main__":
    unittest.main()
