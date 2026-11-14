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


def _left(since: float, seconds: float) -> float:
    """What is left of seconds counted from since, the time a key arrived."""
    return max(0.0, since + seconds - time.monotonic())


class FnClient:
    """The helper only reports keys; all timing is here. See "Fn helper" in the contract."""

    def __init__(self, config, on_hold_start, on_hold_end, path=None,
                 on_dictate_start=None, on_dictate_end=None, on_dictate_cancel=None,
                 on_dictate_mode=None, dictating=None):
        self.config, self.on_hold_start, self.on_hold_end = config, on_hold_start, on_hold_end
        self.on_dictate_start, self.on_dictate_end = on_dictate_start, on_dictate_end
        self.on_dictate_cancel, self.on_dictate_mode = on_dictate_cancel, on_dictate_mode
        # Whether the go Fn started is still listening, given its id. The shortcut, Cancel, a
        # lock or the time limit can end a hands-free go without Fn, and then the next tap is a
        # tap again.
        self.still_dictating = dictating
        self.path = path or os.environ.get("TUXISTANT_FN_SOCKET", SOCKET)
        self.timer = None
        self.holding = False
        self.dictating = None       # None, "pressed" (second press still down), HOLD or TAP
        self.go = None              # the id of the go Fn started, which is all Fn may end or drop
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
            events = asyncio.Queue()
            reading = asyncio.create_task(self._read(reader, events))
            try:
                while event := await events.get():
                    await self.handle(*event)
            except (OSError, ValueError):
                pass
            finally:
                reading.cancel()
                writer.close()
                await self.lost()
            await asyncio.sleep(RETRY_S)

    @staticmethod
    async def _read(reader, events):
        """Each key is timed as it arrives. Handling one can wait on the controller, stopping a
        reply for up to 2 s, and a tap timed after that wait would look like a hold."""
        try:
            while line := await reader.readline():
                events.put_nowait((line.decode("ascii", "replace").strip(), time.monotonic()))
        except (OSError, ValueError):
            pass
        finally:
            events.put_nowait(None)

    async def lost(self):
        """A lost helper must not leave the mic open: with no Fn, nothing could tap to stop it."""
        self._forget_if_over()
        self._disarm()
        self.pressed_at = NONE
        if self.dictating:
            await self.on_dictate_end(go=self._forget())
        elif self.holding:
            self.holding = False
            await self.on_hold_end()

    async def handle(self, word: str, now: float | None = None):
        """now: when the key arrived from the helper."""
        now = time.monotonic() if now is None else now
        self._forget_if_over()
        if word == "down":
            await self._down(now)
        elif word == "other":
            self._disarm()
            self.pressed_at = NONE
            if self.dictating == TAP:   # a Fn shortcut while dictating hands-free: not a stop
                self.stopping = False
            elif self.dictating:        # the second press was a Fn shortcut, not dictation
                await self.on_dictate_cancel(go=self._forget())
        elif word == "up":
            await self._up(now)

    async def _down(self, now: float):
        self._disarm()
        self.pressed_at = now
        if self.dictating == TAP:
            self.stopping = True
        elif self._second_press(now):
            self.tapped_at = NONE
            # A go that was already listening, as one from the shortcut, is not Fn's to take
            # over: Fn+key would drop it. So only a go this press started is Fn's.
            go = await self.on_dictate_start()
            if go is not None:
                self.dictating, self.go = "pressed", go
                self.timer = asyncio.create_task(self._still_down(_left(now, TAP_S)))
        elif self.config["fn_hold_enabled"] and not self.holding and not self.dictating:
            self.timer = asyncio.create_task(self._fire(now))

    async def _up(self, now: float):
        self._disarm()
        quick = now - self.pressed_at < TAP_S
        if self.dictating == TAP:     # the tap that ends it is no tap towards the next go
            if self.stopping:
                await self.on_dictate_end(go=self._forget())
        elif self.dictating and quick:
            self.dictating = TAP
            await self._mode(TAP)
        elif self.dictating:
            await self.on_dictate_end(go=self._forget())
        elif self.holding:
            self.holding = False
            await self.on_hold_end()
        elif quick:
            self.tapped_at = now

    def _second_press(self, now: float) -> bool:
        return (self.on_dictate_start is not None and self.config["dictation_enabled"]
                and not self.holding and not self.dictating
                and now - self.tapped_at < SECOND_PRESS_S)

    def _forget(self) -> str | None:
        """Lets go of Fn's go, and returns its id."""
        go, self.go = self.go, None
        self.dictating = None
        self.stopping = False
        return go

    def _forget_if_over(self):
        if self.dictating and self.still_dictating and not self.still_dictating(self.go):
            self._disarm()
            self._forget()

    async def _mode(self, mode: str):
        if self.on_dictate_mode:
            await self.on_dictate_mode(mode, go=self.go)

    def _disarm(self):
        if self.timer and not self.holding:
            self.timer.cancel()
        self.timer = None

    async def _fire(self, pressed: float):
        """held tells the turn how long ago Fn went down: a question on screen is answered by a
        press made while it was up, however long Fn takes to count as held."""
        await asyncio.sleep(_left(pressed, self.config["fn_hold_ms"] / 1000))
        self.holding = True
        await self.on_hold_start(held=max(0.0, time.monotonic() - pressed))

    async def _still_down(self, delay: float):
        await asyncio.sleep(delay)
        if self.dictating == "pressed":
            self.dictating = HOLD
            await self._mode(HOLD)
