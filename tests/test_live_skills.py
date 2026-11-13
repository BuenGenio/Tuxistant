"""Live skills end to end: the real folder watcher, the real sandbox and the real protocol.

Every folder is a temporary one, and no skill here has the network: the user skills ask for
none, and the bundled lights are started with it taken away and no account, so no light is ever
reached. Nothing waits for a reload by hand: each change is only seen through inotify.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import skillbox  # noqa: E402
from tuxistant.config import Config  # noqa: E402
from tuxistant.controller import Controller, Turn  # noqa: E402
from tuxistant.gate import Session  # noqa: E402
from tuxistant.history import History  # noqa: E402
from tuxistant.skillhost import BUNDLED, UPDATED, SkillHost  # noqa: E402
from tuxistant.skills import BUILTIN  # noqa: E402

WAIT_S = 10
MANIFEST = """id: {id}
name: Echo
description: Says back what it is given
version: 1.0.0
run: [python3, main.py]
permissions: {permissions}
actions:
  {action}:
    description: Say the text back
    params:
      text: {{ type: string, description: "What to say" }}
"""
MAIN = """from skillkit import Skill
import os

VERSION = {version}
skill = Skill("org.example.echo")


@skill.action("echo.say")
def say(args, config):
    return {{"text": args.get("text", ""), "version": VERSION, "cwd": os.getcwd(),
            "home": os.environ.get("HOME"), "sees_home": os.path.exists("/home"),
            "sees_bus": "DBUS_SESSION_BUS_ADDRESS" in os.environ}}


