"""The state machine with a scripted server and fake audio."""
import asyncio
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import CHUNK, Case, FakeSpeaker, FakeSpeech, silence, tone  # noqa: E402
from tuxistant import controller, skills  # noqa: E402
from tuxistant.gate import Speak  # noqa: E402
from tuxistant.realtime import Realtime, RealtimeError  # noqa: E402

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
    def setUp(self):
        super().setUp()
        self.heard = "what time is it"
        self.sent = []      # the audio that went to speech to text
        self.sorted = []    # what was checked for being a fact
        self.a_fact = False

        async def transcribe(_config, pcm):
            self.sent.append(pcm)
            return self.heard

        async def is_fact(heard, _config):
            self.sorted.append(heard)
            return self.a_fact

        for target, name, value in ((controller, "transcribe", transcribe),
                                    (controller.knowledge, "is_fact", is_fact)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
        await c.reply()

    async def test_hold_turn(self):
        server, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(server.sessions[0]["audio"]["input"], {"turn_detection": None})
        types = server.conn.types()
        self.assertLess(types.index("conversation.item.create"), types.index("response.create"))
        item = server.conn.log[types.index("conversation.item.create")]["item"]
        self.assertEqual(item["content"], [{"type": "input_text", "text": "what time is it"}])
        self.assertEqual(rig.of("Transcript"), [("what time is it", True)])
        self.assertEqual(rig.of("StateChanged"),
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        self.assertEqual(rig.history.recent()[0]["heard"], "what time is it")
        self.assertEqual(rig.history.recent()[0]["status"], "ok")
        self.assertEqual(rig.journal, ["mic.start", "mic.stop", "speaker.start", "speaker.drained"])

    async def test_the_server_replies_to_any_commit_so_none_is_sent(self):
        server, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertNotIn("input_audio_buffer.commit", server.conn.types())
        self.assertEqual(bytes(server.conn.audio), b"")

    async def test_all_the_audio_is_heard(self):
        _, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(self.sent, [VOICE])

    async def test_only_what_was_said_is_sorted(self):
        _, rig = await self.holding(self.answer)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(self.sorted, ["what time is it"])

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

    async def test_no_words_go_quietly_to_idle(self):
        self.heard = "[BLANK_AUDIO]"
        server, rig = await self.holding(None)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("StateChanged"), ["connecting", "listening", "thinking", "idle"])
        self.assertEqual(rig.history.recent(), [])
        self.assertEqual(self.sorted, [])
        self.assertNotIn("response.create", server.conn.types())

    async def test_speech_to_text_failing_is_an_error(self):
        async def fails(_config, _pcm):
            raise RealtimeError("Cannot reach the server at home")

        server, rig = await self.holding(None)
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01), \
                mock.patch.object(controller, "transcribe", fails):
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.of("Error"), ["Cannot reach the server at home"])
        self.assertNotIn("response.create", server.conn.types())

    async def test_cancel_while_the_words_are_being_got(self):
        started, stay = asyncio.Event(), asyncio.Event()

        stopped = []

        async def slow(_config, _pcm):
            started.set()
            try:
                await stay.wait()
            except asyncio.CancelledError:
                stopped.append(True)
                raise
            return "what time is it"

        server, rig = await self.holding(None)
        with mock.patch.object(controller, "transcribe", slow):
            await rig.ctl.hold_end()
            await started.wait()
            await rig.ctl.cancel()
        self.assertEqual(stopped, [True])
        self.assertEqual(rig.ctl.state, "idle")
        self.assertNotIn("response.create", server.conn.types())
        self.assertEqual(rig.of("Error"), [])

    async def test_a_session_that_fails_does_not_lose_a_fact_on_its_way(self):
        self.a_fact = True
        go = asyncio.Event()

        async def answer(_args, config, session):
            await go.wait()
            session.after.append(Speak("Paris.", config["voice"]))
            return {"done": True}

        server, rig = await self.holding(None)
        with mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}), \
                mock.patch.object(controller, "speech", mock.AsyncMock(return_value=tone(0.1))):
            await rig.ctl.hold_end()
            await rig.state("thinking")
            await asyncio.sleep(0.02)
            await server.conn.ws.close()
            await asyncio.sleep(0.05)
            go.set()
            await rig.finished()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("Reply"), ["Paris."])

    async def test_a_reply_that_fails_does_not_lose_what_the_app_was_to_read_out(self):
        """The app had its answer, the model's own reply then failed, and the answer went with
        it: the user heard only the error."""
        async def answer(_args, config, session):
            session.after.append(Speak("Astatine.", config["voice"]))
            return {"done": True}

        async def script(c):
            await c.expect("response.create")
            await c.tool_call("research_run", "a", '{"question": "the rarest element"}')
            await c.done()
            await c.expect("response.create", 2)
            await c.done("failed", status_details={"error": {"message": "Out of memory"}})

        speech = mock.AsyncMock(return_value=tone(0.1))
        server, rig = await self.holding(script)
        with mock.patch.dict(skills.HANDLERS, {"research.run": answer}), \
                mock.patch.object(controller, "speech", speech), \
                mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.of("Reply"), ["Astatine."])
        self.assertEqual(speech.await_args.args[1], "Astatine.")
        self.assertEqual(len(rig.of("Error")), 1)

    async def test_a_fact_that_fails_itself_is_still_an_error_after_the_session_goes(self):
        self.a_fact = True
        go = asyncio.Event()

        async def answer(_args, _config, _session):
            await go.wait()
            raise RealtimeError("Cannot reach the server at home")

        server, rig = await self.holding(None)
        with mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}), \
                mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await rig.ctl.hold_end()
            await rig.state("thinking")
            await asyncio.sleep(0.02)
            await server.conn.ws.close()
            await asyncio.sleep(0.05)
            go.set()
            await rig.finished()
        self.assertEqual(rig.of("Error"), ["Cannot reach the server at home"])

    async def test_words_that_get_no_reply_still_leave_a_row(self):
        async def script(c):
            await c.expect("response.create")
            await c.done("failed", status_details={"error": {"code": "input_not_committed"}})

        _, rig = await self.holding(script)
        await rig.ctl.hold_end()
        await rig.finished()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.history.recent()[0]["heard"], "what time is it")

    async def test_a_server_that_opens_and_goes_silent_is_noticed_after_a_release(self):
        server = await self.server(None, delay=0.1)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True, "pace": 0.0005})
        with mock.patch.object(controller, "REPLY_TIMEOUT_S", 0.2), \
                mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            await rig.ctl.hold_start()
            await asyncio.sleep(0.05)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.of("Error"), ["The server stopped responding"])

    async def test_the_connection_limit_still_holds_after_the_words_are_in(self):
        rig = self.rig(1, mic_pcm=VOICE, mic={"pace": 0})
        with mock.patch.object(controller, "CONNECT_LIMIT_S", 0.3), \
                mock.patch.object(controller, "ERROR_HOLD_S", 0.01), \
                mock.patch.object(Realtime, "connect", lambda _self: asyncio.sleep(30)):
            await rig.ctl.hold_start()
            await asyncio.sleep(0.1)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(self.sorted, ["what time is it"])
        self.assertEqual(rig.of("Error"), ["The server stopped responding"])

    async def asking_a_fact(self, handler, **options):
        """A hold turn on a question of fact, with handler as knowledge.answer."""
        self.a_fact = True
        speech = mock.AsyncMock(return_value=tone(0.1))
        for patcher in (mock.patch.dict(skills.HANDLERS, {"knowledge.answer": handler}),
                        mock.patch.object(controller, "speech", speech)):
            patcher.start()
            self.addCleanup(patcher.stop)
        server, rig = await self.holding(None, **options)
        await rig.ctl.hold_end()
        await rig.finished()
        return server, rig, speech

    async def test_a_fact_never_reaches_the_voice_model(self):
        self.heard = "What is the rarest element?"
        asked = []

        async def answer(args, config, session):
            asked.append(args["question"])
            session.after.append(Speak("Astatine.", config["voice"]))
            return {"done": True}

        server, rig, speech = await self.asking_a_fact(answer)
        self.assertEqual(asked, ["What is the rarest element?"])
        self.assertEqual(server.conn.types(), ["session.update"])
        self.assertEqual(speech.await_args.args[1], "Astatine.")
        self.assertEqual(rig.of("Reply"), ["Astatine."])
        self.assertEqual(rig.of("Error"), [])
        row = rig.history.recent()[0]
        self.assertEqual((row["heard"], row["reply"], row["status"]),
                         ("What is the rarest element?", "Astatine.", "ok"))
        self.assertEqual([a["name"] for a in row["actions"]], ["knowledge.answer"])

    async def test_an_answer_is_shown_even_after_the_blob_hid_on_release(self):
        async def answer(_args, config, session):
            session.after.append(Speak("Astatine.", config["voice"]))
            return {"done": True}

        _, rig, _ = await self.asking_a_fact(answer)
        self.assertEqual(rig.of("Presence"), [True, False, True, False])

    async def test_a_fact_that_goes_wrong_is_an_error_not_a_silence(self):
        async def answer(_args, _config, _session):
            raise RealtimeError("Cannot reach the server at home")

        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            _, rig, _ = await self.asking_a_fact(answer)
        self.assertEqual(rig.of("Error"), ["Cannot reach the server at home"])
        self.assertEqual(rig.history.recent()[0]["status"], "error")

    async def test_a_fact_is_answered_without_waiting_for_the_session(self):
        self.a_fact = True
        wait = asyncio.Event()

        async def answer(_args, config, session):
            session.after.append(Speak("Paris.", config["voice"]))
            wait.set()
            return {"done": True}

        server = await self.server(None, delay=5)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True, "pace": 0.0005})
        with mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}), \
                mock.patch.object(controller, "speech", mock.AsyncMock(return_value=tone(0.1))):
            await rig.ctl.hold_start()
            await asyncio.sleep(0.08)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertTrue(wait.is_set())
        self.assertEqual(rig.of("Reply"), ["Paris."])

    async def test_release_before_the_session_is_ready(self):
        server = await self.server(self.answer, delay=0.15)
        rig = self.rig(server.port, mic_pcm=VOICE, mic={"block": True, "pace": 0.0005})
        await rig.ctl.hold_start()
        await asyncio.sleep(0.08)
        self.assertEqual(rig.ctl.state, "connecting")
        await rig.ctl.hold_end()
        await rig.state("thinking")
        await rig.finished()
        self.assertEqual(self.sent, [VOICE])
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
        self.assertIn("conversation.item.create", server.conn.types())
        await rig.ctl.hold_end()    # the late real release finds nothing to do

    async def test_a_notice_is_said_while_the_action_runs(self):
        said, finish = [], asyncio.Event()

        async def speech(_config, text, _voice, _model=None):
            said.append(text)
            return tone(0.1)

        async def research(_args, config, session):
            session.notice(Speak("Let me look into that.", config["voice"]))
            await finish.wait()
            return {"done": True}

        async def script(c):
            await c.expect("response.create")
            await c.tool_call("research_run", arguments='{"question": "x"}')
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("Done.")

        with mock.patch.dict(skills.HANDLERS, {"research.run": research}), \
                mock.patch.object(controller, "speech", speech):
            _, rig = await self.holding(script)
            await rig.ctl.hold_end()
            async with asyncio.timeout(5):
                while not said:
                    await asyncio.sleep(0.002)
            await rig.state("thinking")     # back to waiting, with the action still running
            self.assertEqual(said, ["Let me look into that."])
            self.assertTrue(rig.speakers[0].drained)
            finish.set()
            await rig.finished()
        self.assertEqual(rig.of("Error"), [])

    async def test_a_notice_that_cant_be_said_changes_nothing(self):
        async def speech(_config, _text, _voice, _model=None):
            raise RealtimeError("no voice")

        async def research(_args, config, session):
            session.notice(Speak("Let me look into that.", config["voice"]))
            return {"done": True}

        async def script(c):
            await c.expect("response.create")
            await c.tool_call("research_run", arguments='{"question": "x"}')
            await c.done()
            await c.expect("response.create", 2)
            await c.reply("Done.")

        with mock.patch.dict(skills.HANDLERS, {"research.run": research}), \
                mock.patch.object(controller, "speech", speech):
            _, rig = await self.holding(script)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("Reply"), ["Done."])

    async def notice_then_answer(self, speech, **options):
        """A fact whose action says something first, and then answers."""
        self.a_fact = True

        async def answer(_args, config, session):
            session.notice(Speak("Let me look into that.", config["voice"]))
            session.after.append(Speak("Paris.", ""))
            return {"done": True}

        with mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}), \
                mock.patch.object(controller, "speech", speech):
            _, rig = await self.holding(None, **options)
            await rig.ctl.hold_end()
            await rig.finished()
        return rig

    async def test_the_speaker_of_a_notice_is_not_used_again(self):
        async def replies(_config, _text, _voice, _model=None):
            return tone(0.1)

        self.a_fact = True

        async def answer(_args, config, session):
            session.notice(Speak("Let me look into that.", config["voice"]))
            await asyncio.sleep(0.2)
            session.after.append(Speak("Paris.", config["voice"]))
            return {"done": True}

        with mock.patch.dict(skills.HANDLERS, {"knowledge.answer": answer}), \
                mock.patch.object(controller, "speech", replies):
            _, rig = await self.holding(None)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(len(rig.speakers), 2)
        self.assertTrue(all(speaker.drained for speaker in rig.speakers))

    async def test_a_speaker_that_fails_on_a_notice_does_not_lose_the_answer(self):
        async def replies(_config, _text, _voice, _model=None):
            return tone(0.1)

        real = FakeSpeaker.drain

        async def drain(speaker):
            await real(speaker)
            raise OSError("Nothing is playing the audio output")

        with mock.patch.object(FakeSpeaker, "drain", drain):
            rig = await self.notice_then_answer(replies)
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("Reply"), ["Paris."])

    async def test_a_voice_that_stalls_on_a_notice_does_not_stall_the_turn(self):
        async def stalls(_config, _text, _voice, _model=None):
            await asyncio.sleep(30)

        with mock.patch.object(controller, "NOTICE_WAIT_S", 0.05):
            rig = await self.notice_then_answer(stalls)
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(rig.of("Reply"), ["Paris."])

    async def test_what_arrived_counts_even_if_the_deadline_has_gone(self):
        turn = controller.Turn.__new__(controller.Turn)
        turn.inbox = asyncio.Queue()
        turn.inbox.put_nowait(("done",))
        self.assertEqual(await turn._next(time.monotonic() - 5), ("done",))

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
        with mock.patch.object(controller, "transcribe", mock.AsyncMock(return_value="hello")), \
                mock.patch.object(controller.knowledge, "is_fact",
                                  mock.AsyncMock(return_value=False)):
            await rig.ctl.hold_start()
            await rig.state("listening")
            await asyncio.sleep(0.15)
            await rig.ctl.hold_end()
            await rig.finished()
        self.assertEqual(rig.of("Reply"), ["It is noon."])  # or it never got as far as the question
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
