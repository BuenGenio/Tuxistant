"""Hold-Fn-to-talk: turns the helper's raw key events into HoldStart and HoldEnd."""
import asyncio
import os
import time

SOCKET = "/run/tuxistant-fn/events.sock"
RETRY_S = 5
TAP_S = 0.3             # a press this short is a tap
SECOND_PRESS_S = 0.4    # and the press that starts dictation comes this soon after it


class FnClient:
    """The helper only reports keys; all timing is here. See "Fn helper" in the contract."""

    def __init__(self, config, on_hold_start, on_hold_end, path=None,
                 on_dictate_start=None, on_dictate_end=None, on_dictate_cancel=None):
        self.config, self.on_hold_start, self.on_hold_end = config, on_hold_start, on_hold_end
        self.on_dictate_start, self.on_dictate_end = on_dictate_start, on_dictate_end
        self.on_dictate_cancel = on_dictate_cancel
        self.path = path or os.environ.get("TUXISTANT_FN_SOCKET", SOCKET)
        self.timer = None
        self.holding = False
        self.dictating = False
        self.pressed_at = self.tapped_at = float("-inf")

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
                await self.handle("up")     # a lost helper must not leave the mic open
            await asyncio.sleep(RETRY_S)

    async def handle(self, word: str):
        now = time.monotonic()
        if word == "down":
            self._disarm()
            self.pressed_at = now
            if self._second_press(now):
                self.dictating = True
                self.tapped_at = float("-inf")
                await self.on_dictate_start()
            elif self.config["fn_hold_enabled"] and not self.holding and not self.dictating:
                self.timer = asyncio.create_task(self._fire(self.config["fn_hold_ms"] / 1000))
        elif word == "other":
            self._disarm()
            self.pressed_at = float("-inf")
            if self.dictating:      # a Fn shortcut, not dictation
                self.dictating = False
                await self.on_dictate_cancel()
        elif word == "up":
            self._disarm()
            if self.dictating:
                self.dictating = False
                await self.on_dictate_end()
            elif self.holding:
                self.holding = False
                await self.on_hold_end()
            elif now - self.pressed_at < TAP_S:
                self.tapped_at = now

    def _second_press(self, now: float) -> bool:
        return (self.on_dictate_start is not None and self.config["dictation_enabled"]
                and not self.holding and not self.dictating
                and now - self.tapped_at < SECOND_PRESS_S)

    def _disarm(self):
        if self.timer and not self.holding:
            self.timer.cancel()
        self.timer = None

    async def _fire(self, delay: float):
        await asyncio.sleep(delay)
        self.holding = True
        await self.on_hold_start()
