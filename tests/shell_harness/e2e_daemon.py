#!/usr/bin/env python3
"""Test only: the real daemon with a scripted microphone and a speaker that plays nothing.

Everything except the two ends of the audio path is production code: tuxistant.service.run, the
controller, the realtime client and D-Bus. pw-record and pw-cat are never started, so this cannot
touch real audio. TUX_E2E_DIR is the folder shared with the scenario:

    mic.pcm        what the next microphone hears, then silence. Taken when the mic opens
    speaker-N.pcm  what each speaker was given
    audio.log      one JSON line per audio event
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tuxistant.audio import Mic, Speaker  # noqa: E402
from tuxistant.service import run  # noqa: E402

FOLDER = Path(os.environ["TUX_E2E_DIR"])


def note(device: str, event: str, **details):
    with open(FOLDER / "audio.log", "a") as log:
        print(json.dumps({"t": time.monotonic(), "device": device, "event": event, **details}),
              file=log)


class ScriptedMic(Mic):
    """Paced like a real capture: one 40 ms chunk every 40 ms, for as long as it stays open."""

    async def start(self):
        clip = FOLDER / "mic.pcm"
        self.pcm = clip.read_bytes() if clip.exists() else b""
        clip.unlink(missing_ok=True)
        self.sent = 0
        self.next_at = time.monotonic()
        note("mic", "start", clip_bytes=len(self.pcm))

    async def read(self) -> bytes:
        self.next_at += self.CHUNK / 48000
        await asyncio.sleep(max(0.0, self.next_at - time.monotonic()))
        chunk = self.pcm[self.sent:self.sent + self.CHUNK]
        self.sent += len(chunk)
        if chunk and self.sent >= len(self.pcm):
            note("mic", "clip-end")
        return chunk + bytes(self.CHUNK - len(chunk))

    async def stop(self):
        note("mic", "stop")


class Sink:
    """Stands in for the pw-cat process under the real Speaker, which keeps its pacing and levels."""

    def __init__(self, path: Path):
        self.file = open(path, "wb")
        self.stdin = self
        self.returncode = None
        self.closed = asyncio.Event()

    def write(self, data: bytes):
        self.file.write(data)
        self.file.flush()

    async def drain(self):
        pass

    def close(self):
        self.file.close()
        self.returncode = 0
        self.closed.set()

    terminate = kill = close

    async def wait(self):
        await self.closed.wait()
        return self.returncode


class RecordingSpeaker(Speaker):
    count = 0

    async def start(self):
        RecordingSpeaker.count += 1
        self.number = RecordingSpeaker.count
        self.proc = Sink(FOLDER / f"speaker-{self.number}.pcm")
        self.task = asyncio.create_task(self._pump())
        note("speaker", "start", number=self.number)

    async def drain(self):
        await super().drain()
        note("speaker", "drained", number=self.number)

    async def stop(self):
        await super().stop()
        note("speaker", "stopped", number=self.number)


if __name__ == "__main__":
    sys.exit(asyncio.run(run(mic_factory=ScriptedMic, speaker_factory=RecordingSpeaker)))
