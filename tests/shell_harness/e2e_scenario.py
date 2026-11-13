#!/usr/bin/env python3
"""The end-to-end scenario, run by e2e.sh inside the isolated shell.

The steps act like the user: bus calls, real pointer and key events, raw key records for the Fn
helper. Every check goes to report.json, and one failed step does not stop the next.
"""
import argparse
import array
import asyncio
import json
import math
import os
import signal
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT)]
from fake_daemon import BUS_NAME, BUS_PATH, Test  # noqa: E402
from tuxistant.audio import BYTES_PER_S  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.realtime import speech  # noqa: E402

DISPLAY = "tux-e2e"
QUESTION = "Please tell me what time it is"
JOKE = "Tell me a very short joke about penguins"
LEAD_IN_S = 0.5
REPLY_S = 60            # a turn's whole reply, cold pipeline included
SHELL_S = 90            # a loaded, swapping machine stalled the shell for 40 s; step_responsive times it
EVENT = struct.Struct("@llHHi")
EV_SYN, EV_KEY = 0, 1
KEY_A, KEY_FN = 30, 0x1D0
# Inside the Settings window, as seen in its screenshot: where the sidebar rows are.
SIDEBAR_X, SIDEBAR_Y, SIDEBAR_STEP = 100, 71, 44
PAGES = ("general", "voice", "appearance", "skills", "history")
CTRL, SUPER, SPACE = 0xffe3, 0xffeb, 0x20
# More of the same, relative to the window.
TRY_SCANNER, ROW_SCANNER, ROW_ORB = (739, 245), (429, 245), (429, 190)
PLAY_NICOLE, CLEAR_HISTORY, CONFIRM_CLEAR = (750, 512), (768, 85), (513, 430)


def rms(pcm: bytes) -> float:
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))


class Harness:
    def __init__(self, bus, test, work: Path, out: Path):
        self.bus, self.test, self.work, self.out = bus, test, work, out
        self.audio = work / "audio"
        self.signals = []   # (monotonic time, name, args)
        self.checks = []
        self.timings = {}
        self.clips = {}
        self.env = {**os.environ, "WAYLAND_DISPLAY": DISPLAY, "GDK_BACKEND": "wayland",
                    "GDK_DEBUG": "no-portals", "ADW_DISABLE_PORTAL": "1", "GTK_A11Y": "none",
                    "TUXISTANT_FN_SOCKET": str(work / "run/fn.sock"), "TUX_E2E_DIR": str(self.audio)}
        self.daemon = self.helper = None
        self.keyboard = None
        self.wrapper = str(work / "bin/tuxistant")

    def check(self, name: str, ok, detail=None):
        ok = bool(ok)
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else f': {detail}'}", flush=True)

    def on_message(self, message):
        if message.message_type == MessageType.SIGNAL and message.interface == BUS_NAME:
            if message.path == BUS_PATH:
                self.signals.append((time.monotonic(), message.member, message.body))

    def mark(self) -> int:
        return len(self.signals)

    def seen(self, mark: int, name: str, *args) -> list:
        return [s for s in self.signals[mark:]
                if s[1] == name and (not args or list(args) == s[2][:len(args)])]

    async def signal(self, mark: int, name: str, *args, timeout: float = REPLY_S) -> tuple:
        deadline = time.monotonic() + timeout
        while True:
            if found := self.seen(mark, name, *args):
                return found[0]
            if time.monotonic() > deadline:
                raise TimeoutError(f"no {name} {args} in {timeout} s")
            await asyncio.sleep(0.01)

    async def call(self, member: str, signature: str = "", *body):
        reply = await self.bus.call(Message(
            destination=BUS_NAME, path=BUS_PATH, interface=BUS_NAME, member=member,
            signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name} {reply.body}")
        return reply.body[0] if reply.body else None

    async def set_config(self, key: str, value):
        await self.call("SetConfig", "ss", key, json.dumps(value))

    async def config(self) -> dict:
        return json.loads(await self.call("GetConfig"))

    async def history(self, limit: int = 20) -> list:
        return json.loads(await self.call("GetHistory", "i", limit))

    async def owner(self) -> str | None:
        reply = await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="NameHasOwner", signature="s", body=[BUS_NAME]))
        return reply.body[0]

    async def until_owner(self, wanted: bool, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while await self.owner() != wanted:
            if time.monotonic() > deadline:
                raise TimeoutError(f"daemon owner should be {wanted}")
            await asyncio.sleep(0.05)

    async def inspect(self) -> dict:
        return await self.test.command("inspect", timeout=SHELL_S)

    async def shoot(self, name: str):
        await self.test.command("shoot", timeout=SHELL_S, path=str(self.out / f"{name}.png"))

    async def js(self, code: str):
        return await self.test.command("eval", timeout=SHELL_S, code=code)

    def mic(self, clip: bytes):
        (self.audio / "mic.tmp").write_bytes(clip)
        (self.audio / "mic.tmp").rename(self.audio / "mic.pcm")

    def audio_events(self) -> list:
        path = self.audio / "audio.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def audio_event(self, after: float, device: str, event: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for e in self.audio_events():
                if e["t"] >= after and e["device"] == device and e["event"] == event:
                    return e
            if time.monotonic() > deadline:
                raise TimeoutError(f"no {device} {event} in {timeout} s")
            await asyncio.sleep(0.02)

    def speaker_bytes(self, number: int) -> bytes:
        path = self.audio / f"speaker-{number}.pcm"
        return path.read_bytes() if path.exists() else b""

    def speakers_since(self, after: float) -> list[int]:
        return [e["number"] for e in self.audio_events()
                if e["t"] >= after and e["device"] == "speaker" and e["event"] == "start"]

    async def to_idle(self):
        await self.call("Cancel")
        await asyncio.sleep(0.8)

    async def start_daemon(self):
        log = open(self.work / "daemon.log", "ab")
        self.daemon = subprocess.Popen(
            [sys.executable, "-B", str(HERE / "e2e_daemon.py")], env=self.env, stdout=log, stderr=log)
        await self.until_owner(True)
        await asyncio.sleep(1.0)    # the extension's refresh, and the helper socket

    async def stop_process(self, process) -> int:
        process.send_signal(signal.SIGTERM)
        return await asyncio.to_thread(process.wait, 10)

    async def wrapper_run(self, *args: str, timeout: float = 60) -> tuple[int, str]:
        done = await asyncio.to_thread(subprocess.run, [self.wrapper, *args], env=self.env,
                                       cwd=self.work / "home", capture_output=True, text=True,
                                       timeout=timeout)
        return done.returncode, done.stdout + done.stderr

    def key(self, code: int, value: int):
        os.write(self.keyboard, EVENT.pack(0, 0, EV_KEY, code, value) + EVENT.pack(0, 0, EV_SYN, 0, 0))


async def click_actor(h: Harness, expression: str, button: int = 1):
    """Moves the real pointer over an actor found by JS and clicks it."""
    box = await h.js(f"""
        const actor = {expression};
        const [x, y] = actor.get_transformed_position();
        const [width, height] = actor.get_transformed_size();
        return {{x, y, width, height}};""")
    await h.test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2,
                         button=button)


