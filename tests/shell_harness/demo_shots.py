#!/usr/bin/env python3
"""The demo video's scenes, filmed by tools/demo_video.py through card.sh.

    TUX_DRIVER=demo_shots.py TUX_SCENARIO=demo-close TUX_MONITORS=1920x1080 TUX_SCALE=2 \\
        tests/shell_harness/card.sh tux <out-dir>
    TUX_DRIVER=demo_shots.py TUX_SCENARIO=demo-wide tests/shell_harness/card.sh tux <out-dir>

Two takes, as Settings needs the whole 1600x900 screen and Tux is small on one. The close take is
the same desktop at twice the scale, so the film can show the corner Tux lives in and stay sharp.

Runs on the isolated bus like every other card.sh scenario, so nothing here sees the real desktop.
The extension, the Settings app, the click on Allow and the typing into the window are real. The
requests, the replies and what Settings shows are made up in demo_script.py and docs_shots.py, and
no microphone, model, light, file or Trash is involved. Writes frames/ and film.json to <out-dir>.
If <out-dir>/voice.json is there, each line lasts as long as its recording and moves the blob as
that sound would.
"""
import asyncio
import contextlib
import functools
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import card_daemon
import demo_script
import docs_shots
from demo_script import text

SHOT_MS = 30            # a little faster than the film, which plays 30 frames a second
AT_ONCE = 3             # shots under way before the camera skips one: Waves takes 85 ms to draw
CHUNK_S = 0.04          # the real daemon sends a Level for every 40 ms of sound
LEAD_S = 0.5
THINK_S = 0.9
READ_S = 1.9            # long enough to read the card before the click
PAGE_S = 1.15
CARD = "demo-trash"
NOTES_SIZE = (580, 230)
NOTES_AT = (0.055, 0.3)     # of the screen, clear of the film's label and of Tux
PARKED = (0.45, 0.62)       # where the pointer waits for the card, as a share of the screen
EDGE = 40
NOTES = "global.get_window_actors().map(a => a.meta_window).find(w => w.title === 'Notes')"

# The colours of wallpaper.svg as a plain gradient: the picture's fine grain halves the frame rate.
BACKDROP = """
const settings = new Gio.Settings({schema_id: 'org.gnome.desktop.background'});
settings.set_string('picture-options', 'none');
settings.set_string('color-shading-type', 'vertical');
settings.set_string('primary-color', '#1b2138');
settings.set_string('secondary-color', '#4a3a6b');
"""

# The camera runs inside the shell: asked for over the bus, a shot takes ten times as long.
ROLL = """
const film = globalThis.tuxFilm = {times: [], shots: [], busy: 0};
const shoot = async () => {
    const index = film.times.push(GLib.get_monotonic_time() / 1e6) - 1;
    const stream = Gio.File.new_for_path(`%(folder)s/${String(index).padStart(5, '0')}.png`)
        .replace(null, false, Gio.FileCreateFlags.NONE, null);
    const shooter = new Shell.Screenshot();
    film.busy++;
    try {
        await new Promise((resolve, reject) => shooter.screenshot(%(pointer)s, stream, (_s, result) => {
            try {
                shooter.screenshot_finish(result);
                resolve();
            } catch (e) {
                reject(e);
            }
        }));
    } finally {
        film.busy--;
        stream.close(null);
    }
};
film.timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, %(every_ms)d, () => {
    if (film.busy < %(at_once)d)
        film.shots.push(shoot());
    return GLib.SOURCE_CONTINUE;
});
"""

CUT = """
const film = globalThis.tuxFilm;
GLib.source_remove(film.timer);
await Promise.all(film.shots);
return film.times;
"""

GLIDE = card_daemon.POINTER + """
const [fromX, fromY] = global.get_pointer();
const steps = Math.max(1, Math.round(%(ms)d / 16));
for (let step = 1; step <= steps; step++) {
    const t = step / steps, eased = t * t * (3 - 2 * t);
    pointer.notify_absolute_motion(time(), fromX + (%(x)f - fromX) * eased, fromY + (%(y)f - fromY) * eased);
    await sleep(16);
}
"""


class Film:
    """Scenes, each with its frames and the time every one was taken, and where the sounds fall."""

    def __init__(self, test, out):
        self.test, self.out = test, out
        self.scenes, self.sounds = [], []

    @contextlib.asynccontextmanager
    async def scene(self, name, pointer=False):
        folder = self.out / "frames" / name
        folder.mkdir(parents=True)
        start = time.monotonic()
        await self.test.command("eval", code=ROLL % {
            "folder": folder, "pointer": json.dumps(pointer), "every_ms": SHOT_MS, "at_once": AT_ONCE})
        try:
            yield
        finally:
            frames = await self.test.command("eval", code=CUT)
            self.scenes.append({"name": name, "start": start, "end": time.monotonic(), "frames": frames})

    def heard(self, line):
        self.sounds.append({"line": line, "at": time.monotonic()})

    def save(self):
        (self.out / "film.json").write_text(json.dumps({"scenes": self.scenes, "sounds": self.sounds}, indent=1))


