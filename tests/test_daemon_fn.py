"""Fn hold timing against a fake helper socket."""
import asyncio
import os
import sys
import time
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
        async def start(held=0.0):
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
    """Tap Fn, then press it again: a tap is hands-free, a hold is push-to-talk. Real times, as the
    tap rules are the point."""

    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.config.values.update(fn_hold_ms=HOLD_MS, fn_hold_enabled=True)
        self.calls = []
        self.going = False      # what the daemon would say about dictating

        def note(word):
            async def call(*args, **_how):
                self.calls.append(" ".join((word, *args)))
                if word == "dictate":
                    self.going = True
                    return "go"
                elif word in ("typed", "dropped"):
                    self.going = False
            return call

        self.client = FnClient(self.config, note("hold"), note("hold end"), path="unused",
                               on_dictate_start=note("dictate"), on_dictate_end=note("typed"),
                               on_dictate_cancel=note("dropped"), on_dictate_mode=note("mode"),
                               dictating=lambda _go: self.going)
        self.addCleanup(self.client._disarm)

    async def send(self, *words, gap=0.01):
        for word in words:
            await self.client.handle(word)
            await asyncio.sleep(gap)

    async def hands_free(self):
        await self.send("down", "up", "down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap"])

    async def test_tap_tap_keeps_dictating_after_letting_go(self):
        await self.hands_free()
        await asyncio.sleep(fnclient.TAP_S + PAST_HOLD)
        self.assertEqual(self.calls, ["dictate", "mode tap"], "letting go must not end it")

    async def test_the_next_tap_ends_hands_free_and_types(self):
        await self.hands_free()
        await asyncio.sleep(0.05)
        await self.send("down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_holding_fn_while_hands_free_is_no_talk_hold_and_letting_go_ends_it(self):
        await self.hands_free()
        await self.send("down")
        await asyncio.sleep(PAST_HOLD)
        self.assertEqual(self.calls, ["dictate", "mode tap"])
        await self.send("up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_a_fn_shortcut_while_hands_free_neither_stops_nor_drops_it(self):
        await self.hands_free()
        await self.send("down", "other", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap"])
        await self.send("down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_the_tap_that_stops_it_does_not_start_the_next_go(self):
        await self.hands_free()
        await self.send("down", "up", "down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_ended_elsewhere_the_next_taps_start_a_new_go(self):
        """The shortcut, Cancel, a lock or the time limit end it without Fn."""
        await self.hands_free()
        self.going = False
        await self.send("down", "up", "down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "dictate", "mode tap"])

    async def test_a_tap_then_a_hold_is_push_to_talk_and_says_so(self):
        await self.send("down", "up", "down")
        self.assertEqual(self.calls, ["dictate"])
        await asyncio.sleep(fnclient.TAP_S + 0.05)
        self.assertEqual(self.calls, ["dictate", "mode hold"])
        await self.send("up")
        self.assertEqual(self.calls, ["dictate", "mode hold", "typed"])

    async def test_holding_on_after_starting_never_becomes_a_talk_hold(self):
        await self.send("down", "up", "down")
        await asyncio.sleep(fnclient.TAP_S + PAST_HOLD)
        self.assertEqual(self.calls, ["dictate", "mode hold"])

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
        await asyncio.sleep(fnclient.TAP_S)
        self.assertEqual(self.calls, ["dictate", "dropped"])

    async def test_a_go_fn_did_not_start_is_not_fns_to_drop_or_name(self):
        """A go from the shortcut was already listening, so the second press started none: a
        Fn shortcut on it must not drop that go, nor a tap make it hands-free."""
        calls = []

        async def start():
            calls.append("dictate")     # and returns no go: one was already listening

        def note(word):
            async def call(*_args, **_how):
                calls.append(word)
            return call

        client = FnClient(self.config, note("hold"), note("hold end"), path="unused",
                          on_dictate_start=start, on_dictate_end=note("typed"),
                          on_dictate_cancel=note("dropped"), on_dictate_mode=note("mode"),
                          dictating=lambda *_go: True)
        self.addCleanup(client._disarm)
        for words in (("down", "up", "down", "other", "up"), ("down", "up", "down", "up")):
            await asyncio.sleep(fnclient.SECOND_PRESS_S)
            for word in words:
                await client.handle(word)
                await asyncio.sleep(0.01)
        await asyncio.sleep(fnclient.TAP_S + 0.05)
        self.assertEqual(calls, ["dictate", "dictate"])

    async def test_it_can_be_turned_off(self):
        self.config.values["dictation_enabled"] = False
        await self.send("down", "up", "down", "up")
        self.assertEqual(self.calls, [])

    async def test_it_works_with_talk_holds_turned_off(self):
        self.config.values["fn_hold_enabled"] = False
        await self.hands_free()
        await self.send("down", "up")
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_without_the_callbacks_a_double_press_is_nothing(self):
        client = FnClient(self.config, mock.AsyncMock(), mock.AsyncMock(), path="unused")
        self.addCleanup(client._disarm)
        for word in ("down", "up", "down", "up"):
            await client.handle(word)

    async def test_without_a_mode_callback_hands_free_still_works(self):
        calls = []

        async def start():
            calls.append("dictate")
            return "go"

        async def end(go):
            calls.append("typed")

        client = FnClient(self.config, mock.AsyncMock(), mock.AsyncMock(), path="unused",
                          on_dictate_start=start, on_dictate_end=end)
        self.addCleanup(client._disarm)
        for word in ("down", "up", "down", "up", "down", "up"):
            await client.handle(word)
            await asyncio.sleep(0.01)
        self.assertEqual(calls, ["dictate", "typed"])

    async def test_losing_the_helper_ends_a_held_go(self):
        await self.send("down", "up", "down")
        await self.client.lost()
        self.assertEqual(self.calls, ["dictate", "typed"])

    async def test_losing_the_helper_ends_a_hands_free_go(self):
        """With no Fn, nothing could tap to stop it."""
        await self.hands_free()
        await self.client.lost()
        self.assertEqual(self.calls, ["dictate", "mode tap", "typed"])

    async def test_losing_the_helper_ends_a_talk_hold(self):
        await self.send("down")
        await asyncio.sleep(PAST_HOLD)
        await self.client.lost()
        self.assertEqual(self.calls, ["hold", "hold end"])


class SlowToStart(Case):
    """Starting a go waits for what it replaces to stop, and closing a reply can take 2 s. The keys
    pressed meanwhile keep their own times, through a real socket as the helper sends them."""

    TAP, GAP, START = 0.08, 0.1, fnclient.TAP_S + 0.05

    async def asyncSetUp(self):
        self.config = Config(self.folder / "config.json")
        self.config.values.update(fn_hold_ms=400, fn_hold_enabled=True)     # well past a tap
        self.calls = []
        self.going = False
        path = str(self.folder / "events.sock")
        self.helper = FakeHelper(path)
        await self.helper.start()
        self.addAsyncCleanup(self.helper.stop)

        def note(word):
            async def call(*args, **_how):
                if word == "dictate":
                    await asyncio.sleep(self.START)
                    self.going = True
                self.calls.append(" ".join((word, *args)))
                return "go" if word == "dictate" else None
            return call

        self.client = FnClient(self.config, note("hold"), note("hold end"), path=path,
                               on_dictate_start=note("dictate"), on_dictate_end=note("typed"),
                               on_dictate_cancel=note("dropped"), on_dictate_mode=note("mode"),
                               dictating=lambda _go: self.going)
        task = asyncio.create_task(self.client.run())
        self.addCleanup(task.cancel)
        self.addCleanup(self.client._disarm)
        await asyncio.wait_for(self.helper.connected.wait(), 2)

    async def keys(self, *steps):
        for word, then in steps:
            await self.helper.send(word)
            await asyncio.sleep(then)

    async def test_tap_tap_is_hands_free_however_long_the_start_takes(self):
        """The owner's bug again, while Tux was speaking: the second tap read as a hold."""
        await self.keys(("down", self.TAP), ("up", self.GAP), ("down", self.TAP), ("up", 0))
        await asyncio.sleep(self.START + fnclient.TAP_S + PAST_HOLD)
        self.assertEqual(self.calls, ["dictate", "mode tap"])

    async def test_tap_then_hold_is_still_push_to_talk_when_the_start_is_slow(self):
        await self.keys(("down", self.TAP), ("up", self.GAP), ("down", self.START + 0.1),
                        ("up", 0.05))
        self.assertEqual(self.calls, ["dictate", "mode hold", "typed"])

    async def test_a_hold_after_a_slow_start_is_known_from_when_the_key_went_down(self):
        """Still down at 0.3 s from the press: a hold, at once, not 0.3 s after the start."""
        await self.keys(("down", self.TAP), ("up", self.GAP), ("down", 0))
        await asyncio.sleep(self.START + 0.05)
        self.assertEqual(self.calls, ["dictate", "mode hold"])

    async def test_the_hold_says_how_long_ago_fn_went_down(self):
        """A question on screen is answered by a press made while it was up, so the turn needs
        the press, not HoldStart, which comes fn_hold_ms later."""
        held = []

        async def start(**how):
            held.append(how.get("held"))

        client = FnClient(self.config, start, mock.AsyncMock(), path="unused")
        self.addCleanup(client._disarm)
        await client.handle("down", time.monotonic() - 1)
        await asyncio.sleep(0.02)
        self.assertEqual(len(held), 1)
        self.assertGreaterEqual(held[0] or 0, 1.0)

    async def test_the_talk_timer_counts_from_the_press(self):
        await self.client.handle("down", time.monotonic() - 1)
        await asyncio.sleep(0.02)
        self.assertEqual(self.calls, ["hold"])


class Wiring(Case):
    """The daemon's own wiring, to the real controller."""

    async def test_the_service_gives_fn_the_mode_and_asks_whether_its_go_still_listens(self):
        from tuxistant import service
        ctl = mock.Mock()
        client = service.fn_client(Config(self.folder / "config.json"), ctl, path="unused")
        self.assertIs(client.on_dictate_mode, ctl.dictate_mode)
        self.assertIs(client.on_dictate_start, ctl.dictate_start)
        self.assertIs(client.on_dictate_end, ctl.dictate_end)
        self.assertIs(client.on_dictate_cancel, ctl.drop_go)
        self.assertIs(client.still_dictating, ctl.going)


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