# Steps ----------------------------------------------------------------------------------------

async def step_clips(h: Harness):
    """The microphone's words, from the server's own voice model."""
    config = Config()
    silence = bytes(int(LEAD_IN_S * BYTES_PER_S))
    h.clips["time"] = silence + await speech(config, QUESTION, "af_nicole")
    h.clips["joke"] = silence + await speech(config, JOKE, "am_adam")
    h.check("clips: synthesised speech for the mic", len(h.clips["time"]) > BYTES_PER_S,
            len(h.clips["time"]))


async def step_wrapper_daemon(h: Harness):
    """The installed `tuxistant daemon`. No turn is started: this one has the real audio classes."""
    state = await h.inspect()
    h.check("start: extension offline without a daemon", state["indicator"]["offlineLine"], state["indicator"])
    data, wrapper = h.work / "data", Path(h.wrapper)
    h.check("install: wrapper is executable", os.access(wrapper, os.X_OK))
    h.check("install: extension with compiled schemas", (
        data / "gnome-shell/extensions/tuxistant@buengenio/schemas/gschemas.compiled").is_file())
    unit = (data / "systemd/user/tuxistant.service").read_text()
    h.check("install: unit runs the wrapper", f"ExecStart={wrapper} daemon" in unit, unit)
    activation = (data / "dbus-1/services/org.tuxistant.Daemon.service").read_text()
    h.check("install: D-Bus activation names the unit", "SystemdService=tuxistant.service" in activation, activation)
    config = h.work / "config/tuxistant/config.json"
    h.check("install: config is private", config.stat().st_mode & 0o777 == 0o600, oct(config.stat().st_mode))
    code, text = await h.wrapper_run("--help")
    h.check("wrapper: help", code == 0 and "doctor" in text, text)
    code, text = await h.wrapper_run("cancel")
    h.check("wrapper: a plain error without a daemon", code != 0 and text.startswith("tuxistant:") and
            "Traceback" not in text, text)
    code, text = await h.wrapper_run("doctor")
    h.check("wrapper: doctor passes", code == 0, text)
    for line in text.splitlines():
        print("  ", line, flush=True)
    log = open(h.work / "wrapper-daemon.log", "ab")
    process = subprocess.Popen([h.wrapper, "daemon"], env=h.env, cwd=h.work / "home",
                               stdout=log, stderr=log)
    await h.until_owner(True)
    await asyncio.sleep(0.8)
    state = await h.inspect()
    h.check("wrapper: extension online with `tuxistant daemon`",
            not state["indicator"]["offlineLine"], state["indicator"])
    config = await h.config()
    h.check("wrapper: config hides the key", "api_key" not in config and config["api_key_set"], config)
    themes = json.loads(await h.call("ListThemes"))
    h.check("wrapper: bundled themes found", {t["id"] for t in themes["themes"]} ==
            {"orb", "scanner", "tux", "waves"} and not themes["problems"], themes)
    h.check("wrapper: voices", len(json.loads(await h.call("ListVoices"))) >= 20)
    h.check("wrapper: skills", len(json.loads(await h.call("ListSkills"))) >= 2)
    code, text = await h.wrapper_run("history")
    h.check("wrapper: history runs", code == 0, text)
    code = await h.stop_process(process)
    h.check("wrapper: daemon stops cleanly on SIGTERM", code == 0, code)
    await h.until_owner(False)
    await asyncio.sleep(0.5)
    state = await h.inspect()
    h.check("wrapper: extension sees the daemon go", state["indicator"]["offlineLine"], state["indicator"])


