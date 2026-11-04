"""The state machine with a scripted server and fake audio."""
import asyncio
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import CHUNK, Case, FakeSpeech, silence, tone  # noqa: E402
from tuxistant import controller  # noqa: E402

VOICE = tone(1.0)


async def vad_turn(c, text="what time is it", reply="It is noon."):
    await c.heard_audio(len(c.audio) + 3 * CHUNK)
    await c.send("input_audio_buffer.speech_started", audio_start_ms=100, item_id="")
    await c.committed(text)
    await c.reply(reply)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Vad(Case):
    async def test_full_turn(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.of("StateChanged"),
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertEqual(rig.of("Transcript"), [("what time is it", True)])
        self.assertEqual(rig.of("Reply"), ["It is noon."])
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(bytes(rig.speakers[0].written), tone(0.2))
        self.assertTrue(rig.speakers[0].drained)
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["reply"], row["actions"], row["status"]),
                         ("what time is it", "It is noon.", [], "ok"))
        self.assertEqual(rig.of("HistoryChanged"), [()])

    async def test_mic_stops_when_the_turn_commits_before_any_speech_plays(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.journal, ["mic.start", "mic.stop", "speaker.start", "speaker.drained"])

    async def test_mic_stops_on_the_transcript_if_committed_never_comes(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.transcript("hello")
            await c.reply()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertLess(rig.journal.index("mic.stop"), rig.journal.index("speaker.start"))

    async def test_silence_ends_the_turn_quietly(self):
        server = await self.server()
        rig = self.rig(server.port, listen_timeout_s=0.05)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.of("StateChanged"), ["connecting", "listening", "idle"])
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.history.recent(), [])
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertIn("mic.stop", rig.journal)

    async def test_speech_started_stops_the_listen_timeout(self):
        async def script(c):
            await c.send("input_audio_buffer.speech_started", audio_start_ms=1, item_id="")
            await asyncio.sleep(0.15)
            await c.committed("late")
            await c.reply()

        server = await self.server(script)
        rig = self.rig(server.port, listen_timeout_s=0.05)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.history.recent()[0]["heard"], "late")

    async def test_a_voice_at_the_timeout_gets_a_moment_and_a_noisy_room_has_a_limit(self):
        server = await self.server()
        rig = self.rig(server.port, mic_pcm=tone(30), listen_timeout_s=0.05)
        start = time.monotonic()
        with mock.patch.multiple(controller, VOICE_GRACE_S=0.05, MAX_LISTEN_S=0.2):
            await rig.ctl.toggle()
            await rig.finished()
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        self.assertEqual(rig.of("Error"), [])

    async def test_speech_during_connecting_reaches_the_server(self):
        server = await self.server(delay=0.1)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"pace": 0.0005}, listen_timeout_s=5)
        await rig.ctl.toggle()
        await rig.state("listening")
        await server.conn.heard_audio(len(VOICE))
        self.assertEqual(bytes(server.conn.audio[:len(VOICE)]), VOICE)
        await rig.ctl.cancel()

    async def test_toggle_while_listening_cancels_with_no_row(self):
        server = await self.server()
        rig = self.rig(server.port)
        await rig.ctl.toggle()
        await rig.state("listening")
        await rig.ctl.toggle()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.history.recent(), [])

    async def test_rapid_toggles_do_not_stack_turns(self):
        server = await self.server()
        rig = self.rig(server.port)
        await asyncio.gather(*(rig.ctl.toggle() for _ in range(5)))
        await rig.state("listening")
        await rig.ctl.cancel()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.journal.count("mic.start"), rig.journal.count("mic.stop"))
        self.assertNotIn(("mic.start", "mic.start"), zip(rig.journal, rig.journal[1:]))


