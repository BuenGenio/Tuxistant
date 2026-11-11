"""One real VAD turn and one real hold turn against the LocalAI server, with fake audio.

Skipped unless asked for, and it never reads or stores the key itself:

    TUXISTANT_LIVE=1 TUXISTANT_SERVER=host:8080 \
    TUXISTANT_API_KEY=... python3 -m unittest -v tests.test_realtime_live
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import FakeMic, FakeSpeaker  # noqa: E402
from tuxistant import __main__ as cli  # noqa: E402
from tuxistant.audio import BYTES_PER_S, level  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant import controller  # noqa: E402
from tuxistant.controller import Controller, VOICE_LEVEL  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.realtime import AuthError, Realtime, RealtimeError, sentences, speech  # noqa: E402
from tuxistant.service import PREVIEW_TEXT  # noqa: E402
from tuxistant.skills import Skills  # noqa: E402

QUESTION = "Please tell me what time it is"
LEAD_IN_S = 0.5     # silence before the question, as if the user took a moment to start


class RealTimeMic(FakeMic):
    """Plays its audio at the pace a real mic would, then silence."""

    def __init__(self, journal, pcm, on_start, on_end):
        super().__init__(journal, pcm, pace=0)
        self.on_start, self.on_end = on_start, on_end
        self.next_at = None

    async def read(self) -> bytes:
        if self.next_at is None:
            self.next_at = time.monotonic()
            self.on_start(self.next_at)
        self.next_at += 0.04
        await asyncio.sleep(max(0.0, self.next_at - time.monotonic()))
        chunk = await super().read()
        if self.sent >= len(self.pcm):
            self.on_end()
        return chunk


class CountingSkills(Skills):
    def __init__(self):
        super().__init__()
        self.ran = []

    def run(self, tool_name, arguments, session=None):
        self.ran.append((tool_name, time.monotonic()))
        return super().run(tool_name, arguments)


@unittest.skipUnless(os.environ.get("TUXISTANT_LIVE"), "live server test")
class Live(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.config = Config(folder / "config.json")
        self.config.values.update(server=os.environ.get("TUXISTANT_SERVER", "localhost:8080"),
                                  api_key=os.environ["TUXISTANT_API_KEY"])
        self.history = History(folder / "history.db")
        self.addCleanup(self.history.db.close)
        self.skills = CountingSkills()
        self.signals = []
        self.spoke_at = None    # when the last of the speech reached the mic
        self.speakers = []
        self.journal = []
        question = await speech(self.config, QUESTION, "af_nicole")
        self.assertEqual(sentences(QUESTION), [QUESTION])
        self.pcm = bytes(int(LEAD_IN_S * BYTES_PER_S)) + question
        self.assertGreater(level(question[:BYTES_PER_S]), VOICE_LEVEL)
        # Kokoro leaves silence after the words: time from the last voiced chunk, not the clip end.
        chunks = range(0, len(self.pcm), 1920)
        last_voiced = max(i for i in chunks if level(self.pcm[i:i + 1920]) >= VOICE_LEVEL)
        self.voice_offset = (last_voiced + 1920) / BYTES_PER_S
        self.voice_ended = None
        self.ctl = Controller(self.config, self.history, self.skills, emit=self.emit,
                              mic_factory=self.new_mic, speaker_factory=self.new_speaker)
        self.addAsyncCleanup(self.ctl.cancel)

    def emit(self, name, *args):
        if name != "Level":
            self.signals.append((time.monotonic(), name, *args))

    def new_mic(self):
        return RealTimeMic(self.journal, self.pcm, self.mic_started, self.speech_ended)

    def mic_started(self, at: float):
        self.voice_ended = self.voice_ended or at + self.voice_offset

    def speech_ended(self):
        self.spoke_at = self.spoke_at or time.monotonic()

    def new_speaker(self, on_level):
        self.speakers.append(FakeSpeaker(self.journal, on_level))
        return self.speakers[-1]

    def at(self, name: str, *args) -> float:
        return next(s[0] for s in self.signals if s[1:] == (name, *args))

    def report(self, title: str, started: float, end_of_speech: float):
        first_audio = self.at("StateChanged", "speaking")
        tool = self.skills.ran[0][1]
        heard = next(s for s in self.signals if s[1] == "Transcript")
        print(f"\n{title}: heard {heard[2]!r}")
        print(f"  reply {self.history.recent()[0]['reply']!r}")
        print(f"  audio back {len(self.speakers[0].written) / BYTES_PER_S:.1f} s")
        print(f"  command to listening     {self.at('StateChanged', 'listening') - started:6.2f} s")
        print(f"  end of speech to thinking {self.at('StateChanged', 'thinking') - end_of_speech:5.2f} s")
        print(f"  end of speech to transcript {heard[0] - end_of_speech:3.2f} s")
        print(f"  end of speech to tool ran   {tool - end_of_speech:3.2f} s")
        print(f"  end of speech to first audio {first_audio - end_of_speech:2.2f} s")

    def check_turn(self):
        row = self.history.recent()[0]
        self.assertEqual(row["status"], "ok", row)
        self.assertIn("time", row["heard"].lower())
        self.assertEqual([a["name"] for a in row["actions"]], ["time.now"])
        self.assertTrue(row["reply"])
        self.assertGreater(len(self.speakers[0].written), BYTES_PER_S // 2)
        self.assertTrue(self.speakers[0].drained)
        self.assertEqual([s for s in self.signals if s[1] == "Error"], [])
        self.assertEqual(self.journal[:3], ["mic.start", "mic.stop", "speaker.start"])

    async def finished(self):
        async with asyncio.timeout(60):
            await asyncio.wait({self.ctl.job})

    async def test_vad_turn(self):
        started = time.monotonic()
        await self.ctl.toggle()
        await self.finished()
        self.check_turn()
        self.assertEqual([s[2] for s in self.signals if s[1] == "StateChanged"],
                         ["connecting", "listening", "thinking", "speaking", "idle"])
        self.report("VAD turn", started, self.voice_ended)

    async def test_hold_turn(self):
        started = time.monotonic()
        await self.ctl.hold_start()
        async with asyncio.timeout(30):
            while self.spoke_at is None:
                await asyncio.sleep(0.01)
        released = time.monotonic()
        await self.ctl.hold_end()
        await self.finished()
        self.check_turn()
        self.assertEqual([s[2] for s in self.signals if s[1] == "Presence"], [True, False])
        self.assertLess(self.at("Presence", False), self.at("StateChanged", "speaking"))
        self.report("Hold turn", started, released)

    async def test_a_fact_on_a_hold_turn(self):
        """The whole route with real speech: Whisper, the sorter, Wikipedia, the model, the voice."""
        self.skills.config = self.config
        self.config.values["fact_lookup"] = True
        question = await speech(self.config, "How many chemical elements are there?", "af_nicole")
        self.pcm = bytes(int(LEAD_IN_S * BYTES_PER_S)) + question
        await self.ctl.hold_start()
        async with asyncio.timeout(30):
            while self.spoke_at is None:
                await asyncio.sleep(0.01)
        released = time.monotonic()
        await self.ctl.hold_end()
        await self.finished()
        row = self.history.recent()[0]
        print(f"\nFact: heard {row['heard']!r}, said {row['reply']!r}, "
              f"first audio {self.at('StateChanged', 'speaking') - released:.2f} s after release")
        self.assertEqual(row["status"], "ok", row)
        self.assertEqual([a["name"] for a in row["actions"]], ["knowledge.answer"])
        self.assertIn("118", row["reply"])
        self.assertGreater(len(self.speakers[0].written), BYTES_PER_S // 2)
        self.assertEqual([s for s in self.signals if s[1] == "Error"], [])

    async def test_conversation_follow_up_on_the_same_session(self):
        self.config.values["conversation_mode"] = True
        pcm = self.pcm
        made = []

        def new_mic():
            made.append(1)      # the third mic is the follow-up nobody answers
            self.pcm = pcm if len(made) < 3 else b""
            return RealTimeMic(self.journal, self.pcm, self.mic_started, self.speech_ended)

        self.ctl.make_mic = new_mic
        with mock.patch.object(controller, "FOLLOW_UP_S", 2):
            await self.ctl.toggle()
            await self.finished()
        rows = self.history.recent()
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"], rows)
        self.assertTrue(all(a["name"] == "time.now" for r in rows for a in r["actions"]))
        self.assertEqual([s[2] for s in self.signals if s[1] == "StateChanged"],
                         ["connecting", "listening", "thinking", "speaking",
                          "listening", "thinking", "speaking", "listening", "idle"])

    async def test_say_sentence_by_sentence(self):
        await self.ctl.say(PREVIEW_TEXT, "bm_fable")
        await self.finished()
        self.assertEqual([s[2] for s in self.signals if s[1] == "StateChanged"],
                         ["thinking", "speaking", "idle"])
        seconds = len(self.speakers[0].written) / BYTES_PER_S
        print(f"\nSay: {seconds:.1f} s of audio for {PREVIEW_TEXT!r}")
        self.assertGreater(seconds, 1)

    async def test_cancel_while_the_reply_is_being_made(self):
        await self.ctl.toggle()
        async with asyncio.timeout(30):
            while self.ctl.state != "thinking":
                await asyncio.sleep(0.005)
        started = time.monotonic()
        await self.ctl.cancel()
        print(f"\nCancel took {time.monotonic() - started:.3f} s")
        self.assertEqual(self.ctl.state, "idle")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([s for s in self.signals if s[1] == "Error"], [])

    async def test_empty_commit_is_quiet(self):
        events = asyncio.Queue()
        rt = Realtime(self.config, self.skills, events.put_nowait, hold=True)
        self.addAsyncCleanup(rt.close)
        await rt.connect()
        await rt._send({"type": "input_audio_buffer.commit"})
        await rt._send({"type": "response.create"})
        async with asyncio.timeout(10):
            while (await events.get())[0] != "quiet":
                pass

    async def test_bad_key_and_bad_pipeline(self):
        wrong = dict(self.config.values, api_key="wrong")
        rt = Realtime(wrong, self.skills, lambda event: None)
        self.addAsyncCleanup(rt.close)
        with self.assertRaises(AuthError):
            await rt.connect()
        wrong = dict(self.config.values, pipeline="tux-no-such-pipeline")
        rt = Realtime(wrong, self.skills, lambda event: None)
        self.addAsyncCleanup(rt.close)
        with self.assertRaisesRegex(RealtimeError, "not a pipeline"):
            await rt.connect()

    def test_doctor_sees_the_server(self):
        results = {what: passed for passed, what, _ in cli.checks(self.config)}
        print(f"\nDoctor: {results}")
        for what in ("server reachable", "API key accepted", "pipeline present",
                     "voice model present", "PipeWire tools present"):
            self.assertTrue(results[what], what)


if __name__ == "__main__":
    unittest.main()
