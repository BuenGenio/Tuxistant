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
from tuxistant import controller, service, skills, timers  # noqa: E402
from tuxistant.gate import Ask, Session, Speak  # noqa: E402
from tuxistant.realtime import Realtime, RealtimeError  # noqa: E402

VOICE = tone(1.0)
LEAD = tone(0.05, amplitude=3000)   # a sound for before the words, as a timer's chime is


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


class WhenFree(Case):
    """say_when_free and sound, which a timer or a reminder speaks through. Unlike say, neither
    may ever stop what is going on."""

    def fake(self, **options):
        http = FakeSpeech(**options)
        self.addCleanup(http.close)
        return http

    async def refused(self, rig):
        """Asks for both, and checks that the job in hand is still the one running."""
        job = rig.ctl.job
        self.assertIsNone(await rig.ctl.say_when_free("Your pasta timer is done.", LEAD))
        await rig.ctl.sound(LEAD)
        await asyncio.sleep(0.05)
        self.assertIs(rig.ctl.job, job)
        self.assertFalse(job.done(), "what was going on was stopped")
        self.assertNotIn("Your pasta timer is done.", rig.of("Reply"))

    async def test_idle_it_speaks_with_the_lead_before_the_words(self):
        http = self.fake()
        rig = self.rig(http.port, voice="bm_george")
        job = await rig.ctl.say_when_free("Your pasta timer is done.", LEAD)
        self.assertIs(job, rig.ctl.job)
        self.assertIsNone(await job, "the job's result is what went wrong")
        [speaker] = rig.speakers
        self.assertEqual(bytes(speaker.written), LEAD + tone(0.1))
        self.assertTrue(speaker.drained)
        self.assertEqual([(r["input"], r["voice"]) for r in http.requests],
                         [("Your pasta timer is done.", "bm_george")])
        self.assertEqual(rig.of("StateChanged"), ["thinking", "speaking", "idle"])
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done."])
        self.assertEqual(rig.history.recent(), [])

    async def test_with_no_lead_it_is_the_words_alone(self):
        http = self.fake()
        rig = self.rig(http.port)
        await (await rig.ctl.say_when_free("Reminder: call mum."))
        self.assertEqual(bytes(rig.speakers[0].written), tone(0.1))
        self.assertEqual(rig.of("StateChanged"), ["thinking", "speaking", "idle"])

    async def test_while_tux_speaks_it_says_not_now_and_cuts_nothing_off(self):
        http = self.fake()
        gate = asyncio.Event()
        rig = self.rig(http.port, speaker={"gate": gate})
        await rig.ctl.say("A long answer that is still being spoken.")
        await rig.state("speaking")
        await self.refused(rig)
        self.assertFalse(rig.speakers[0].stopped)
        gate.set()
        await rig.finished()
        self.assertTrue(rig.speakers[0].drained, "the answer ran to its end")
        self.assertEqual(len(rig.speakers), 1, "and no sound was laid over it")
        self.assertEqual([r["input"] for r in http.requests],
                         ["A long answer that is still being spoken."])

    async def test_while_tux_listens_it_says_not_now_and_the_turn_goes_on(self):
        server = await self.server()
        rig = self.rig(server.port, mic_pcm=tone(0.2), mic={"block": True}, listen_timeout_s=30)
        await rig.ctl.toggle()
        await rig.state("listening")
        await self.refused(rig)
        self.assertEqual(rig.ctl.state, "listening")
        self.assertEqual((rig.journal, rig.speakers), (["mic.start"], []))

    async def test_while_a_card_is_up_it_says_not_now_and_the_card_stays(self):
        async def script(c):
            await c.heard_audio(CHUNK)
            await c.committed("do the thing")
            await c.tool_call()

        server = await self.server(script)
        rig = self.rig(server.port, mic_pcm=VOICE)
        with mock.patch.dict(skills.HANDLERS, {"time.now": lambda *_: Ask("Do it?", "d", dict)}):
            await rig.ctl.toggle()
            async with asyncio.timeout(5):
                while not rig.of("Approval"):
                    await asyncio.sleep(0.002)
            await self.refused(rig)
            self.assertEqual(rig.of("ApprovalClosed"), [], "the card was answered for the user")
            self.assertFalse(rig.ctl.approval[1].done())
            await rig.ctl.cancel()
        self.assertEqual(rig.speakers, [])

    async def test_while_dictating_it_says_not_now_and_no_words_are_lost(self):
        rig = self.rig(self.fake().port, mic_pcm=VOICE, mic={"block": True})
        await rig.ctl.dictate_start()
        await rig.state("listening")
        go = rig.ctl.dictation
        await self.refused(rig)
        self.assertIs(rig.ctl.dictation, go)
        self.assertEqual((rig.journal, rig.speakers), (["mic.start"], []))

    async def test_locked_no_words_are_said_and_a_sound_shows_nothing(self):
        http = self.fake()
        rig = self.rig(http.port)
        await rig.ctl.set_locked(True)
        self.assertIsNone(await rig.ctl.say_when_free("Reminder: call the clinic.", LEAD))
        self.assertEqual((rig.speakers, rig.signals), ([], []))
        await rig.ctl.sound(LEAD)
        await rig.finished()
        [speaker] = rig.speakers
        self.assertEqual(bytes(speaker.written), LEAD)
        self.assertTrue(speaker.drained)
        self.assertEqual(rig.signals, [], "no blob, no caption and no level on a locked screen")
        self.assertIsNone(await rig.ctl.say("anything"), "say still refuses when locked")
        self.assertEqual(http.requests, [], "nothing was sent to be spoken")

    async def test_a_key_press_stops_an_announcement_and_a_cancel_stops_a_sound(self):
        rig = self.rig(self.fake().port, speaker={"gate": asyncio.Event()})
        await rig.ctl.say_when_free("Your pasta timer is done.", LEAD)
        await rig.state("speaking")
        await rig.ctl.toggle()      # the user wants Tux quiet: that always wins
        self.assertTrue(rig.speakers[0].stopped)
        self.assertEqual(rig.ctl.state, "idle")
        await rig.ctl.sound(LEAD)
        await asyncio.sleep(0.05)
        await rig.ctl.cancel()
        self.assertTrue(rig.speakers[1].stopped)

    async def test_with_the_server_down_the_lead_was_played_and_the_error_shows_once(self):
        rig = self.rig(free_port())     # nothing listens there
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
            job = await rig.ctl.say_when_free("Your pasta timer is done.", LEAD)
            self.assertIn("Cannot reach", await job)
        self.assertEqual(bytes(rig.speakers[0].written), LEAD)
        self.assertTrue(rig.speakers[0].drained, "a server failure must not discard the chime")
        self.assertEqual(len(rig.of("Error")), 1)
        self.assertEqual(rig.ctl.state, "idle")

    async def test_whatever_fails_the_words_the_lead_and_what_came_before_play_to_the_end(self):
        """stop() clears the buffer, so only a drain lets the chime be heard. Not only a server
        error: a refused connection, a timeout or a bug in between end the words the same way."""
        rig = self.rig(self.fake().port)
        said = []
        failures = (ConnectionRefusedError("refused"), TimeoutError(), KeyError("voice"))
        for failure in failures:
            async def speech(_config, sentence, _voice):
                if sentence.startswith("Then"):
                    raise failure
                return tone(0.1)

            with self.subTest(type(failure).__name__), \
                    mock.patch.object(controller, "speech", speech), \
                    mock.patch.object(controller, "ERROR_HOLD_S", 0.01):
                for text, lead, heard in (("Then nothing.", LEAD, False),
                                          ("First this. Then nothing.", b"", True)):
                    said.clear()
                    job = await rig.ctl.say_when_free(text, lead, lambda: said.append(True))
                    self.assertTrue(await job, "what went wrong, and never empty: empty is fine")
                    speaker = rig.speakers[-1]
                    self.assertEqual(bytes(speaker.written), lead or tone(0.1))
                    self.assertTrue(speaker.drained, "it was cut off")
                    self.assertFalse(speaker.stopped)
                    self.assertEqual(bool(said), heard, "heard is for words, not the chime")

    async def test_a_key_press_still_cuts_the_chime_while_the_words_are_awaited(self):
        async def speech(*_args):
            await asyncio.Event().wait()

        rig = self.rig(self.fake().port)
        with mock.patch.object(controller, "speech", speech):
            await rig.ctl.say_when_free("Your pasta timer is done.", LEAD)
            await rig.state("speaking")
            await rig.ctl.toggle()
        self.assertTrue(rig.speakers[0].stopped)
        self.assertFalse(rig.speakers[0].drained)