class Tools(Case):
    async def test_tool_turn(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("what time is it")
            await c.tool_call()
            await c.done(output=[{"type": "function_call"}])
            await c.expect("conversation.item.create")
            await c.expect("response.create")
            await c.reply("It is noon.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.of("StateChanged"),
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        row = rig.history.recent()[0]
        self.assertEqual([a["name"] for a in row["actions"]], ["time.now"])
        self.assertIn("time", row["actions"][0]["result"])
        self.assertEqual(row["status"], "ok")

    async def test_duplicate_call_id_runs_once_and_is_recorded_once(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("hi")
            await c.tool_call(call_id="x")
            await c.tool_call(call_id="x")
            await c.done()
            await c.expect("response.create")
            await c.reply()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.skills.ran, ["time_now"])
        self.assertEqual(len(rig.history.recent()[0]["actions"]), 1)
        self.assertEqual(server.conn.types().count("conversation.item.create"), 1)

    async def test_transcript_arriving_after_the_tool_call_is_kept(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed()
            await c.tool_call()
            await c.done()
            await c.expect("response.create")
            await c.transcript("what time is it")
            await c.reply()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.history.recent()[0]["heard"], "what time is it")

    async def test_transcript_arriving_during_playback_is_kept(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed()
            await c.reply()
            await c.transcript("slow transcript")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, speaker={"play_s": 0.05})
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.history.recent()[0]["heard"], "slow transcript")

    async def test_speech_from_several_responses_joins_up(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("hi")
            await c.send("response.output_audio_transcript.done", transcript="Let me check.")
            await c.tool_call()
            await c.done()
            await c.expect("response.create")
            await c.reply("It is noon.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.history.recent()[0]["reply"], "Let me check. It is noon.")


class Hold(Case):
    async def holding(self, script, pcm=VOICE, **options):
        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=pcm, mic={"block": True}, **options)
        await rig.ctl.hold_start()
        await rig.state("listening")
        await asyncio.sleep(0.15)   # the fake mic gets through its audio
        return server, rig

    @staticmethod
    async def answer(c):
        await c.expect("response.create")
        await c.send("input_audio_buffer.committed", item_id="i")
        await c.transcript("what time is it")
        await c.reply()

    async def test_hold_turn(self):
        server, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.sessions[0]["audio"]["input"], {"turn_detection": None})
        types = server.conn.types()
        self.assertLess(types.index("input_audio_buffer.commit"), types.index("response.create"))
        self.assertEqual(rig.of("StateChanged"),
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        self.assertEqual(rig.history.recent()[0]["status"], "ok")
        self.assertEqual(rig.journal, ["mic.start", "mic.stop", "speaker.start", "speaker.drained"])

    async def test_all_the_audio_is_sent_before_the_commit(self):
        server, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.conn.audio_at_commit, len(VOICE))

    async def test_presence_goes_on_release_while_the_reply_still_speaks(self):
        _, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertLess(rig.index("Presence", False), rig.index("StateChanged", "thinking"))
        self.assertLess(rig.index("Presence", False), rig.index("Reply", "It is noon."))

    async def test_presence_stays_with_hide_on_release_off(self):
        _, rig = await self.holding(self.answer, hide_on_release=False)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertGreater(rig.index("Presence", False), rig.index("Reply", "It is noon."))

    async def test_too_short_cancels_without_sending(self):
        server, rig = await self.holding(None, pcm=tone(0.2))
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.conn.types(), ["session.update"])
        self.assertEqual(rig.of("StateChanged"), ["connecting", "listening", "idle"])
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.history.recent(), [])

    async def test_near_silence_cancels_without_sending(self):
        server, rig = await self.holding(None, pcm=tone(1.0, amplitude=20))
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.conn.types(), ["session.update"])
        self.assertEqual(rig.of("Error"), [])

    async def test_a_key_thump_in_silence_is_still_silence(self):
        server, rig = await self.holding(None, pcm=tone(0.04) + silence(1.0))
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.conn.types(), ["session.update"])

    async def test_input_not_committed_goes_quietly_to_idle(self):
        async def script(c):
            await c.expect("response.create")
            await c.send("error", error={"code": "input_not_committed", "message": "nothing"})
            await c.done("failed", status_details={"error": {"code": "input_not_committed"}})

        _, rig = await self.holding(script)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("StateChanged"), ["connecting", "listening", "thinking", "idle"])
        self.assertEqual(rig.history.recent(), [])

    async def test_release_before_the_session_is_ready(self):
        server = await self.server(self.answer, delay=0.15)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True, "pace": 0.0005})
        await rig.ctl.hold_start()
        await asyncio.sleep(0.08)
        self.assertEqual(rig.ctl.state, "connecting")
        await rig.ctl.hold_end()
        await rig.state("thinking")
        await rig.finished()
        self.assertEqual(server.conn.audio_at_commit, len(VOICE))
        self.assertEqual(rig.history.recent()[0]["status"], "ok")

    async def test_hold_end_without_a_hold_does_nothing(self):
        server = await self.server()
        rig = self.rig(server.port)
        await rig.ctl.hold_end()
        await rig.ctl.toggle()
        await rig.state("listening")
        await rig.ctl.hold_end()
        await asyncio.sleep(0.02)
        self.assertEqual(rig.ctl.state, "listening")

    async def test_a_stuck_key_is_treated_as_a_release(self):
        with mock.patch.object(controller, "MAX_HOLD_S", 0.2):
            server, rig = await self.holding(self.answer)
            await rig.finished()
        self.assertIn("input_audio_buffer.commit", server.conn.types())
        await rig.ctl.hold_end()    # the late real release finds nothing to do

    async def test_hold_start_barges_in_on_a_reply(self):
        server, rig = await self.holding(None, speaker={"gate": asyncio.Event()})
        await rig.ctl.hold_end()
        await self.answer(server.conn)
        await rig.state("speaking")
        await rig.ctl.hold_start()
        self.assertTrue(rig.speakers[0].stopped)
        await rig.state("listening")
        self.assertEqual(rig.history.recent()[0]["status"], "cancelled")
        self.assertEqual(rig.of("Presence"), [True, False, True])


