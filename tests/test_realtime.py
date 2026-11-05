"""The realtime client against a scripted server, without the controller."""
import asyncio
import base64
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import Case, CountingSkills, FakeSpeech, tone, wav  # noqa: E402
from tuxistant import realtime  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.realtime import AuthError, Realtime, RealtimeError, list_models, sentences, speech  # noqa: E402


class ClientCase(Case):
    def client(self, port, hold=False, **settings):
        config = Config(self.folder / "config.json")
        config.values.update({"server": f"127.0.0.1:{port}", "api_key": "good", **settings})
        self.skills = CountingSkills()
        self.events = asyncio.Queue()
        rt = Realtime(config, self.skills, self.events.put_nowait, hold=hold)
        self.addAsyncCleanup(rt.close)
        return rt

    async def next(self, *wanted) -> tuple:
        """The next event of those kinds, skipping the others."""
        async with asyncio.timeout(5):
            while True:
                event = await self.events.get()
                if event[0] in wanted:
                    return event

    def kinds(self) -> list[str]:
        return [e[0] for e in self.events._queue]


class Setup(ClientCase):
    async def test_session_update_in_vad_mode(self):
        server = await self.server()
        await self.client(server.port, voice="bm_lewis", instructions="Be brief").connect()
        session = server.sessions[0]
        self.assertEqual(session["type"], "realtime")
        self.assertEqual(session["instructions"], "Be brief")
        self.assertEqual(session["audio"], {"output": {"voice": "bm_lewis"}})
        self.assertLessEqual({"time_now", "disk_usage"}, {t["name"] for t in session["tools"]})

    async def test_hold_mode_turns_server_vad_off(self):
        server = await self.server()
        await self.client(server.port, hold=True).connect()
        self.assertEqual(server.sessions[0]["audio"]["input"], {"turn_detection": None})

    async def test_nothing_is_sent_before_session_created_and_early_audio_survives(self):
        server = await self.server(delay=0.1)
        rt = self.client(server.port)
        connecting = asyncio.create_task(rt.connect())
        await asyncio.sleep(0.02)
        await rt.send_audio(b"\1\2" * 500)
        await connecting
        await rt.send_audio(b"\3\4" * 500)
        await server.conn.heard_audio(2000)
        self.assertEqual(server.events_before_ready, [[], 0])
        self.assertEqual(bytes(server.conn.audio), b"\1\2" * 500 + b"\3\4" * 500)
        self.assertLess(server.conn.types().index("session.update"), len(server.conn.log))

    async def test_setup_does_not_finish_until_session_updated(self):
        async def never_updated(ws):
            await ws.send(json.dumps({"type": "session.created"}))
            await asyncio.sleep(1)

        async with websockets.serve(never_updated, "127.0.0.1", 0) as silent:
            rt = self.client(silent.sockets[0].getsockname()[1])
            with mock.patch.object(realtime, "SETUP_TIMEOUT_S", 0.1):
                with self.assertRaisesRegex(RealtimeError, "Timed out"):
                    await rt.connect()
            self.assertFalse(rt.ready)
            await rt.close()

    async def test_bad_key_is_named(self):
        server = await self.server(key="other")
        with self.assertRaisesRegex(AuthError, "API key"):
            await self.client(server.port).connect()

    async def test_server_down(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaisesRegex(RealtimeError, "Cannot reach"):
            await self.client(port).connect()

    async def test_setup_error_shows_the_server_message(self):
        server = await self.server(setup_error={
            "message": "Model is not a pipeline model", "type": "invalid_request_error",
            "code": "invalid_model"})
        with self.assertRaisesRegex(RealtimeError, "Model is not a pipeline model"):
            await self.client(server.port).connect()

    async def test_pipeline_name_is_url_quoted(self):
        server = await self.server()
        await self.client(server.port, pipeline="a b&c").connect()
        self.assertEqual(server.conn.ws.request.path, "/v1/realtime?model=a%20b%26c")


class Turns(ClientCase):
    async def turn(self, script, hold=False):
        server = await self.server(script)
        rt = self.client(server.port, hold=hold)
        await rt.connect()
        return server, rt

    async def test_events_of_a_plain_reply(self):
        pcm = tone(0.1)

        async def script(c):
            await c.send("input_audio_buffer.speech_started", audio_start_ms=1, item_id="")
            await c.committed("what time is it")
            await c.reply("It is noon.", pcm)

        await self.turn(script)
        self.assertEqual(await self.next("speech_started"), ("speech_started",))
        self.assertEqual(await self.next("committed"), ("committed",))
        self.assertEqual(await self.next("heard"), ("heard", "what time is it"))
        self.assertEqual(await self.next("audio"), ("audio", pcm))
        self.assertEqual(await self.next("reply"), ("reply", "It is noon."))
        self.assertEqual(await self.next("done"), ("done",))

    async def test_beta_event_names(self):
        async def script(c):
            await c.reply("Hi", b"\0\0", beta=True)

        await self.turn(script)
        self.assertEqual(await self.next("audio"), ("audio", b"\0\0"))
        self.assertEqual(await self.next("reply"), ("reply", "Hi"))
        await self.next("done")

    async def test_audio_in_several_deltas(self):
        async def script(c):
            for part in (b"\1\1", b"\2\2"):
                await c.send("response.output_audio.delta",
                             delta=base64.b64encode(part).decode())
            await c.done()

        await self.turn(script)
        self.assertEqual((await self.next("audio"))[1], b"\1\1")
        self.assertEqual((await self.next("audio"))[1], b"\2\2")

    async def test_tool_only_response_does_not_end_the_request(self):
        async def script(c):
            await c.tool_call()
            await c.done(output=[{"type": "function_call"}])
            output = await c.expect("conversation.item.create")
            self.assertEqual(output["item"]["type"], "function_call_output")
            self.assertEqual(output["item"]["call_id"], "call_1")
            self.assertIn("time", json.loads(output["item"]["output"]))
            await c.expect("response.create")
            await c.reply()

        server, _ = await self.turn(script)
        action = await self.next("action")
        self.assertEqual(action[1]["name"], "time.now")
        await self.next("audio")
        await self.next("done")
        self.assertEqual(self.skills.ran, ["time_now"])
        self.assertEqual(server.conn.types().count("response.create"), 1)

    async def test_no_done_until_the_follow_up_response_ends(self):
        checked = asyncio.Event()

        async def script(c):
            await c.tool_call()
            await c.done()
            await c.expect("response.create")
            await asyncio.sleep(0.05)
            self.assertNotIn("done", self.kinds())
            checked.set()
            await c.reply()

        await self.turn(script)
        await self.next("done")
        self.assertTrue(checked.is_set())

    async def test_repeated_call_id_runs_once(self):
        async def script(c):
            await c.tool_call(call_id="same")
            await c.tool_call(call_id="same")
            await c.done()
            await c.expect("response.create")
            await c.reply()

        server, _ = await self.turn(script)
        await self.next("done")
        self.assertEqual(self.skills.ran, ["time_now"])
        self.assertEqual(server.conn.types().count("conversation.item.create"), 1)

    async def test_two_calls_in_one_response_ask_for_one_reply(self):
        async def script(c):
            await c.tool_call(call_id="a")
            await c.tool_call("disk_usage", "b", '{"path": "/"}')
            await c.done()
            await c.expect("response.create")
            await c.reply()

        server, _ = await self.turn(script)
        await self.next("done")
        self.assertEqual(self.skills.ran, ["time_now", "disk_usage"])
        self.assertEqual(server.conn.types().count("conversation.item.create"), 2)
        self.assertEqual(server.conn.types().count("response.create"), 1)

    async def test_a_model_stuck_calling_tools_is_stopped(self):
        async def script(c):
            for i in range(realtime.MAX_TOOL_ROUNDS):
                await c.tool_call(call_id=f"call_{i}")
                await c.done()
                if i < realtime.MAX_TOOL_ROUNDS - 1:
                    await c.expect("response.create", i + 1)

        await self.turn(script)
        self.assertIn("kept calling tools", (await self.next("failed"))[1])

    async def test_failing_tool_is_reported_to_the_model(self):
        async def script(c):
            await c.tool_call("no_such_tool")
            await c.done()
            output = await c.expect("conversation.item.create")
            self.assertIn("error", json.loads(output["item"]["output"]))
            await c.reply()

        await self.turn(script)
        await self.next("done")

    async def test_words_go_in_before_the_reply_is_asked_for(self):
        server, rt = await self.turn(None, hold=True)
        await rt.say("what time is it")
        await server.conn.expect("response.create")
        types = server.conn.types()
        self.assertLess(types.index("conversation.item.create"), types.index("response.create"))
        self.assertNotIn("input_audio_buffer.commit", types)

    async def test_input_not_committed_as_live_server_sends_it(self):
        async def script(c):
            await c.expect("response.create")
            await c.done("failed", status_details={"type": "failed", "error": {
                "message": "no transcript", "type": "invalid_request_error",
                "code": "input_not_committed"}})

        _, rt = await self.turn(script, hold=True)
        await rt.say("hello")
        await self.next("quiet")
        self.assertNotIn("failed", self.kinds())

    async def test_input_not_committed_with_an_error_event_first(self):
        async def script(c):
            await c.expect("response.create")
            await c.send("error", error={"code": "input_not_committed", "message": "none"})
            await c.done("failed", status_details={"error": {"code": "input_not_committed"}})

        _, rt = await self.turn(script, hold=True)
        await rt.say("hello")
        await self.next("quiet")
        await self.next("quiet")
        self.assertNotIn("failed", self.kinds())

    async def test_error_event(self):
        async def script(c):
            await c.send("error", error={"message": "out of memory", "code": "server_error"})

        await self.turn(script)
        self.assertIn("out of memory", (await self.next("failed"))[1])

    async def test_failed_response(self):
        async def script(c):
            await c.done("failed", status_details={"error": {"message": "boom"}})

        await self.turn(script)
        self.assertIn("boom", (await self.next("failed"))[1])

    async def test_cancelled_response_ends_the_request(self):
        async def script(c):
            await c.done("cancelled")

        await self.turn(script)
        await self.next("done")

    async def test_malformed_event_fails_the_turn_instead_of_the_daemon(self):
        async def script(c):
            await c.send("response.output_audio.delta")     # no delta field

        await self.turn(script)
        self.assertIn("Bad message", (await self.next("failed"))[1])

    async def test_lost_connection(self):
        async def script(c):
            c.ws.transport.abort()

        await self.turn(script)
        self.assertIn("Lost the connection", (await self.next("failed"))[1])

    async def test_close_with_abort_cancels_the_response(self):
        server, rt = await self.turn(None)
        await rt.close(abort=True)
        await server.conn.expect("response.cancel")

    async def test_close_is_bounded_on_a_dead_link(self):
        _, rt = await self.turn(None)
        hung = asyncio.Event()
        rt.ws.close = hung.wait
        with mock.patch.object(realtime, "CLOSE_TIMEOUT_S", 0.02):
            await asyncio.wait_for(rt.close(), 1)
        self.assertTrue(rt.ws.transport.is_closing())

    async def test_plain_close_does_not_cancel(self):
        server, rt = await self.turn(None)
        await rt.close()
        await asyncio.sleep(0.02)
        self.assertNotIn("response.cancel", server.conn.types())


class Speech(Case):
    def config(self, port):
        config = Config(self.folder / "config.json")
        config.values.update(server=f"127.0.0.1:{port}", api_key="good")
        return config

    def test_sentences(self):
        self.assertEqual(sentences("Hi, I'm Tux. This is how I sound."),
                         ["Hi, I'm Tux.", "This is how I sound."])
        self.assertEqual(sentences("One!  Two?\nThree"), ["One!", "Two?", "Three"])
        self.assertEqual(sentences("   "), [])

    def test_run_on_sentence_is_cut_at_word_boundaries(self):
        parts = sentences("word " * 400)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= realtime.MAX_SPEECH_CHARS for p in parts))
        self.assertEqual(" ".join(parts), ("word " * 400).strip())

    async def test_speech_returns_pcm(self):
        pcm = tone(0.1)
        http = FakeSpeech(body=wav(pcm))
        self.addCleanup(http.close)
        self.assertEqual(await speech(self.config(http.port), "Hello", "bf_emma"), pcm)
        self.assertEqual(http.requests, [{"model": "tux-voice", "voice": "bf_emma", "input": "Hello"}])

    async def test_speech_errors(self):
        for fake, error in ((FakeSpeech(key="other"), "API key"),
                            (FakeSpeech(speech_status=500), "HTTP 500"),
                            (FakeSpeech(body=b"not audio"), "did not return audio"),
                            (FakeSpeech(body=wav(tone(0.1), channels=2)), "mono")):
            self.addCleanup(fake.close)
            with self.subTest(error), self.assertRaisesRegex(RealtimeError, error):
                await speech(self.config(fake.port), "Hello", "bf_emma")

    async def test_a_voice_at_another_rate_comes_out_at_the_speakers_rate(self):
        http = FakeSpeech(body=wav(tone(0.1, rate=16000), rate=16000))
        self.addCleanup(http.close)
        pcm = await speech(self.config(http.port), "Привіт", "", "voice-uk-lada-x-low")
        self.assertEqual(len(pcm), 2 * 2400)
        self.assertEqual(http.requests[0]["model"], "voice-uk-lada-x-low")

    def test_list_models(self):
        http = FakeSpeech()
        self.addCleanup(http.close)
        self.assertEqual(list_models(self.config(http.port)), ["tux-realtime", "tux-voice", "whisper-large-turbo-q5_0"])

    def test_urls(self):
        self.assertEqual(realtime._urls("box:8080"), ("http://box:8080", "ws://box:8080"))
        self.assertEqual(realtime._urls("https://box/"), ("https://box", "wss://box"))


if __name__ == "__main__":
    unittest.main()
