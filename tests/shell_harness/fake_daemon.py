#!/usr/bin/env python3
"""Fake org.tuxistant.Daemon for the headless shell harness.

Implements the docs/CONTRACT.md bus API with canned data and runs a scripted scenario. A second
interface, org.tuxistant.Test, talks to the tuxistant-shot@test driver extension inside the shell:
the Command signal asks it to act, and it answers by calling CommandDone. Exit status 0 means the
scenario finished; anything else fails the run.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method, dbus_signal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tuxistant import config as tux_config, themes as tux_themes  # noqa: E402

BUS_NAME = "org.tuxistant.Daemon"
BUS_PATH = "/org/tuxistant/Daemon"
SETTLE_S = 1.6
HISTORY = [{"id": i, "ts": 1700000000.0 + i, "heard": f"Request number {i}", "reply": f"Reply {i}",
            "actions": [], "status": "ok"} for i in range(12, 0, -1)]


class Daemon(ServiceInterface):
    def __init__(self, theme):
        super().__init__(BUS_NAME)
        self.config = {k: v for k, v in tux_config.DEFAULTS.items() if k != "api_key"}
        self.config.update(api_key_set=False, theme=theme)
        self.state = "idle"
        self.calls = []
        self.list_delay = 0.0

    @dbus_method()
    def Toggle(self):
        self.calls.append(["Toggle"])

    @dbus_method()
    def HoldStart(self):
        self.calls.append(["HoldStart"])

    @dbus_method()
    def HoldEnd(self):
        self.calls.append(["HoldEnd"])

    @dbus_method()
    def Cancel(self):
        self.calls.append(["Cancel"])

    @dbus_method()
    def GetState(self) -> "s":
        return self.state

    @dbus_method()
    def GetConfig(self) -> "s":
        return json.dumps(self.config)

    @dbus_method()
    def SetConfig(self, key: "s", json_value: "s"):
        self.calls.append(["SetConfig", key, json_value])
        self.config[key] = json.loads(json_value)
        self.ConfigChanged(key)

    @dbus_method()
    def ListVoices(self) -> "s":
        return "[]"

    @dbus_method()
    def PreviewVoice(self, voice: "s"):
        self.calls.append(["PreviewVoice", voice])

    @dbus_method()
    def Say(self, text: "s"):
        self.calls.append(["Say", text])

    @dbus_method()
    async def ListThemes(self) -> "s":
        await asyncio.sleep(self.list_delay)
        themes, problems = tux_themes.discover()
        return json.dumps({"themes": themes, "problems": problems})

    @dbus_method()
    def PreviewTheme(self, id: "s"):
        self.ThemePreview(id)

    @dbus_method()
    def ListSkills(self) -> "s":
        return "[]"

    @dbus_method()
    def GetHistory(self, limit: "i") -> "s":
        self.calls.append(["GetHistory", limit])
        return json.dumps(HISTORY[:limit])

    @dbus_method()
    def ClearHistory(self):
        self.HistoryChanged()

    @dbus_method()
    def OpenSettings(self):
        self.calls.append(["OpenSettings"])

    @dbus_signal()
    def StateChanged(self, state) -> "s":
        return state

    @dbus_signal()
    def Presence(self, present) -> "b":
        return present

    @dbus_signal()
    def Level(self, level) -> "d":
        return level

    @dbus_signal()
    def Transcript(self, text, final) -> "sb":
        return [text, final]

    @dbus_signal()
    def Reply(self, text) -> "s":
        return text

    @dbus_signal()
    def Error(self, text) -> "s":
        return text

    @dbus_signal()
    def ConfigChanged(self, key) -> "s":
        return key

    @dbus_signal()
    def ThemePreview(self, id) -> "s":
        return id

    @dbus_signal()
    def HistoryChanged(self):
        pass

    def set_state(self, state):
        self.state = state
        self.StateChanged(state)


class Test(ServiceInterface):
    """The driver link. command() sends one operation and waits for the shell's answer."""

    def __init__(self):
        super().__init__("org.tuxistant.Test")
        self.ready = asyncio.Event()
        self.pending = {}
        self.next_id = 0

    @dbus_method()
    def Ready(self):
        self.ready.set()

    @dbus_method()
    def CommandDone(self, id: "s", json_result: "s"):
        answer = self.pending.pop(id)
        if not answer.done():   # a late reply to a command that already timed out
            answer.set_result(json.loads(json_result))

    @dbus_signal()
    def Command(self, id, json_command) -> "ss":
        return [id, json_command]

    async def command(self, op, timeout=20, **args):
        self.next_id += 1
        cid = str(self.next_id)
        self.pending[cid] = asyncio.get_running_loop().create_future()
        self.Command(cid, json.dumps({"op": op, **args}))
        result = await asyncio.wait_for(self.pending[cid], timeout)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"{op} failed in the shell: {result['error']}")
        return result