class Cancel(Case):
    async def speaking(self):
        gate = asyncio.Event()      # never set: playback lasts until something stops it
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE, speaker={"gate": gate})
        await rig.ctl.toggle()
        await rig.state("speaking")
        return server, rig

    async def test_cancel_mid_speech(self):
        server, rig = await self.speaking()
        await rig.ctl.cancel()
        self.assertEqual(rig.ctl.state, "idle")
        speaker = rig.speakers[0]
        self.assertTrue(speaker.stopped)
        self.assertFalse(speaker.drained)
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["status"]), ("what time is it", "cancelled"))
        self.assertEqual(rig.of("Presence"), [True, False])
        await server.conn.expect("response.cancel")

    async def test_toggle_mid_speech_is_a_barge_in(self):
        _, rig = await self.speaking()
        await rig.ctl.toggle()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertTrue(rig.speakers[0].stopped)
        self.assertEqual(rig.history.recent()[0]["status"], "cancelled")

    async def test_cancel_while_thinking(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("hello")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.state("thinking")
        await asyncio.sleep(0.02)
        await rig.ctl.cancel()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.history.recent()[0]["status"], "cancelled")
        await server.conn.expect("response.cancel")

    async def test_cancel_while_connecting(self):
        server = await self.server(delay=0.2)
        rig = self.rig(server.port)
        await rig.ctl.toggle()
        await rig.state("connecting")
        await asyncio.sleep(0.02)
        await rig.ctl.cancel()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertEqual(rig.of("Error"), [])

    async def test_cancel_when_idle_is_harmless(self):
        rig = self.rig(free_port())
        await rig.ctl.cancel()
        self.assertEqual(rig.signals, [])


