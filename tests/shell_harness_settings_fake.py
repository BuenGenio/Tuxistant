#!/usr/bin/env python3
"""Fake org.tuxistant.Daemon for the Settings app: only what Settings calls.

Config and History are the real classes on a temp folder, so writes are validated as the daemon
would validate them. A second interface, org.tuxistant.Test, lets a test read the calls it got
and act like the rest of the system. Refuses to run on the user's real session bus.

    dbus-run-session -- python3 tests/shell_harness_settings_fake.py --dir /tmp/somewhere
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method, dbus_signal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import BUS_NAME, BUS_PATH, voices  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.skillhost import offline  # noqa: E402
from tuxistant.skills import risk_of  # noqa: E402

TEST_INTERFACE = "org.tuxistant.Test"
DAY = 86400

# Names with markup characters prove that outside text is never parsed as Pango markup.
THEMES = {
    "themes": [
        {"id": "orb", "name": "Orb", "author": "Tuxistant", "version": "1.0.0"},
        {"id": "rock", "name": "Rock & <Roll", "author": "A <b>bold</b> & loud author",
         "version": "1.0.0"},
        {"id": "waves", "name": "Waves", "author": "Tuxistant", "version": "1.0.0"},
    ],
    "problems": [
        {"path": "/home/tux/.local/share/tuxistant/themes/broken",
         "error": "id must match the folder name"},
        {"path": "/home/tux/.local/share/tuxistant/themes/bad&<name",
         "error": "layout.kind must be orb or band, got <wobble> & more"},
    ],
}
MARKUP_NAME = '<b>Bold</b> &amp; <span foreground="red">red</span>'
MARKUP_TEXT = ('Does <b>things</b> &amp; <span foreground="red">more</span> '
               + "z" * 300 + " " + "long words " * 40)
# A recipe of nearly 64 KB, the most a manifest may be, with markup in a comment, and a code skill.
RECIPE = """id: org.example.tides
name: Tides
description: Says when the next high tide is &amp; more
version: "1.0.0"
permissions:
  network: [api.tides.example]
actions:
  tides.next:
    description: Say when the next high tide is at a seaside town
    params:
      town: { type: text, max: 40, description: "The town, like Brighton" }
    required: [town]
    get: https://api.tides.example/v1/next?town={town}
    say: "The next high tide in {town} is soon."
# <span foreground="red">Not markup</span> &amp; <b>not bold</b>
""" + "# filler line to make it long, with <b>tags</b> & ampersands\n" * 1000
MOON = """id: org.example.moon
name: Moon
description: Tells you the phase of the moon
version: 1.0.0
run: [python3, main.py]
permissions: { network: true }
actions:
  moon.phase:
    description: Say what phase the moon is in tonight