skill.run()
"""


def write_skill(root: Path, name: str, id="org.example.echo", action="echo.say",
                permissions="{}", version=1) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(MAIN.format(version=version))
    (folder / "skill.yaml").write_text(MANIFEST.format(id=id, action=action,
                                                        permissions=permissions))
    return folder


class FakeRealtime:
    def __init__(self, config, skills, on_event, hold=False):
        self.skills = skills
        self.session = Session(skills)


@unittest.skipUnless(skillbox.available(), "bubblewrap is not working here")
class Live(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        env = {f"XDG_{kind}_HOME": str(self.root / kind.lower())
               for kind in ("DATA", "CONFIG", "STATE", "CACHE")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.root / "data" / "tuxistant" / "skills"
        self.config = Config(self.root / "config.json")     # no TP-Link account
        self.history = History(self.root / "history.db")
        self.addCleanup(self.history.db.close)
        self.builtin = BUILTIN

    async def start(self, **folders):
        from tuxistant.service import Daemon
        self.host = SkillHost(self.config, **{"builtin": self.builtin, "user": self.user,
                                              **folders})
        self.daemon = Daemon(self.config, self.history, self.host)
        self.signals = mock.patch.object(self.daemon, "SkillsChanged").start()
        self.addCleanup(mock.patch.stopall)
        self.host.on_change = self.daemon.skills_changed
        await self.host.start()
        self.addAsyncCleanup(self.host.stop)
        self.assertIsNotNone(self.host.watcher, "the folders should be watched")
        self.assertFalse(self.host.watcher.polling, "inotify should be in use, not polling")

    async def until(self, check, what):
        for _ in range(int(WAIT_S / 0.05)):
            if check():
                return
            await asyncio.sleep(0.05)
        self.fail(f"waited {WAIT_S} s for {what}; problems: {self.host.problems}")

    def tools(self, skills=None):
        return {tool["name"] for tool in (skills or self.host.current()).tools()}

    async def act(self, tool, arguments="{}", skills=None):
        skills = skills or self.host.current()
        return (await skills.execute(tool, arguments, Session(skills)))[1]

    def process(self, folder):
        return self.host.running[str(folder)].process

    async def test_a_skill_comes_changes_and_goes_while_the_daemon_runs(self):
        await self.start()
        signals = self.signals.call_count
        folder = write_skill(self.user, "echo")
        await self.until(lambda: "echo_say" in self.tools(), "the new skill")
        self.assertGreater(self.signals.call_count, signals, "SkillsChanged should fire")
        listing = json.loads(json.dumps(self.daemon.controller.skills.listing()))
        self.assertIn(("org.example.echo", "yours"), [(s["id"], s["source"]) for s in listing])

        result = await self.act("echo_say", '{"text": "hi"}')
        self.assertEqual(result, {"text": "hi", "version": 1, "cwd": "/skill", "home": "/state",
                                  "sees_home": False, "sees_bus": False})
        first, old = self.host.current(), self.process(folder)
        self.assertTrue(old.running)
        turn = Turn(Controller(self.config, self.history, self.host,
                               realtime_factory=FakeRealtime), hold=False)
        self.assertIs(turn.skills, first)

        (folder / "main.py").write_text(MAIN.format(version=2))
        await self.until(lambda: self.host.current() is not first, "the edit to be seen")
        new = self.process(folder)
        self.assertIsNot(new, old)
        self.assertEqual((await self.act("echo_say", '{"text": "again"}'))["version"], 2)
        await self.until(lambda: not old.running, "the old process to stop")
        self.assertTrue(new.running)
        # The turn that began before the edit still offers what it offered, and says to ask again.
        self.assertIs(turn.skills, first)
        self.assertIs(turn.rt.session.skills, first)
        self.assertIn("echo_say", self.tools(turn.skills))
        self.assertEqual(await self.act("echo_say", "{}", skills=turn.skills), {"error": UPDATED})

        signals = self.signals.call_count
        shutil.rmtree(folder)
        await self.until(lambda: "echo_say" not in self.tools(), "the skill to go")
        self.assertGreater(self.signals.call_count, signals)
        self.assertNotIn(str(folder), self.host.running)
        await self.until(lambda: not new.running, "its process to stop")
        self.assertEqual(self.host.problems, {})
        self.assertEqual(json.loads(self.daemon.reload_skills()), {})

    async def test_skills_that_ask_too_much_or_stand_in_for_others_are_refused(self):
        await self.start()
        refused = {
            "shelly": write_skill(self.user, "shelly", id="org.example.shelly",
                                  action="shelly.run", permissions="{ shell: true }"),
            "reader": write_skill(self.user, "reader", id="org.example.reader",
                                  action="reader.read", permissions="{ files_read: true }"),
            "fake": write_skill(self.user, "fake", id="org.tuxistant.fake", action="fake.go"),
            "clock": write_skill(self.user, "clock", id="org.example.clock", action="time.now"),
            "lamp": write_skill(self.user, "lamp", id="org.example.lamp", action="lights.set"),
        }
        keys = {str(folder) for folder in refused.values()}
        await self.until(lambda: keys <= set(self.host.problems), "every problem")
        problems = self.host.problems
        self.assertIn("may only ask for the network", problems[str(refused["shelly"])])
        self.assertIn("files_read", problems[str(refused["reader"])])
        self.assertIn("org.tuxistant.", problems[str(refused["fake"])])
        self.assertIn("already has the action time.now", problems[str(refused["clock"])])
        self.assertIn("already has the action lights.set", problems[str(refused["lamp"])])
        for problem in problems.values():     # spoken aloud: plain sentences, no tracebacks
            self.assertTrue(problem.endswith("."), problem)
            self.assertNotIn("Traceback", problem)
            self.assertNotIn("\n", problem)
        self.assertFalse({"shelly_run", "reader_read", "fake_go"} & self.tools())
        self.assertFalse(keys & set(self.host.running))
        skills = self.host.current()
        self.assertEqual(skills.actions["time.now"]["source"], "built-in")
        self.assertEqual(skills.actions["lights.set"]["source"], "bundled")

    async def test_a_built_in_manifest_edit_reloads_live(self):
        self.builtin = self.root / "builtin"
        self.builtin.mkdir()
        shutil.copy(BUILTIN / "time.yaml", self.builtin / "time.yaml")
        await self.start(bundled=self.root / "no-bundled")
        manifest = yaml.safe_load((self.builtin / "time.yaml").read_text())
        manifest["actions"]["time.now"]["description"] = "Say what time it is, live"
        (self.builtin / "time.yaml").write_text(yaml.safe_dump(manifest))
        await self.until(lambda: self.host.current().actions["time.now"]["description"]
                         == "Say what time it is, live", "the edit to be seen")
        self.assertIn("time", await self.act("time_now"))

    async def test_the_bundled_lights_run_in_the_sandbox(self):
        """Started with the network taken away as well as with no account, so that even a
        mistake in it could not reach a light."""
        asked = []
        prepare = skillbox.prepare

        def offline(folder, run, permissions, state_dir):
            asked.append(permissions)
            return prepare(folder, run, {}, state_dir)
        with mock.patch.object(skillbox, "prepare", offline):
            await self.start()
            folder = (BUNDLED / "lights").resolve()
            declared = yaml.safe_load((folder / "skill.yaml").read_text())["actions"]
            skills = self.host.current()
            self.assertLessEqual({"lights.set", "lights.status"}, set(skills.actions))
            self.assertFalse({"lights_set", "lights_status"} & self.tools(),
                             "with no TP-Link account the model isn't told of them")
            self.assertEqual(skills.skills["org.tuxistant.lights"]["source"], "bundled")
            self.assertEqual(skills.skills["org.tuxistant.lights"]["risk"], "medium")
            for name, action in declared.items():
                self.assertEqual(skills.actions[name]["params"], action["params"])
            served = await self.process(BUNDLED / "lights").list_tools()
            self.assertEqual({tool["name"] for tool in served}, set(declared))
            result = await self.act("lights_status")
            self.assertIn("TP-Link email and password", result["error"])
            # Only what the model is told changes: the bulbs stay out of reach in this test.
            self.config.values.update(tapo_email="fake@example.test", tapo_password="made up")
            self.assertLessEqual({"lights_set", "lights_status"}, self.tools())
        self.assertEqual(asked, [{"network": True}])


if __name__ == "__main__":
    unittest.main()