async def pump_levels(daemon):
    """About 30 Hz, like the real daemon: a lively level only while audio would be flowing."""
    start = time.monotonic()
    while True:
        await asyncio.sleep(1 / 30)
        if daemon.state in ("listening", "speaking"):
            t = time.monotonic() - start
            daemon.Level(0.6 + 0.2 * math.sin(t * 7) + 0.1 * math.sin(t * 17))


async def scenario_shots(daemon, test, args):
    out = Path(args.out)
    daemon.Presence(True)
    pump = asyncio.create_task(pump_levels(daemon))
    for state in ("listening", "thinking", "speaking"):
        daemon.set_state(state)
        await asyncio.sleep(SETTLE_S)
        await test.command("shoot", path=str(out / f"{state}.png"))
    pump.cancel()


async def scenario_probe(daemon, test, args):
    """Developer aid: runs the JS file named by TUX_PROBE_JS in the shell while listening."""
    daemon.Presence(True)
    daemon.set_state("listening")
    await asyncio.sleep(SETTLE_S)
    print(json.dumps(await test.command("eval", code=Path(os.environ["TUX_PROBE_JS"]).read_text()),
                     indent=1), file=sys.stderr)


async def scenario_verify(daemon, test, args):
    """Drives every extension feature and writes report.json and PNGs for the unit test to judge."""
    report = {}
    try:
        await verify_steps(daemon, test, args, report)
    finally:
        report["calls"] = daemon.calls
        (Path(args.out) / "report.json").write_text(json.dumps(report, indent=1))


