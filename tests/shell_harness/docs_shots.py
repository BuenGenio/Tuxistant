#!/usr/bin/env python3
"""The pictures in the README and docs, taken by tools/docs_images.py through card.sh.

Runs on the isolated bus like every other card.sh scenario, so nothing here sees the real desktop.
What is on screen is the real extension and the real Settings app. What they show is made up here,
so that no picture carries a real server name, file or request. Card wording copies what the skills
really write, and the skill and voice lists are the real ones.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dbus_fast.service import dbus_method

import card_daemon
import fake_daemon
from fake_daemon import SETTLE_S
from tuxistant import files, shellgate, voices
from tuxistant.skills import Skills

ROOT = Path(__file__).resolve().parents[2]
WALLPAPER = Path(__file__).with_name("wallpaper.svg")
THEMES = ("orb", "scanner", "tux", "waves")
STATES = ("listening", "thinking", "speaking")
PAGES = ("general", "voice", "appearance", "skills", "history")
SIDEBAR_X, SIDEBAR_Y, SIDEBAR_STEP = 100, 71, 44    # as in e2e_scenario.py
WINDOW_MARGIN = 28      # keeps the window's shadow
SETTINGS = "global.get_window_actors().map(a => a.meta_window).filter(w => w.title === 'Tuxistant settings')"

CONFIG = {"server": "ai-box.local:8080", "api_key_set": True, "location": "Lisbon",
          "phone_device": "Pixel", "translate_server": ""}
PHONES = [{"id": "1", "name": "Pixel", "type": "phone", "connected": True, "paired": True}]
CALENDARS = [{"id": "1", "name": "Family", "account": "me@example.org"}]
TURNS = [
    ("What's the weather?", "It's nineteen degrees and sunny in Lisbon, with a light breeze.", []),
    ("Find my 2024 tax return", "I found three. The best match is tax-return-2024.pdf in Documents.",
     ["file.find"]),
    ("How do you say thank you in Ukrainian?", "Dyakuyu.", ["translate.text"]),
    ("Which folder is the biggest?", "Videos, at 41 gigabytes.", ["disk.folders"]),
    ("Is docker running?", "Yes, docker is running.", ["system.running"]),
    ("Where's my phone?", "Ringing your Pixel now.", ["phone.ring"]),
]
HISTORY = [{"id": i, "ts": 1793800000.0 - i * 2700, "heard": heard, "reply": reply, "status": "ok",
            "actions": [{"name": name, "args": {}, "result": {}} for name in actions]}
           for i, (heard, reply, actions) in enumerate(TURNS)]
ADMIN = (shellgate.ADMIN_TITLE, "apt-get install -y vlc")
GB, MB = 1000 ** 3, 1000 ** 2
FRAME_S = 0.09


def trash_card():
    """The real card for an imaginary sweep of Downloads, so its wording can't drift from the skill."""
    home = Path.home() / "Downloads"
    ask = files._card([
        files._Target(home / "ubuntu-24.04-desktop.iso", (1, 1), int(2.6 * GB)),
        files._Target(home / "holiday-photos-2023.zip", (1, 2), 412 * MB),
        files._Target(home / "invoice-march.pdf", (1, 3), 2 * MB),
        files._Target(home / "old-invoices", (1, 4), 38 * MB, 41, True),
    ])
    return ask.title, ask.detail


class Daemon(card_daemon.Daemon):
    def __init__(self, theme):
        super().__init__(theme)
        self.config.update(CONFIG)

    @dbus_method()
    def ListVoices(self) -> "s":
        return json.dumps(voices.catalog())

    @dbus_method()
    def ListSkills(self) -> "s":
        return json.dumps(Skills().listing())

    @dbus_method()
    def ListPhones(self) -> "s":
        return json.dumps(PHONES)

    @dbus_method()
    def ListCalendars(self) -> "s":
        return json.dumps(CALENDARS)

    @dbus_method()
    def GetHistory(self, limit: "i") -> "s":
        return json.dumps(HISTORY[:limit])


class Album:
    """Shots, plus the part of each one worth keeping, for docs_images.py to crop to."""

    def __init__(self, run):
        self.run, self.test, self.daemon = run, run.test, run.daemon
        self.crops = {}

    async def shoot(self, name, crop=None):
        await self.run.shoot(name)
        self.crops[name] = crop

    async def blob_box(self):
        return (await self.test.command("inspect"))["blob"]["box"]

    async def turn(self, state, settle=SETTLE_S):
        self.daemon.Presence(True)
        self.daemon.set_state(state)
        await asyncio.sleep(settle)

    async def quiet(self):
        await self.run.end_turn()
        await asyncio.sleep(0.4)


async def wallpaper(album):
    await album.test.command("eval", code=f"""
        const settings = new Gio.Settings({{schema_id: 'org.gnome.desktop.background'}});
        for (const key of ['picture-uri', 'picture-uri-dark'])
            settings.set_string(key, {json.dumps(WALLPAPER.as_uri())});
        settings.set_string('picture-options', 'zoom');
    """)
    await asyncio.sleep(1.5)