async def step_vad(h: Harness):
    h.mic(h.clips["time"])
    mark = h.mark()
    started = time.monotonic()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "connecting", timeout=5)
    await h.signal(mark, "Presence", True, timeout=5)
    listening = await h.signal(mark, "StateChanged", "listening", timeout=30)
    h.timings["toggle_to_listening"] = listening[0] - started
    await asyncio.sleep(1.0)
    state = await h.inspect()
    blob = state["blob"]
    h.check("toggle: blob shows while listening", blob["visible"] and blob["mapped"], blob)
    h.check("toggle: blob uses the configured theme", blob["theme"] == "orb", blob)
    h.check("toggle: blob reacts to the level", blob["values"]["u_level"] > 0.02 or h.seen(mark, "Level"), blob)
    await h.shoot("toggle-listening")
    heard = (await h.signal(mark, "Transcript"))[2][0]
    h.check("toggle: transcript is the question", "time" in heard.lower(), heard)
    await h.signal(mark, "StateChanged", "thinking", timeout=REPLY_S)
    await h.signal(mark, "StateChanged", "speaking", timeout=REPLY_S)
    reply = (await h.signal(mark, "Reply"))[2][0]
    await asyncio.sleep(0.4)
    state = await h.inspect()
    h.check("toggle: caption shows the transcript", state["caption"]["heard"] == heard, state["caption"])
    h.check("toggle: caption shows the reply", state["caption"]["reply"].startswith(reply[:60]), state["caption"])
    h.check("toggle: caption is on screen", state["caption"]["visible"] and state["caption"]["opacity"] > 200,
            state["caption"])
    await h.shoot("toggle-speaking")
    await h.signal(mark, "StateChanged", "idle")
    await asyncio.sleep(0.7)
    state = await h.inspect()
    h.check("toggle: blob gone after the turn", not state["blob"]["visible"], state["blob"])
    h.check("toggle: caption lingers after the turn", state["caption"]["visible"], state["caption"])
    await h.shoot("toggle-idle")
    rows = await h.history()
    h.check("toggle: history row", rows and rows[0]["heard"] == heard and rows[0]["reply"] and
            rows[0]["status"] == "ok", rows[:1])
    h.check("toggle: the time skill ran", [a["name"] for a in rows[0]["actions"]] == ["time.now"], rows[0])
    h.check("toggle: speaker got audio", any(
        rms(h.speaker_bytes(n)) > 100 for n in h.speakers_since(started)))
    levels = [s[2][0] for s in h.seen(mark, "Level")]
    h.check("toggle: Level moved with the voice", max(levels) > 0.3 and len(levels) > 40,
            (len(levels), max(levels, default=0)))

    await h.test.command("open_indicator")
    await asyncio.sleep(0.6)
    state = await h.inspect()
    recent = state["indicator"]["recent"]
    h.check("status menu: open and lists history", state["indicator"]["open"] and
            recent and heard.startswith(recent[0].rstrip("…")), state["indicator"])
    await h.shoot("status-menu")
    await h.test.command("activate", menu="recent", label=recent[0])
    await asyncio.sleep(0.4)
    copied = (await h.test.command("clipboard"))["text"]
    h.check("status menu: a recent item copies its reply", copied == rows[0]["reply"], copied)
    await h.test.command("close_indicator")


async def hold(h: Harness, clip: str) -> tuple[int, float]:
    h.mic(h.clips[clip])
    mark = h.mark()
    started = time.monotonic()
    await h.call("HoldStart")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(0.8)
    state = await h.inspect()
    h.check("hold: blob shows while held", state["blob"]["visible"], state["blob"])
    await h.audio_event(started, "mic", "clip-end", timeout=20)
    await h.call("HoldEnd")
    return mark, started


async def step_hold_hide(h: Harness):
    rows = len(await h.history())
    mark, started = await hold(h, "joke")
    released = time.monotonic()
    await h.signal(mark, "Presence", False, timeout=3)
    await asyncio.sleep(0.8)
    state = await h.inspect()
    h.check("hide_on_release: blob hides at release", not state["blob"]["visible"], state["blob"])
    h.check("hide_on_release: turn goes on", (await h.call("GetState")) in ("thinking", "speaking"))
    speaking = await h.signal(mark, "StateChanged", "speaking")
    h.timings["release_to_speaking"] = speaking[0] - released
    await asyncio.sleep(0.5)
    state = await h.inspect()
    h.check("hide_on_release: blob stays hidden during the reply",
            not state["blob"]["visible"] and await h.call("GetState") == "speaking", state["blob"])
    numbers = h.speakers_since(started)
    h.check("hide_on_release: reply is being played", numbers and h.speaker_bytes(numbers[-1]), numbers)
    await h.shoot("hold-hidden-speaking")
    await h.signal(mark, "StateChanged", "idle")
    after = await h.history()
    h.check("hold: history row", len(after) == rows + 1 and after[0]["status"] == "ok" and after[0]["reply"],
            after[:1])
    h.check("hold: transcript", "penguin" in after[0]["heard"].lower() or "joke" in after[0]["heard"].lower(),
            after[0])