class Failures(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(controller, "ERROR_HOLD_S", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_failed(self, rig, message):
        self.assertRegex(rig.of("Error")[0], message)
        states = rig.of("StateChanged")
        self.assertEqual(states[-2:], ["error", "idle"])
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertEqual(rig.ctl.state, "idle")

    async def test_server_down_then_the_next_turn_works(self):
        rig = self.rig(free_port())
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "Cannot reach the server")
        self.assertEqual(rig.history.recent()[0]["status"], "error")

        server = await self.server(vad_turn)
        rig.config.values["server"] = f"127.0.0.1:{server.port}"
        rig.mic_pcm = VOICE
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.history.recent()[0]["status"], "ok")

    async def test_bad_key(self):
        server = await self.server(key="other")
        rig = self.rig(server.port)
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "API key")

    async def test_model_load_error_at_setup(self):
        server = await self.server(setup_error={"message": "failed to load model",
                                                 "code": "server_error"})
        rig = self.rig(server.port)
        await rig.ctl.hold_start()
        await rig.finished()
        self.assert_failed(rig, "failed to load model")

    async def test_error_event_mid_turn(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("hello")
            await c.send("error", error={"message": "out of memory", "code": "server_error"})

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "out of memory")
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["status"]), ("hello", "error"))

    async def test_lost_connection_mid_turn(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            c.ws.transport.abort()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "Lost the connection")

    async def test_server_that_goes_silent(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("hello")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.object(controller, "REPLY_TIMEOUT_S", 0.05):
            await rig.ctl.toggle()
            await rig.finished()
        self.assert_failed(rig, "stopped responding")

    async def test_dead_microphone(self):
        server = await self.server()
        rig = self.rig(server.port, mic={"fail": True})
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "microphone")

    async def test_missing_mic_tool(self):
        server = await self.server()
        rig = self.rig(server.port)

        def no_tool():
            mic = rig.__class__.new_mic(rig)
            mic.start = mock.AsyncMock(side_effect=FileNotFoundError(2, "gone", "pw-record"))
            return mic

        rig.ctl.make_mic = no_tool
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "pw-record is not installed")

    async def test_missing_speaker_tool(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE)

        def no_tool():
            speaker = rig.new_speaker(lambda value: None)
            speaker.start = mock.AsyncMock(side_effect=FileNotFoundError(2, "gone", "pw-cat"))
            return speaker

        rig.ctl.new_speaker = no_tool
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "pw-cat is not installed")

    async def test_stalled_speaker_during_drain(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE)

        def stalled():
            speaker = rig.new_speaker(lambda value: None)
            speaker.drain = mock.AsyncMock(side_effect=OSError("Nothing is playing the audio output"))
            return speaker

        rig.ctl.new_speaker = stalled
        await rig.ctl.toggle()
        await rig.finished()
        self.assert_failed(rig, "Nothing is playing")
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["status"]), ("what time is it", "error"))

    async def test_bug_in_a_turn_does_not_escape(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE)

        def broken(*_, **__):
            raise RuntimeError("kaboom")

        rig.ctl.new_speaker = broken
        with self.assertLogs("tuxistant", "ERROR"):
            await rig.ctl.toggle()
            await rig.finished()
        self.assert_failed(rig, "Something went wrong")
        self.assertNotIn("kaboom", rig.of("Error")[0])
        self.assertEqual(rig.history.recent()[0]["status"], "error")

    async def test_toggle_during_the_error_state_clears_it(self):
        rig = self.rig(free_port())
        with mock.patch.object(controller, "ERROR_HOLD_S", 5):
            await rig.ctl.toggle()
            await rig.state("error")
            await rig.ctl.toggle()
        self.assertEqual(rig.ctl.state, "idle")


