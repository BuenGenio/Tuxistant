"""Dictation with a fake server, a fake mic and no desktop. The shell's half is in
test_dictation_shell.py."""
import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import CHUNK, Case, FakeSpeech, tone  # noqa: E402
from tuxistant import controller, dictation  # noqa: E402
from tuxistant.controller import NO_TYPING  # noqa: E402
from tuxistant.dictation import NOT_TYPED, clean  # noqa: E402

VOICE = tone(1.0)


class Clean(unittest.TestCase):
    def test_a_line_break_cannot_press_enter(self):
        for text in ("rm -rf ~\nls", "rm -rf ~\r\nls", "rm -rf ~ ls", "rm -rf ~ ls",
                     "rm -rf ~\x0bls", "rm -rf ~\x0cls", "rm -rf ~\x85ls"):
            self.assertEqual(clean(text), "rm -rf ~ ls", repr(text))

    def test_control_characters_go(self):
        self.assertEqual(clean("one\x1b[Atwo\x07three\x00four"), "one [Atwo three four")

    def test_invisible_format_characters_go(self):
        self.assertEqual(clean("a\u202eb\u200bc\ufeffd"), "abcd")

    def test_the_joiners_that_are_spelling_stay(self):
        for text in ("می\u200cخواهم", "👩\u200d💻"):
            self.assertEqual(clean(text), text)

    def test_ordinary_words_of_any_language_stay(self):
        for text in ("Mañana, ¿de acuerdo?", "Привіт, світе!", "你好，世界。", "مرحبا بالعالم",
                     "Call me (maybe) tomorrow.", "It costs $5 [approx]."):
            self.assertEqual(clean(text), text)

    def test_whisper_saying_there_were_no_words(self):
        for text in ("[BLANK_AUDIO]", " [BLANK_AUDIO] ", "(music)", "♪", "♪♪♪", "[ Silence ]",
                     "*sighs*", "♪ la la ♪"):
            self.assertEqual(clean(text), "", text)

    def test_a_sentence_with_brackets_at_both_ends_is_still_words(self):
        for text in ("(a) first point, (b) second point, and (c) third (final)",
                     "*Important*: call Ana about the *contract*", "[one] and [two]"):
            self.assertEqual(clean(text), text)

    def test_nothing_is_nothing(self):
        self.assertEqual(clean(""), "")
        self.assertEqual(clean(" \n\t "), "")


