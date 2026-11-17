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


FAILED = "org.tuxistant.Daemon.Error.Failed"
REGISTRY_ADDRESS = "https://example.org/registry/"
# What the registry offers, as the daemon would send it after checking. Each recipe is loaded by
# the real loader, so FetchSkill's tools are what the model would be sent.
REGISTRY_RECIPES = {
    "io.github.ada.tides": """id: io.github.ada.tides
name: Tides
description: Says when the next high tide is
version: "1.1.0"
permissions:
  network: [tides.example.org]
actions:
  tides.next:
    description: Say when the next high tide is at a seaside town
    params:
      town: { type: text, max: 40, description: "The town, like Brighton" }
    required: [town]
    get: https://tides.example.org/v1/next?town={town}
    say: "The next high tide in {town} is soon."
""",
    "io.github.bob.prices": """id: io.github.bob.prices
name: Prices
description: Says what a coin is worth
version: "1.1.0"
permissions:
  network: [prices.example.org]
actions:
  prices.coin:
    description: Say what a coin is worth now
    params:
      coin: { type: choice, of: [bitcoin, ether], description: "Which coin" }
    required: [coin]
    get: https://prices.example.org/v1/{coin}
    say: "It is worth a lot."
""",
    "io.github.eve.lunar": """id: io.github.eve.lunar
name: Moon phase
description: Says that the moon is up there
version: "2.0.0"
permissions: {}
actions:
  lunar.phase:
    description: Say something about the moon tonight
    say: "The moon is up there somewhere."
""",
    "io.github.mal.markup": """id: io.github.mal.markup
name: Markup
description: Says hello
version: "1.0.0"
permissions:
  network: [markup.example.org]
actions:
  markup.hello:
    description: Say hello from the markup host
    get: https://markup.example.org/hello
# <span foreground="red">Not markup</span> &amp; <b>not bold</b>
""",
}
MARKUP_SUMMARY = 'Says <b>hello</b> &amp; <span foreground="red">more</span>'
SPY_REASON = "It sent <b>your</b> mail & more to a host it didn't name"
PRICES_REASON = "Version 1.0.0 asked for <i>far</i> too much"


def registry_listing(skills: Path) -> dict:
    """What BrowseSkills sends, with every state Browse shows: installed with an update that asks
    for more, edited, banned, not installed, and a stranger's markup everywhere it can be."""
    def mine(skill_id, version, edited=False, off=None):
        return {"version": version, "file": str(skills / f"{skill_id}.yaml"), "edited": edited,
                "off": off}

    def entry(skill_id, name, summary, tags, version, date, hosts, **more):
        return {"id": skill_id, "author": skill_id.split(".")[2], "name": name,
                "summary": summary, "tags": tags, "licence": "MIT", "links": [],
                "version": version, "date": date, "hosts": hosts,
                "risk": None if version is None else "medium" if hosts else "low",
                "risk_because": None if version is None else ["network"] if hosts else [],
                "installed": None, "update": None, **more}

    return {
        "address": REGISTRY_ADDRESS,
        "index": {"seq": 4, "date": "2026-11-12T09:30:00Z", "accepted": "2026-11-12T10:02:11Z"},
        "problems": {"revocations": "Tux couldn't reach the skills registry."},
        "categories": [
            {"id": "information", "name": "Information", "tags": ["weather", "prices"]},
            {"id": "home", "name": "Home & <b>notes</b>", "tags": ["notes", "lights"]},
            {"id": "travel", "name": "Travel", "tags": ["transport"]},
        ],
        "skills": [
            entry("io.github.ada.notes", "Notes", "Reads out your notes", ["notes"], "1.2.0",
                  "2026-11-02T08:00:00Z", [], installed=mine("io.github.ada.notes", "1.0.0", True),
                  update={"version": "1.2.0", "new": [], "problem": ""}),
            entry("io.github.ada.tides", "Tides", "Says when the next high tide is at a seaside "
                  "town", ["weather"], "1.1.0", "2026-11-10T08:41:00Z", ["tides.example.org"],
                  links=[{"title": "Source", "url": "https://example.org/ada/tides"}],
                  installed=mine("io.github.ada.tides", "1.0.0"),
                  update={"version": "1.1.0", "new": ["to reach tides.example.org"],
                          "problem": ""}),
            entry("io.github.bob.prices", "Prices", "Says what a coin is worth", ["prices"],
                  "1.1.0", "2026-11-12T09:00:00Z", ["prices.example.org"]),
            entry("io.github.eve.lunar", "Moon phase", "Says that the moon is up there",
                  ["weather"], "2.0.0", "2026-11-05T21:00:00Z", []),
            entry("io.github.mal.markup", MARKUP_NAME, MARKUP_SUMMARY, ["notes"], "1.0.0",
                  "2026-11-08T12:00:00Z", ["markup.example.org"],
                  links=[{"title": "<i>Source</i> & more", "url": "https://example.org/<b>x</b>"},
                         {"title": "Not a web page", "url": "file:///etc/passwd"}]),
            entry("io.github.mal.spy", "Spy", "Tells you the weather", ["notes"], None, None,
                  None, installed=mine("io.github.mal.spy", "1.0.0",
                                       off={"kind": "ban", "reason": SPY_REASON})),
        ],
    }