async def step_hold_show(h: Harness):
    await h.set_config("hide_on_release", False)
    await asyncio.sleep(0.2)
    mark, _ = await hold(h, "time")
    await asyncio.sleep(0.8)
    state = await h.inspect()
    h.check("hold shown: blob stays after release", state["blob"]["visible"], state["blob"])
    await h.signal(mark, "StateChanged", "speaking")
    reply = (await h.signal(mark, "Reply"))[2][0]
    await asyncio.sleep(0.4)
    state = await h.inspect()
    h.check("hold shown: caption has the reply", state["caption"]["reply"].startswith(reply[:60]), state["caption"])
    await h.shoot("hold-shown-speaking")
    await h.signal(mark, "StateChanged", "idle")
    await h.set_config("hide_on_release", True)


async def step_hotkey(h: Harness):
    await asyncio.sleep(0.5)
    mark = h.mark()
    await h.test.command("keys", keyvals=[CTRL, SUPER, SPACE])
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    h.check("hotkey: starts a turn", True)
    await asyncio.sleep(0.5)
    mark = h.mark()
    await h.test.command("keys", keyvals=[CTRL, SUPER, SPACE])
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    h.check("hotkey: second press cancels", True)
    await asyncio.sleep(0.5)
    mark = h.mark()
    await h.test.command("open_indicator")
    await asyncio.sleep(0.4)
    await h.test.command("activate", menu="indicator", label="Talk")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(0.4)
    await h.test.command("open_indicator")
    await asyncio.sleep(0.4)
    items = (await h.inspect())["indicator"]["items"]
    h.check("status menu: Talk starts a turn and turns into Stop", "Stop" in items, items)
    mark = h.mark()
    await h.test.command("activate", menu="indicator", label="Stop")
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    h.check("status menu: Stop cancels", True)


async def step_theme_menu(h: Harness):
    await h.set_config("listen_timeout_s", 90)
    mark = h.mark()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(1.2)
    box = (await h.inspect())["blob"]["box"]
    await h.test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2, button=3)
    await asyncio.sleep(0.6)
    state = await h.inspect()
    h.check("theme menu: right click opens it", state["blobMenu"]["open"], state["blobMenu"])
    names = [t[0] for t in state["blobMenu"]["themes"]]
    h.check("theme menu: lists every theme", set(names) == {"Listening Tux", "Orb", "Scanner", "Waves"}, names)
    await click_actor(h, "ext._blobMenu._themes")
    await asyncio.sleep(0.5)
    await h.shoot("theme-menu")
    config_mark = h.mark()
    await click_actor(h, "ext._blobMenu._themes.menu._getMenuItems()"
                         ".find(i => i.label.text === 'Scanner')")
    await h.signal(config_mark, "ConfigChanged", "theme", timeout=5)
    await asyncio.sleep(1.2)
    state = await h.inspect()
    h.check("theme menu: daemon config changed", (await h.config())["theme"] == "scanner")
    h.check("theme menu: blob switched live", state["blob"]["theme"] == "scanner", state["blob"])
    h.check("theme menu: layout followed", state["blob"]["box"]["width"] == 300 and
            state["blob"]["box"]["x"] == 650, state["blob"]["box"])
    h.check("theme menu: menu closed", not state["blobMenu"]["open"], state["blobMenu"])
    h.check("theme menu: the turn goes on", await h.call("GetState") == "listening")
    await h.shoot("theme-scanner")

    for theme in ("tux", "waves"):
        await h.set_config("theme", theme)
        await asyncio.sleep(1.2)
        state = await h.inspect()
        h.check(f"theme: {theme} live", state["blob"]["theme"] == theme, state["blob"])
        await h.shoot(f"theme-{theme}")
    box = state["blob"]["box"]
    picked = await h.test.command("pick", x=box["x"] + 100, y=box["y"] + 100)
    h.check("theme: band is click-through", picked["name"] != "tuxistant-blob", picked)
    await h.set_config("theme", "orb")
    await asyncio.sleep(1.2)
    await h.set_config("listen_timeout_s", 8)
    box = (await h.inspect())["blob"]["box"]
    await h.test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2, button=3)
    await asyncio.sleep(0.6)
    mark = h.mark()
    await click_actor(h, "ext._blobMenu.menu._getMenuItems().find(i => i.label?.text === 'Stop')")
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    h.check("blob menu: Stop cancels", True)
    await asyncio.sleep(0.8)

    # At the default timeout the turn ends with the menu still open, mid-choice.
    mark = h.mark()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(1.2)
    box = (await h.inspect())["blob"]["box"]
    await h.test.command("click", x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2, button=3)
    await asyncio.sleep(0.6)
    h.check("blob menu: opens for the outlasting check", (await h.inspect())["blobMenu"]["open"])
    await h.signal(mark, "StateChanged", "idle", timeout=15)
    await asyncio.sleep(1.5)
    state = await h.inspect()
    h.check("blob menu: outlasts the turn", state["blobMenu"]["open"] and state["blob"]["mapped"], state["blob"])
    config_mark = h.mark()
    await click_actor(h, "ext._blobMenu._themes")
    await asyncio.sleep(0.5)
    await click_actor(h, "ext._blobMenu._themes.menu._getMenuItems()"
                         ".find(i => i.label.text === 'Waves')")
    await h.signal(config_mark, "ConfigChanged", "theme", timeout=5)
    await asyncio.sleep(1.5)
    state = await h.inspect()
    h.check("blob menu: a choice after the turn still counts", (await h.config())["theme"] == "waves")
    h.check("blob menu: the blob leaves with the menu", not state["blob"]["visible"], state["blob"])

    # Between turns, and with a band that lets clicks through, the panel icon has the themes.
    await h.test.command("open_indicator")
    await asyncio.sleep(0.8)
    menu = (await h.inspect())["indicator"]
    h.check("status menu: themes between turns", menu["themesShown"] and
            {"Listening Tux", "Orb", "Scanner", "Waves"} == {t[0] for t in menu["themes"]}, menu)
    config_mark = h.mark()
    await h.test.command("activate", menu="indicatorTheme", label="Orb")
    await h.signal(config_mark, "ConfigChanged", "theme", timeout=5)
    h.check("status menu: picks a theme", (await h.config())["theme"] == "orb")
    await h.test.command("close_indicator")
    await asyncio.sleep(0.8)


