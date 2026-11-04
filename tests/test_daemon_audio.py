"""The real Mic and Speaker, driven against stand-ins for pw-record and pw-cat.

PATH holds only the stand-ins, so the real tools cannot run: nothing here can reach the speakers
or the microphone.
"""
import asyncio
import os
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import CHUNK, Case, silence, tone  # noqa: E402
from tuxistant import audio  # noqa: E402
from tuxistant.audio import BYTES_PER_S, FORMAT, Mic, Speaker, level  # noqa: E402

COPY_STDIN = '#!/bin/sh\necho "$@" > "$OUT.args"\nexec /usr/bin/cat > "$OUT"\n'
NEVER_READS = "#!/bin/sh\nexec /usr/bin/sleep 30\n"
IGNORES_TERM = "#!/bin/sh\ntrap '' TERM\n/usr/bin/cat > /dev/null\nwhile :; do /usr/bin/sleep 1; done\n"
GIVES_FILE = '#!/bin/sh\necho "$@" > "$OUT.args"\n/usr/bin/cat "$OUT.in"\n'
FAILS = "#!/bin/sh\nexit 1\n"


class Tools(Case):
    def setUp(self):
        super().setUp()
        self.out = self.folder / "out"
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.folder), "OUT": str(self.out)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, name: str, script: str):
        path = self.folder / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def speaker(self, levels=None) -> Speaker:
        speaker = Speaker(levels.append if levels is not None else None)
        self.addAsyncCleanup(speaker.stop)
        return speaker


class Playback(Tools):
    async def test_plays_everything_with_the_right_arguments(self):
        self.tool("pw-cat", COPY_STDIN)
        pcm = tone(0.5)
        levels = []
        speaker = self.speaker(levels)
        await speaker.start()
        speaker.write(pcm)
        await speaker.drain()
        self.assertEqual(self.out.read_bytes(), pcm)
        self.assertEqual(Path(f"{self.out}.args").read_text().split(),
                         ["--playback", *FORMAT, "-"])
        self.assertGreater(max(levels), 0.1)
        self.assertEqual(levels[-1], 0.0)

    async def test_paces_at_real_time_less_the_lead(self):
        self.tool("pw-cat", COPY_STDIN)
        speaker = self.speaker()
        await speaker.start()
        start = time.monotonic()
        speaker.write(tone(0.6))
        await speaker.drain()
        elapsed = time.monotonic() - start
        self.assertGreater(elapsed, 0.6 - Speaker.LEAD_S - 0.1)
        self.assertLess(elapsed, 0.6 + 0.3)

    async def test_stop_is_instant_and_drops_the_rest(self):
        self.tool("pw-cat", COPY_STDIN)
        levels = []
        speaker = self.speaker(levels)
        await speaker.start()
        speaker.write(tone(5))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await speaker.stop()
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertLess(self.out.stat().st_size, BYTES_PER_S)
        self.assertEqual(levels[-1], 0.0)
        self.assertIsNotNone(speaker.proc.returncode)

    async def test_write_during_playback(self):
        self.tool("pw-cat", COPY_STDIN)
        speaker = self.speaker()
        await speaker.start()
        speaker.write(tone(0.1))
        await asyncio.sleep(0.2)    # runs dry, as between sentences
        speaker.write(silence(0.1))
        await speaker.drain()
        self.assertEqual(self.out.read_bytes(), tone(0.1) + silence(0.1))

    async def test_output_that_takes_no_audio_is_an_error_not_a_hang(self):
        self.tool("pw-cat", NEVER_READS)
        speaker = self.speaker()
        with mock.patch.object(audio, "STALL_S", 0.1), mock.patch.object(Speaker, "LEAD_S", 100):
            await speaker.start()
            speaker.write(tone(30))
            with self.assertRaisesRegex(OSError, "Nothing is playing"):
                await asyncio.wait_for(speaker.drain(), 8)

    async def test_a_process_ignoring_sigterm_is_killed(self):
        self.tool("pw-cat", IGNORES_TERM)
        speaker = self.speaker()
        await speaker.start()
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await speaker.stop()
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsNotNone(speaker.proc.returncode)

    async def test_dying_output_is_an_error_not_a_hang(self):
        self.tool("pw-cat", FAILS)
        speaker = self.speaker()
        await speaker.start()
        speaker.write(tone(2))
        with self.assertRaisesRegex(OSError, "stopped working"):
            await asyncio.wait_for(speaker.drain(), 8)

    async def test_missing_tool(self):
        with self.assertRaises(FileNotFoundError):
            await Speaker().start()


class Ending(unittest.IsolatedAsyncioTestCase):
    async def test_a_process_that_never_reports_its_exit_cannot_stall_a_cancel(self):
        class Stuck:
            returncode = None
            terminate = kill = mock.Mock()

            async def wait(self):
                await asyncio.Event().wait()

        with mock.patch.object(audio, "END_WAIT_S", 0.01):
            await asyncio.wait_for(audio._end(Stuck()), 1)
        self.assertEqual(Stuck.kill.call_count, 2)


class Capture(Tools):
    async def test_reads_chunks_with_the_right_arguments(self):
        Path(f"{self.out}.in").write_bytes(tone(0.2))
        self.tool("pw-record", GIVES_FILE)
        mic = Mic()
        self.addAsyncCleanup(mic.stop)
        await mic.start()
        got = b""
        while chunk := await mic.read():
            self.assertLessEqual(len(chunk), Mic.CHUNK)
            got += chunk
        self.assertEqual(got, tone(0.2))
        self.assertEqual(Path(f"{self.out}.args").read_text().split(), [*FORMAT, "-"])
        self.assertEqual(Mic.CHUNK, CHUNK)

    async def test_stop_twice_and_before_start(self):
        self.tool("pw-record", NEVER_READS)
        mic = Mic()
        await mic.stop()
        await mic.start()
        await mic.stop()
        await mic.stop()


class Level(unittest.TestCase):
    def test_silence_and_speech(self):
        self.assertEqual(level(b""), 0.0)
        self.assertEqual(level(silence(0.04)), 0.0)
        self.assertGreater(level(tone(0.04)), 0.5)
        self.assertLessEqual(level(tone(0.04, 32767)), 1.0)


if __name__ == "__main__":
    unittest.main()