class Voice:
    """Says a line: marks where its sound goes in the film and sends the levels that sound would."""

    def __init__(self, daemon, film, out):
        self.daemon, self.film = daemon, film
        clips = out / "voice.json"
        self.clips = json.loads(clips.read_text()) if clips.exists() else {}

    def seconds(self, line):
        return self.clips[line]["seconds"] if line in self.clips else demo_script.estimate(line)

    def _levels(self, line):
        """With no recording, the lively level fake_daemon.py sends."""
        recorded = self.clips.get(line, {}).get("levels")
        count = math.ceil(self.seconds(line) / CHUNK_S)
        return recorded or [0.6 + 0.2 * math.sin(i * 0.28) + 0.1 * math.sin(i * 0.68) for i in range(count)]

    async def say(self, line):
        self.film.heard(line)
        start = time.monotonic()
        for index, level in enumerate(self._levels(line)):
            self.daemon.Level(level)
            await asyncio.sleep(max(0, start + (index + 1) * CHUNK_S - time.monotonic()))
        self.daemon.Level(0.0)


class Show:
    def __init__(self, daemon, test, args):
        self.daemon, self.test = daemon, test
        self.run = card_daemon.Run(daemon, test, args)
        self.album = docs_shots.Album(self.run)
        self.film = Film(test, self.run.out)
        self.voice = Voice(daemon, self.film, self.run.out)

    def scene(self, name, pointer=False):
        return self.film.scene(name, pointer)

    async def measure(self):
        self.screen = await self.test.command(
            "eval", code="return {width: global.stage.width, height: global.stage.height};")

    async def park(self, x, y):
        await self.glide(self.screen["width"] * x, self.screen["height"] * y, 50)

    async def glide(self, x, y, ms=600):
        await self.test.command("eval", code=GLIDE % {"x": x, "y": y, "ms": ms})

    async def glide_to(self, box, ms=600):
        await self.glide(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, ms)

    async def hear(self, line):
        """A request as the daemon reports one: the words arrive while they are being said."""
        daemon, words = self.daemon, text(line).split()
        daemon.Presence(True)
        daemon.set_state("listening")
        await asyncio.sleep(LEAD_S)
        saying = asyncio.create_task(self.voice.say(line))
        for count in range(1, len(words) + 1):
            await asyncio.sleep(self.voice.seconds(line) / len(words))
            daemon.Transcript(" ".join(words[:count]), count == len(words))
        await saying
        daemon.set_state("thinking")
        await asyncio.sleep(THINK_S)

    async def reply(self, line):
        self.daemon.set_state("speaking")
        self.daemon.Reply(text(line))
        await self.voice.say(line)

    async def rest(self, seconds=0.6):
        await self.run.end_turn()
        await asyncio.sleep(seconds)


async def intro(show):
    await show.run.use_theme("tux")
    async with show.scene("intro"):
        await asyncio.sleep(2.2)


async def ask(show):
    async with show.scene("ask"):
        await show.hear("weather-ask")
        await show.reply("weather")
        await show.rest(1.2)


async def click_allow(show):
    """Only this click answers the card, so a film of it going ahead is a film of a real Allow."""
    card = await show.run.card(card_daemon.ARMED_S)
    await asyncio.sleep(READ_S)
    await show.glide_to(card["allow"])
    await asyncio.sleep(0.25)
    await show.run.click(card["allow"])
    await asyncio.sleep(0.3)
    if ["Answer", CARD, True] not in show.run.answers():
        raise RuntimeError("the click on Allow never reached the daemon")
    await show.run.take_down(CARD)
    await show.glide(show.screen["width"] * PARKED[0], show.screen["height"] * PARKED[1], 500)


async def approve(show):
    await show.park(*PARKED)
    async with show.scene("approve", pointer=True):
        await show.hear("trash-ask")
        show.daemon.Approval(CARD, *docs_shots.trash_card(), "click", 45)
        await click_allow(show)
        await show.reply("trash")
        await show.rest(1.0)


def notes_window(show):
    log = show.run.out / "notes.txt"
    env = {**os.environ, "WAYLAND_DISPLAY": os.environ["TUX_WAYLAND_DISPLAY"],
           "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo"}
    window = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).with_name("demo_window.py")), str(log), *map(str, NOTES_SIZE)],
        env=env, stderr=subprocess.DEVNULL)
    return window, log