async def step_cancel_reply(h: Harness):
    await asyncio.sleep(0.5)
    h.mic(h.clips["joke"])
    mark = h.mark()
    started = time.monotonic()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "speaking")
    await asyncio.sleep(0.2)
    pressed = time.monotonic()
    await h.call("Toggle")
    idle = await h.signal(mark, "StateChanged", "idle", timeout=5)
    h.timings["cancel_to_idle"] = idle[0] - pressed
    h.check("cancel: toggling mid-reply stops within a second", idle[0] - pressed < 1, idle[0] - pressed)
    stopped = await h.audio_event(started, "speaker", "stopped", timeout=3)
    h.check("cancel: speaker stopped", stopped)
    await asyncio.sleep(0.3)
    rows = await h.history(1)
    h.check("cancel: row says cancelled", rows[0]["status"] == "cancelled", rows)


async def step_timeout(h: Harness):
    await h.set_config("listen_timeout_s", 3)
    await asyncio.sleep(0.3)
    rows = len(await h.history(100))
    mark = h.mark()
    await h.call("Toggle")
    listening = await h.signal(mark, "StateChanged", "listening", timeout=30)
    idle = await h.signal(mark, "StateChanged", "idle", timeout=6)
    h.check("timeout: silence ends the turn on time", 2.8 < idle[0] - listening[0] < 4.5, idle[0] - listening[0])
    h.check("timeout: quietly", not h.seen(mark, "Error") and len(await h.history(100)) == rows)
    await h.set_config("listen_timeout_s", 8)


async def step_conversation(h: Harness):
    await h.set_config("conversation_mode", True)
    await asyncio.sleep(0.3)
    rows = len(await h.history(100))
    h.mic(h.clips["time"])
    mark = h.mark()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "speaking")
    h.mic(h.clips["joke"])
    second = h.mark()
    await h.signal(second, "StateChanged", "listening")
    await asyncio.sleep(0.5)
    state = await h.inspect()
    h.check("conversation: listens again after the reply", state["blob"]["visible"], state["blob"])
    await h.signal(second, "StateChanged", "speaking")
    third = h.mark()
    await h.signal(third, "StateChanged", "listening")
    idle = await h.signal(third, "StateChanged", "idle", timeout=15)
    h.check("conversation: one session, no idle between replies",
            not h.seen(mark, "StateChanged", "idle")[:-1] and h.seen(mark, "StateChanged", "idle")[0] is idle)
    added = await h.history(100)
    h.check("conversation: a row per exchange", len(added) == rows + 2 and
            ("penguin" in added[0]["heard"].lower() or "joke" in added[0]["heard"].lower()), added[:2])
    await h.set_config("conversation_mode", False)


async def step_errors(h: Harness):
    key, server = Config().values["api_key"], Config().values["server"]
    await asyncio.sleep(0.5)
    # Port 9 was not chosen for the run. The daemon drops the key when the server changes, and
    # swapping it first keeps the real one off port 9 even if that ever stopped.
    await h.set_config("api_key", "not-the-key")
    await h.set_config("server", "127.0.0.1:9")
    mark = h.mark()
    await h.call("Toggle")
    error = (await h.signal(mark, "Error", timeout=10))[2][0]
    await asyncio.sleep(0.3)
    state = await h.inspect()
    h.check("errors: unreachable server shown", "reach" in error and state["caption"]["error"] and
            state["caption"]["reply"] == error, state["caption"])
    await h.shoot("error-server")
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    await h.set_config("server", server)
    # Restoring the server dropped the key again, and an empty key is not a wrong one.
    await h.set_config("api_key", "not-the-key")
    await asyncio.sleep(0.5)
    mark = h.mark()
    await h.call("Toggle")
    error = (await h.signal(mark, "Error", timeout=10))[2][0]
    h.check("errors: rejected key shown", "API key" in error, error)
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    await h.set_config("api_key", key)
    await asyncio.sleep(0.5)