async def verify_steps(daemon, test, args, report):
    out = Path(args.out)

    async def snap(name, settle=0.0):
        if settle:
            await asyncio.sleep(settle)
        report[name] = await test.command("inspect")
        return report[name]

    async def shoot(name):
        await test.command("shoot", path=str(out / f"{name}.png"))

    async def use_theme(theme):
        daemon.config["theme"] = theme
        daemon.ConfigChanged("theme")
        await asyncio.sleep(0.4)

    async def end_turn():
        daemon.set_state("idle")
        daemon.Presence(False)
        await asyncio.sleep(0.6)

    async def begin_turn():
        daemon.Presence(True)
        daemon.set_state("listening")
        await asyncio.sleep(SETTLE_S)

    pump = asyncio.create_task(pump_levels(daemon))
    await snap("idle")

    # Show and hide rules
    daemon.Presence(True)
    await snap("presence_only", 0.4)
    daemon.Presence(False)
    daemon.set_state("listening")
    await snap("state_only", 0.4)
    daemon.Presence(True)
    await snap("listening", SETTLE_S)
    await shoot("orb")
    report["pick_orb"] = await test.command(
        "pick", x=report["listening"]["blob"]["box"]["x"] + 5, y=report["listening"]["blob"]["box"]["y"] + 5)

    # Anchors
    report["anchors"] = {}
    for anchor in ("bottom-right", "bottom-left", "top-right", "top-left", "bottom-center", "center"):
        await use_theme(f"anchor-{anchor}")
        report["anchors"][anchor] = (await test.command("inspect"))["blob"]["box"]
    await use_theme("solid-orb")

    # Caption
    daemon.Transcript("turn on", False)
    await snap("heard_partial", 0.3)
    daemon.Transcript("turn on the lights please", True)
    daemon.set_state("thinking")
    await snap("heard_final", 0.3)
    report["pick_caption"] = await test.command("pick", x=report["heard_final"]["caption"]["box"]["x"] + 8,
                                                y=report["heard_final"]["caption"]["box"]["y"] + 8)
    daemon.set_state("speaking")
    daemon.Reply("Done. The lights are on, and I also dimmed the hallway a little because it is late.")
    await snap("reply", 0.4)
    await shoot("caption")
    daemon.set_state("idle")
    await snap("reply_lingers", 0.8)
    await asyncio.sleep(6)
    await snap("reply_gone")
    daemon.Presence(False)

    # Presence false after release: blob goes, caption stays quiet, errors still show
    await begin_turn()
    daemon.Presence(False)
    daemon.set_state("thinking")
    daemon.Reply("Hidden on release")
    await snap("released", 0.6)
    daemon.Error("Microphone not found")
    await snap("error", 0.4)
    await shoot("error")
    await end_turn()

    # Right-click menu
    await begin_turn()
    box = (await test.command("inspect"))["blob"]["box"]
    await test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2, button=3)
    await snap("context_menu", 0.5)
    await shoot("context_menu")
    await test.command("activate", menu="theme", label="Solid band")
    await snap("band", 0.7)
    await shoot("band")
    report["set_config"] = [c for c in daemon.calls if c[0] == "SetConfig"]
    box = report["band"]["blob"]["box"]
    report["pick_band"] = await test.command("pick", x=box["x"] + 200, y=box["y"] + 20)
    await test.command("click", x=box["x"] + 200, y=box["y"] + 20, button=3)
    await snap("band_right_click", 0.3)
    await use_theme("solid-orb")
    box = (await test.command("inspect"))["blob"]["box"]
    await test.command("click", x=box["x"] + 20, y=box["y"] + 20, button=3)
    await asyncio.sleep(0.5)
    await test.command("activate", menu="blob", label="Stop")
    report["cancels_from_the_menu"] = len([c for c in daemon.calls if c[0] == "Cancel"])
    await test.command("click", x=box["x"] + 20, y=box["y"] + 20, button=3)
    await asyncio.sleep(0.5)
    await test.command("activate", menu="blob", label="Settings")
    await asyncio.sleep(0.2)
    await end_turn()

    # Status menu
    await test.command("open_indicator")
    await snap("indicator_open", 0.5)
    await shoot("indicator")
    await test.command("activate", menu="recent", label=HISTORY[1]["heard"])
    await asyncio.sleep(0.2)
    report["clipboard"] = await test.command("clipboard")
    HISTORY.insert(0, {**HISTORY[0], "id": 99, "heard": "Newest request after a change"})
    await test.command("open_indicator")
    await asyncio.sleep(0.3)
    daemon.HistoryChanged()
    await snap("indicator_refreshed", 0.3)
    await test.command("activate", menu="indicator", label="Talk")
    await asyncio.sleep(0.2)
    await test.command("open_indicator")
    await asyncio.sleep(0.2)
    await test.command("activate", menu="indicator", label="Settings")
    await asyncio.sleep(0.2)
    report["styles"] = {}
    for state in ("listening", "thinking", "speaking", "idle"):
        daemon.set_state(state)
        report["styles"][state] = (await snap(f"style_{state}", 0.15))["indicator"]["style"]

    # Hotkey: a real key event through mutter
    before = len([c for c in daemon.calls if c[0] == "Toggle"])
    await test.command("keys", keyvals=[0xffe3, 0xffeb, 0x20])  # Control_L Super_L space
    await asyncio.sleep(0.3)
    report["hotkey_toggles"] = len([c for c in daemon.calls if c[0] == "Toggle"]) - before

    # ThemePreview: 4 s cycle, nothing about the real state changes
    calls_before = len(daemon.calls)
    daemon.ThemePreview("solid-band")
    await snap("preview_listening", 1.0)
    await snap("preview_thinking", 1.3)
    await snap("preview_speaking", 1.4)
    await asyncio.sleep(1.0)
    await snap("preview_over", 0.6)
    report["preview_calls"] = [c for c in daemon.calls[calls_before:] if c[0] != "GetHistory"]
    report["preview_state"] = daemon.state

    # The blob's menu outlives the turn, and the turn does not pull the blob out from under it
    await begin_turn()
    box = (await test.command("inspect"))["blob"]["box"]
    await test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2, button=3)
    await snap("menu_in_turn", 0.5)
    await end_turn()
    await snap("menu_after_turn", 1.2)
    await test.command("eval", code="ext._blobMenu.menu.close();")
    await snap("menu_closed", 1.0)

    # The panel icon has the themes when there is no blob to click
    await test.command("open_indicator")
    await snap("indicator_themes", 0.4)
    await test.command("activate", menu="indicatorTheme", label="Solid band")
    await asyncio.sleep(0.4)
    report["set_config_from_panel"] = [c for c in daemon.calls if c[0] == "SetConfig"][-1]
    await test.command("close_indicator")
    await use_theme("solid-orb")

    # A turn that ends while the extension is still loading must not stay on screen
    daemon.Presence(True)
    daemon.set_state("speaking")
    await asyncio.sleep(0.2)
    before = len(daemon.calls)
    await test.command("set_enabled", enabled=False)
    await asyncio.sleep(0.2)
    report["calls_on_disable"] = daemon.calls[before:]
    daemon.list_delay = 0.6
    await test.command("set_enabled", enabled=True)
    await asyncio.sleep(0.2)
    daemon.state = "idle"
    daemon.StateChanged("idle")
    daemon.Presence(False)
    await snap("stale_refresh", 1.5)
    daemon.list_delay = 0.0

    # Offline and back
    await begin_turn()
    await args.bus.release_name(BUS_NAME)
    await snap("offline", 0.8)
    await test.command("eval", code="""
        ext._calls = 0;
        const call = ext._daemon.call;
        ext._daemon.call = function (...args) {
            ext._calls++;
            return call.apply(this, args);
        };
    """)
    await test.command("open_indicator")
    await snap("offline_menu", 0.4)
    report["calls_from_offline_menu"] = await test.command("eval", code="return ext._calls;")
    await shoot("offline")
    await test.command("activate", menu="indicator", label="Talk")
    await snap("offline_talk", 0.4)
    await args.bus.request_name(BUS_NAME)
    await snap("back_online", 0.6)
    await begin_turn()
    await snap("back_listening")

    # enable/disable with the blob on screen, a caption timer running and signals flowing
    daemon.Transcript("something", True)
    daemon.Reply("something else")
    daemon.set_state("idle")
    await asyncio.sleep(0.2)
    await begin_turn()
    report["counts_enabled"] = await test.command("counts")
    report["cycles"] = await test.command("cycle", count=3, timeout=30)
    await test.command("set_enabled", enabled=False)
    daemon.set_state("speaking")
    daemon.Presence(True)
    daemon.Reply("nobody is listening")
    daemon.HistoryChanged()
    await asyncio.sleep(0.5)
    report["counts_disabled"] = await test.command("counts")
    before = len(daemon.calls)
    await test.command("keys", keyvals=[0xffe3, 0xffeb, 0x20])
    await asyncio.sleep(0.3)
    report["hotkey_after_disable"] = daemon.calls[before:]
    await test.command("set_enabled", enabled=True)
    await snap("reenabled", SETTLE_S)
    await shoot("reenabled")

    pump.cancel()


SCENARIOS = {"shots": scenario_shots, "probe": scenario_probe, "verify": scenario_verify}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scenario", default="shots")
    parser.add_argument("--ready-timeout", type=float, default=90)
    args = parser.parse_args()

    themes, problems = tux_themes.discover()
    if args.theme not in {t["id"] for t in themes}:
        for p in problems:
            print(f"theme problem: {p['path']}: {p['error']}", file=sys.stderr)
        print(f"theme not found: {args.theme}", file=sys.stderr)
        return 3

    bus = await MessageBus().connect()
    daemon, test = Daemon(args.theme), Test()
    bus.export(BUS_PATH, daemon)
    bus.export(BUS_PATH, test)
    await bus.request_name(BUS_NAME)
    print("ready for the shell", flush=True)
    args.bus = bus
    await asyncio.wait_for(test.ready.wait(), args.ready_timeout)
    await SCENARIOS[args.scenario](daemon, test, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (TimeoutError, RuntimeError, AssertionError) as e:
        print(f"harness failure: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