async def focus_on_notes(show):
    for _ in range(100):
        if await show.test.command("eval", code="return global.display.focus_window?.title ?? null;") == "Notes":
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("the notes window never got the focus")
    x, y = round(show.screen["width"] * NOTES_AT[0]), round(show.screen["height"] * NOTES_AT[1])
    await show.test.command("eval", code=f"{NOTES}.move_frame(true, {x}, {y});")
    await asyncio.sleep(0.8)
    await show.glide(x + NOTES_SIZE[0] * 0.7, y + NOTES_SIZE[1] * 0.75, 50)


async def speak_into_notes(show, log):
    """The daemon's own order: the blob listens, the turn ends, then the words reach the window."""
    daemon, words = show.daemon, text("dictation") + " "
    daemon.Presence(True)
    daemon.set_state("listening")
    daemon.DictationStarted("demo-dictation")
    await asyncio.sleep(LEAD_S)
    await show.voice.say("dictation")
    await show.run.end_turn()
    daemon.TypeText("demo-dictation", words, False)
    await asyncio.sleep(0.6)
    if ["Typed", "demo-dictation", "typed"] not in daemon.calls or log.read_text() != words:
        raise RuntimeError("the dictated words never reached the window")


async def dictate(show):
    window, log = notes_window(show)
    try:
        await focus_on_notes(show)
        async with show.scene("dictate", pointer=True):
            await asyncio.sleep(0.6)
            await speak_into_notes(show, log)
            await asyncio.sleep(2.0)
    finally:
        window.terminate()
        window.wait(timeout=5)
    await asyncio.sleep(0.6)


async def lights(show):
    async with show.scene("lights"):
        await show.hear("lights-ask")
        await show.reply("lights")
        await show.rest(1.0)


async def one_theme(show, theme):
    """A turn of its own, so that the caption with the last theme's name has gone."""
    daemon = show.daemon
    await show.run.use_theme(theme)
    async with show.scene(f"theme-{theme}"):
        daemon.Presence(True)
        for state, seconds in (("listening", 0.7), ("thinking", 0.5)):
            daemon.set_state(state)
            await asyncio.sleep(seconds)
        await show.reply(f"theme-{theme}")
        await asyncio.sleep(0.4)
    await show.rest(0.2)


async def themes(show):
    for theme in demo_script.THEMES:
        await one_theme(show, theme)
    await show.run.use_theme("tux")


async def pages(show, frame):
    for index in range(1, len(docs_shots.PAGES)):     # it opens on the first
        x, y = frame["x"] + docs_shots.SIDEBAR_X, frame["y"] + docs_shots.SIDEBAR_Y + index * docs_shots.SIDEBAR_STEP
        await show.glide(x, y, 350)
        await show.test.command("click", x=x, y=y, button=1)
        await asyncio.sleep(PAGE_S)


async def to_the_right(show, frame):
    """Leaves the left of the screen for the film's label."""
    x = show.screen["width"] - frame["width"] - EDGE
    await show.test.command("eval", code=f"{docs_shots.SETTINGS}[0].move_frame(true, {x}, {frame['y']});")
    return await docs_shots.window_frame(show.album)


async def settings(show):
    env = {**os.environ, "WAYLAND_DISPLAY": os.environ["TUX_WAYLAND_DISPLAY"],
           "GDK_BACKEND": "wayland", "GSK_RENDERER": "cairo", "ADW_DEBUG_COLOR_SCHEME": "prefer-dark"}
    app = subprocess.Popen([sys.executable, "-B", "-m", "tuxistant", "settings"], cwd=docs_shots.ROOT,
                           env=env, stderr=subprocess.DEVNULL)
    try:
        frame = await to_the_right(show, await docs_shots.window_frame(show.album))
        await show.glide(frame["x"] + docs_shots.SIDEBAR_X + 60, frame["y"] + docs_shots.SIDEBAR_Y + 250, 50)
        await asyncio.sleep(1.5)
        async with show.scene("settings", pointer=True):
            await asyncio.sleep(PAGE_S)
            await pages(show, frame)
    finally:
        app.terminate()
        app.wait(timeout=5)
    await asyncio.sleep(0.8)


async def outro(show):
    async with show.scene("outro"):
        show.daemon.Presence(True)
        await show.reply("outro")
        await show.rest(1.4)


async def take(steps, daemon, test, args):
    show = Show(daemon, test, args)
    try:
        await show.measure()
        await test.command("eval", code=BACKDROP)
        await asyncio.sleep(1.5)
        for step in steps:
            await step(show)
    finally:
        show.film.save()


TAKES = {"close": (intro, ask, approve, dictate, lights, themes, outro), "wide": (settings,)}
for name, steps in TAKES.items():
    card_daemon.SCENARIOS[f"demo-{name}"] = functools.partial(take, steps)

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(card_daemon.main()))
    except (TimeoutError, RuntimeError, AssertionError) as e:
        print(f"harness failure: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