REGISTRY_VERSIONS = {
    "io.github.ada.notes": [("1.0.0", "2026-11-01T08:00:00Z", None),
                            ("1.2.0", "2026-11-02T08:00:00Z", None)],
    "io.github.ada.tides": [("1.0.0", "2026-11-01T08:41:00Z", None),
                            ("1.1.0", "2026-11-10T08:41:00Z", None)],
    "io.github.bob.prices": [("1.0.0", "2026-11-03T09:00:00Z",
                              {"kind": "suspend", "reason": PRICES_REASON}),
                             ("1.1.0", "2026-11-12T09:00:00Z", None)],
    "io.github.eve.lunar": [("2.0.0", "2026-11-05T21:00:00Z", None)],
    "io.github.mal.markup": [("1.0.0", "2026-11-08T12:00:00Z", None)],
    "io.github.mal.spy": [("1.0.0", "2026-11-04T10:00:00Z",
                           {"kind": "ban", "reason": SPY_REASON})],
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
        self.skills_dir = folder / "skills"
        self.registry = registry_listing(self.skills_dir)
        self.registry_host = offline(self.config, user=self.registry_skills(folder / "registry"))
        self.card = None            # the answer an approval card is waiting for
        self.install_problem = ""
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

    @staticmethod
    def registry_skills(folder: Path) -> Path:
        (folder / "skills").mkdir(parents=True, exist_ok=True)
        for skill_id, text in REGISTRY_RECIPES.items():
            (folder / "skills" / f"{skill_id}.yaml").write_text(text)
        return folder / "skills"

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

    def listed(self, skill_id: str) -> dict:
        skill = next((s for s in self.registry["skills"] if s["id"] == skill_id), None)
        if skill is None:
            raise DBusError(FAILED, "The skills registry doesn't list that skill.")
        return skill

    async def ask(self):
        """Like the shell's card: a click on Allow, on Deny, or no answer in time."""
        self.card = asyncio.get_running_loop().create_future()
        try:
            answer = await self.card
        finally:
            self.card = None
        if answer == "timeout":
            raise DBusError(FAILED, "The card wasn't answered in time, so nothing was installed.")
        if answer != "allow":
            raise DBusError(FAILED, "You didn't allow it, so nothing was installed.")

    def installed_reply(self, skill: dict) -> str:
        return json.dumps({"id": skill["id"], "version": skill["installed"]["version"],
                           "file": skill["installed"]["file"], "problem": self.install_problem})

    @dbus_method()
    async def BrowseSkills(self) -> "s":
        await self.enter("BrowseSkills")
        return json.dumps(self.registry)

    @dbus_method()
    async def BrowseSkill(self, skill_id: "s") -> "s":
        await self.enter("BrowseSkill", skill_id)
        versions = [{"version": version, "date": date, "revoked": revoked}
                    for version, date, revoked in REGISTRY_VERSIONS.get(skill_id, [])]
        return json.dumps({**self.listed(skill_id), "versions": versions})

    @dbus_method()
    async def FetchSkill(self, skill_id: "s") -> "s":
        await self.enter("FetchSkill", skill_id)
        skill = self.listed(skill_id)
        details = self.registry_host.current().details(skill_id)
        if not skill["version"] or details is None:
            raise DBusError(FAILED, "The skills registry doesn't list that skill.")
        return json.dumps({"id": skill_id, "version": skill["version"],
                           "text": REGISTRY_RECIPES[skill_id],
                           "actions": [{"name": action["name"], "tool": action["tool"]}
                                       for action in details["actions"]]})

    @dbus_method()
    async def InstallSkill(self, skill_id: "s") -> "s":
        await self.enter("InstallSkill", skill_id)
        skill = self.listed(skill_id)
        if skill["installed"]:
            raise DBusError(FAILED, "That skill is already installed. Update it instead.")
        await self.ask()
        skill["installed"] = {"version": skill["version"], "edited": False, "off": None,
                              "file": str(self.skills_dir / f"{skill_id}.yaml")}
        self.RegistryChanged()
        return self.installed_reply(skill)

    @dbus_method()
    async def UpdateSkill(self, skill_id: "s") -> "s":
        await self.enter("UpdateSkill", skill_id)
        skill = self.listed(skill_id)
        installed, update = skill["installed"], skill["update"]
        if not installed:
            raise DBusError(FAILED, "Tux didn't install that skill from the registry, so it "
                                    "left it alone.")
        if not update:
            raise DBusError(FAILED, "That skill is up to date.")
        if installed["edited"]:
            raise DBusError(FAILED, f"You've changed {installed['file']}, so Tux won't update it.")
        if installed["off"] or update["new"] is None or update["new"]:
            await self.ask()
        skill["installed"] = {**installed, "version": update["version"], "off": None}
        skill["update"] = None
        self.RegistryChanged()
        return self.installed_reply(skill)

    @dbus_method()
    async def RemoveSkill(self, skill_id: "s") -> "s":
        await self.enter("RemoveSkill", skill_id)
        skill = self.listed(skill_id)
        installed = skill["installed"]
        if not installed:
            raise DBusError(FAILED, "Tux didn't install that skill from the registry, so it "
                                    "left it alone.")
        if installed["edited"]:
            raise DBusError(FAILED, f"You've changed {installed['file']}, so Tux left it. Take "
                                    "it out of your skills folder yourself if you want it gone.")
        skill["installed"] = skill["update"] = None
        if not skill["version"]:    # Browse leaves out one with nothing to offer
            self.registry["skills"].remove(skill)
        self.RegistryChanged()
        return json.dumps({"id": skill_id, "file": installed["file"]})

    @dbus_method()
    async def CheckRegistry(self) -> "s":
        await self.enter("CheckRegistry")
        self.registry["problems"].pop("revocations", None)
        self.registry["index"]["accepted"] = "2026-11-12T11:00:00Z"
        self.RegistryChanged()
        return json.dumps(self.registry)

    @dbus_signal()
    def ConfigChanged(self, key) -> "s":
        return key

    @dbus_signal()
    def RegistryChanged(self):
        pass

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
    def SetRegistry(self, listing: "s"):
        """What the daemon holds now, as after a check. An empty string is the fixture."""
        daemon = self.daemon
        daemon.registry = json.loads(listing) if listing else registry_listing(daemon.skills_dir)
        daemon.RegistryChanged()

    @dbus_method()
    def CardUp(self) -> "b":
        return self.daemon.card is not None

    @dbus_method()
    def AnswerCard(self, answer: "s"):
        """allow, deny or timeout, as the shell's card would end."""
        self.daemon.card.set_result(answer)

    @dbus_method()
    def SetInstallProblem(self, problem: "s"):
        self.daemon.install_problem = problem

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