async def step_cli(h: Harness):
    """Commands of the installed wrapper, against the daemon with fake audio."""
    await asyncio.sleep(0.5)
    mark = h.mark()
    started = time.monotonic()
    code, text = await h.wrapper_run("say", "Hello from the terminal.")
    h.check("wrapper: say", code == 0, text)
    await h.signal(mark, "StateChanged", "speaking", timeout=30)
    await asyncio.sleep(0.3)
    state = await h.inspect()
    h.check("say: caption and blob", state["blob"]["visible"] and "terminal" in state["caption"]["reply"], state)
    await h.signal(mark, "StateChanged", "idle", timeout=30)
    numbers = h.speakers_since(started)
    h.check("say: audio", numbers and rms(h.speaker_bytes(numbers[-1])) > 100, numbers)

    mark = h.mark()
    code, text = await h.wrapper_run("toggle")
    h.check("wrapper: toggle", code == 0, text)
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    code, text = await h.wrapper_run("cancel")
    h.check("wrapper: cancel", code == 0, text)
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    code, text = await h.wrapper_run("history", "--limit", "3")
    h.check("wrapper: history lists turns", code == 0 and "time" in text.lower(), text)
    await asyncio.sleep(0.3)

    mark = h.mark()
    started = time.monotonic()
    await h.call("PreviewVoice", "s", "bm_george")
    await h.signal(mark, "StateChanged", "speaking", timeout=30)
    await h.signal(mark, "StateChanged", "idle", timeout=30)
    h.check("voice preview: sample played, config untouched",
            h.speakers_since(started) and (await h.config())["voice"] == "af_nicole")

    await asyncio.sleep(0.5)
    await h.call("PreviewTheme", "s", "tux")
    await asyncio.sleep(1.5)
    state = await h.inspect()
    h.check("theme preview: shows while idle", state["blob"]["visible"] and state["blob"]["theme"] == "tux", state["blob"])
    await h.shoot("theme-preview")
    await asyncio.sleep(3.5)
    state = await h.inspect()
    h.check("theme preview: ends", not state["blob"]["visible"], state["blob"])


async def step_fn(h: Harness):
    await asyncio.sleep(0.5)
    rows = len(await h.history(100))

    mark = h.mark()
    h.key(KEY_FN, 1)
    await asyncio.sleep(0.3)
    h.key(KEY_FN, 0)
    await asyncio.sleep(2.0)
    h.check("fn: short tap does nothing", not h.seen(mark, "StateChanged"), h.signals[mark:])

    mark = h.mark()
    h.key(KEY_FN, 1)
    await asyncio.sleep(0.4)
    h.key(KEY_A, 1)
    h.key(KEY_A, 0)
    await asyncio.sleep(1.6)
    h.check("fn: Fn plus a key does nothing", not h.seen(mark, "StateChanged"), h.signals[mark:])
    h.key(KEY_FN, 0)
    await asyncio.sleep(0.4)
    h.check("fn: release after Fn plus a key does nothing", not h.seen(mark, "StateChanged"))

    h.mic(h.clips["time"])
    mark = h.mark()
    pressed = time.monotonic()
    h.key(KEY_FN, 1)
    connecting = await h.signal(mark, "StateChanged", "connecting", timeout=4)
    h.timings["fn_hold"] = connecting[0] - pressed
    h.check("fn: hold activates after 1.5 s", 1.45 <= connecting[0] - pressed <= 1.8, connecting[0] - pressed)
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(0.8)
    state = await h.inspect()
    h.check("fn: blob shows during the hold", state["blob"]["visible"], state["blob"])
    await h.shoot("fn-held")
    await h.audio_event(pressed, "mic", "clip-end", timeout=20)
    released = time.monotonic()
    h.key(KEY_FN, 0)
    stopped = await h.audio_event(released, "mic", "stop", timeout=2)
    h.check("fn: release closes the mic", stopped["t"] - released < 0.5, stopped["t"] - released)
    await h.signal(mark, "Presence", False, timeout=2)
    await h.signal(mark, "StateChanged", "thinking", timeout=2)
    await h.signal(mark, "StateChanged", "speaking")
    await asyncio.sleep(0.4)
    state = await h.inspect()
    h.check("fn: blob hidden during the reply", not state["blob"]["visible"], state["blob"])
    await h.signal(mark, "StateChanged", "idle")
    after = await h.history(100)
    h.check("fn: one history row for the hold", len(after) == rows + 1 and after[0]["reply"], after[:1])

    await asyncio.sleep(0.5)
    mark = h.mark()
    h.key(KEY_FN, 1)
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(0.5)
    h.key(KEY_FN, 0)
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    await asyncio.sleep(0.2)
    h.check("fn: a silent hold goes quietly back to idle",
            len(await h.history(100)) == len(after) and not h.seen(mark, "Error"), h.signals[mark:])


SETTINGS_WINDOWS = """global.get_window_actors().map(a => a.meta_window)
    .filter(w => w.title === 'Tuxistant settings')"""


