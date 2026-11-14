"""Hold-Fn-to-talk and Fn dictation: turns the helper's raw key events into HoldStart, HoldEnd and
dictation. See "Fn helper" and "Dictation" in docs/CONTRACT.md."""
import asyncio
import os
import time

SOCKET = "/run/tuxistant-fn/events.sock"
RETRY_S = 5
TAP_S = 0.3             # a press this short is a tap
SECOND_PRESS_S = 0.4    # and the press that starts dictation comes this soon after it
NONE = float("-inf")

# How a go that Fn started ends, as the shell is told it.
HOLD = "hold"           # the second press was held: letting go ends it
TAP = "tap"             # the second press was a tap too: hands-free until the next Fn tap


class FnClient:
    """The helper only reports keys; all timing is here. See "Fn helper" in the contract."""

    def __init__(self, config, on_hold_start, on_hold_end, path=None,
                 on_dictate_start=None, on_dictate_end=None, on_dictate_cancel=None,
                 on_dictate_mode=None, dictating=None):
        self.config, self.on_hold_start, self.on_hold_end = config, on_hold_start, on_hold_end
        self.on_dictate_start, self.on_dictate_end = on_dictate_start, on_dictate_end
        self.on_dictate_cancel, self.on_dictate_mode = on_dictate_cancel, on_dictate_mode
        # Whether the daemon is still dictating. The shortcut, Cancel, a lock or the time limit
        # can end a hands-free go without Fn, and then the next tap is a tap again.
        self.still_dictating = dictating
        self.path = path or os.environ.get("TUXISTANT_FN_SOCKET", SOCKET)
        self.timer = None
        self.holding = False
        self.dictating = None       # None, "pressed" (second press still down), HOLD or TAP
        self.stopping = False       # hands-free, and Fn is down with no other key yet
        self.pressed_at = self.tapped_at = NONE

    async def run(self):
        """Forever. The helper is optional, so a missing socket is retried without complaint."""
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path)
            except OSError:
                await asyncio.sleep(RETRY_S)
                continue
            try:
                while line := await reader.readline():
                    await self.handle(line.decode("ascii", "replace").strip())
            except (OSError, ValueError):
                pass
            finally:
                writer.close()
                await self.lost()
            await asyncio.sleep(RETRY_S)

    async def lost(self):
        """A lost helper must not leave the mic open: with no Fn, nothing could tap to stop it."""
        self._forget_if_over()
        self._disarm()
        self.pressed_at = NONE
        if self.dictating:
            self._forget()
            await self.on_dictate_end()
        elif self.holding:
            self.holding = False
            await self.on_hold_end()

    async def handle(self, word: str):
        now = time.monotonic()
        self._forget_if_over()
        if word == "down":
            await self._down(now)
        elif word == "other":
            self._disarm()
            self.pressed_at = NONE
            if self.dictating == TAP:   # a Fn shortcut while dictating hands-free: not a stop
                self.stopping = False
            elif self.dictating:        # the second press was a Fn shortcut, not dictation
                self._forget()
                await self.on_dictate_cancel()
        elif word == "up":
            await self._up(now)

    async def _down(self, now: float):
        self._disarm()
        self.pressed_at = now
        if self.dictating == TAP:
            self.stopping = True
        elif self._second_press(now):
            self.dictating = "pressed"
            self.tapped_at = NONE
            await self.on_dictate_start()
            if self.dictating == "pressed":
                self.timer = asyncio.create_task(self._still_down(TAP_S))
        elif self.config["fn_hold_enabled"] and not self.holding and not self.dictating:
            self.timer = asyncio.create_task(self._fire(self.config["fn_hold_ms"] / 1000))

    async def _up(self, now: float):
        self._disarm()
        quick = now - self.pressed_at < TAP_S
        if self.dictating == TAP:     # the tap that ends it is no tap towards the next go
            if self.stopping:
                self._forget()
                await self.on_dictate_end()
        elif self.dictating and quick:
            self.dictating = TAP
            await self._mode(TAP)
        elif self.dictating:
            self._forget()
            await self.on_dictate_end()
        elif self.holding:
            self.holding = False
            await self.on_hold_end()
        elif quick:
            self.tapped_at = now

    def _second_press(self, now: float) -> bool:
        return (self.on_dictate_start is not None and self.config["dictation_enabled"]
                and not self.holding and not self.dictating
                and now - self.tapped_at < SECOND_PRESS_S)

    def _forget(self):
        self.dictating = None
        self.stopping = False

    def _forget_if_over(self):
        if self.dictating and self.still_dictating and not self.still_dictating():
            self._disarm()
            self._forget()

    async def _mode(self, mode: str):
        if self.on_dictate_mode:
            await self.on_dictate_mode(mode)

    def _disarm(self):
        if self.timer and not self.holding:
            self.timer.cancel()
        self.timer = None

    async def _fire(self, delay: float):
        await asyncio.sleep(delay)
        self.holding = True
        await self.on_hold_start()

    async def _still_down(self, delay: float):
        await asyncio.sleep(delay)
        if self.dictating == "pressed":
            self.dictating = HOLD
            await self._mode(HOLD)