async def themes(album):
    for theme in THEMES:
        await album.run.use_theme(theme)
        for state in STATES:
            await album.turn(state)
            box = await album.blob_box()
            await album.shoot(f"theme-{theme}-{state}", box)
        await album.quiet()


async def turn(album):
    daemon = album.daemon
    await album.run.use_theme("tux")
    await album.turn("listening", 0.6)
    daemon.Transcript("What's the weather?", False)
    await asyncio.sleep(SETTLE_S)
    await album.shoot("turn-listening")
    daemon.Transcript("What's the weather?", True)
    await album.turn("thinking")
    await album.shoot("turn-thinking")
    daemon.set_state("speaking")
    daemon.Reply(TURNS[0][1])
    await asyncio.sleep(SETTLE_S)
    await album.shoot("turn-speaking")
    await album.quiet()


async def film(album, folder, stop):
    """As fast as the shell will go. Shaders run on the clock, so each frame keeps its own time."""
    folder.mkdir(exist_ok=True)
    times = []
    while not stop.is_set():
        times.append(time.monotonic())
        await album.test.command("shoot", path=str(folder / f"{len(times):04}.png"))
        await asyncio.sleep(FRAME_S)
    (folder / "times.json").write_text(json.dumps(times))


async def motion(album):
    daemon, stop = album.daemon, asyncio.Event()
    await album.run.use_theme("tux")
    camera = asyncio.create_task(film(album, album.run.out / "motion", stop))
    await asyncio.sleep(0.8)
    await album.turn("listening", 0.8)
    words = TURNS[0][0].split()
    for count in range(1, len(words) + 1):
        daemon.Transcript(" ".join(words[:count]), count == len(words))
        await asyncio.sleep(0.45)
    await album.turn("thinking", 1.2)
    daemon.set_state("speaking")
    daemon.Reply(TURNS[0][1])
    await asyncio.sleep(3.6)
    await album.run.end_turn()
    await asyncio.sleep(1.0)
    stop.set()
    await camera


async def cards(album):
    daemon, run = album.daemon, album.run
    await run.use_theme("orb")
    await album.turn("thinking")
    daemon.Transcript("Move everything in Downloads older than a year to the Trash", True)
    await asyncio.sleep(0.4)
    for name, (title, detail), level in (("card-trash", trash_card(), "click"), ("card-admin", ADMIN, "admin")):
        daemon.Approval(name, title, detail, level, 45)
        card = await run.card(card_daemon.ARMED_S)
        await album.shoot(name)
        album.crops[f"{name}-only"] = card["box"]
        await run.take_down(name)
        await asyncio.sleep(0.5)
    await album.quiet()


async def panel(album):
    await album.test.command("open_indicator")
    await asyncio.sleep(0.8)
    await album.shoot("panel-menu")
    await album.test.command("close_indicator")
    await asyncio.sleep(0.4)


async def window_frame(album):
    last = None
    for _ in range(100):
        found = await album.test.command("eval", code=f"""return {SETTINGS}.map(w => {{
            const r = w.get_frame_rect();
            return {{x: r.x, y: r.y, width: r.width, height: r.height}};
        }});""")
        if found and found[0] == last and last["x"] > 0:
            return last
        last = found[0] if found else None
        await asyncio.sleep(0.25)
    raise RuntimeError("the Settings window never settled")


def with_margin(frame):
    return {"x": frame["x"] - WINDOW_MARGIN, "y": frame["y"] - WINDOW_MARGIN,
            "width": frame["width"] + 2 * WINDOW_MARGIN, "height": frame["height"] + 2 * WINDOW_MARGIN}


async def settings(album):
    env = {**os.environ, "WAYLAND_DISPLAY": os.environ["TUX_WAYLAND_DISPLAY"],
           "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo", "ADW_DEBUG_COLOR_SCHEME": "prefer-dark"}
    app = subprocess.Popen([sys.executable, "-B", "-m", "tuxistant", "settings"], cwd=ROOT, env=env,
                           stderr=subprocess.DEVNULL)
    try:
        frame = await window_frame(album)
        await asyncio.sleep(1.5)
        for index, page in enumerate(PAGES):
            await album.test.command("click", x=frame["x"] + SIDEBAR_X,
                                     y=frame["y"] + SIDEBAR_Y + index * SIDEBAR_STEP, button=1)
            await asyncio.sleep(1.0)
            await album.shoot(f"settings-{page}", with_margin(frame))
    finally:
        app.terminate()
        app.wait(timeout=5)


async def scenario_docs(daemon, test, args):
    run = card_daemon.Run(daemon, test, args)
    album = Album(run)
    pump = asyncio.create_task(fake_daemon.pump_levels(daemon))
    try:
        for step in (wallpaper, themes, turn, motion, cards, panel, settings):
            await step(album)
    finally:
        pump.cancel()
        (run.out / "crops.json").write_text(json.dumps(album.crops, indent=1))


card_daemon.Daemon = Daemon
card_daemon.SCENARIOS["docs"] = scenario_docs

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(card_daemon.main()))
    except (TimeoutError, RuntimeError, AssertionError) as e:
        print(f"harness failure: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