async def settings_window(h: Harness) -> dict | None:
    """Its frame once it has settled: a new window is first reported before it is placed."""
    last = None
    for _ in range(100):
        found = await h.js(f"""return {SETTINGS_WINDOWS}.map(w => {{
            const r = w.get_frame_rect();
            return {{x: r.x, y: r.y, width: r.width, height: r.height}};
        }});""")
        if found and found[0] == last and last["x"] > 0:
            return last
        last = found[0] if found else None
        await asyncio.sleep(0.25)
    return None


async def click_in(h: Harness, window: dict, point: tuple):
    await h.test.command("click", x=window["x"] + point[0], y=window["y"] + point[1], button=1)


async def open_page(h: Harness, window: dict, page: str):
    await click_in(h, window, (SIDEBAR_X, SIDEBAR_Y + PAGES.index(page) * SIDEBAR_STEP))
    await asyncio.sleep(1.0)


async def close_settings(h: Harness):
    await h.js(f"""const now = global.display.get_current_time_roundtrip();
        {SETTINGS_WINDOWS}.forEach(w => w.delete(now));""")
    await asyncio.sleep(1.2)


async def step_settings(h: Harness):
    await h.call("OpenSettings")
    window = await settings_window(h)
    h.check("settings: OpenSettings opened a window", window, window)
    await asyncio.sleep(1.5)
    shots = {}
    for page in PAGES:
        await open_page(h, window, page)
        await h.shoot(f"settings-{page}")
        shots[page] = (h.out / f"settings-{page}.png").read_bytes()
    h.check("settings: every page looks different", len(set(shots.values())) == len(PAGES))

    await open_page(h, window, "appearance")
    mark = h.mark()
    await click_in(h, window, TRY_SCANNER)
    await h.signal(mark, "ThemePreview", "scanner", timeout=5)
    await asyncio.sleep(1.2)
    state = await h.inspect()
    h.check("settings: Try shows the theme on the desktop", state["blob"]["visible"] and
            state["blob"]["theme"] == "scanner", state["blob"])
    await h.shoot("settings-try")
    await asyncio.sleep(3.8)
    mark = h.mark()
    await click_in(h, window, ROW_SCANNER)
    await h.signal(mark, "ConfigChanged", "theme", timeout=5)
    h.check("settings: choosing a theme reaches the daemon", (await h.config())["theme"] == "scanner")
    mark = h.mark()
    await click_in(h, window, ROW_ORB)
    await h.signal(mark, "ConfigChanged", "theme", timeout=5)

    await open_page(h, window, "voice")
    await h.call("PreviewTheme", "s", "waves")
    await asyncio.sleep(1.0)
    state = await h.inspect()
    h.check("settings: band covers the play button", state["blob"]["visible"] and
            state["blob"]["box"]["y"] < window["y"] + PLAY_NICOLE[1], state["blob"])
    await h.shoot("band-over-window")
    mark = h.mark()
    await click_in(h, window, PLAY_NICOLE)
    await h.signal(mark, "StateChanged", "speaking", timeout=30)
    h.check("band: a click goes through to the window below", True)
    await h.signal(mark, "StateChanged", "idle", timeout=30)
    await asyncio.sleep(1.0)

    focus = "return global.display.focus_window?.title ?? null;"
    before = await h.js(focus)
    mark = h.mark()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    await asyncio.sleep(0.6)
    during = await h.js(focus)
    await h.shoot("blob-over-window")
    await h.call("Cancel")
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    await asyncio.sleep(0.5)
    h.check("focus: a turn never takes focus from the window",
            before == during == await h.js(focus) == "Tuxistant settings", (before, during))
    await close_settings(h)
    h.check("settings: window closes", not await settings_window(h))

    log = open(h.work / "settings.log", "ab")
    process = subprocess.Popen([h.wrapper, "settings"], env=h.env, cwd=h.work / "home",
                               stdout=log, stderr=log)
    h.check("wrapper: settings opens a window", await settings_window(h))
    await close_settings(h)
    h.check("wrapper: settings exits with its window",
            await asyncio.to_thread(process.wait, 10) == 0)

    done = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-B", str(HERE / "e2e_settings_probe.py")], env=h.env,
        capture_output=True, text=True, timeout=30)
    print(done.stderr[-2000:], flush=True)
    filled = json.loads(done.stdout.splitlines()[-1])
    print("  ", filled, flush=True)
    h.check("settings: general shows the server", filled["server"] == Config().values["server"], filled)
    h.check("settings: voices populate", filled["voices"] >= 20 and filled["voice"] == ["af_nicole"], filled)
    h.check("settings: themes populate", set(filled["themes"]) == {"orb", "scanner", "tux", "waves"} and
            filled["theme"] == ["orb"] and not filled["problems"], filled)
    h.check("settings: skills populate", len(filled["skills"]) >= 2, filled)
    rows = await h.history(100)
    h.check("settings: history populates", [r["heard"] or "Nothing heard" for r in rows] == filled["history"],
            filled["history"])


