"""Fn hold timing against a fake helper socket."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_daemon_fakes import Case  # noqa: E402
from tuxistant import fnclient  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.fnclient import FnClient  # noqa: E402

HOLD_MS = 60
PAST_HOLD = 0.12
INSIDE_HOLD = 0.02


class FakeHelper:
    """The socket side of tuxistant-fn."""

    def __init__(self, path):
        self.path = path
        self.clients = []
        self.connected = asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_unix_server(self._client, self.path)

    async def _client(self, _reader, writer):
        self.clients.append(writer)
        self.connected.set()

    async def send(self, *words):
        for word in words:
            self.clients[-1].write(word.encode() + b"\n")
        await self.clients[-1].drain()

    async def stop(self):
        for client in self.clients:
            client.close()
        self.server.close()
        await self.server.wait_closed()


class Timing(Case):
    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.config.values.update(fn_hold_ms=HOLD_MS, fn_hold_enabled=True)
        self.calls = []
        self.path = str(self.folder / "events.sock")
        self.helper = FakeHelper(self.path)
        await self.helper.start()
        self.addAsyncCleanup(self.helper.stop)
        self.run_client()
        await asyncio.wait_for(self.helper.connected.wait(), 2)

    def run_client(self):
        async def start():
            self.calls.append("start")

        async def end():
            self.calls.append("end")

        self.client = FnClient(self.config, start, end, path=self.path)
        task = asyncio.create_task(self.client.run())
        self.addCleanup(task.cancel)

    async def test_hold_fires_after_fn_hold_ms_and_up_ends_it(self):
        await self.helper.send("down")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, [])
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, ["start"])
        await self.helper.send("up")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, ["start", "end"])

    async def test_early_up_does_nothing(self):
        await self.helper.send("down")
        await asyncio.sleep(INSIDE_HOLD)
        await self.helper.send("up")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, [])

    async def test_other_key_cancels_the_timer(self):
        await self.helper.send("down")
        await asyncio.sleep(INSIDE_HOLD)
        await self.helper.send("other")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, [])
        await self.helper.send("up")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, [])

    async def test_other_key_after_it_fired_changes_nothing(self):
        await self.helper.send("down")
        await asyncio.sleep(PAST_HOLD)
        await self.helper.send("other")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, ["start"])
        await self.helper.send("up")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, ["start", "end"])

    async def test_disabled_ignores_fn(self):
        self.config.values["fn_hold_enabled"] = False
        await self.helper.send("down")
        await asyncio.sleep(PAST_HOLD)
        await self.helper.send("up")
        await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, [])

    async def test_fn_hold_ms_is_read_at_each_press(self):
        self.config.values["fn_hold_ms"] = 400
        await self.helper.send("down")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, [])

    async def test_a_second_hold_works(self):
        for _ in range(2):
            await self.helper.send("down")
            await asyncio.sleep(PAST_HOLD)
            await self.helper.send("up")
            await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, ["start", "end", "start", "end"])

    async def test_unknown_words_are_ignored(self):
        await self.helper.send("rm -rf", "", "down", "KEY_A")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, ["start"])

    async def test_losing_the_helper_mid_hold_ends_the_hold(self):
        with mock.patch.object(fnclient, "RETRY_S", 0.02):
            await self.helper.send("down")
            await asyncio.sleep(PAST_HOLD)
            self.helper.clients[-1].close()
            await asyncio.sleep(INSIDE_HOLD)
        self.assertEqual(self.calls, ["start", "end"])

    async def test_losing_the_helper_before_the_timer_cancels_it(self):
        with mock.patch.object(fnclient, "RETRY_S", 0.02):
            await self.helper.send("down")
            self.helper.clients[-1].close()
            await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, [])


class Dictating(Case):
    """Tap Fn, then press and hold it. Real times, as the tap rules are the point."""

    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.config.values.update(fn_hold_ms=HOLD_MS, fn_hold_enabled=True)
        self.calls = []

        def note(word):
            async def call():
                self.calls.append(word)
            return call

        self.client = FnClient(self.config, note("hold"), note("hold end"), path="unused",
                               on_dictate_start=note("dictate"), on_dictate_end=note("typed"),
                               on_dictate_cancel=note("dropped"))
        self.addCleanup(self.client._disarm)

    async def send(self, *words, gap=0.01):
        for word in words:
            await self.client.handle(word)
            await asyncio.sleep(gap)

    async def test_a_tap_then_a_press_dictates_at_once_and_up_ends_it(self):
        await self.send("down", "up", "down")
        self.assertEqual(self.calls, ["dictate"])
        await self.send("up")
        self.assertEqual(self.calls, ["dictate", "typed"])

    async def test_holding_on_after_starting_never_becomes_a_talk_hold(self):
        await self.send("down", "up", "down")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, ["dictate"])

    async def test_one_press_is_still_a_talk_hold(self):
        await self.send("down")
        await asyncio.sleep(PAST_HOLD)
        await self.send("up")
        self.assertEqual(self.calls, ["hold", "hold end"])

    async def test_a_slow_second_press_is_not_dictation(self):
        await self.send("down", "up")
        with mock.patch.object(fnclient, "SECOND_PRESS_S", 0.001):
            await self.send("down")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, ["hold"])

    async def test_a_long_first_press_is_not_a_tap(self):
        with mock.patch.object(fnclient, "TAP_S", 0.001):
            await self.send("down", "up", "down")
        self.assertNotIn("dictate", self.calls)

    async def test_a_fn_shortcut_is_not_a_tap(self):
        await self.send("down", "other", "up", "down")
        self.assertNotIn("dictate", self.calls)

    async def test_a_fn_shortcut_on_the_second_press_drops_the_dictation(self):
        await self.send("down", "up", "down", "other", "up")
        self.assertEqual(self.calls, ["dictate", "dropped"])

    async def test_a_tap_only_counts_once(self):
        await self.send("down", "up", "down", "up", "down")
        self.assertEqual(self.calls, ["dictate", "typed"])

    async def test_it_can_be_turned_off(self):
        self.config.values["dictation_enabled"] = False
        await self.send("down", "up", "down")
        self.assertNotIn("dictate", self.calls)

    async def test_it_works_with_talk_holds_turned_off(self):
        self.config.values["fn_hold_enabled"] = False
        await self.send("down", "up", "down", "up")
        self.assertEqual(self.calls, ["dictate", "typed"])

    async def test_without_the_callbacks_a_double_press_is_nothing(self):
        client = FnClient(self.config, mock.AsyncMock(), mock.AsyncMock(), path="unused")
        self.addCleanup(client._disarm)
        for word in ("down", "up", "down", "up"):
            await client.handle(word)

    async def test_losing_the_helper_ends_it(self):
        await self.send("down", "up", "down")
        await self.client.handle("up")      # what run() does when the socket goes
        self.assertEqual(self.calls, ["dictate", "typed"])


class Connection(Case):
    async def test_missing_socket_is_retried_quietly_until_it_appears(self):
        path = str(self.folder / "late.sock")
        config = Config(self.folder / "config.json")
        with mock.patch.object(fnclient, "RETRY_S", 0.02):
            client = FnClient(config, mock.AsyncMock(), mock.AsyncMock(), path=path)
            task = asyncio.create_task(client.run())
            self.addCleanup(task.cancel)
            await asyncio.sleep(0.06)
            self.assertFalse(task.done())
            helper = FakeHelper(path)
            await helper.start()
            self.addAsyncCleanup(helper.stop)
            await asyncio.wait_for(helper.connected.wait(), 2)

    async def test_reconnects_after_the_helper_restarts(self):
        path = str(self.folder / "events.sock")
        config = Config(self.folder / "config.json")
        with mock.patch.object(fnclient, "RETRY_S", 0.02):
            helper = FakeHelper(path)
            await helper.start()
            client = FnClient(config, mock.AsyncMock(), mock.AsyncMock(), path=path)
            task = asyncio.create_task(client.run())
            self.addCleanup(task.cancel)
            await asyncio.wait_for(helper.connected.wait(), 2)
            await helper.stop()
            again = FakeHelper(path)
            await again.start()
            self.addAsyncCleanup(again.stop)
            await asyncio.wait_for(again.connected.wait(), 2)

    def test_default_path_and_env_override(self):
        config = Config(self.folder / "config.json")
        with mock.patch.dict(os.environ):
            os.environ.pop("TUXISTANT_FN_SOCKET", None)
            self.assertEqual(FnClient(config, None, None).path, "/run/tuxistant-fn/events.sock")
            os.environ["TUXISTANT_FN_SOCKET"] = "/tmp/x.sock"
            self.assertEqual(FnClient(config, None, None).path, "/tmp/x.sock")


if __name__ == "__main__":
    unittest.main()