"""
EXTRA_SKILLS = [
    {"id": "org.example.web", "name": "Web lookup", "description": "Searches the web for you",
     "permissions": {"network": ["api.example.org"], "files_read": True},
     "actions": ["web.search"]},
    {"id": "org.example.shell", "name": "Run commands", "description": "Runs commands you ask for",
     "permissions": {"shell": True, "files_write": ["~/Documents"], "clipboard": True,
                     "camera": True},
     "actions": ["shell.run"]},
    {"id": "org.example.markup", "name": MARKUP_NAME, "description": MARKUP_TEXT,
     "permissions": {"network": True, "<b>odd</b>": True}, "actions": ["markup.go"],
     "source": "yours"},
]
HOSTILE_FOLDER = "/home/tux/.local/share/tuxistant/skills/<b>m</b>\nComes with Tuxistant: a code skill"
SANDBOX_PROBLEM = "Skills with code of their own need bubblewrap, and it isn't working here."
# A daemon checks what a skill you add says, but Settings must not count on that: a built-in
# manifest isn't held to it, and a comment in any manifest can say anything.
DETAILS = {"org.example.markup": {
    **EXTRA_SKILLS[-1], "risk": "medium", "recipe": False, "version": "<i>1</i>",
    "risk_because": ["network"], "file": HOSTILE_FOLDER + "/skill.yaml", "folder": HOSTILE_FOLDER,
    # A line break in a name could fake a second file or another origin. 2,000 files, the most a
    # skill may have, mustn't hold the page either.
    "files": ["skill.yaml", "<b>main</b>.py", "x" * 300, "README\nskill.yaml"]
             + [f"lib/f{i:04d}_" + "n" * 190 + ".py" for i in range(1996)],
    "manifest": "# <span foreground=\"red\">red</span> &amp; <b>bold</b>\n" + "y" * 400 + "\n"
                + "# <b>filler</b> &lt;\n" * 3098,
    "actions": [{"name": "markup.go", "offered": True, "held_back": "", "tool": {
        "type": "function", "name": "markup_go", "description": MARKUP_TEXT,
        "parameters": {"type": "object", "required": ["<i>q</i>"], "properties": {
            "<i>q</i>": {"type": "string", "description": "<span foreground=\"red\">q</span>"}}}}}],
}}
SKILL_PROBLEMS = {
    "/home/tux/.local/share/tuxistant/skills/broken.yaml":
        "It asks for shell, and a recipe may only name the hosts it reaches.",
}


CALENDARS = [{"id": "cal-family", "name": "Family", "account": "me@example.org"},
             {"id": "cal-local", "name": "Personal", "account": "On This Computer"}]
PHONES = [
    {"id": "a1b2", "name": "Pixel <8> & co", "type": "phone", "connected": True},
    {"id": "c3d4", "name": "Galaxy Tab", "type": "tablet", "connected": False},
]


class Daemon(ServiceInterface):
    def __init__(self, folder: Path):
        super().__init__(BUS_NAME)
        self.config = Config(folder / "config.json")
        self.history = History(folder / "history.db")
        self.calls = []
        self.failures = {}
        self.delays = {}
        self.phones = PHONES
        self.calendars = CALENDARS
        self.skill_problems = SKILL_PROBLEMS
        self.host = offline(self.config, user=self.user_skills(folder / "skills"))
        if not self.history.recent(1):
            self.seed_history()

    @staticmethod
    def user_skills(folder: Path) -> Path:
        (folder / "moon" / "lib").mkdir(parents=True, exist_ok=True)
        (folder / "tides.yaml").write_text(RECIPE)
        (folder / "moon" / "skill.yaml").write_text(MOON)
        (folder / "moon" / "main.py").write_text("print('moon')\n")
        (folder / "moon" / "lib" / "phase.py").write_text("FULL = 1\n")
        return folder

    def seed_history(self):
        add = self.history.add
        add("What's the <time> & date?", "It is half past ten.",
            [{"name": "time.now", "arguments": "{}", "result": {"time": "10:30"}}], "ok")
        add("How much disk is free?", "You have 120 gigabytes free.",
            [{"name": "disk.usage", "arguments": "{}", "result": {"free_gb": 120}}], "ok")
        add("", "Sorry, I didn't catch that.", [], "error")
        add("Check a folder that doesn't exist", "I couldn't read that folder.",
            [{"name": "disk.usage", "arguments": "{}", "result": {"error": "no such folder"}}],
            "ok")
        self.history.db.execute("UPDATE requests SET ts = ts - ? WHERE id = 1", (9 * DAY,))
        self.history.db.execute("UPDATE requests SET ts = ts - ? WHERE id = 2", (DAY,))
        self.history.db.commit()

    async def enter(self, method, *args):
        self.calls.append([method, *args])
        if method in self.delays:
            await asyncio.sleep(self.delays.pop(method) / 1000)
        if method in self.failures:
            name, message = self.failures.pop(method)
            raise DBusError(name, message)

    @dbus_method()
    async def GetConfig(self) -> "s":
        await self.enter("GetConfig")
        return json.dumps(self.config.public())

    @dbus_method()
    async def SetConfig(self, key: "s", value: "s"):
        await self.enter("SetConfig", key, value)
        try:
            self.config.set(key, json.loads(value))
        except (KeyError, TypeError, ValueError) as e:
            raise DBusError("org.tuxistant.Error.Invalid", str(e)) from e
        self.ConfigChanged(key)

    @dbus_method()
    async def ListVoices(self) -> "s":
        await self.enter("ListVoices")
        return json.dumps(voices.catalog())

    @dbus_method()
    async def PreviewVoice(self, voice: "s"):
        await self.enter("PreviewVoice", voice)

    @dbus_method()
    async def ListThemes(self) -> "s":
        await self.enter("ListThemes")
        return json.dumps(THEMES)

    @dbus_method()
    async def PreviewTheme(self, theme: "s"):
        await self.enter("PreviewTheme", theme)

    @dbus_method()
    async def ListSkills(self) -> "s":
        await self.enter("ListSkills")
        extra = [{**s, "risk": risk_of(s["permissions"])} for s in EXTRA_SKILLS]
        return json.dumps(self.host.current().listing() + extra)

    @dbus_method()
    async def SkillDetails(self, skill_id: "s") -> "s":
        await self.enter("SkillDetails", skill_id)
        return json.dumps(DETAILS.get(skill_id) or self.host.current().details(skill_id))

    @dbus_method()
    async def SkillProblems(self) -> "s":
        await self.enter("SkillProblems")
        return json.dumps(self.skill_problems)

    @dbus_method()
    async def ListPhones(self) -> "s":
        await self.enter("ListPhones")
        return json.dumps(self.phones)

    @dbus_method()
    async def ListCalendars(self) -> "s":
        await self.enter("ListCalendars")
        return json.dumps(self.calendars)

    @dbus_method()
    async def GetHistory(self, limit: "i") -> "s":
        reply = json.dumps(self.history.recent(limit))  # before any delay: it can go stale
        await self.enter("GetHistory", limit)
        return reply

    @dbus_method()
    async def ClearHistory(self):
        await self.enter("ClearHistory")
        self.history.clear()
        self.HistoryChanged()

    @dbus_signal()
    def ConfigChanged(self, key) -> "s":
        return key

    @dbus_signal()
    def HistoryChanged(self):
        pass

    @dbus_signal()
    def SkillsChanged(self):
        pass


class Control(ServiceInterface):
    def __init__(self, daemon: Daemon, stop: asyncio.Event):
        super().__init__(TEST_INTERFACE)
        self.daemon = daemon
        self.stop = stop

    @dbus_method()
    def Calls(self) -> "s":
        return json.dumps(self.daemon.calls)

    @dbus_method()
    def Forget(self):
        self.daemon.calls.clear()

    @dbus_method()
    def Change(self, key: "s", value: "s"):
        """Another client changed a setting."""
        self.daemon.config.set(key, json.loads(value))
        self.daemon.ConfigChanged(key)

    @dbus_method()
    def Stored(self, key: "s") -> "s":
        return json.dumps(self.daemon.config[key])

    @dbus_method()
    def AddRequest(self, heard: "s", reply: "s"):
        self.daemon.history.add(heard, reply, [], "ok")
        self.daemon.HistoryChanged()

    @dbus_method()
    def AddRecord(self, heard: "s", reply: "s", actions: "s", status: "s"):
        """A finished request as the daemon would keep it, through the real History."""
        self.daemon.history.add(heard, reply, json.loads(actions), status)
        self.daemon.HistoryChanged()

    @dbus_method()
    def SetSkillProblems(self, problems: "s"):
        """A reload found these, as after a file was dropped into the skills folder."""
        self.daemon.skill_problems = json.loads(problems)
        self.daemon.SkillsChanged()

    @dbus_method()
    def SetSandbox(self, working: "b"):
        """As a computer without bubblewrap: Lights, which needs it, isn't listed, only a problem."""
        host = self.daemon.host
        host._sandbox_problem = (lambda: "") if working else (lambda: SANDBOX_PROBLEM)
        problems = host.reload()
        self.daemon.skill_problems = SKILL_PROBLEMS if working else {**SKILL_PROBLEMS, **problems}
        self.daemon.SkillsChanged()

    @dbus_method()
    def SetPhones(self, phones: "s"):
        self.daemon.phones = json.loads(phones)

    @dbus_method()
    def SetCalendars(self, calendars: "s"):
        self.daemon.calendars = json.loads(calendars)

    @dbus_method()
    def FailNext(self, method: "s", message: "s"):
        self.daemon.failures[method] = ("org.tuxistant.Error.Failed", message)

    @dbus_method()
    def FailNextAs(self, method: "s", name: "s", message: "s"):
        """As a daemon older than the method would: org.freedesktop.DBus.Error.UnknownMethod."""
        self.daemon.failures[method] = (name, message)

    @dbus_method()
    def DelayNext(self, method: "s", ms: "i"):
        self.daemon.delays[method] = ms

    @dbus_method()
    def Quit(self):
        self.stop.set()


async def serve(folder: Path):
    stop = asyncio.Event()
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    daemon = Daemon(folder)
    bus.export(BUS_PATH, daemon)
    bus.export(BUS_PATH, Control(daemon, stop))
    await bus.request_name(BUS_NAME)
    print("ready", flush=True)
    await stop.wait()
    await asyncio.sleep(0.05)  # let the reply to Quit go out
    bus.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    folder = parser.parse_args().dir
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address or f"/run/user/{os.getuid()}/bus" in address:
        sys.exit("refusing to run: this needs a private bus, use dbus-run-session")
    asyncio.run(serve(folder))


if __name__ == "__main__":
    main()
