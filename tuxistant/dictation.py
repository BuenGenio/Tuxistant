"""Dictation: the microphone to the focused window, with no model in between.

The user's own key press starts it and no tool can, and the words are only ever typed, so nothing
here needs an approval card. See "Dictation" in docs/CONTRACT.md.
"""
import asyncio
import logging
import re
import secrets
import time
import unicodedata

from .audio import MIN_HOLD_BYTES, MIN_VOICED_BYTES, VOICE_LEVEL, level
from .realtime import RealtimeError

log = logging.getLogger("tuxistant")

MAX_DICTATE_S = 300     # a stuck key must not leave the mic open for ever
TAIL_S = 0.2            # people let go on the last word, not after it
TYPE_WAIT_S = 3         # the shell answers at once if it answers at all
NOT_TYPED = "The desktop didn't take the text, so nothing was typed"
# A go that types nothing says so: the shell said "Dictating…", and silence would look like a fault.
TOO_SHORT = "That was too short, so nothing was typed."
NOTHING_HEARD = "I didn't hear any words, so nothing was typed."
SLEPT = "The computer went to sleep, so I stopped dictating and nothing was typed."
SLEEP_CHECK_S = 1.0     # how soon after waking a go that nobody holds a key for is ended
MODES = ("hold", "tap", "shortcut")     # how it stops: let go of Fn, tap Fn, or the shortcut again
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


def asleep_s() -> float:
    """How long the computer has been suspended since it booted: the boot clock counts that time,
    the monotonic one doesn't."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


class Dictation:
    """One go at dictating, from the key press to the words reaching the window."""

    def __init__(self, ctl, mode: str | None = None):
        self.ctl = ctl
        self.mode = mode        # None until Fn says whether the second press was a tap or a hold
        self.started = False    # the shell has heard DictationStarted, so it can hear the mode
        self.id = secrets.token_hex(16)
        self.pcm = bytearray()
        self.voiced = 0
        self.released = asyncio.Event()
        self.ran_out = False    # nobody let go, so nobody is there to see words being typed
        self.slept = False      # the computer was suspended while it listened

    def finish(self):
        self.released.set()

    async def run(self):
        """The listening. The words go on from here without this job: see Controller.deliver."""
        ctl = self.ctl
        ctl.set_presence(True)
        ctl.set_state("listening")
        ctl.emit("DictationStarted", self.id)
        self.started = True
        if self.mode:
            ctl.emit("DictationMode", self.id, self.mode)
        broke = False
        try:
            await self._record()
        except RealtimeError as e:
            broke = True
            if not self.slept:
                ctl.emit("Error", str(e))   # what it heard before that still counts
        if self.slept:
            ctl.emit("Error", SLEPT)
        elif self._enough():
            ctl.deliver(self)
        elif not broke:
            ctl.emit("Error", TOO_SHORT if len(self.pcm) < MIN_HOLD_BYTES else NOTHING_HEARD)

    def _enough(self) -> bool:
        return len(self.pcm) >= MIN_HOLD_BYTES and self.voiced >= MIN_VOICED_BYTES

    def set_mode(self, mode: str):
        if mode in MODES and mode != self.mode:
            self.mode = mode
            if self.started:
                self.ctl.emit("DictationMode", self.id, mode)

    async def _record(self):
        """Letting go ends this whether or not the microphone has anything to say."""
        listening = asyncio.create_task(self._listen())
        released = asyncio.create_task(self.released.wait())
        before = asleep_s()
        woke = asyncio.create_task(self._woke(before))
        try:
            await asyncio.wait({listening, released, woke}, timeout=MAX_DICTATE_S,
                               return_when=asyncio.FIRST_COMPLETED)
            self.slept = self._slept_since(before) and not self.released.is_set()
            self.ran_out = not listening.done() and not self.released.is_set()
            if not listening.done() and not self.slept:
                await asyncio.wait({listening}, timeout=TAIL_S)
        finally:
            for task in (listening, released, woke):
                task.cancel()
            await asyncio.gather(listening, released, woke, return_exceptions=True)
        if not listening.cancelled() and listening.exception():
            raise listening.exception()

    async def _woke(self, before: float):
        """Returns on waking from a suspend. A go with no key held would listen on after it, and
        the time limit runs on the monotonic clock, which stops while the computer sleeps."""
        while not self._slept_since(before):
            await asyncio.sleep(SLEEP_CHECK_S)

    def _slept_since(self, before: float) -> bool:
        """A held Fn is let go by the helper on waking, so a held go ends the usual way. A
        microphone that dies on the way into a suspend doesn't make it count less."""
        return asleep_s() - before > 1.0 and self.mode != "hold"

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
