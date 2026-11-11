"""One test or more for each failure the code review reproduced, through the real classes."""
import asyncio
import contextlib
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_audio import NEVER_READS, Tools  # noqa: E402
from tests.test_daemon_fakes import CHUNK, Case, CountingSkills, FakeSpeech, tone  # noqa: E402
from tuxistant import audio, controller, homeassistant, realtime, skills  # noqa: E402
from tuxistant.config import Config, clean_server  # noqa: E402
from tuxistant.fnclient import FnClient  # noqa: E402
from tuxistant.gate import Ask, Speak  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.realtime import Realtime, RealtimeError, _urls, list_models, speech  # noqa: E402

VOICE = tone(1.0)
SECRET = "sk-very-secret"


def spoken(c, text="do it"):
    """A script opening: the user said something and the server committed it."""
    async def run():
        await c.heard_audio(3 * CHUNK)
        await c.committed(text)
    return run()


async def raw_server(handler):
    """A WebSocket endpoint with no manners, as a context manager that yields the port."""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class ConnectFailures(Case):
    """Whatever goes wrong while connecting, the turn ends with an Error and a closed mic."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(controller, "ERROR_HOLD_S", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def ended_with_error(self, rig):
        await rig.ctl.toggle()
        await rig.finished(timeout=8)
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(len(rig.of("Error")), 1, rig.signals)
        self.assertEqual(rig.journal.count("mic.start"), rig.journal.count("mic.stop"))
        self.assertEqual(rig.history.recent()[0]["status"], "error")

    async def test_addresses_only_the_network_stack_refuses(self):
        for server in ("stormshadow..lan:8080", ".host:8080", "a" * 64 + ":8080", "myhost:99999",
                       "host:80a0"):
            with self.subTest(server):
                await self.ended_with_error(self.rig(1, server=server))

    async def test_frames_that_are_not_json_objects(self):
        for frame in ("not json", "[1, 2]", "5"):
            async def handler(ws, frame=frame):
                await ws.send(frame)
                await asyncio.sleep(5)

            server, port = await raw_server(handler)
            self.addCleanup(server.close)
            with self.subTest(frame):
                await self.ended_with_error(self.rig(port))

    async def test_a_connect_that_never_returns_is_bounded(self):
        async def forever(ws):
            await asyncio.sleep(30)

        server, port = await raw_server(forever)
        self.addCleanup(server.close)
        rig = self.rig(port)
        with mock.patch.object(controller, "CONNECT_LIMIT_S", 0.1), \
                mock.patch.object(Realtime, "connect", lambda _self: asyncio.sleep(30)):
            await self.ended_with_error(rig)

    async def test_a_released_hold_does_not_think_for_ever(self):
        rig = self.rig(1, mic_pcm=VOICE, mic={"pace": 0})
        with mock.patch.object(controller, "CONNECT_LIMIT_S", 0.3), \
                mock.patch.object(Realtime, "connect", lambda _self: asyncio.sleep(30)), \
                mock.patch.object(controller, "transcribe", mock.AsyncMock(return_value="hello")), \
                mock.patch.object(controller.knowledge, "is_fact",
                                  mock.AsyncMock(return_value=False)):
            await rig.ctl.hold_start()
            await asyncio.sleep(0.1)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.of("Error"), ["The server stopped responding"])

    async def test_flushing_early_audio_is_inside_the_setup_timeout(self):
        server = await self.server()
        config = Config(self.folder / "config.json")
        config.values.update(server=f"127.0.0.1:{server.port}", api_key="good")
        rt = Realtime(config, CountingSkills(), lambda event: None)
        self.addAsyncCleanup(rt.close)
        rt.pending += b"\0" * 100
        with mock.patch.object(rt, "_append", lambda pcm: asyncio.sleep(30)), \
                mock.patch.object(realtime, "SETUP_TIMEOUT_S", 0.2):
            with self.assertRaisesRegex(RealtimeError, "Timed out"):
                await rt.connect()

    async def test_any_mic_failure_ends_the_turn(self):
        server = await self.server()
        rig = self.rig(server.port)
        with mock.patch.object(Realtime, "send_audio", side_effect=ValueError("boom")):
            await rig.ctl.toggle()
            await rig.finished()
        self.assertEqual(len(rig.of("Error")), 1)
        self.assertNotIn("boom", rig.of("Error")[0])


class ToolFailures(Case):
    async def test_an_unexpected_exception_in_a_tool_is_an_answer(self):
        async def script(c):
            await spoken(c)
            await c.tool_call("disk_usage", arguments='{"path": "~no_such_user_xyz"}')
            await c.done()
            await c.expect("response.create")
            await c.reply("That did not work.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        started = time.monotonic()
        await rig.ctl.toggle()
        await rig.finished()
        self.assertLess(time.monotonic() - started, 5)
        row = rig.history.recent()[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["actions"][0]["name"], "disk.usage")
        self.assertIn("error", row["actions"][0]["result"])
        output = next(e for e in server.conn.log if e["type"] == "conversation.item.create")
        self.assertIn("error", output["item"]["output"])

    async def test_a_dead_reader_is_a_failure_not_a_silent_wait(self):
        async def script(c):
            await spoken(c)
            await c.tool_call()
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.object(rig.skills, "execute", side_effect=RuntimeError("bad")):
            await rig.ctl.toggle()
            await rig.finished()
        self.assertEqual(len(rig.of("Error")), 1)
        self.assertEqual(rig.history.recent()[0]["status"], "error")

    async def test_a_tool_response_that_did_not_complete_does_not_swallow_the_next_done(self):
        async def script(c):
            await spoken(c, "one")
            await c.tool_call()
            await c.done(status="incomplete")
            await c.heard_audio(len(c.audio) + 3 * CHUNK)
            await c.committed("two")
            await c.reply("Second.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, conversation_mode=True)
        await rig.ctl.toggle()
        async with asyncio.timeout(5):
            while len(rig.history.recent()) < 2:
                await asyncio.sleep(0.01)
        await rig.ctl.cancel()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(server.conn.types().count("response.create"), 0)
        self.assertEqual([r["status"] for r in rig.history.recent(2)], ["ok", "ok"])


class Recording(Case):
    """What ran belongs in the history, however the turn ended."""

    def slow(self, rig, started):
        real = rig.skills.run

        def run(name, arguments, session=None):
            started.set()
            time.sleep(0.3)
            return real(name, arguments, session)
        rig.skills.run = run

    async def test_cancelling_while_a_tool_runs_keeps_the_action(self):
        async def script(c):
            await spoken(c)
            await c.tool_call()
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        started = threading.Event()
        self.slow(rig, started)
        await rig.ctl.toggle()
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.005)
        await rig.ctl.cancel()
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["status"]), ("do it", "cancelled"))
        self.assertEqual([(a["name"], a["result"]) for a in row["actions"]], [("time.now", None)])

    async def test_a_tool_before_the_transcript_still_leaves_a_row(self):
        async def script(c):
            await c.heard_audio(3 * CHUNK)
            await c.tool_call()
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        started = threading.Event()
        self.slow(rig, started)
        await rig.ctl.toggle()
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.005)
        await rig.ctl.cancel()
        self.assertEqual([a["name"] for a in rig.history.recent()[0]["actions"]], ["time.now"])

    async def test_a_reply_timeout_keeps_the_action_too(self):
        async def script(c):
            await spoken(c)
            await c.tool_call()
            await c.done()
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.object(controller, "REPLY_TIMEOUT_S", 0.2):
            await rig.ctl.toggle()
            await rig.finished()
        row = rig.history.recent()[0]
        self.assertEqual((row["status"], [a["name"] for a in row["actions"]]),
                         ("error", ["time.now"]))


class ClosingIsNotInterrupted(Case):
    async def wait_closing(self, rig):
        turn = rig.ctl.turn
        async with asyncio.timeout(5):
            while not turn.closing:
                await asyncio.sleep(0)

    async def test_a_command_during_a_natural_end_leaves_no_socket_open(self):
        for command in ("cancel", "toggle", "hold_start"):
            with self.subTest(command):
                server = await self.server()
                rig = self.rig(server.port, listen_timeout_s=0.05)
                await rig.ctl.toggle()
                await self.wait_closing(rig)
                await getattr(rig.ctl, command)()
                await rig.ctl.cancel()
                async with asyncio.timeout(3):
                    await server.connections[0].ws.wait_closed()
                self.assertEqual(rig.ctl.state, "idle")

    async def test_the_error_survives_a_cancel_during_the_close(self):
        async def script(c):
            await c.send("error", error={"message": "server trouble"})
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port)
        await rig.ctl.toggle()
        await self.wait_closing(rig)
        await rig.ctl.cancel()
        self.assertEqual(rig.of("Error"), ["Server error: server trouble"])
        async with asyncio.timeout(3):
            await server.conn.ws.wait_closed()

    async def test_cancel_cuts_the_error_hold_short(self):
        async def script(c):
            await c.send("error", error={"message": "server trouble"})
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port)
        await rig.ctl.toggle()
        await rig.state("error")
        started = time.monotonic()
        await rig.ctl.cancel()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(rig.ctl.state, "idle")


class Locked(Case):
    async def test_nothing_starts_on_a_locked_screen(self):
        server = await self.server()
        rig = self.rig(server.port)
        await rig.ctl.set_locked(True)
        await rig.ctl.toggle()
        await rig.ctl.hold_start()
        self.assertIsNone(await rig.ctl.say("hello"))
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.signals, [])
        self.assertEqual(server.connections, [])

    async def test_holding_fn_is_ignored_too(self):
        server = await self.server()
        rig = self.rig(server.port, fn_hold_ms=1)
        client = FnClient(rig.config, rig.ctl.hold_start, rig.ctl.hold_end, path="/nonexistent")
        await rig.ctl.set_locked(True)
        await client.handle("down")
        await asyncio.sleep(0.05)
        await client.handle("up")
        self.assertEqual((rig.journal, rig.signals), ([], []))

    async def test_locking_stops_a_turn_and_its_microphone(self):
        server = await self.server()
        rig = self.rig(server.port, listen_timeout_s=30, conversation_mode=True)
        await rig.ctl.toggle()
        await rig.state("listening")
        await rig.ctl.set_locked(True)
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])

    async def test_locking_stops_speech(self):
        http = FakeSpeech(key="good")
        self.addCleanup(http.close)
        gate = asyncio.Event()
        rig = self.rig(http.port, speaker={"gate": gate})
        await rig.ctl.say("A long reply.")
        await rig.state("speaking")
        await rig.ctl.set_locked(True)
        self.assertEqual(rig.ctl.state, "idle")
        self.assertTrue(rig.speakers[0].stopped)

    async def test_it_works_again_after_unlocking(self):
        server = await self.server()
        rig = self.rig(server.port, listen_timeout_s=30)
        await rig.ctl.set_locked(True)
        await rig.ctl.set_locked(False)
        await rig.ctl.toggle()
        await rig.state("listening")


class KeyNeverLeaks(Case):
    BAD = f"{SECRET}\nsecond-line"

    async def test_not_in_the_error_signal_or_the_log_on_speech(self):
        http = FakeSpeech(key="good")
        self.addCleanup(http.close)
        rig = self.rig(http.port, api_key=self.BAD)
        with self.assertNoLogs("tuxistant", logging.ERROR):
            await rig.ctl.say("hello")
            await rig.finished()
        self.assertEqual(len(rig.of("Error")), 1)
        self.assertNotIn(SECRET, rig.of("Error")[0])

    async def test_not_in_list_models(self):
        http = FakeSpeech(key="good")
        self.addCleanup(http.close)
        config = Config(self.folder / "config.json")
        config.values.update(server=f"127.0.0.1:{http.port}", api_key=self.BAD)
        with self.assertRaises(RealtimeError) as caught:
            list_models(config)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_never_put_on_the_wire_by_the_websocket(self):
        server = await self.server(key=self.BAD)
        rig = self.rig(server.port, api_key=self.BAD)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(server.connections, [])
        self.assertNotIn(SECRET, rig.of("Error")[0])

    async def test_an_unknown_error_shows_no_text_of_its_own(self):
        with self.assertLogs("tuxistant", logging.ERROR):
            text = controller.describe(ValueError(f"header {SECRET}"))
        self.assertNotIn(SECRET, text)

    def test_config_refuses_it_without_repeating_it(self):
        config = Config(self.folder / "config.json")
        for bad in (self.BAD, f"{SECRET}\r", f"{SECRET} x", "ключ"):
            with self.subTest(bad), self.assertRaises(ValueError) as caught:
                config.set("api_key", bad)
            self.assertNotIn(SECRET, str(caught.exception))
        config.set("api_key", "Good-key_1.2/3+4=")
        self.assertEqual(config["api_key"], "Good-key_1.2/3+4=")


class Server(unittest.TestCase):
    def test_tls_is_never_dropped(self):
        for text in ("https://ai.example.com:8443/", "HTTPS://ai.example.com:8443",
                     "wss://ai.example.com:8443", "WSS://ai.example.com:8443/"):
            with self.subTest(text):
                server = clean_server(text)
                self.assertEqual(_urls(server), ("https://ai.example.com:8443",
                                                 "wss://ai.example.com:8443"))
                self.assertEqual(_urls(text), _urls(server))

    def test_no_port_stays_the_default_of_the_scheme(self):
        self.assertEqual(_urls(clean_server("https://host")), ("https://host", "wss://host"))

    def test_plain_addresses(self):
        self.assertEqual(clean_server("http://host:1/"), "host:1")
        self.assertEqual(_urls("host:1"), ("http://host:1", "ws://host:1"))

    def test_what_idna_or_the_port_parser_would_raise_on(self):
        for bad in ("stormshadow..lan:8080", ".host:8080", "a" * 64 + ":1", "a." * 130 + "b",
                    "host:99999", "host:0", "ftp://host", "host\n:1", ""):
            with self.subTest(bad):
                self.assertIsNone(clean_server(bad))


class ConfigRules(Case):
    def config(self):
        config = Config(self.folder / "config.json")
        config.values.update(server="stormshadow:8080", api_key="the-key")
        return config

    def test_set_validates_the_server(self):
        config = self.config()
        for bad in ("", "not a host !!", "h:99999"):
            with self.subTest(bad), self.assertRaises(ValueError):
                config.set("server", bad)
        self.assertEqual(config["api_key"], "the-key")

    def test_a_new_server_does_not_inherit_the_key(self):
        config = self.config()
        config.set("server", "127.0.0.1:4444")
        self.assertEqual((config["server"], config["api_key"]), ("127.0.0.1:4444", ""))

    def test_setting_the_same_server_keeps_the_key(self):
        config = self.config()
        config.set("server", "http://stormshadow:8080/")
        self.assertEqual((config["server"], config["api_key"]), ("stormshadow:8080", "the-key"))


class Listener:
    """A plain HTTP endpoint that records what reaches it."""

    def __init__(self, status=200, location=None):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                body = b'{"data": []}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class KeyStaysWithTheServer(Case):
    def listener(self, *args, **kwargs):
        listener = Listener(*args, **kwargs)
        self.addCleanup(listener.close)
        return listener

    def config(self, port):
        config = Config(self.folder / "config.json")
        config.values.update(server=f"127.0.0.1:{port}", api_key=SECRET)
        return config

    def test_http_proxies_are_ignored(self):
        proxy, target = self.listener(), self.listener()
        with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.port}",
                                          "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
                                          "no_proxy": "", "NO_PROXY": ""}):
            list_models(self.config(target.port))
        self.assertEqual(proxy.seen, [])
        self.assertEqual(target.seen, [("/v1/models", f"Bearer {SECRET}")])

    def test_http_redirects_are_refused(self):
        other = self.listener()
        redirecting = self.listener(302, f"http://127.0.0.2:{other.port}/v1/models")
        with self.assertRaisesRegex(RealtimeError, "HTTP 302"):
            list_models(self.config(redirecting.port))
        self.assertEqual(other.seen, [])

    def home(self, server):
        homeassistant._cache.clear()
        return {"ha_server": server, "ha_token": SECRET}

    def test_the_home_assistant_token_ignores_http_proxies(self):
        proxy, target = self.listener(), self.listener()
        with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{proxy.port}",
                                          "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
                                          "no_proxy": "", "NO_PROXY": ""}):
            with self.assertRaisesRegex(ConnectionError, "not the way Home Assistant does"):
                homeassistant.status({"name": "hall"}, self.home(f"127.0.0.1:{target.port}"))
        self.assertEqual(proxy.seen, [])
        self.assertEqual(target.seen, [("/api/states", f"Bearer {SECRET}")])

    def test_the_home_assistant_token_follows_no_redirect(self):
        other = self.listener()
        redirecting = self.listener(302, f"http://127.0.0.2:{other.port}/api/states")
        for act in (homeassistant.status, homeassistant.set_state):
            with self.assertRaises(Exception) as refused:
                act({"name": "hall", "state": "on"}, self.home(f"127.0.0.1:{redirecting.port}"))
            self.assertEqual(str(refused.exception), homeassistant.REDIRECTED)
        self.assertEqual(other.seen, [])

    def test_the_home_assistant_token_never_goes_over_plain_http_to_the_internet(self):
        with mock.patch.object(homeassistant._opener, "open",
                               side_effect=AssertionError("a request was made")):
            for server in ("8.8.8.8:8123", "[2606:4700::1111]:8123"):
                with self.assertRaisesRegex(ValueError, "plain http to an address on the"):
                    homeassistant.status({"name": "hall"}, self.home(server))

    def test_a_new_home_assistant_address_does_not_inherit_the_token(self):
        config = Config(self.folder / "config.json")
        config.values.update(ha_server="ha.lan:8123", ha_token=SECRET)
        config.set("ha_server", "127.0.0.1:4444")
        self.assertEqual((config["ha_server"], config["ha_token"]), ("127.0.0.1:4444", ""))
        self.assertNotIn(SECRET, json.dumps(config.public()))

    async def test_speech_redirects_are_refused(self):
        other = self.listener()
        redirecting = self.listener(302, f"http://127.0.0.2:{other.port}/")
        config = self.config(redirecting.port)
        config.values["tts_model"] = "tux-voice"
        with self.assertRaisesRegex(RealtimeError, "HTTP 302"):
            await speech(config, "secret words", "af_nicole")
        self.assertEqual(other.seen, [])

    async def test_websocket_redirects_are_refused(self):
        other = await self.server()

        def redirect(connection, _request):
            response = connection.respond(302, "elsewhere\n")
            response.headers["Location"] = f"ws://127.0.0.2:{other.port}/v1/realtime"
            return response

        server = await websockets.serve(lambda ws: asyncio.sleep(1), "127.0.0.1", 0,
                                        process_request=redirect)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        rt = Realtime(self.config(port), CountingSkills(), lambda event: None)
        self.addAsyncCleanup(rt.close)
        with self.assertRaisesRegex(RealtimeError, "HTTP 302"):
            await rt.connect()
        self.assertEqual(other.connections, [])


class Permissions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)

    def mode(self, path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_new_history_is_private_whatever_the_umask(self):
        path = self.folder / "data/tuxistant/history.db"
        history = History(path)
        self.addCleanup(history.db.close)
        history.add("heard", "reply", [], "ok")
        self.assertEqual((self.mode(path), self.mode(path.parent)), (0o600, 0o700))

    def test_an_existing_history_is_tightened(self):
        folder = self.folder / "old"
        folder.mkdir(mode=0o775)
        path = folder / "history.db"
        History(path).db.close()
        path.chmod(0o644)
        folder.chmod(0o775)
        history = History(path)
        self.addCleanup(history.db.close)
        self.assertEqual((self.mode(path), self.mode(folder)), (0o600, 0o700))


class ProcessesEnd(Tools):
    """A cancel can land on any await of a stop. The process has to end all the same."""

    def alive(self, pid: int) -> bool:
        with contextlib.suppress(OSError):
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        return False

    async def gone(self, pid: int):
        async with asyncio.timeout(3):
            while self.alive(pid):
                await asyncio.sleep(0.01)

    async def test_mic_under_a_second_cancel_at_every_tick(self):
        self.tool("pw-record", NEVER_READS)
        for ticks in range(8):
            with self.subTest(ticks=ticks):
                mic = audio.Mic()
                await mic.start()
                pid = mic.proc.pid

                async def pump():
                    try:
                        await mic.read()
                    finally:
                        await mic.stop()

                task = asyncio.create_task(pump())
                await asyncio.sleep(0.01)
                task.cancel()
                for _ in range(ticks):
                    await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.gone(pid)

    async def test_speaker_stop_under_a_cancel_at_every_tick(self):
        self.tool("pw-cat", NEVER_READS)
        for ticks in range(1, 8):       # with 0 the task would be cancelled before it starts
            with self.subTest(ticks=ticks):
                speaker = audio.Speaker()
                await speaker.start()
                pid = speaker.proc.pid
                task = asyncio.create_task(speaker.stop())
                for _ in range(ticks):
                    await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.gone(pid)
                await speaker.stop()

    async def test_the_signal_goes_out_before_anything_can_be_cancelled(self):
        class Stuck:
            returncode = None
            terminated = False

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.returncode = -9

            async def wait(self):
                await asyncio.sleep(30)

        proc = Stuck()
        with mock.patch.object(audio, "END_WAIT_S", 0.01):
            task = asyncio.create_task(audio._end(proc))
            await asyncio.sleep(0)
            self.assertTrue(proc.terminated)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with asyncio.timeout(1):
                while proc.returncode is None:      # the kill still comes
                    await asyncio.sleep(0.005)


class Gates(Case):
    """Found by the review of the skills round, as whole turns on the fake server."""

    async def test_an_action_run_by_another_has_its_own_history_record(self):
        async def outer(_args, _config, session):
            await session.execute("disk_usage", '{"path": "/"}')
            return {"finished": True}

        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await c.done()
            await c.expect("response.create")
            await c.reply("Done.")
            await c.done()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.dict(skills.HANDLERS, {"time.now": outer}):
            await rig.ctl.toggle()
            await rig.finished()
        actions = rig.history.recent()[0]["actions"]
        self.assertEqual([a["name"] for a in actions], ["time.now", "disk.usage"])
        self.assertIn("total_gb", actions[1]["result"])

    async def test_a_card_lands_on_the_action_that_asked_not_the_one_that_called_it(self):
        async def outer(_args, _config, session):
            await session.execute("disk_usage", "{}")
            return {"finished": True}

        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await c.done()
            await c.expect("response.create")
            await c.reply("Done.")
            await c.done()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        ask = lambda *_: Ask("Do it?", "the detail", lambda: {"ran": True})  # noqa: E731
        with mock.patch.dict(skills.HANDLERS, {"time.now": outer, "disk.usage": ask}):
            await rig.ctl.toggle()
            while not rig.of("Approval"):
                await asyncio.sleep(0.01)
            rig.ctl.answer(rig.of("Approval")[0][0], True)
            await rig.finished()
        outer_record, inner = rig.history.recent()[0]["actions"]
        self.assertNotIn("asked", outer_record)
        self.assertEqual(inner["asked"][0]["title"], "Do it?")
        self.assertTrue(inner["asked"][0]["allowed"])

    async def test_a_card_ended_by_a_cancel_stays_in_the_history(self):
        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await asyncio.sleep(5)

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        ask = lambda *_: Ask("Do it?", "the detail", lambda: {"ran": True})  # noqa: E731
        with mock.patch.dict(skills.HANDLERS, {"time.now": ask}):
            await rig.ctl.toggle()
            while not rig.of("Approval"):
                await asyncio.sleep(0.01)
            await rig.ctl.cancel()
        row = rig.history.recent()[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["actions"][0]["asked"][0]["allowed"], False)

    async def test_with_no_card_to_show_the_model_hears_why(self):
        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await c.done()
            await c.expect("response.create")
            await c.reply("Log out first.")
            await c.done()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        rig.ctl.face = False
        ran = []
        ask = lambda *_: Ask("Do it?", "d", lambda: ran.append(1))  # noqa: E731
        with mock.patch.dict(skills.HANDLERS, {"time.now": ask}):
            await rig.ctl.toggle()
            await rig.finished()
        self.assertEqual(ran, [])
        self.assertFalse(rig.of("Approval"))
        self.assertIn("Log out", rig.history.recent()[0]["actions"][0]["result"]["error"])

    async def test_a_voice_that_fails_leaves_the_translation_on_screen_and_the_turn_ok(self):
        def said(_args, _config, session):
            session.after.append(Speak("Привіт", "voice-uk", "voice-uk"))
            return {"done": True}

        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await c.done()
            await c.expect("response.create")
            await c.reply("Here it is.")
            await c.done()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        broken = mock.AsyncMock(side_effect=RealtimeError("Server error: HTTP 500"))
        with mock.patch.dict(skills.HANDLERS, {"time.now": said}), \
                mock.patch.object(controller, "speech", broken):
            await rig.ctl.toggle()
            await rig.finished()
        self.assertFalse(rig.of("Error"))
        self.assertIn("Привіт", rig.of("Reply"))
        self.assertEqual(rig.history.recent()[0]["status"], "ok")

    async def test_the_second_one_still_means_something_in_the_next_turn(self):
        seen = []

        def find(_args, _config, session):
            session.found = ["/home/x/a.pdf", "/home/x/b.pdf"]
            return {"count": 2}

        def then(_args, _config, session):
            seen.append(list(session.found))
            return {"ok": True}

        async def script(c):
            await spoken(c)
            await c.tool_call("time_now")
            await c.done()
            await c.expect("response.create")
            await c.reply("Two.")
            await c.done()

        async def next_script(c):
            await spoken(c)
            await c.tool_call("disk_usage")
            await c.done()
            await c.expect("response.create")
            await c.reply("Opened.")
            await c.done()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.dict(skills.HANDLERS, {"time.now": find, "disk.usage": then}):
            await rig.ctl.toggle()
            await rig.finished()
            server.script = next_script
            await rig.ctl.toggle()
            await rig.finished()
        self.assertEqual(seen, [["/home/x/a.pdf", "/home/x/b.pdf"]])


if __name__ == "__main__":
    unittest.main()
