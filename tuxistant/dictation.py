"""Dictation: the microphone to the focused window, with no model in between.

The user's own key press starts it and no tool can, and the words are only ever typed, so nothing
here needs an approval card. See "Dictation" in docs/CONTRACT.md.
"""
import asyncio
import logging
import re
import secrets
import unicodedata

from .audio import MIN_HOLD_BYTES, MIN_VOICED_BYTES, VOICE_LEVEL, level
from .realtime import RealtimeError

log = logging.getLogger("tuxistant")

MAX_DICTATE_S = 300     # a stuck key must not leave the mic open for ever
TAIL_S = 0.2            # people let go on the last word, not after it
TYPE_WAIT_S = 3         # the shell answers at once if it answers at all
NOT_TYPED = "The desktop didn't take the text, so nothing was typed"
# Whisper's words for no words: [BLANK_AUDIO], (music), ♪. One group only, or a real sentence with
# brackets at both ends would match.
NO_WORDS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪[^♪]*♪|♪+")
JOINERS = "\u200c\u200d"     # invisible, but they're spelling in Persian and in emoji


def clean(text: str) -> str:
    """Words that are safe to type. A line break is Enter in a terminal or a chat box."""
    def safe(c: str) -> str:
        kind = unicodedata.category(c)
        if kind in ("Cc", "Zl", "Zp"):
            return " "
        return "" if kind == "Cf" and c not in JOINERS else c

    words = " ".join("".join(map(safe, text)).split())
    return "" if NO_WORDS.fullmatch(words) else words


class Dictation:
    """One go at dictating, from the key press to the words reaching the window."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.id = secrets.token_hex(16)
        self.pcm = bytearray()
        self.voiced = 0
        self.released = asyncio.Event()
        self.ran_out = False    # nobody let go, so nobody is there to see words being typed

    def finish(self):
        self.released.set()

    async def run(self):
        """The listening. The words go on from here without this job: see Controller.deliver."""
        ctl = self.ctl
        ctl.set_presence(True)
        ctl.set_state("listening")
        ctl.emit("DictationStarted", self.id)
        try:
            await self._record()
        except RealtimeError as e:
            ctl.emit("Error", str(e))   # what it heard before that still counts
        if len(self.pcm) >= MIN_HOLD_BYTES and self.voiced >= MIN_VOICED_BYTES:
            ctl.deliver(self)

    async def _record(self):
        """Letting go ends this whether or not the microphone has anything to say."""
        listening = asyncio.create_task(self._listen())
        released = asyncio.create_task(self.released.wait())
        try:
            await asyncio.wait({listening, released}, timeout=MAX_DICTATE_S,
                               return_when=asyncio.FIRST_COMPLETED)
            self.ran_out = not listening.done() and not self.released.is_set()
            if not listening.done():
                await asyncio.wait({listening}, timeout=TAIL_S)
        finally:
            listening.cancel()
            released.cancel()
            await asyncio.gather(listening, released, return_exceptions=True)
        if not listening.cancelled() and listening.exception():
            raise listening.exception()

    async def _listen(self):
        mic = self.ctl.make_mic()
        try:
            await mic.start()
            while True:
                pcm = await mic.read()
                if not pcm:
                    raise RealtimeError("The microphone stopped working")
                loudness = level(pcm)
                self.ctl.emit("Level", loudness)
                self.pcm += pcm
                if loudness >= VOICE_LEVEL:
                    self.voiced += len(pcm)
        finally:
            await mic.stop()
            self.ctl.emit("Level", 0.0)

    async def type(self, text: str):
        ctl = self.ctl
        done = asyncio.get_running_loop().create_future()
        ctl.typing = (self.id, done)
        ctl.emit("TypeText", self.id, text, self.ran_out)
        try:
            log.info("dictation: %s", await asyncio.wait_for(done, TYPE_WAIT_S))
        except TimeoutError:
            raise RealtimeError(NOT_TYPED) from None
        finally:
            ctl.typing = None
