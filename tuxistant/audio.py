"""PipeWire capture and playback through pw-record / pw-cat, 24 kHz mono s16."""
import array
import asyncio
import math
import time

RATE = 24000
BYTES_PER_S = RATE * 2
FORMAT = ["--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw"]
MIN_HOLD_BYTES = int(0.4 * BYTES_PER_S)
MIN_VOICED_BYTES = int(0.12 * BYTES_PER_S)  # more than the thump of the key itself
VOICE_LEVEL = 0.15      # level() of a quiet voice. Whisper invents words for silence
STALL_S = 3     # pw-cat took no audio for this long: nothing is playing it
END_WAIT_S = 1


_ending = set()     # the event loop only keeps weak references to tasks


async def _finish(proc, steps: list):
    for stop, wait in steps:
        if stop and proc.returncode is None:
            stop()
        try:
            return await asyncio.wait_for(proc.wait(), wait)
        except TimeoutError:
            pass


async def _end(proc, patience: float = 0.0):
    """Lets a process exit within patience, then ends it. A stuck pw tool must not stall a cancel.

    A cancel can land on any await, and a live pw-record is a live microphone. So with no patience
    the signal goes out before the first await, and the rest runs in a task of its own that a
    cancel of the caller does not stop.
    """
    steps = [(None, patience), (proc.terminate, END_WAIT_S), (proc.kill, END_WAIT_S)]
    if not patience:
        if proc.returncode is None:
            proc.terminate()
        steps[:2] = [(None, END_WAIT_S)]
    task = asyncio.create_task(_finish(proc, steps))
    _ending.add(task)
    task.add_done_callback(_ending.discard)
    await asyncio.shield(task)


def level(pcm: bytes) -> float:
    """Perceptual 0..1 loudness of a s16 chunk (speech peaks near 1)."""
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return min(1.0, math.sqrt(rms) * 2.2)


class Mic:
    """Yields 40 ms chunks from the default source, the rate Level signals are wanted at."""

    CHUNK = BYTES_PER_S // 25

    def __init__(self):
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record", *FORMAT, "-", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

    async def read(self) -> bytes:
        try:
            return await self.proc.stdout.readexactly(self.CHUNK)
        except asyncio.IncompleteReadError as e:
            return e.partial

    async def stop(self):
        if self.proc:
            await _end(self.proc)
        self.proc = None


class Speaker:
    """Real-time paced playback, so levels track what is heard and cancelling is instant."""

    SLICE = BYTES_PER_S // 25  # 40 ms
    LEAD_S = 0.2    # stay this far ahead of real time: pw-cat does blocking reads and would underrun

    def __init__(self, on_level=None):
        self.on_level = on_level or (lambda v: None)
        self.buffer = bytearray()
        self.finished = False
        self.proc = None
        self.task = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "pw-cat", "--playback", *FORMAT, "-", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._pump())

    def write(self, pcm: bytes):
        self.buffer += pcm

    async def _pump(self):
        deadline = time.monotonic()
        while self.buffer or not self.finished:
            if not self.buffer:
                await asyncio.sleep(0.01)
                deadline = time.monotonic()
                continue
            chunk = bytes(self.buffer[: self.SLICE])
            del self.buffer[: self.SLICE]
            self.proc.stdin.write(chunk)
            try:
                await asyncio.wait_for(self.proc.stdin.drain(), STALL_S)
            except TimeoutError:
                raise OSError("Nothing is playing the audio output") from None
            except ConnectionError:
                raise OSError("The audio output stopped working") from None
            self.on_level(level(chunk))
            deadline += len(chunk) / BYTES_PER_S
            await asyncio.sleep(max(0.0, deadline - self.LEAD_S - time.monotonic()))

    async def drain(self):
        """Finish playing everything written so far."""
        self.finished = True
        await self.task
        self.proc.stdin.close()
        await _end(self.proc, self.LEAD_S + 2)
        self.on_level(0.0)

    async def stop(self):
        """Stop immediately (barge-in or cancel)."""
        self.buffer.clear()
        self.finished = True
        if self.task:
            self.task.cancel()
        try:
            if self.proc:   # before any await, so that a cancel cannot leave pw-cat running
                await _end(self.proc)
            if self.task:
                await asyncio.gather(self.task, return_exceptions=True)
        finally:
            self.on_level(0.0)