class Conversation(Case):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(controller, "FOLLOW_UP_S", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_listening_reopens_then_closes_quietly(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE, conversation_mode=True)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.of("StateChanged"), ["connecting", "listening", "thinking",
                                                  "speaking", "listening", "idle"])
        self.assertEqual(len(rig.history.recent()), 1)
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("Presence"), [True, False])

    async def test_mic_only_reopens_after_playback(self):
        server = await self.server(vad_turn)
        rig = self.rig(server.port, mic_pcm=VOICE, conversation_mode=True,
                       speaker={"play_s": 0.05})
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual(rig.journal, ["mic.start", "mic.stop", "speaker.start", "speaker.drained",
                                       "mic.start", "mic.stop"])

    async def test_a_follow_up_is_a_second_row_on_the_same_session(self):
        async def script(c):
            await vad_turn(c, "first", "One.")
            await vad_turn(c, "second", "Two.")

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, conversation_mode=True)
        await rig.ctl.toggle()
        await rig.finished()
        self.assertEqual([r["heard"] for r in rig.history.recent()], ["second", "first"])
        self.assertEqual(len(server.connections), 1)

    async def test_a_hold_turn_never_reopens_the_mic(self):
        async def script(c):
            await c.expect("response.create")
            await c.reply()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True}, conversation_mode=True)
        await rig.ctl.hold_start()
        await rig.state("listening")
        await asyncio.sleep(0.15)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(rig.journal.count("mic.start"), 1)
        self.assertEqual(rig.ctl.state, "idle")


class Say(Case):
    def fake(self, **options):
        http = FakeSpeech(**options)
        self.addCleanup(http.close)
        return http

    async def test_speaks_sentence_by_sentence_in_the_configured_voice(self):
        http = self.fake()
        rig = self.rig(http.port, voice="bm_george")
        await rig.ctl.say("One. Two.")
        await rig.finished()
        self.assertEqual([(r["input"], r["voice"], r["model"]) for r in http.requests],
                         [("One.", "bm_george", "tux-voice"), ("Two.", "bm_george", "tux-voice")])
        self.assertEqual(len(rig.speakers), 1)
        self.assertEqual(len(rig.speakers[0].written), 2 * len(tone(0.1)))
        self.assertEqual(rig.of("StateChanged"), ["thinking", "speaking", "idle"])
        self.assertEqual(rig.of("Reply"), ["One. Two."])

    async def test_a_voice_can_be_given_without_changing_config(self):
        http = self.fake()
        rig = self.rig(http.port, voice="bm_george")
        await rig.ctl.say("Hi.", "af_bella")
        await rig.finished()
        self.assertEqual(http.requests[0]["voice"], "af_bella")
        self.assertEqual(rig.config["voice"], "bm_george")

    async def test_replaces_what_was_going_on(self):
        http = self.fake()
        gate = asyncio.Event()
        rig = self.rig(http.port, speaker={"gate": gate})
        await rig.ctl.say("First.")
        await rig.state("speaking")
        await rig.ctl.say("Second.")
        self.assertTrue(rig.speakers[0].stopped)
        gate.set()
        await rig.finished()
        self.assertEqual([r["input"] for r in http.requests], ["First.", "Second."])

    async def test_toggle_stops_it(self):
        http = self.fake()
        rig = self.rig(http.port, speaker={"gate": asyncio.Event()})
        await rig.ctl.say("Hello.")
        await rig.state("speaking")
        await rig.ctl.toggle()
        self.assertEqual(rig.ctl.state, "idle")
        self.assertTrue(rig.speakers[0].stopped)

    async def test_server_error_becomes_an_error_signal(self):
        http = self.fake(speech_status=500)
        rig = self.rig(http.port)
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await rig.ctl.say("Hello.")
            await rig.finished()
        self.assertRegex(rig.of("Error")[0], "HTTP 500")
        self.assertEqual(rig.ctl.state, "idle")

    async def test_is_not_history(self):
        http = self.fake()
        rig = self.rig(http.port)
        await rig.ctl.say("Hello.")
        await rig.finished()
        self.assertEqual(rig.history.recent(), [])


if __name__ == "__main__":
    unittest.main()