async def step_responsive(h: Harness):
    """A turn over a window must leave the shell answering: it shares its main loop with the blob."""
    await h.call("OpenSettings")
    await settings_window(h)
    await asyncio.sleep(1.0)
    await h.set_config("listen_timeout_s", 60)
    mark = h.mark()
    await h.call("Toggle")
    await h.signal(mark, "StateChanged", "listening", timeout=30)
    frames = await h.js("""
        let count = 0;
        const id = global.stage.connect('after-paint', () => count++);
        await new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2000, () => {
            resolve();
            return GLib.SOURCE_REMOVE;
        }));
        global.stage.disconnect(id);
        return count;""")
    worst = 0.0
    for _ in range(60):
        started = time.monotonic()
        await h.js("return 1;")
        worst = max(worst, time.monotonic() - started)
        await asyncio.sleep(0.05)
    h.timings["responsive_worst"] = worst
    h.timings["responsive_fps"] = frames / 2
    h.check("responsive: shell answers within a second during a turn", worst < 1, worst)
    h.check("responsive: frames keep coming", frames / 2 > 20, frames / 2)
    await h.call("Cancel")
    await h.signal(mark, "StateChanged", "idle", timeout=5)
    await h.set_config("listen_timeout_s", 8)
    await close_settings(h)


async def step_clear_history(h: Harness):
    await h.call("OpenSettings")
    window = await settings_window(h)
    await asyncio.sleep(1.5)
    await open_page(h, window, "history")
    await click_in(h, window, CLEAR_HISTORY)
    await asyncio.sleep(0.8)
    await h.shoot("settings-clear-dialog")
    mark = h.mark()
    await click_in(h, window, CONFIRM_CLEAR)
    await h.signal(mark, "HistoryChanged", timeout=5)
    await asyncio.sleep(0.6)
    h.check("settings: Clear empties the history", await h.history() == [])
    await h.shoot("settings-history-empty")
    await close_settings(h)
    await h.test.command("open_indicator")
    await asyncio.sleep(0.5)
    state = await h.inspect()
    h.check("status menu: follows", state["indicator"]["recent"] == ["No requests yet"], state["indicator"])
    await h.test.command("close_indicator")


STEPS = {
    "clips": step_clips, "wrapper_daemon": step_wrapper_daemon, "vad": step_vad,
    "hold_hide": step_hold_hide, "hold_show": step_hold_show, "hotkey": step_hotkey,
    "theme_menu": step_theme_menu, "cancel_reply": step_cancel_reply, "timeout": step_timeout,
    "conversation": step_conversation, "cli": step_cli, "fn": step_fn, "errors": step_errors,
    "settings": step_settings, "responsive": step_responsive, "clear_history": step_clear_history,
}
BEFORE_DAEMON = ("clips", "wrapper_daemon")


async def wait_for_shell(test):
    probe = ("return {up: !Main.layoutManager._startingUp,"
             " state: Main.extensionManager.lookup('tuxistant@buengenio')?.state};")
    for _ in range(240):
        try:
            result = await test.command("eval", timeout=0.5, code=probe)
            if result["up"] and result["state"] == 1:
                await asyncio.sleep(0.5)
                return
        except (TimeoutError, RuntimeError):
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError("the shell never came up")


async def run(args) -> int:
    work, out = Path(args.work), Path(args.out)
    bus = await MessageBus().connect()
    test = Test()
    bus.export(BUS_PATH, test)
    h = Harness(bus, test, work, out)
    bus.add_message_handler(h.on_message)
    await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                           interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                           body=[f"type='signal',interface='{BUS_NAME}'"]))
    await wait_for_shell(test)

    fifo = work / "fn.fifo"
    os.mkfifo(fifo)
    h.keyboard = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)   # also keeps EOF away from the helper
    h.helper = subprocess.Popen([str(ROOT / "helper/tuxistant-fn"), "--socket", h.env["TUXISTANT_FN_SOCKET"],
                                 "--event", str(fifo), "--rescan", "0.3"], env=h.env)
    await asyncio.sleep(0.5)

    only = set(filter(None, os.environ.get("TUX_ONLY", "").split(",")))
    failures = 0
    try:
        for name, step in STEPS.items():
            if name not in BEFORE_DAEMON and h.daemon is None:
                await h.start_daemon()
            if only and name not in only and name != "clips":
                continue
            print(f"--- {name}", flush=True)
            try:
                await step(h)
            except Exception as e:
                failures += 1
                h.check(f"{name}: step finished", False, f"{type(e).__name__}: {e}")
                traceback.print_exc()
                await h.shoot(f"failed-{name}")
                if h.daemon:
                    await h.to_idle()
    finally:
        for process in (h.daemon, h.helper):
            if process:
                code = await h.stop_process(process)
                if process is h.daemon:
                    h.check("daemon: stops cleanly", code == 0, code)
        (out / "report.json").write_text(json.dumps(
            {"checks": h.checks, "timings": h.timings,
             "signals": [[round(t, 3), n, a] for t, n, a in h.signals if n != "Level"]}, indent=1))
    failed = [c["name"] for c in h.checks if not c["ok"]]
    print(f"{len(h.checks) - len(failed)} of {len(h.checks)} checks passed", flush=True)
    return 1 if failed or failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    sys.exit(asyncio.run(run(parser.parse_args())))