class Flow(Case):
    def speech(self, **options):
        http = FakeSpeech(**options)
        self.addCleanup(http.close)
        return http

    def rig_for(self, http, **options):
        options.setdefault("mic_pcm", VOICE)
        options.setdefault("mic", {"block": True})
        return self.rig(http.port, **options)

    async def heard_all_of(self, rig):
        async with asyncio.timeout(5):
            while not rig.mics or rig.mics[0].sent < len(rig.mic_pcm):
                await asyncio.sleep(0.002)

    async def wait_for(self, rig, signal):
        async with asyncio.timeout(5):
            while not rig.of(signal):
                await asyncio.sleep(0.002)

    async def delivered(self, rig):
        await rig.finished()
        if rig.ctl.deliveries:
            await asyncio.wait(rig.ctl.deliveries, timeout=5)

    async def spoke(self, rig, mic=0):
        await rig.ctl.dictate_start()
        async with asyncio.timeout(5):
            while len(rig.mics) <= mic or rig.mics[mic].sent < len(rig.mic_pcm):
                await asyncio.sleep(0.002)
        await rig.ctl.dictate_end()
        await rig.finished()

    async def dictated(self, rig, how="typed"):
        """A whole go, with the shell answering."""
        await self.spoke(rig)
        await self.wait_for(rig, "TypeText")
        request, text, _ = rig.of("TypeText")[0]
        rig.ctl.typed(request, how)
        await self.delivered(rig)
        return text

    @classmethod
    def setUpClass(cls):
        patcher = mock.patch.object(dictation, "TAIL_S", 0.0)
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    async def test_the_words_go_to_the_shell_with_a_space_for_the_next_go(self):
        rig = self.rig_for(self.speech(transcript=" Hello there. "))
        self.assertEqual(await self.dictated(rig), "Hello there. ")
        self.assertEqual(rig.of("StateChanged"), ["listening", "idle"])
        self.assertEqual(rig.of("Presence"), [True, False])
        self.assertEqual(rig.of("Error"), [])

    async def test_the_shell_hears_which_go_this_is_before_the_mic_opens(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        await self.dictated(rig)
        (started,) = rig.of("DictationStarted")
        self.assertEqual(started, rig.of("TypeText")[0][0])
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])

    async def test_the_audio_goes_up_as_a_wav_and_the_model_is_named(self):
        http = self.speech(transcript="Hello.")
        await self.dictated(self.rig_for(http))
        (upload,) = http.uploads
        self.assertIn(b'name="model"\r\n\r\nwhisper-large-turbo-q5_0\r\n', upload)
        self.assertIn(b"RIFF", upload)
        self.assertIn(VOICE[:CHUNK], upload)

    async def test_the_words_are_not_kept_anywhere(self):
        rig = self.rig_for(self.speech(transcript="My private thoughts."))
        await self.dictated(rig)
        self.assertEqual(rig.of("Transcript"), [])
        self.assertEqual(rig.of("Reply"), [])
        self.assertEqual(rig.of("HistoryChanged"), [])
        self.assertEqual(rig.history.recent(), [])

    async def test_no_tool_and_no_model_is_involved(self):
        rig = self.rig_for(self.speech(transcript="Delete all my files."))
        await self.dictated(rig)
        self.assertEqual(rig.skills.ran, [])
        self.assertEqual(rig.of("Approval"), [])

    async def test_a_line_break_from_the_server_never_reaches_the_shell(self):
        rig = self.rig_for(self.speech(transcript="rm -rf ~\n"))
        self.assertEqual(await self.dictated(rig), "rm -rf ~ ")

    async def test_a_hold_too_short_to_be_speech_ends_quietly(self):
        http = self.speech(transcript="Thank you.")
        rig = self.rig_for(http, mic_pcm=VOICE[:CHUNK])
        await self.spoke(rig)
        await self.delivered(rig)
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.of("Error"), [])

    async def test_silence_is_not_sent_for_whisper_to_invent_words(self):
        http = self.speech(transcript="Thank you.")
        rig = self.rig_for(http, mic_pcm=bytes(len(VOICE)))
        await self.spoke(rig)
        await self.delivered(rig)
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.of("TypeText"), [])

    async def test_no_words_means_nothing_is_typed(self):
        rig = self.rig_for(self.speech(transcript="[BLANK_AUDIO]"))
        await self.spoke(rig)
        await self.delivered(rig)
        self.assertEqual(rig.of("TypeText"), [])
        self.assertEqual(rig.of("Error"), [])

    async def test_an_old_extension_is_told_so_and_the_mic_stays_closed(self):
        rig = self.rig_for(self.speech())
        rig.ctl.typist = False
        await rig.ctl.dictate_start()
        await rig.finished()
        self.assertEqual(rig.of("Error"), [NO_TYPING])
        self.assertEqual(rig.journal, [])
        self.assertEqual(rig.of("DictationStarted"), [])

    async def test_a_locked_screen_does_not_listen(self):
        rig = self.rig_for(self.speech())
        await rig.ctl.set_locked(True)
        await rig.ctl.dictate_start()
        await rig.finished()
        self.assertEqual(rig.journal, [])

    async def test_locking_while_it_listens_drops_it_and_closes_the_mic(self):
        http = self.speech(transcript="Hello.")
        rig = self.rig_for(http)
        await rig.ctl.dictate_start()
        await self.heard_all_of(rig)
        await rig.ctl.set_locked(True)
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.ctl.state, "idle")

    async def test_locking_on_the_way_to_the_server_types_nothing(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        http = self.speech(transcript="Hello.", wait=gate)
        rig = self.rig_for(http)
        await self.spoke(rig)
        async with asyncio.timeout(5):
            while not http.uploads:
                await asyncio.sleep(0.002)
        await rig.ctl.set_locked(True)
        gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(rig.of("TypeText"), [])

    async def test_cancel_drops_it(self):
        http = self.speech(transcript="Hello.")
        rig = self.rig_for(http)
        await rig.ctl.dictate_start()
        await self.heard_all_of(rig)
        await rig.ctl.cancel()
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.of("TypeText"), [])
        self.assertEqual(rig.ctl.dictation, None)

    async def test_the_toggle_starts_it_and_the_next_one_types(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        await rig.ctl.dictate_toggle()
        await self.heard_all_of(rig)
        self.assertTrue(rig.ctl.dictation)
        await rig.ctl.dictate_toggle()
        await self.wait_for(rig, "TypeText")
        rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
        await self.delivered(rig)
        self.assertEqual(rig.of("Error"), [])

    async def test_turned_off_in_settings_nothing_starts_it(self):
        rig = self.rig_for(self.speech(), dictation_enabled=False)
        await rig.ctl.dictate_start()
        self.assertIsNone(rig.ctl.dictation)
        await rig.ctl.dictate_toggle()
        self.assertIsNone(rig.ctl.dictation)
        await asyncio.sleep(0.02)
        self.assertEqual(rig.journal, [])
        self.assertEqual(rig.ctl.state, "idle")

    async def test_starting_again_while_listening_keeps_the_go_going(self):
        http = self.speech(transcript="Hello.")
        rig = self.rig_for(http)
        await rig.ctl.dictate_start()
        await self.heard_all_of(rig)
        first = rig.ctl.dictation
        await rig.ctl.dictate_start()
        self.assertIs(rig.ctl.dictation, first)
        self.assertEqual(len(rig.mics), 1)

    async def test_the_next_go_does_not_lose_the_words_at_the_server(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        http = self.speech(transcript="Hello.", wait=gate)
        rig = self.rig_for(http)
        await self.spoke(rig)
        await self.spoke(rig, mic=1)
        gate.set()
        await self.wait_for(rig, "TypeText")
        rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
        async with asyncio.timeout(5):
            while len(rig.of("TypeText")) < 2:
                await asyncio.sleep(0.002)
        rig.ctl.typed(rig.of("TypeText")[1][0], "typed")
        await self.delivered(rig)
        self.assertEqual(rig.of("Error"), [])
        self.assertEqual(len(http.uploads), 2)

    async def test_words_arrive_in_the_order_they_were_said(self):
        said = iter([("First.", 0.1), ("Second.", 0.0)])

        async def hears(_config, _pcm):
            words, delay = next(said)
            await asyncio.sleep(delay)
            return words

        rig = self.rig_for(self.speech())
        with mock.patch.object(controller, "transcribe", hears):
            await self.spoke(rig)
            await self.spoke(rig, mic=1)
            await self.wait_for(rig, "TypeText")
            self.assertEqual(rig.of("TypeText")[0][1], "First. ")
            await asyncio.sleep(0.15)
            self.assertEqual(len(rig.of("TypeText")), 1, "the second overtook the shell's answer")
            rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
            async with asyncio.timeout(5):
                while len(rig.of("TypeText")) < 2:
                    await asyncio.sleep(0.002)
            rig.ctl.typed(rig.of("TypeText")[1][0], "typed")
            await self.delivered(rig)
        self.assertEqual(rig.of("TypeText")[1][1], "Second. ")

    async def test_a_talk_hold_does_not_lose_the_words_at_the_server(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        rig = self.rig_for(self.speech(transcript="Hello.", wait=gate))
        await self.spoke(rig)
        with mock.patch.object(rig.ctl, "_start_turn"):
            await rig.ctl.hold_start()
        gate.set()
        await self.wait_for(rig, "TypeText")
        rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
        await self.delivered(rig)

    async def test_cancel_drops_the_words_at_the_server_too(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        http = self.speech(transcript="Hello.", wait=gate)
        rig = self.rig_for(http)
        await self.spoke(rig)
        await rig.ctl.cancel()
        gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(rig.of("TypeText"), [])

    async def test_losing_the_shell_stops_the_mic_and_drops_the_words(self):
        http = self.speech(transcript="Hello.")
        rig = self.rig_for(http)
        await rig.ctl.dictate_start()
        await self.heard_all_of(rig)
        await rig.ctl.shell_gone()
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.ctl.state, "idle")

    async def test_talking_to_tux_ends_dictation(self):
        http = self.speech(transcript="Hello.")
        rig = self.rig_for(http)
        await rig.ctl.dictate_start()
        await self.heard_all_of(rig)
        with mock.patch.object(rig.ctl, "_start_turn"):
            await rig.ctl.hold_start()
        self.assertEqual(http.uploads, [])
        self.assertEqual(rig.ctl.dictation, None)

    async def test_it_stops_by_itself_in_the_end_and_only_copies(self):
        rig = self.rig_for(self.speech(transcript="A long speech."), mic={})
        with mock.patch.object(dictation, "MAX_DICTATE_S", 0.2):
            await rig.ctl.dictate_start()
            await self.wait_for(rig, "TypeText")
        request, _, only_copy = rig.of("TypeText")[0]
        self.assertTrue(only_copy, "nobody let go, so nobody is there to see it being typed")
        rig.ctl.typed(request, "copied")
        await self.delivered(rig)
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])

    async def test_letting_go_is_typed_not_only_copied(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        await self.dictated(rig)
        self.assertFalse(rig.of("TypeText")[0][2])

    async def test_a_shell_that_never_answers_is_reported(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        with mock.patch.object(dictation, "TYPE_WAIT_S", 0.02):
            await self.spoke(rig)
            await self.delivered(rig)
        self.assertEqual(rig.of("Error"), [NOT_TYPED])
        self.assertEqual(rig.ctl.typing, None)

    async def test_an_answer_for_some_other_go_does_nothing(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        await self.spoke(rig)
        await self.wait_for(rig, "TypeText")
        rig.ctl.typed("a" * 32, "typed")
        await asyncio.sleep(0.02)
        self.assertFalse(rig.ctl.typing[1].done())
        rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
        await self.delivered(rig)

    async def test_a_late_answer_with_nothing_waiting_is_harmless(self):
        rig = self.rig_for(self.speech())
        rig.ctl.typed("whatever", "typed")

    async def test_an_unreachable_server_says_so(self):
        http = self.speech()
        rig = self.rig_for(http)
        http.close()
        await self.spoke(rig)
        await self.delivered(rig)
        self.assertEqual(len(rig.of("Error")), 1)
        self.assertIn("Cannot reach the server", rig.of("Error")[0])
        self.assertEqual(rig.ctl.state, "idle")

    async def test_a_server_error_says_so(self):
        rig = self.rig_for(self.speech(speech_status=500))
        await self.spoke(rig)
        await self.delivered(rig)
        self.assertEqual(rig.of("Error"), ["Server error: HTTP 500"])

    async def test_a_reply_that_is_not_text_says_so(self):
        for body in (b"not json", b'{"text": 5}', b'{"text": null}', b"[]"):
            rig = self.rig_for(self.speech(body=body))
            await self.spoke(rig)
            await self.delivered(rig)
            self.assertEqual(rig.of("Error"), ["The server did not return any text"], body)

    async def test_a_stuck_mic_cannot_hold_up_letting_go(self):
        rig = self.rig_for(self.speech(transcript="Hello."))    # gives its audio, then nothing
        await self.spoke(rig)
        await self.wait_for(rig, "TypeText")
        self.assertEqual(rig.journal, ["mic.start", "mic.stop"])

    async def test_a_mic_that_dies_part_way_keeps_what_it_heard(self):
        class Dying:
            sent = 0

            async def start(self):
                pass

            async def stop(self):
                pass

            async def read(self):
                await asyncio.sleep(0.001)
                chunk = VOICE[self.sent:self.sent + CHUNK]
                self.sent += len(chunk)
                return chunk

        http = self.speech(transcript="Half a thought.")
        rig = self.rig_for(http)
        rig.ctl.make_mic = Dying
        await rig.ctl.dictate_start()
        await rig.finished()
        await self.wait_for(rig, "TypeText")
        self.assertEqual(rig.of("Error"), ["The microphone stopped working"])
        self.assertEqual(rig.of("TypeText")[0][1], "Half a thought. ")

    async def test_letting_go_at_once_is_not_missed(self):
        rig = self.rig_for(self.speech(transcript="Hello."))
        await rig.ctl.dictate_start()
        await rig.ctl.dictate_end()
        await rig.finished()
        self.assertEqual(rig.ctl.state, "idle")

    async def test_a_dead_mic_says_so_and_types_nothing(self):
        rig = self.rig_for(self.speech(transcript="Hello."), mic={"fail": True})
        await rig.ctl.dictate_start()
        await rig.finished()
        self.assertEqual(rig.of("Error"), ["The microphone stopped working"])
        self.assertEqual(rig.of("TypeText"), [])

    async def test_it_keeps_listening_for_the_tail_of_the_last_word(self):
        rig = self.rig_for(self.speech(transcript="Hello."), mic={})
        with mock.patch.object(dictation, "TAIL_S", 0.05):
            await rig.ctl.dictate_start()
            await self.heard_all_of(rig)
            go = rig.ctl.dictation
            sent = rig.mics[0].sent
            await rig.ctl.dictate_end()
            await self.wait_for(rig, "TypeText")
        self.assertGreater(len(go.pcm), sent)
        rig.ctl.typed(rig.of("TypeText")[0][0], "typed")
        await self.delivered(rig)


if __name__ == "__main__":
    unittest.main()