class Notes:
    """Where timers.notify would send a notification. Nothing is on the other end."""

    def __init__(self):
        self.sent = []

    async def call(self, message):
        self.sent.append(message.body[3])


class TimersComeDue(Case):
    """timers.py on the real controller, put together as service.run does it: service.tick, an
    Announcer and a store in the temp folder. Nothing here attaches that store for the model."""

    def setUp(self):
        super().setUp()
        self.tts = FakeSpeech()
        self.addCleanup(self.tts.close)
        # A test adds its item the moment the rig exists, which looks like one that came due
        # while the daemon was away. The two tests about that wait set their own.
        for patcher in (mock.patch.object(service, "START_GRACE_S", 0),
                        mock.patch.object(timers, "RETRY_S", 0.02)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def wired(self, lock=None, overdue=(), **options):
        rig = self.rig(self.tts.port, **options)
        self.notes, ctl = Notes(), rig.ctl
        self.store = timers.Store(self.folder / "data" / "timers.json")
        for label, ago in overdue:      # as if set before the daemon last stopped
            self.store.add("reminder", label, time.time() - ago, time.time() - ago - 600)
        self.crier = timers.Announcer(
            say=ctl.say_when_free, sound=ctl.sound,
            notify=lambda item: timers.notify(self.notes, item),
            locked=lambda: ctl.locked, chimes=lambda: rig.config["timer_chime"])
        known = asyncio.create_task(lock(ctl) if lock else asyncio.sleep(0))  # watch_lock's place
        self.ticking = asyncio.create_task(service.tick(self.store, self.crier, known))
        self.addAsyncCleanup(asyncio.gather, self.ticking, known, return_exceptions=True)
        self.addCleanup(known.cancel)
        self.addCleanup(self.ticking.cancel)
        return rig

    async def until(self, condition, timeout=5):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.005)

    def due_now(self, label="pasta", kind="timer"):
        now = timers.epoch()
        return self.store.add(kind, label, now, now - 480)

    async def test_a_failed_lock_query_keeps_speech_locked(self):
        bus = mock.Mock()
        shield = bus.get_proxy_object.return_value.get_interface.return_value
        shield.call_get_active = mock.AsyncMock(side_effect=ConnectionError("test failure"))
        ctl = mock.Mock(set_locked=mock.AsyncMock())
        with self.assertLogs(service.log, level="WARNING"):
            await service.watch_lock(bus, ctl)
        ctl.set_locked.assert_awaited_once_with(True)

    async def test_a_lock_check_that_fails_in_its_setup_keeps_speech_locked(self):
        bus = mock.Mock()
        bus.get_proxy_object.side_effect = RuntimeError("test setup failure")
        ctl = mock.Mock(set_locked=mock.AsyncMock())
        with self.assertLogs(service.log, level="WARNING"):
            await service.watch_lock(bus, ctl)
        ctl.set_locked.assert_awaited_once_with(True)

    async def test_when_the_lock_setup_fails_the_timers_notify_and_say_nothing(self):
        broken = mock.Mock()
        broken.get_proxy_object.return_value.get_interface.side_effect = RuntimeError("test")
        await self.silent_after(lambda ctl: service.watch_lock(broken, ctl))

    async def test_when_the_lock_task_fails_the_timers_notify_and_say_nothing(self):
        async def lock_then_fail(controller):   # as if set_locked's own cancel had failed
            await controller.set_locked(True)
            raise RuntimeError("test lock failure")
        await self.silent_after(lock_then_fail)

    async def silent_after(self, check):
        with self.assertLogs(service.log, level="WARNING"):
            rig = self.wired(lock=check)
            self.due_now()
            await self.until(lambda: self.notes.sent)
            await asyncio.sleep(0.2)        # several retries' worth
        self.assertTrue(rig.ctl.locked)
        self.assertEqual((rig.speakers, self.tts.requests), ([], []), "nothing is heard")
        self.assertEqual(len(self.store.pending()), 1, "still owed its words")
        self.assertFalse(self.ticking.done(), "the timers go on")

    async def test_the_scheduler_starts_again_after_a_failure(self):
        runs = []

        async def run(*_args, **_kwargs):
            runs.append(True)
            if len(runs) == 1:
                raise RuntimeError("test scheduler failure")
            await asyncio.Event().wait()

        known = asyncio.get_running_loop().create_future()
        known.set_exception(RuntimeError("test lock failure"))  # not a DBusError, and not fatal
        with mock.patch.object(service.timers, "run", run), \
                mock.patch.object(service, "RESTART_S", 0), \
                self.assertLogs(service.log, level="ERROR") as logged:
            store = mock.Mock(due=lambda _now: [])
            ticking = asyncio.create_task(service.tick(store, mock.Mock(), known))
            await self.until(lambda: len(runs) == 2)
        self.assertFalse(ticking.done(), "the timers go on")
        ticking.cancel()
        await asyncio.gather(ticking, return_exceptions=True)
        self.assertIn("test lock failure", " ".join(logged.output))
        self.assertIn("test scheduler failure", " ".join(logged.output))

    async def test_scheduler_failure_is_logged(self):
        task = asyncio.get_running_loop().create_future()
        task.set_exception(RuntimeError("test scheduler failure"))
        with self.assertLogs(service.log, level="ERROR") as logged:
            service._timers_stopped(task)
        self.assertIn("test scheduler failure", " ".join(logged.output))

    async def test_no_announcement_before_a_slow_lock_check_finishes(self):
        ready = asyncio.Event()
        async def slow_lock(controller):
            await ready.wait()
            await controller.set_locked(True)
        with mock.patch.object(service, "LOCK_KNOWN_S", 0.01, create=True):
            rig = self.wired(lock=slow_lock)
            self.due_now()
            await asyncio.sleep(0.08)
            self.assertTrue(self.store.pending())
            self.assertFalse(rig.ctl.active)
            ready.set()
            await asyncio.sleep(0.08)
            self.assertTrue(rig.ctl.locked)
            self.assertFalse(rig.ctl.active)

    async def test_as_installed_it_is_noted_and_said_with_no_chime(self):
        rig = self.wired()
        self.assertIs(rig.config["timer_chime"], False, "the chime is the owner's to turn on")
        self.due_now()
        await self.until(lambda: not self.store.pending())
        self.assertEqual(self.notes.sent, ["Pasta timer is done"])
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done."])
        self.assertEqual(rig.of("StateChanged"), ["thinking", "speaking", "idle"])
        self.assertEqual(rig.of("Presence"), [True, False], "the blob shows for it")
        self.assertEqual(bytes(rig.speakers[0].written), tone(0.1), "the words alone")
        self.assertEqual(self.store.kept()[0].state, "done")
        self.assertEqual(rig.history.recent(), [])

    async def test_with_the_chime_on_it_leads_the_words(self):
        rig = self.wired(timer_chime=True)
        self.due_now()
        await self.until(lambda: not self.store.pending())
        [speaker] = rig.speakers
        self.assertEqual(bytes(speaker.written), timers.chime() + tone(0.1))
        self.assertEqual(self.tts.requests[0]["input"], "Your pasta timer is done.")

    async def test_as_installed_a_locked_screen_stays_silent_and_the_words_come_at_the_unlock(self):
        rig = self.wired()
        await rig.ctl.set_locked(True)
        made = self.due_now()
        await self.until(lambda: self.notes.sent)
        await asyncio.sleep(0.2)        # several retries' worth
        self.assertEqual(self.notes.sent, ["Pasta timer is done"], "the trace is there at once")
        self.assertEqual((rig.speakers, rig.signals, self.tts.requests), ([], [], []),
                         "nothing is heard, shown or sent to be spoken on a locked screen")
        self.assertEqual(len(self.store.pending()), 1, "still owed its words")
        with mock.patch.object(timers, "epoch", lambda: made.due + 300), \
                mock.patch.object(self.crier, "now", lambda: made.due + 300):
            await rig.ctl.set_locked(False)
            await self.until(lambda: not self.store.pending())
        self.assertEqual(rig.of("Reply"), ["Your pasta timer finished 5 minutes ago."])
        self.assertEqual(len(self.notes.sent), 1, "one notification, not two")

    async def test_with_the_chime_on_a_locked_screen_hears_it_once_and_no_words(self):
        rig = self.wired(timer_chime=True)
        await rig.ctl.set_locked(True)
        made = self.due_now()
        await self.until(lambda: rig.speakers and rig.speakers[0].drained)
        await asyncio.sleep(0.2)
        self.assertEqual([bytes(s.written) for s in rig.speakers], [timers.chime()], "once")
        self.assertEqual((rig.signals, self.tts.requests), ([], []), "no words and no blob")
        self.assertIsNone(await rig.ctl.say("anything"), "say still refuses when locked")
        with mock.patch.object(timers, "epoch", lambda: made.due + 300), \
                mock.patch.object(self.crier, "now", lambda: made.due + 300):
            await rig.ctl.set_locked(False)
            await self.until(lambda: not self.store.pending())
        self.assertEqual(rig.of("Reply"), ["Your pasta timer finished 5 minutes ago."])
        self.assertEqual(bytes(rig.speakers[1].written), tone(0.1), "no chime before late words")

    async def test_while_tux_is_speaking_it_waits_and_cuts_nothing_off(self):
        gate = asyncio.Event()
        rig = self.wired(speaker={"gate": gate})
        await rig.ctl.say("A long answer that is still being spoken.")
        await rig.state("speaking")
        self.due_now()
        await self.until(lambda: self.notes.sent)
        await asyncio.sleep(0.2)
        self.assertEqual(rig.of("Reply"), ["A long answer that is still being spoken."])
        self.assertFalse(rig.speakers[0].stopped, "the answer was not interrupted")
        self.assertEqual(len(self.store.pending()), 1)
        gate.set()
        await self.until(lambda: not self.store.pending())
        self.assertEqual(rig.of("Reply"), ["A long answer that is still being spoken.",
                                           "Your pasta timer is done."])
        self.assertTrue(rig.speakers[0].drained, "the answer ran to its end")

    async def test_while_tux_is_listening_it_waits_and_the_turn_goes_on(self):
        server = await self.server()
        rig = self.wired(mic={"block": True}, mic_pcm=tone(0.2), listen_timeout_s=30)
        rig.config.values["server"] = f"127.0.0.1:{server.port}"
        await rig.ctl.toggle()
        await rig.state("listening")
        turn = rig.ctl.job
        self.due_now()
        await self.until(lambda: self.notes.sent)
        await asyncio.sleep(0.2)
        self.assertEqual(rig.ctl.state, "listening")
        self.assertIs(rig.ctl.job, turn)
        self.assertFalse(turn.done(), "the turn was not stopped")
        self.assertEqual(rig.of("Reply"), [])
        rig.config.values["server"] = f"127.0.0.1:{self.tts.port}"    # where speech is faked
        await rig.ctl.cancel()      # the user ends the turn
        await self.until(lambda: not self.store.pending())
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done."])

    async def test_cut_short_by_a_key_press_it_still_counts_as_said(self):
        rig = self.wired(speaker={"gate": asyncio.Event()})
        self.due_now()
        await rig.state("speaking")
        await rig.ctl.toggle()
        self.assertTrue(rig.speakers[0].stopped)
        await self.until(lambda: not self.store.pending())
        self.assertEqual(self.store.kept()[0].state, "done")
        await asyncio.sleep(0.1)
        self.assertEqual(len(rig.of("Reply")), 1, "and it is not said again")

    async def test_a_lock_before_any_word_is_heard_leaves_it_owed_until_the_unlock(self):
        asked = asyncio.Event()

        async def speech(*_args):       # the words are still on their way when the screen locks
            asked.set()
            await asyncio.Event().wait()

        rig = self.wired()
        with mock.patch.object(controller, "speech", speech):
            made = self.due_now()
            await self.until(asked.is_set)
            await rig.ctl.set_locked(True)
            await asyncio.sleep(0.2)
        self.assertEqual(len(self.store.pending()), 1, "nothing was heard, so it is still owed")
        self.assertEqual(rig.of("Error"), [])
        with mock.patch.object(timers, "epoch", lambda: made.due + 300), \
                mock.patch.object(self.crier, "now", lambda: made.due + 300):
            await rig.ctl.set_locked(False)
            await self.until(lambda: not self.store.pending())
        self.assertEqual(self.store.kept()[0].state, "done")
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done.",
                                           "Your pasta timer finished 5 minutes ago."])
        self.assertEqual([r["input"] for r in self.tts.requests],
                         ["Your pasta timer finished 5 minutes ago."])

    async def test_words_the_server_refuses_hold_back_no_other_and_none_overlap(self):
        # With the chime on, pasta opens a speaker before its words fail, and that speaker
        # takes a while to drain. Tea's must not start until it has.
        rig = self.wired(timer_chime=True, speaker={"play_s": 0.05})
        open_at_start = []

        async def speech(_config, sentence, _voice):
            *before, own = rig.speakers     # own is this item's chime
            open_at_start.append([s for s in before if not (s.drained or s.stopped)])
            if "pasta" in sentence:
                raise RuntimeError("The server could not say that.")
            return tone(0.1)

        with mock.patch.object(controller, "speech", speech), \
                mock.patch.object(controller, "ERROR_HOLD_S", 0.01), self.assertLogs(timers.log):
            self.due_now("pasta")
            self.due_now("tea")
            await self.until(lambda: "tea" not in [i.label for i in self.store.pending()])
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done.", "Your tea timer is done."])
        self.assertEqual([i.label for i in self.store.pending()], ["pasta"], "still owed")
        self.assertEqual([(i.label, i.state) for i in self.store.kept()], [("tea", "done")])
        self.assertEqual(len(rig.speakers), 2, "one chime each")
        self.assertEqual(open_at_start, [[], []], "each starts once the one before is over")
        self.assertEqual(rig.journal, ["speaker.start", "speaker.drained"] * 2,
                         "pasta's chime plays to its end before tea's starts")

    async def test_with_the_server_down_it_waits_shows_the_error_once_and_is_said_later(self):
        rig = self.wired()
        rig.config.values["server"] = f"127.0.0.1:{free_port()}"    # nothing listens there
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01), \
                mock.patch.object(timers, "FAILED_WAIT_S", 0.5), self.assertLogs(timers.log):
            self.due_now()
            await self.until(lambda: rig.of("Error"))
            await asyncio.sleep(0.2)
            self.assertEqual(len(rig.of("Error")), 1, "not every two seconds")
            self.assertEqual(len(self.store.pending()), 1, "nothing was said, so it is still owed")
            rig.config.values["server"] = f"127.0.0.1:{self.tts.port}"    # back again
            await self.until(lambda: not self.store.pending())
        self.assertEqual(self.store.kept()[0].state, "done")
        self.assertEqual(rig.of("Reply"), ["Your pasta timer is done."] * 2)
        self.assertEqual(len(self.tts.requests), 1)
        self.assertEqual(self.notes.sent, ["Pasta timer is done"], "one notification")

    async def test_with_the_server_down_for_good_it_is_missed_in_the_end(self):
        rig = self.wired()
        rig.config.values["server"] = f"127.0.0.1:{free_port()}"
        with mock.patch.object(controller, "ERROR_HOLD_S", 0.01), \
                mock.patch.object(timers, "FAILED_WAIT_S", 0.02), self.assertLogs(timers.log):
            self.due_now()
            await self.until(lambda: not self.store.pending())
        self.assertEqual(self.store.kept()[0].state, "missed")
        self.assertEqual(len(rig.of("Error")), timers.FAILED_TRIES)

    async def test_started_on_a_locked_screen_with_one_overdue_it_waits_to_know(self):
        learned = asyncio.Event()

        async def slow_lock(ctl):       # the shell takes a moment to say the screen is locked
            await learned.wait()
            await ctl.set_locked(True)

        with mock.patch.object(service, "START_GRACE_S", 0.05):
            rig = self.wired(lock=slow_lock, overdue=[("call mum", 7200)])
            await asyncio.sleep(0.2)
            self.assertEqual((self.notes.sent, rig.speakers, rig.signals), ([], [], []))
            learned.set()
            await self.until(lambda: self.notes.sent)
            await asyncio.sleep(0.2)
            self.assertEqual((rig.of("Reply"), rig.speakers), ([], []), "locked: no words")
            await rig.ctl.set_locked(False)
            await self.until(lambda: not self.store.pending())
        [said] = rig.of("Reply")
        self.assertRegex(said, r"^You missed a reminder .* at .*: call mum\.$")

    async def test_what_was_overdue_at_the_start_waits_for_the_desktop_to_come_up(self):
        with mock.patch.object(service, "START_GRACE_S", 0.3):
            rig = self.wired(overdue=[("call mum", 600)])
            await asyncio.sleep(0.15)
            self.assertEqual((self.notes.sent, rig.of("Reply")), ([], []), "not at once")
            await self.until(lambda: not self.store.pending())
        self.assertEqual(len(rig.of("Reply")), 1)

    async def test_set_by_the_model_it_is_said_when_its_time_comes(self):
        rig = self.wired()
        timers.attach(self.store)
        self.addCleanup(timers.attach, None)
        asked = '{"duration": "1 second", "label": "tea"}'
        self.assertEqual(await rig.skills.execute("timer_set", asked, Session(rig.skills)),
                         ("timer.set", {"set": "Tea timer set for 1 second."}))
        self.assertEqual(rig.of("Reply"), [])
        await self.until(lambda: not self.store.pending())
        self.assertEqual(rig.of("Reply"), ["Your tea timer is done."])
        self.assertEqual(self.notes.sent, ["Tea timer is done"])


if __name__ == "__main__":
    unittest.main()
