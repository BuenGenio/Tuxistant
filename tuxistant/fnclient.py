"""Hold-Fn-to-talk: turns the helper's raw key events into HoldStart and HoldEnd."""
import asyncio
import os

SOCKET = "/run/tuxistant-fn/events.sock"
RETRY_S = 5


class FnClient:
    """The helper only reports keys; all timing is here. See "Fn helper" in the contract."""

    def __init__(self, config, on_hold_start, on_hold_end, path=None):
        self.config, self.on_hold_start, self.on_hold_end = config, on_hold_start, on_hold_end
        self.path = path or os.environ.get("TUXISTANT_FN_SOCKET", SOCKET)
        self.timer = None
        self.holding = False

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
        if word == "down":
            self._disarm()
            if self.config["fn_hold_enabled"] and not self.holding:
                self.timer = asyncio.create_task(self._fire(self.config["fn_hold_ms"] / 1000))
        elif word == "other":
            self._disarm()
        elif word == "up":
            self._disarm()
            if self.holding:
                self.holding = False
                await self.on_hold_end()

    def _disarm(self):
        if self.timer and not self.holding:
            self.timer.cancel()
        self.timer = None

    async def _fire(self, delay: float):
        await asyncio.sleep(delay)
        self.holding = True
        await self.on_hold_start()
