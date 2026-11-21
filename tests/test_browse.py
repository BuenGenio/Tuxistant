"""Browse in Settings, the daemon's half: the registry's address, the fetch, the daily check,
and installing, updating and removing with the shell's card. Offline: throwaway keys made by
test_registry's fixtures, a fake fetch or a loopback HTTPS server with a key made here, a fake
card answered in the test, and temporary folders. Skipped without ssh-keygen."""
import asyncio
import contextlib
import errno
import hashlib
import http.server
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_registry import (BLUETOOTH, EXAMPLE, KEYGEN, NOT_RECORDED, NOW,  # noqa: E402
                                 QUIET, RegistryCase, fingerprint, sign)
from tuxistant import browse, controller, registry, skillhost, webfetch  # noqa: E402
from tuxistant.browse import Browse, Fetcher, usable  # noqa: E402
from tuxistant.config import DEFAULTS  # noqa: E402
from tuxistant.controller import Controller  # noqa: E402
from tuxistant.registry import RECIPE_NS, Refused  # noqa: E402
from tuxistant.skillhost import SkillHost  # noqa: E402
from tuxistant.skills import BUILTIN  # noqa: E402

ADDRESS = "https://registry.example/skills/"
OPENSSL = shutil.which("openssl")
TIDES = """id: io.github.ada.tides
name: {name}
description: {description}
version: "{version}"
permissions:
  network: [{host}]
actions:
  tides.next:
    description: {action}
    params:
      town: {{ type: text, max: 40, description: "The town, like Brighton" }}
    required: [town]
    get: https://{host}/v1/next?town={{town}}
    pick:
      time: [high, 0, time]
    say: "The next high tide in {{town}} is at {{time}}."
"""
EVENING = """id: io.github.ada.evening
name: Evening
description: Gets the desk ready for the evening
version: "{version}"
permissions: {{}}
actions:
  evening.dark:
    description: Turn dark style on
    desktop: {{ action: dark_style, turn: on }}
{more}"""
EVENING_MORE = """  evening.night:
    description: Turn Night Light on
    desktop: { action: night_light, turn: on }
  evening.calendar:
    description: Open the calendar
    open: { app: org.gnome.Calendar }
"""
WIDE = """id: io.github.ada.wide
name: Wide
description: Asks four hosts
version: "{version}"
permissions:
  network: [{hosts}]
actions:
{actions}"""
WIDE_ACTION = """  wide.{name}:
    description: Ask one of the four hosts
    get: https://{host}/v1/now
    say: "Done."
"""
WORDS = {"name": "Tides", "description": "Says when the next high tide is",
         "action": "Say when the next high tide is at a seaside town"}


def tides(version: str, host: str = "api.tides.example", **words) -> bytes:
    return TIDES.format(version=version, host=host, **{**WORDS, **words}).encode()


def longest_host(letter: str) -> str:
    """253 characters, the most a recipe's host may have."""
    return ".".join([letter * 63] * 3 + [letter * 61])


def wide(version: str, hosts: list) -> bytes:
    actions = "".join(WIDE_ACTION.format(name=name, host=host)
                      for name, host in zip(("one", "two", "three", "four"), hosts))
    return WIDE.format(version=version, hosts=", ".join(hosts), actions=actions).encode()


class BrowseCase(RegistryCase, unittest.IsolatedAsyncioTestCase):
    """A registry served by a fake fetch, the daemon's SkillHost on temporary folders, and the
    real Controller, whose cards the test answers."""

    def setUp(self):
        super().setUp()
        self.builtin = self.root / "builtin"
        self.builtin.mkdir()
        shutil.copy(BUILTIN / "time.yaml", self.builtin / "time.yaml")
        self.host = SkillHost(DEFAULTS, self.builtin, self.root / "bundled", self.skills,
                              launcher=lambda *_: None)
        self.host.reload()
        self.signals, self.cards, self.notices = [], [], []
        self.config = {**DEFAULTS, "registry_url": ADDRESS}
        self.ctl = Controller(self.config, None, self.host, emit=self.emitted)
        self.ctl.face = True
        self.browse = self.new_browse()

    def new_browse(self, address: str = ADDRESS) -> Browse:
        made = Browse({**self.config, "registry_url": address}, self.host, self.ctl,
                      emit=self.emitted, state=self.state, signers=self.signers,
                      fetch=self.fetch, clock=lambda: NOW)

        async def notify(summary, body):
            self.notices.append((summary, body))
        made.notify = notify
        return made

    def restarted(self, address: str = ADDRESS) -> Browse:
        """The daemon started again: nothing it held in memory, and a new SkillHost and
        Controller reading the files."""
        registry._UNWRITTEN.pop(self.skills.parent / registry.INSTALLED, None)
        self.host = SkillHost(DEFAULTS, self.builtin, self.root / "bundled", self.skills,
                              launcher=lambda *_: None)
        self.host.reload()
        self.ctl = Controller(self.config, None, self.host, emit=self.emitted)
        self.ctl.face = True
        return self.new_browse(address)

    def emitted(self, name, *args):
        self.signals.append(name)
        if name == "Approval":
            self.cards.append(args)

    def release(self, data: bytes, version: str, permissions=None) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        self.files[f"recipes/{digest}.yaml"] = data
        permissions = {} if permissions is None else permissions
        return {"version": version, "date": "2026-11-14T10:00:00Z", "sha256": digest,
                "size": len(data), "key": fingerprint(self.ada[1]),
                "sig": sign(self.ada[0], RECIPE_NS, data).decode(),
                "permissions": permissions, "risk": "medium" if permissions else "low"}

    def tides_release(self, version: str, host: str = "api.tides.example", **words) -> dict:
        return self.release(tides(version, host, **words), version, {"network": [host]})

    def bluetooth(self, version: str) -> dict:
        return self.release(BLUETOOTH.format(version=version).encode(), version)

    async def answer(self, call, allow=True):
        """Runs call, answers the card it puts up, if it puts one up, and returns its result."""
        before = len(self.cards)
        task = asyncio.create_task(call)
        async with asyncio.timeout(20):
            while len(self.cards) == before and not task.done():
                await asyncio.sleep(0.005)
        if len(self.cards) > before:
            self.ctl.answer(self.cards[-1][0], allow)
        return await task

    async def card_for(self, call) -> asyncio.Task:
        """Starts call and waits for the card it puts up, which it leaves unanswered."""
        before = len(self.cards)
        task = asyncio.create_task(call)
        async with asyncio.timeout(20):
            while len(self.cards) == before and not task.done():
                await asyncio.sleep(0.005)
        self.assertFalse(task.done(), "no card came up")
        return task

    async def refuses(self, call, contains: str) -> str:
        with self.assertRaises(Refused) as caught:
            await call
        self.assertIn(contains, str(caught.exception))
        return str(caught.exception)

    def file(self, short: str) -> Path:
        return self.skills / f"io.github.ada.{short}.yaml"

    def grant(self, skill_id: str):
        return self.host.granted(skill_id)

    def loaded(self) -> set:
        return {skill["id"] for skill in self.host.current().listing()}

    def recipe_fetches(self) -> list:
        return [path for path, _ in self.fetched if path.startswith("recipes/")]


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class AddressTests(BrowseCase):
    def test_what_counts_as_an_address(self):
        for good in ("https://example.org/", "https://example.org/registry/",
                     "https://example.org:8443/a/b/", "https://127.0.0.1/r/",
                     "https://[2001:db8::1]:443/"):
            self.assertEqual(usable(good), good)
        for bad in ("", "http://example.org/", "https://example.org", "https://example.org/r",
                    "https://user@example.org/", "https://user:pw@example.org/",
                    "https://example.org/?q=1", "https://example.org/#top",
                    "https://example.org:0/", "https://example.org:99999/",
                    "https://exa mple.org/", "https://-x.org/", "https://example.org//",
                    "https://[nothing]/", None, 7, "https://example.org/" + "a/" * 100):
            self.assertEqual(usable(bad), "", bad)

    async def test_nothing_is_fetched_without_a_usable_address(self):
        self.publish(self.index())
        for address in ("", "http://registry.example/skills/", "https://registry.example"):
            made = self.new_browse(address)
            view = await made.check()
            self.assertEqual(view["address"], "")
            self.assertEqual(view["problems"], {} if address == "" else
                             {"address": browse.BAD_ADDRESS})
            await asyncio.wait_for(made.run(), 5)     # no daily check either
            for call in (made.fetch, made.install, made.update):
                await self.refuses(call("io.github.ada.bluetooth"), browse.NO_REGISTRY)
        self.assertEqual(self.fetched, [])

    async def test_config_has_it_empty_and_set_config_refuses_it(self):
        self.assertEqual(DEFAULTS["registry_url"], "")
        from tuxistant.config import EDITABLE, Config
        self.assertNotIn("registry_url", EDITABLE)
        with self.assertRaises(KeyError):
            Config(self.root / "config.json").set("registry_url", ADDRESS)


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class CheckTests(BrowseCase):
    def setUp(self):
        super().setUp()
        self.v1, self.v2 = self.bluetooth("1.0.0"), self.bluetooth("1.1.0")

    def bluetooth_index(self, *versions, seq=1, **changes) -> dict:
        return self.index(seq=seq, skills=[self.skill("bluetooth", list(versions))], **changes)

    async def installed_v1(self):
        self.publish(self.bluetooth_index(self.v1))
        await self.browse.check()
        return await self.answer(self.browse.install("io.github.ada.bluetooth"))

    async def test_the_check_fetches_the_four_files_and_shows_the_index(self):
        self.publish(self.bluetooth_index(self.v1))
        self.revoked(1, [])
        view = await self.browse.check()
        self.assertEqual([path for path, _ in self.fetched], [
            "index.json", "index.json.sig", "revocations.json", "revocations.json.sig"])
        self.assertEqual(view["address"], ADDRESS)
        self.assertEqual(view["problems"], {})
        self.assertEqual(view["index"]["seq"], 1)
        self.assertEqual(view["index"]["date"], "2026-11-20T09:00:00Z")
        self.assertRegex(view["index"]["accepted"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(view["categories"][0]["id"], "desktop")
        entry = view["skills"][0]
        self.assertEqual(set(entry), {"id", "author", "name", "summary", "tags", "licence",
                                      "links", "version", "date", "hosts", "risk",
                                      "risk_because", "installed", "update"})
        self.assertEqual((entry["version"], entry["hosts"], entry["risk"],
                          entry["risk_because"], entry["installed"], entry["update"]),
                         ("1.0.0", [], "low", [], None, None))
        self.assertIn("RegistryChanged", self.signals)
        self.assertEqual(json.loads(json.dumps(view)), view)

    async def test_the_daily_check_downloads_the_new_version_of_each_skill_installed(self):
        """So the host can tell which skills are installed at an address. CONTRACT.md and
        SECURITY.md say so; whether the check should hide it is the owner's to decide."""
        await self.installed_v1()
        self.revoked(1, [])
        self.publish(self.bluetooth_index(self.v1, self.v2, seq=2))
        four = ["index.json", "index.json.sig", "revocations.json", "revocations.json.sig"]
        self.fetched.clear()
        await self.browse.check()
        self.assertEqual([path for path, _ in self.fetched],
                         four + [f"recipes/{self.v2['sha256']}.yaml"])
        self.fetched.clear()
        await self.browse.check()
        self.assertEqual([path for path, _ in self.fetched], four, "it is installed now")

    async def test_a_check_that_cant_save_says_so_and_still_takes_the_revocations(self):
        """A review filled the disk: the check stopped before the revocations, and Settings
        showed nothing wrong."""
        self.publish(self.bluetooth_index(self.v1))
        await self.browse.check()
        self.publish(self.bluetooth_index(self.v1, seq=2))
        self.revoked(1, [])
        real = registry._write

        def full(file, data, mode=0o600):
            if file.parent == self.state:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real(file, data, mode)

        self.fetched.clear()
        with mock.patch.object(registry, "_write", full):
            view = await self.browse.check()
        self.assertEqual(view["problems"]["index"], registry.UNSAVED.format(
            folder=registry._shown(self.state), why=os.strerror(errno.ENOSPC)))
        self.assertIn("revocations.json", [path for path, _ in self.fetched])
        self.assertIn("revocations", view["problems"])
        self.assertEqual(view["index"]["seq"], 1)
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.RETRY_S,
                         "what it couldn't keep may revoke a skill, so it isn't left for a day")
        view = await self.browse.check()
        self.assertEqual((view["problems"], view["index"]["seq"]), ({}, 2))
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.CHECK_EVERY_S)

    async def test_a_switch_off_tux_cant_record_holds_and_is_tried_again_an_hour_later(self):
        """A review filled the disk as a revocation came: the check counted as done, so the
        recipe ran on, with nothing said, until the next check a day later."""
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        ledger = self.skills.parent / registry.INSTALLED
        with self.full(ledger):
            view = await self.browse.check()
            self.assertNotIn("io.github.ada.bluetooth", self.loaded())
            self.host.reload()
            self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "a reload keeps it off")
        self.assertEqual(view["problems"], {"state": registry.NOT_RECORDED.format(
            file=registry._shown(ledger), why=os.strerror(errno.ENOSPC))})
        self.assertEqual(self.notices, [("Tuxistant switched off io.github.ada.bluetooth",
                                         "The skills registry has switched it off: Gone bad.\n"
                                         + browse.NOT_RECORDED_YET)])
        self.assertEqual(view["skills"][0]["installed"]["off"],
                         {"kind": "suspend", "reason": "Gone bad."})
        name = self.file("bluetooth").name
        self.assertNotIn("off", json.loads(ledger.read_text())[name], "only in memory")
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.RETRY_S)
        view = await self.browse.check()
        self.assertEqual(view["problems"], {})
        self.assertIn("off", json.loads(ledger.read_text())[name])
        self.assertEqual(len(self.notices), 1, "a switch-off is told once")
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.CHECK_EVERY_S)

    async def test_a_switch_off_saved_but_never_recorded_is_made_again_at_a_restart(self):
        """A review filled the disk as a revocation came, then restarted the daemon: the record
        had no switch-off, so the recipe ran, shown as on, until the next check a day later."""
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        ledger, name = self.skills.parent / registry.INSTALLED, self.file("bluetooth").name
        with self.full(ledger):
            await self.browse.check()
        told = [("Tuxistant switched off io.github.ada.bluetooth",
                 "The skills registry has switched it off: Gone bad.\n" + browse.NOT_RECORDED_YET)]
        self.assertEqual(self.notices, told)
        self.assertNotIn("off", json.loads(ledger.read_text())[name])
        held = self.state / registry.HELD_INDEX
        with self.full(ledger):     # still full at the restart
            later = self.restarted()
            self.assertIn("io.github.ada.bluetooth", self.loaded(), "the record has no off")
            await later.start()
            self.assertNotIn("io.github.ada.bluetooth", self.loaded())
            self.host.reload()
            self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "a reload keeps it off")
        view = later.listing()
        self.assertEqual(view["skills"][0]["installed"]["off"],
                         {"kind": "suspend", "reason": "Gone bad."})
        self.assertEqual(view["problems"], {"state": registry.NOT_RECORDED.format(
            file=registry._shown(ledger), why=os.strerror(errno.ENOSPC))})
        self.assertEqual(self.notices, told * 2, "told again, as the record never had it")
        os.utime(held, (NOW - 3600,) * 2)      # a fresh index waits a day, but not now
        self.assertEqual(later._due(), NOW + browse.FIRST_CHECK_S)
        later = self.restarted()    # with room on the disk
        await later.start()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertIn("off", json.loads(ledger.read_text())[name])
        self.assertEqual(later.listing()["problems"], {})
        self.assertEqual(self.notices[2:], [(told[0][0], told[0][1].partition("\n")[0])],
                         "written this time, so with nothing more to say")
        self.assertEqual(later._due(), NOW - 3600 + browse.CHECK_EVERY_S)
        later = self.restarted()    # written now, so nothing more to do or say
        await later.start()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(len(self.notices), 3)

    async def test_a_lift_saved_but_never_recorded_is_made_at_a_restart(self):
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"})])
        await self.browse.check()
        self.publish(self.bluetooth_index(self.v1, seq=2, lifted=[1]))
        with self.full(self.skills.parent / registry.INSTALLED):
            await self.browse.check()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "a lift waits for the write")
        self.assertEqual(len(self.notices), 1)
        later = self.restarted()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        await later.start()
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.notices[1:], [("Tuxistant switched io.github.ada.bluetooth back on",
                                             browse.LIFTED)])
        self.assertIsNone(later.listing()["skills"][0]["installed"]["off"])

    async def test_revocations_tux_cant_save_switch_off_all_the_same(self):
        """With state.json full as a revocation came, the list was checked, then dropped
        unsaved, so the recipe ran on until a check could save it."""
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        state, ledger = self.state / registry.STATE, self.skills.parent / registry.INSTALLED
        with self.full(state):
            view = await self.browse.check()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.notices, [("Tuxistant switched off io.github.ada.bluetooth",
                                         "The skills registry has switched it off: Gone bad.")])
        unsaved = registry.UNSAVED.format(folder=registry._shown(self.state),
                                          why=os.strerror(errno.ENOSPC))
        self.assertEqual(view["problems"], {"index": unsaved, "revocations": unsaved})
        self.assertEqual(json.loads(state.read_text())["moderated"], {}, "only in memory")
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.RETRY_S)
        name = self.file("bluetooth").name
        self.assertIn("off", json.loads(ledger.read_text())[name], "recorded as any switch-off")
        later = self.restarted()
        await later.start()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "the record holds it off")
        view = await later.check()
        self.assertEqual(view["problems"], {})
        self.assertNotEqual(json.loads(state.read_text())["moderated"], {})
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(len(self.notices), 1, "a switch-off is told once")

    async def test_a_switch_off_neither_file_could_keep_is_said_a_restart_may_lose(self):
        """A review filled the disk under state.json and the record as a revocation came, then
        restarted the daemon: the recipe Tux had said it switched off ran again, shown as on,
        with nothing said, and the CONTRACT said it was still off after a restart. That is a
        known limit, so the words say a restart may lose it while it isn't written."""
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        state, ledger = self.state / registry.STATE, self.skills.parent / registry.INSTALLED
        with self.full(state), self.full(ledger):
            view = await self.browse.check()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(view["problems"]["state"], NOT_RECORDED.format(
            file=registry._shown(ledger), why=os.strerror(errno.ENOSPC)))
        self.assertEqual(self.notices, [(
            "Tuxistant switched off io.github.ada.bluetooth",
            "The skills registry has switched it off: Gone bad.\nTux couldn't record this yet, "
            "so if Tux restarts before it can, it may run again until a check gets the "
            "registry's list of revoked skills again.")])
        os.utime(self.state / registry.HELD_INDEX, (NOW - 3600,) * 2)    # last saved then
        later = self.restarted()
        await later.start()
        self.assertIn("io.github.ada.bluetooth", self.loaded(), "lost at the restart")
        view = later.listing()
        self.assertEqual((view["problems"], view["skills"][0]["installed"]["off"]), ({}, None))
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(later._due(), NOW - 3600 + browse.CHECK_EVERY_S,
                         "a day after the last check that saved the list")
        view = await later.check()      # that check, or Check now, takes it again
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(view["problems"], {})
        self.assertEqual(self.notices[1:], [("Tuxistant switched off io.github.ada.bluetooth",
                                             "The skills registry has switched it off: Gone bad.")])

    async def test_once_its_revocation_is_saved_a_switch_off_held_in_memory_outlasts_a_restart(
            self):
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        state, ledger = self.state / registry.STATE, self.skills.parent / registry.INSTALLED
        recorded = {"state": registry.NOT_RECORDED.format(file=registry._shown(ledger),
                                                          why=os.strerror(errno.ENOSPC))}
        with self.full(state), self.full(ledger):
            await self.browse.check()
        with self.full(ledger):
            view = await self.browse.check()
            self.assertEqual(view["problems"], recorded, "the same words, whatever was saved")
            later = self.restarted()
            await later.start()
            self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "made again at the start")
        self.assertEqual(later.listing()["problems"], recorded)
        self.assertEqual(self.notices[1:], [("Tuxistant switched off io.github.ada.bluetooth",
                                             "The skills registry has switched it off: Gone bad.\n"
                                             + browse.NOT_RECORDED_YET)])

    async def test_an_install_or_a_removal_that_writes_the_record_writes_what_was_held(self):
        """A review let the record be written again, then installed a skill: the held
        switch-off was written with it, but Settings still said the record couldn't be, and
        that a restart might lose it, until the next check an hour later."""
        await self.installed_v1()
        self.publish(self.index(seq=2, skills=[self.skill("bluetooth", [self.v1]),
                                               self.skill("tides", [self.tides_release("1.0.0")])]))
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        ledger, name = self.skills.parent / registry.INSTALLED, self.file("bluetooth").name
        with self.full(ledger):
            view = await self.browse.check()
        self.assertIn("state", view["problems"])
        await self.answer(self.browse.install("io.github.ada.tides"))
        view = self.browse.listing()
        self.assertEqual(view["problems"], {}, "written with the install")
        self.assertIn("off", json.loads(ledger.read_text())[name])
        self.assertNotIn(ledger, registry._UNWRITTEN)
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.publish(self.index(seq=3, skills=[self.skill("bluetooth", [self.v1]),
                                               self.skill("tides", [self.tides_release("1.0.0")])],
                                lifted=[1]))
        with self.full(ledger):
            view = await self.browse.check()
        self.assertIn("state", view["problems"])
        self.assertNotIn("io.github.ada.bluetooth", self.loaded(), "a lift waits for the write")
        await self.browse.remove("io.github.ada.tides")
        self.assertEqual(self.browse.listing()["problems"], {}, "written with the removal")
        self.assertNotIn("off", json.loads(ledger.read_text())[name])
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.notices[1:], [("Tuxistant switched io.github.ada.bluetooth back on",
                                             "The skills registry lifted what switched it off.")])

    async def test_with_no_usable_address_what_a_start_couldnt_record_is_tried_again(self):
        """A review cleared registry_url, then restarted the daemon with a switch-off saved in
        state.json but not in the record, which couldn't be written: NOT_RECORDED said Tux
        tries again within an hour, but with no address there was no check, so nothing did."""
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"}, reason="Gone bad.")])
        ledger, name = self.skills.parent / registry.INSTALLED, self.file("bluetooth").name
        with self.full(ledger):
            await self.browse.check()
        later, now = self.restarted(""), [NOW]
        later.clock = lambda: now[0]
        self.fetched.clear()
        with mock.patch.object(browse, "WAKE_S", 0.01):
            with self.full(ledger):
                running = asyncio.create_task(later.run())
                async with asyncio.timeout(10):
                    while "state" not in later.listing()["problems"]:
                        await asyncio.sleep(0.01)
                self.assertNotIn("io.github.ada.bluetooth", self.loaded())
                now[0] = NOW + browse.FIRST_CHECK_S
                async with asyncio.timeout(10):
                    while later.last is None and not running.done():
                        await asyncio.sleep(0.01)
                self.assertFalse(running.done(), "it carries on while the record isn't written")
                self.assertEqual(later._due(), now[0] + browse.RETRY_S, "then hourly")
            self.assertIn("state", later.listing()["problems"])
            self.assertNotIn("off", json.loads(ledger.read_text())[name])
            view = await later.check()      # Check now, with room on the disk
            self.assertEqual(view["problems"], {})
            self.assertIn("off", json.loads(ledger.read_text())[name])
            async with asyncio.timeout(10):
                await running       # nothing left to try
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.fetched, [])

    async def test_an_update_the_check_installs_writes_what_was_held(self):
        """The check's own update wrote the record, the held switch-off with it, but Settings
        still said it couldn't be written, and a lift waited, until the next check an hour
        later."""
        await self.installed_v1()
        tides = [self.tides_release("1.0.0")]
        self.publish(self.index(seq=2, skills=[self.skill("bluetooth", [self.v1]),
                                               self.skill("tides", tides)]))
        await self.browse.check()
        await self.answer(self.browse.install("io.github.ada.tides"))
        self.publish(self.index(seq=3, skills=[
            self.skill("bluetooth", [self.v1]),
            self.skill("tides", tides + [self.tides_release("1.1.0")])]))
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"})])
        ledger, real, failed = self.skills.parent / registry.INSTALLED, registry._write, []

        def once(file, data, mode=0o600):     # room on the disk again by the update
            if file == ledger and not failed:
                failed.append(file)
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real(file, data, mode)

        with mock.patch.object(registry, "_write", once):
            view = await self.browse.check()
        self.assertEqual(failed, [ledger])
        self.assertIn('"1.1.0"', self.file("tides").read_text(), "updated by the check")
        self.assertEqual(view["problems"], {}, "written with the update")
        self.assertNotIn(ledger, registry._UNWRITTEN)
        self.assertIn("off", json.loads(ledger.read_text())[self.file("bluetooth").name])
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.CHECK_EVERY_S)

    async def test_a_list_saved_without_its_state_isnt_used_after_a_restart(self):
        """A known limit: index.json is written before state.json, which names it, so a new
        list Tux saved when state.json couldn't be is no list at all after a restart, and the
        first check waits until that index.json is a day old."""
        await self.installed_v1()
        self.revoked(1, [])
        self.publish(self.bluetooth_index(self.v1, seq=2))
        with self.full(self.state / registry.STATE):
            view = await self.browse.check()
        self.assertEqual(view["index"]["seq"], 1, "while it runs, the list it saved before")
        os.utime(self.state / registry.HELD_INDEX, (NOW - 3600,) * 2)
        later = self.restarted()
        await later.start()
        view = later.listing()
        self.assertEqual((view["index"], view["skills"], view["problems"]), (None, [], {}))
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(later._due(), NOW - 3600 + browse.CHECK_EVERY_S)
        view = await later.check()      # Check now
        self.assertEqual((view["index"]["seq"], view["problems"]), (2, {}))

    async def test_a_damaged_state_at_a_start_leaves_the_daily_check_running(self):
        """start() runs apply(), which refuses a damaged state.json. Were that to end run(), no
        check would run all session, and Settings would never be told why."""
        await self.installed_v1()
        # A day old by the test's clock, so the check is due at once. Left at the file's real
        # time, the check never came once real time passed NOW minus a day, as the test's
        # clock stands still, and the test timed out.
        os.utime(self.state / registry.HELD_INDEX, (NOW - browse.CHECK_EVERY_S,) * 2)
        (self.state / registry.STATE).write_text("damaged")
        later = self.restarted()
        with mock.patch.object(browse, "FIRST_CHECK_S", 0), self.assertLogs(
                "tuxistant", "WARNING") as logged:
            running = asyncio.create_task(later.run())
            async with asyncio.timeout(10):
                while later.last is None and not running.done():
                    await asyncio.sleep(0.01)
            self.assertFalse(running.done(), "the daily check carries on")
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
        self.assertEqual(later.listing()["problems"]["state"], registry.STATE_DAMAGED.format(
            folder=registry._shown(self.state)))
        self.assertEqual([r.getMessage() for r in logged.records if r.levelno >= logging.ERROR],
                         [], "a damaged state is Settings' to say, not a failure")

    @unittest.skipIf(os.geteuid() == 0, "root can write a folder that is 0500")
    async def test_a_state_folder_that_cant_be_made_is_a_problem(self):
        self.publish(self.bluetooth_index(self.v1))
        os.chmod(self.root, 0o500)
        self.addCleanup(os.chmod, self.root, 0o755)
        view = await self.browse.check()
        self.assertIn("Tux couldn't save what it took from the skills registry",
                      view["problems"]["index"])
        self.assertIsNone(view["index"])

    async def test_a_check_that_fails_tries_again_an_hour_later_not_at_once(self):
        tries = []

        def fails():
            tries.append(1)
            raise RuntimeError("something unforeseen")

        self.browse.registry.refresh = fails
        with mock.patch.object(browse, "FIRST_CHECK_S", 0), self.assertLogs("tuxistant", "ERROR"):
            running = asyncio.create_task(self.browse.run())
            async with asyncio.timeout(10):
                while self.browse.last is None:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
        self.assertEqual(len(tries), 1, "not again and again while it fails")
        self.assertEqual(self.browse._due(), NOW + browse.RETRY_S)

    async def test_after_a_restart_settings_is_told_once_the_held_list_is_read(self):
        await self.installed_v1()
        later = self.new_browse()
        self.signals.clear()
        self.assertIsNone(later.listing()["index"], "nothing is read before start")
        await later.start()
        self.assertEqual(self.signals, ["RegistryChanged"])
        self.assertEqual(later.listing()["skills"][0]["installed"]["version"], "1.0.0")

    async def test_a_skill_taken_out_while_switched_off_installs_again_only_after_a_lift(self):
        """A review took out a skill a retired moderation key had switched off and put it back
        with one Allow."""
        await self.installed_v1()
        self.revoked(1, [self.entry(7, {"author": "ada"}, reason="Junk.")])
        await self.browse.check()
        self.publish(self.bluetooth_index(self.v1, seq=2, moderation_keys=[self.other[1]]))
        await self.browse.check()
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        await self.browse.remove("io.github.ada.bluetooth")
        cards, notices = len(self.cards), len(self.notices)
        await self.refuses(self.browse.install("io.github.ada.bluetooth"), "switched it off: Junk.")
        self.assertEqual(len(self.cards), cards, "refused before any card")
        self.assertFalse(self.file("bluetooth").exists())
        self.publish(self.bluetooth_index(self.v1, seq=3, moderation_keys=[self.other[1]],
                                          lifted=[7]))
        await self.browse.check()
        self.assertEqual(len(self.notices), notices, "nothing was there to switch back on")
        result = await self.answer(self.browse.install("io.github.ada.bluetooth"))
        self.assertEqual(result["problem"], "")
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    async def test_a_registry_that_cant_be_reached_is_a_problem_not_a_failure(self):
        view = await self.browse.check()
        self.assertEqual(view["problems"]["index"], registry.NOT_REACHED)
        self.assertIsNone(view["index"])
        self.assertEqual(self.browse.last[1], False)
        self.assertEqual(self.browse._due(), self.browse.last[0] + browse.RETRY_S)

    async def test_a_settings_page_is_named_from_tuxs_own_table(self):
        await self.installed_v1()
        self.assertEqual(self.cards[-1][2].splitlines()[1:], [
            "It reaches nothing on the network.", "Risk: low.", "Its actions: bluetooth.open.",
            "It can open Bluetooth in Settings."])

    def test_an_installed_version_the_index_doesnt_list_has_no_update(self):
        skill = self.skill("bluetooth", [self.v1, self.v2])
        self.assertTrue(browse._after(skill, "1.1.0", "1.0.0"))
        self.assertFalse(browse._after(skill, "1.0.0", "1.1.0"))
        self.assertFalse(browse._after(skill, "1.1.0", "0.9.0"))

    async def test_a_revocation_at_the_check_switches_off_with_one_notification(self):
        result = await self.installed_v1()
        self.assertEqual(result["problem"], "")
        self.assertIn("io.github.ada.bluetooth", self.loaded())
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth"},
                                    reason="Its host & owner changed.")])
        view = await self.browse.check()
        self.assertEqual(self.notices, [(
            "Tuxistant switched off io.github.ada.bluetooth",
            "The skills registry has switched it off: Its host &amp; owner changed.")])
        self.assertNotIn("io.github.ada.bluetooth", self.loaded())
        self.assertEqual(self.host.problems[str(self.file("bluetooth"))],
                         "The skills registry has switched it off: Its host & owner changed.")
        entry = view["skills"][0]
        self.assertEqual(entry["installed"]["off"],
                         {"kind": "suspend", "reason": "Its host & owner changed."})
        await self.browse.check()
        self.assertEqual(len(self.notices), 1, "a switch-off is told once")
        self.publish(self.bluetooth_index(self.v1, seq=2, lifted=[1]))
        await self.browse.check()
        self.assertEqual(self.notices[-1], ("Tuxistant switched io.github.ada.bluetooth back on",
                                            browse.LIFTED))
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    async def test_an_update_that_asks_for_nothing_new_installs_at_the_check_with_no_card(self):
        await self.installed_v1()
        cards = len(self.cards)
        self.publish(self.bluetooth_index(self.v1, self.v2, seq=2))
        view = await self.browse.check()
        self.assertEqual(len(self.cards), cards)
        self.assertIn('"1.1.0"', self.file("bluetooth").read_text())
        self.assertEqual(view["skills"][0]["installed"]["version"], "1.1.0")
        self.assertIsNone(view["skills"][0]["update"])
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    async def test_a_switched_off_skill_is_never_updated_without_a_card(self):
        await self.installed_v1()
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.bluetooth",
                                        "versions": ["1.0.0"]})])
        await self.browse.check()
        self.publish(self.bluetooth_index(self.v1, self.v2, seq=2))
        cards = len(self.cards)
        view = await self.browse.check()
        self.assertEqual(len(self.cards), cards)
        self.assertIn('"1.0.0"', self.file("bluetooth").read_text())
        self.assertEqual(view["skills"][0]["update"],
                         {"version": "1.1.0", "new": [], "problem": ""})
        result = await self.answer(self.browse.update("io.github.ada.bluetooth"))
        self.assertIn(browse.SWITCHES_ON, self.cards[-1][2])
        self.assertEqual(self.cards[-1][1], browse.UPDATE_TITLE)
        self.assertEqual((result["version"], result["problem"]), ("1.1.0", ""))
        self.assertIn("io.github.ada.bluetooth", self.loaded())

    async def test_an_edited_file_is_never_updated_or_removed(self):
        await self.installed_v1()
        file = self.file("bluetooth")
        file.write_text(file.read_text().replace("Bluetooth page", "My Bluetooth"))
        self.publish(self.bluetooth_index(self.v1, self.v2, seq=2))
        view = await self.browse.check()
        self.assertIn("My Bluetooth", file.read_text())
        self.assertTrue(view["skills"][0]["installed"]["edited"])
        await self.refuses(self.browse.update("io.github.ada.bluetooth"), "You've changed")
        await self.refuses(self.browse.remove("io.github.ada.bluetooth"),
                           "Take it out of your skills folder yourself")
        self.assertIn("My Bluetooth", file.read_text())
        self.assertIn(file.name, registry.installed(self.skills))

    async def test_the_schedule(self):
        self.assertEqual(self.browse._due(), NOW + browse.FIRST_CHECK_S)
        self.publish(self.bluetooth_index(self.v1))
        self.revoked(1, [])
        await self.browse.check()
        self.assertEqual(self.browse._due(), NOW + browse.CHECK_EVERY_S)
        later = self.new_browse()      # a restart soon after: the held index is fresh
        held = self.state / registry.HELD_INDEX
        os.utime(held, (NOW - 3600, NOW - 3600))
        self.assertEqual(later._due(), NOW - 3600 + browse.CHECK_EVERY_S)
        os.utime(held, (NOW - 2 * browse.CHECK_EVERY_S,) * 2)
        self.assertEqual(later._due(), NOW + browse.FIRST_CHECK_S)

    async def test_the_daily_check_runs_on_its_own(self):
        self.publish(self.bluetooth_index(self.v1))
        with mock.patch.object(browse, "FIRST_CHECK_S", 0):
            running = asyncio.create_task(self.browse.run())
            async with asyncio.timeout(10):
                while self.browse.last is None:     # the whole check, threads and all
                    await asyncio.sleep(0.01)
            running.cancel()
        self.assertEqual(self.fetched[0][0], "index.json")
        self.assertEqual(self.browse.listing()["index"]["seq"], 1)


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class InstallTests(BrowseCase):
    def setUp(self):
        super().setUp()
        self.t1 = self.tides_release("1.0.0")
        self.t2 = self.tides_release("1.1.0", "tides.example.org")
        self.publish(self.index(skills=[self.skill("tides", [self.t1, self.t2])]))

    async def asyncSetUp(self):
        await self.browse.check()

    async def test_install_puts_up_a_card_and_allow_writes_the_checked_bytes(self):
        result = await self.answer(self.browse.install("io.github.ada.tides"))
        self.assertEqual(result, {"id": "io.github.ada.tides", "version": "1.1.0",
                                  "file": str(self.file("tides")), "problem": ""})
        self.assertEqual(self.file("tides").read_bytes(), tides("1.1.0", "tides.example.org"))
        self.assertEqual(self.grant("io.github.ada.tides"),
                         {"permissions": {"network": ["tides.example.org"]}, "config": [],
                          "folder": "io.github.ada.tides.yaml"})
        self.assertIn("io.github.ada.tides", self.loaded())
        self.assertEqual(self.signals.count("RegistryChanged"), 2)
        await self.refuses(self.browse.install("io.github.ada.tides"), browse.ALREADY)
        await self.refuses(self.browse.update("io.github.ada.tides"), browse.UP_TO_DATE)

    async def test_the_cards_words_come_from_the_checked_recipe(self):
        self.t2 = self.tides_release("1.1.0", "tides.example.org", name="Quokka Tides",
                                     description="Wombat says when", action="Platypus asks")
        self.publish(self.index(seq=2, skills=[self.skill(
            "tides", [self.t1, self.t2], summary="Echidna summary", name="Quokka Tides",
            links=[{"title": "Numbat", "url": "https://example.org/numbat"}])]))
        await self.browse.check()
        await self.refuses(self.answer(self.browse.install("io.github.ada.tides"), allow=False),
                           browse.NOT_ALLOWED)
        _id, title, detail, level, _seconds = self.cards[-1]
        self.assertEqual(title, "Install a skill from the skills registry?")
        self.assertEqual(level, "click")
        self.assertEqual(detail.splitlines(), [
            "io.github.ada.tides, version 1.1.0, from the GitHub account ada.",
            "It reaches tides.example.org.",
            "Risk: medium, because it reaches the network.",
            "Its actions: tides.next."])
        for words in ("Quokka", "Wombat", "Platypus", "Echidna", "Numbat"):
            self.assertNotIn(words, title + detail)

    async def test_four_hosts_at_their_longest_are_on_the_card_whole(self):
        """A review cut the line at 600 characters, so the fourth host was granted on Allow
        without being on the card."""
        hosts = [longest_host(letter) for letter in "abcd"]
        first = self.release(wide("1.0.0", hosts), "1.0.0", {"network": hosts})
        self.publish(self.index(seq=2, skills=[self.skill("wide", [first])]))
        await self.browse.check()
        await self.answer(self.browse.install("io.github.ada.wide"))
        detail = self.cards[-1][2]
        self.assertEqual(detail.splitlines()[1],
                         f"It reaches {hosts[0]}, {hosts[1]}, {hosts[2]} and {hosts[3]}.")
        self.assertNotIn("more characters", detail)
        self.assertEqual(self.grant("io.github.ada.wide")["permissions"], {"network": hosts})
        others = [longest_host(letter) for letter in "efgh"]
        second = self.release(wide("1.1.0", others), "1.1.0", {"network": others})
        self.publish(self.index(seq=3, skills=[self.skill("wide", [first, second])]))
        await self.browse.check()
        await self.answer(self.browse.update("io.github.ada.wide"))
        new = ", ".join(f"to reach {host}" for host in others[:3]) + f" and to reach {others[3]}"
        self.assertIn(f"New in this version: {new}.", self.cards[-1][2].splitlines())

    async def test_a_card_that_cant_be_shown_whole_is_refused(self):
        """No recipe the format allows comes near the 4000 characters a card shows, so the
        limit is lowered here to see the refusal."""
        with mock.patch.object(browse, "MAX_DETAIL_CHARS", 100):
            await self.refuses(self.browse.install("io.github.ada.tides"), browse.TOO_LONG)
        self.assertEqual(self.cards, [])
        self.assertFalse(self.file("tides").exists())

    async def test_a_version_the_registry_drops_while_the_card_is_up_isnt_installed(self):
        task = await self.card_for(self.browse.install("io.github.ada.tides"))
        self.publish(self.index(seq=2, skills=[self.skill("tides", [self.t1])]))
        await self.browse.check()       # the lock isn't held while the card waits
        self.ctl.answer(self.cards[-1][0], True)
        await self.refuses(task, registry.DROPPED)
        self.assertFalse(self.file("tides").exists())
        self.assertIsNone(self.grant("io.github.ada.tides"), "the grant was put back")
        self.assertEqual(self.browse.listing()["skills"][0]["installed"], None)

    async def test_install_looks_again_after_the_card(self):
        task = await self.card_for(self.browse.install("io.github.ada.tides"))
        self.publish(self.index(seq=2, skills=[self.skill("bluetooth", [self.bluetooth("1.0.0")])]))
        await self.browse.check()
        self.ctl.answer(self.cards[-1][0], True)
        await self.refuses(task, registry.NOT_LISTED)
        self.publish(self.index(seq=3, skills=[self.skill("tides", [self.t1, self.t2])]))
        await self.browse.check()
        task = await self.card_for(self.browse.install("io.github.ada.tides"))
        (self.skills / "mine.yaml").write_bytes(tides("1.0.0"))
        self.host.reload()
        self.ctl.answer(self.cards[-1][0], True)
        await self.refuses(task, browse.SAME_ID.format(id="io.github.ada.tides"))
        self.assertFalse(self.file("tides").exists())
        self.assertEqual(registry.installed(self.skills), {})

    async def test_an_id_a_file_of_yours_has_is_yours_while_it_doesnt_load(self):
        self.host.grants.file.write_text(json.dumps({"io.github.ada.tides": {
            "permissions": {}, "config": [], "folder": "mine.yaml"}}))
        (self.skills / "mine.yaml").write_bytes(tides("1.0.0"))
        problems = self.host.reload()
        self.assertIn("It now asks to reach api.tides.example",
                      problems[str(self.skills / "mine.yaml")])
        self.assertNotIn("io.github.ada.tides", self.loaded())
        await self.refuses(self.browse.install("io.github.ada.tides"),
                           browse.SAME_ID.format(id="io.github.ada.tides"))
        self.assertEqual(self.cards, [])

    async def test_a_new_index_drops_what_was_checked_against_the_old_one(self):
        await self.browse.fetch("io.github.ada.tides")
        republished = self.tides_release("1.1.0", "tides.example.org", action="Say the high tide")
        self.publish(self.index(seq=2, skills=[self.skill("tides", [self.t1, republished])]))
        await self.browse.check()
        result = await self.answer(self.browse.install("io.github.ada.tides"))
        self.assertEqual(result["problem"], "")
        self.assertIn(b"Say the high tide", self.file("tides").read_bytes())
        self.assertEqual(len(self.recipe_fetches()), 2)

    async def test_fetch_skill_shows_the_recipe_and_install_uses_what_it_checked(self):
        fetched = await self.browse.fetch("io.github.ada.tides")
        self.assertEqual(fetched["text"], tides("1.1.0", "tides.example.org").decode())
        self.assertEqual(fetched["actions"][0]["name"], "tides.next")
        self.assertEqual(fetched["actions"][0]["tool"]["name"], "tides_next")
        self.assertEqual(len(self.recipe_fetches()), 1)
        await self.answer(self.browse.install("io.github.ada.tides"))
        self.assertEqual(len(self.recipe_fetches()), 1)

    async def test_before_the_card(self):
        for state, sentence in (("locked", browse.SCREEN_LOCKED), ("face", browse.NO_CARD),
                                ("approval", browse.CARD_OPEN)):
            with mock.patch.object(self.ctl, state, state != "face" and ("x", None)):
                await self.refuses(self.browse.install("io.github.ada.tides"), sentence)
        self.assertEqual(self.cards, [])
        self.assertFalse(self.file("tides").exists())

    async def test_a_deny_ends_no_conversation_and_leaves_everything(self):
        turn = mock.Mock(ends_talk=False)
        self.ctl.turn = turn
        await self.refuses(self.answer(self.browse.install("io.github.ada.tides"), allow=False),
                           browse.NOT_ALLOWED)
        self.assertFalse(turn.ends_talk, "the card belongs to no conversation")
        self.assertFalse(self.file("tides").exists())
        self.assertIsNone(self.grant("io.github.ada.tides"))
        self.assertEqual(registry.installed(self.skills), {})

    async def test_a_lock_while_the_card_is_up_answers_it(self):
        task = asyncio.create_task(self.browse.install("io.github.ada.tides"))
        async with asyncio.timeout(10):
            while not self.cards:
                await asyncio.sleep(0.005)
        await self.ctl.set_locked(True)
        await self.refuses(task, browse.SCREEN_LOCKED)
        self.assertFalse(self.file("tides").exists())

    async def test_a_revocation_while_the_card_is_up_stops_the_install(self):
        task = asyncio.create_task(self.browse.install("io.github.ada.tides"))
        async with asyncio.timeout(10):
            while not self.cards:
                await asyncio.sleep(0.005)
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.tides", "versions": ["1.1.0"]},
                                    reason="Bad.")])
        await self.browse.check()       # the lock isn't held while the card waits
        self.ctl.answer(self.cards[-1][0], True)
        await self.refuses(task, "switched it off: Bad.")
        self.assertFalse(self.file("tides").exists())
        self.assertIsNone(self.grant("io.github.ada.tides"), "the grant was put back")

    async def test_the_errors_before_anything_is_fetched(self):
        await self.refuses(self.browse.install("io.github.ada.nothing"), registry.NOT_LISTED)
        await self.refuses(self.browse.install("../../etc/passwd"), registry.NOT_LISTED)
        await self.refuses(self.browse.update("io.github.ada.tides"), registry.NOT_OURS)
        await self.refuses(self.browse.remove("io.github.ada.tides"), registry.NOT_OURS)
        self.file("tides").write_bytes(tides("1.0.0"))      # one of yours, by hand
        await self.refuses(self.browse.install("io.github.ada.tides"),
                           "You already have a skill file called io.github.ada.tides.yaml")
        self.file("tides").rename(self.skills / "mine.yaml")
        self.host.reload()
        await self.refuses(self.browse.install("io.github.ada.tides"),
                           "You already have a skill with the id io.github.ada.tides")
        self.assertEqual(self.recipe_fetches(), [])

    async def test_every_version_revoked_says_why_and_leaves_the_list(self):
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.tides",
                                        "versions": ["1.1.0"]}, reason="Older."),
                         self.entry(2, {"author": "ada"}, reason="Newest.")])
        view = await self.browse.check()
        self.assertEqual(view["skills"], [])
        page = self.browse.page("io.github.ada.tides")
        self.assertEqual([version["revoked"] for version in page["versions"]], [
            {"kind": "suspend", "reason": "Newest."}, {"kind": "suspend", "reason": "Older."}])
        self.assertIsNone(page["version"])
        await self.refuses(self.browse.install("io.github.ada.tides"),
                           "switched it off: Older.")

    async def test_the_newest_version_not_revoked_is_offered(self):
        self.revoked(1, [self.entry(1, {"skill": "io.github.ada.tides",
                                        "versions": ["1.1.0"]})])
        view = await self.browse.check()
        self.assertEqual((view["skills"][0]["version"], view["skills"][0]["hosts"]),
                         ("1.0.0", ["api.tides.example"]))
        result = await self.answer(self.browse.install("io.github.ada.tides"))
        self.assertEqual(result["version"], "1.0.0")

    async def test_browse_skill_has_the_versions_oldest_first(self):
        page = self.browse.page("io.github.ada.tides")
        self.assertEqual([(v["version"], v["revoked"]) for v in page["versions"]],
                         [("1.0.0", None), ("1.1.0", None)])
        with self.assertRaises(Refused):
            self.browse.page("io.github.ada.other")

    async def test_remove_takes_out_the_file_then_its_record(self):
        await self.answer(self.browse.install("io.github.ada.tides"))
        signals = self.signals.count("RegistryChanged")
        self.assertEqual(await self.browse.remove("io.github.ada.tides"),
                         {"id": "io.github.ada.tides", "file": str(self.file("tides"))})
        self.assertFalse(self.file("tides").exists())
        self.assertEqual(registry.installed(self.skills), {})
        self.assertNotIn("io.github.ada.tides", self.loaded())
        self.assertEqual(self.signals.count("RegistryChanged"), signals + 1)
        self.assertIsNone(self.browse.listing()["skills"][0]["installed"])


    async def test_a_damaged_state_doesnt_fail_a_removal(self):
        """Browse runs apply() after a removal only while the record couldn't be written, as
        apply() refuses a damaged state.json, and would fail a removal already done."""
        await self.answer(self.browse.install("io.github.ada.tides"))
        (self.state / registry.STATE).write_text("damaged")
        later = self.restarted()
        await later.start()
        self.assertEqual(await later.remove("io.github.ada.tides"),
                         {"id": "io.github.ada.tides", "file": str(self.file("tides"))})
        self.assertFalse(self.file("tides").exists())
        self.assertEqual(registry.installed(self.skills), {})
        self.assertEqual(later.listing()["problems"]["state"], registry.STATE_DAMAGED.format(
            folder=registry._shown(self.state)))

@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class UpdateTests(BrowseCase):
    """Tides 1.0.0 is installed, reaching api.tides.example; 1.1.0 reaches tides.example.org."""

    def setUp(self):
        super().setUp()
        self.t1 = self.tides_release("1.0.0")
        self.t2 = self.tides_release("1.1.0", "tides.example.org")
        self.publish(self.index(skills=[self.skill("tides", [self.t1])]))

    async def asyncSetUp(self):
        await self.browse.check()
        await self.answer(self.browse.install("io.github.ada.tides"))
        self.publish(self.index(seq=2, skills=[self.skill("tides", [self.t1, self.t2])]))
        self.view = await self.browse.check()
        self.before = (self.file("tides").read_bytes(), self.grant("io.github.ada.tides"),
                       registry.installed(self.skills))

    def unchanged(self):
        self.assertEqual((self.file("tides").read_bytes(), self.grant("io.github.ada.tides"),
                          registry.installed(self.skills)), self.before)

    async def test_one_that_asks_for_more_waits_for_update_and_its_card(self):
        self.assertEqual(self.file("tides").read_bytes(), tides("1.0.0"))
        self.assertEqual(self.view["skills"][0]["update"], {
            "version": "1.1.0", "new": ["to reach tides.example.org"], "problem": ""})
        cards = len(self.cards)
        result = await self.answer(self.browse.update("io.github.ada.tides"))
        self.assertEqual(len(self.cards), cards + 1)
        title, detail = self.cards[-1][1:3]
        self.assertEqual(title, "Update a skill from the skills registry?")
        self.assertIn("New in this version: to reach tides.example.org.", detail.splitlines())
        self.assertEqual(result["problem"], "")
        self.assertEqual(self.file("tides").read_bytes(), tides("1.1.0", "tides.example.org"))
        self.assertEqual(self.grant("io.github.ada.tides")["permissions"],
                         {"network": ["tides.example.org"]})
        self.assertIn("io.github.ada.tides", self.loaded())
        self.assertNotIn(str(self.file("tides")), self.host.problems)

    async def test_the_same_file_put_there_by_hand_is_refused(self):
        self.file("tides").write_bytes(tides("1.1.0", "tides.example.org"))
        problems = self.host.reload()
        self.assertIn("It now asks to reach tides.example.org", problems[str(self.file("tides"))])
        await self.refuses(self.browse.update("io.github.ada.tides"), "You've changed")

    async def test_a_hand_edit_after_an_allowed_update_is_still_refused(self):
        await self.answer(self.browse.update("io.github.ada.tides"))
        self.file("tides").write_bytes(tides("1.1.0", "evil.example"))
        problems = self.host.reload()
        self.assertIn("It now asks to reach evil.example", problems[str(self.file("tides"))])
        self.assertEqual(self.grant("io.github.ada.tides")["permissions"],
                         {"network": ["tides.example.org"]})

    async def test_a_deny_leaves_the_file_and_the_grant(self):
        await self.refuses(self.answer(self.browse.update("io.github.ada.tides"), allow=False),
                           browse.NOT_ALLOWED)
        self.unchanged()

    async def test_a_card_that_times_out_leaves_the_file_and_the_grant(self):
        with mock.patch.object(controller, "APPROVAL_WAIT_S", 0.05):
            await self.refuses(self.browse.update("io.github.ada.tides"), browse.TOO_LATE)
        self.unchanged()

    async def test_a_failed_write_leaves_the_file_and_the_grant(self):
        os.chmod(self.skills, 0o555)
        self.addCleanup(os.chmod, self.skills, 0o755)
        await self.refuses(self.answer(self.browse.update("io.github.ada.tides")),
                           "Tux couldn't write the skill into your skills folder")
        os.chmod(self.skills, 0o755)
        self.unchanged()

    async def test_a_grant_that_cant_be_saved_writes_nothing(self):
        self.host.grants.file.write_text("[broken")
        await self.refuses(self.answer(self.browse.update("io.github.ada.tides")),
                           "Tux couldn't read what your skills were allowed")
        self.assertEqual(self.file("tides").read_bytes(), tides("1.0.0"))
        self.assertEqual(self.host.grants.file.read_text(), "[broken")

    async def test_a_removal_while_the_card_is_up_stops_the_update(self):
        task = await self.card_for(self.browse.update("io.github.ada.tides"))
        await self.browse.remove("io.github.ada.tides")     # needs no card, so it needn't wait
        self.ctl.answer(self.cards[-1][0], True)
        await self.refuses(task, registry.NOT_OURS)
        self.assertFalse(self.file("tides").exists())
        self.assertEqual(registry.installed(self.skills), {})

    async def test_what_it_is_allowed_that_cant_be_saved_installs_nothing(self):
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(skillhost, "_write_json", side_effect=full):
            why = await self.refuses(self.answer(self.browse.update("io.github.ada.tides")),
                                     "so nothing was installed")
        self.assertEqual(why, skillhost.NOT_ALLOWED.format(file=self.host.grants.file,
                                                           why=os.strerror(errno.ENOSPC)))
        self.unchanged()
        self.host.grants.writable = False
        await self.refuses(self.answer(self.browse.update("io.github.ada.tides")),
                           "(nothing is saved here), so nothing was installed")
        self.unchanged()

    async def test_what_counts_as_new(self):
        v1 = self.release(EVENING.format(version="1.0.0", more="").encode(), "1.0.0")
        v2 = self.release(EVENING.format(version="2.0.0", more=EVENING_MORE).encode(), "2.0.0")
        self.publish(self.index(seq=3, skills=[self.skill("evening", [v1]),
                                               self.skill("tides", [self.t1, self.t2])]))
        await self.browse.check()
        await self.answer(self.browse.install("io.github.ada.evening"))
        self.publish(self.index(seq=4, skills=[self.skill("evening", [v1, v2]),
                                               self.skill("tides", [self.t1, self.t2])]))
        view = await self.browse.check()
        self.assertEqual(view["skills"][0]["update"]["new"], [
            "the action evening.night", "the action evening.calendar",
            "the desktop action night_light", "to open org.gnome.Calendar"])
        self.assertIn('"1.0.0"', self.file("evening").read_text(), "it waits for Update")
        await self.answer(self.browse.update("io.github.ada.evening"))
        detail = self.cards[-1][2].splitlines()
        self.assertIn("It can open org.gnome.Calendar.", detail)
        self.assertIn("Its desktop actions: dark_style, night_light.", detail)
        self.assertEqual(detail[2], "Risk: low.")


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class ExampleTests(BrowseCase):
    """The signed example in docs/examples/registry, as Browse shows and installs it."""

    async def test_the_signed_example(self):
        self.files = {path.relative_to(EXAMPLE).as_posix(): path.read_bytes()
                      for path in EXAMPLE.rglob("*") if path.is_file()}
        self.signers = EXAMPLE / "registry_allowed_signers"
        self.browse = self.new_browse()
        view = await self.browse.check()
        self.assertEqual((view["problems"], view["index"]["seq"]), ({}, 4))
        self.assertEqual([(s["id"], s["version"], s["hosts"]) for s in view["skills"]], [
            ("io.github.example-ada.tides", "1.1.0", ["tides.example.org"]),
            ("io.github.example-bo.bluetooth", "1.0.0", [])])
        page = self.browse.page("io.github.example-ada.tides")
        self.assertEqual(page["versions"][0]["revoked"], {
            "kind": "suspend",
            "reason": "Its host, api.tides.example, has been taken over by someone else."})
        result = await self.answer(self.browse.install("io.github.example-ada.tides"))
        self.assertEqual((result["version"], result["problem"]), ("1.1.0", ""))
        self.assertEqual(self.cards[-1][2].splitlines()[:3], [
            "io.github.example-ada.tides, version 1.1.0, from the GitHub account example-ada.",
            "It reaches tides.example.org.", "Risk: medium, because it reaches the network."])
        self.assertIn("io.github.example-ada.tides", self.loaded())


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class GrantTests(BrowseCase):
    def test_allow_sets_exactly_what_the_manifest_asks_and_put_back_undoes_it(self):
        file = self.skills / "io.github.ada.tides.yaml"
        manifest = {"id": "io.github.ada.tides", "permissions": {"network": ["a.example"]}}
        self.assertEqual(self.host.allow(manifest, file), "")
        saved = json.loads(self.host.grants.file.read_text())
        self.assertEqual(saved["io.github.ada.tides"], {
            "permissions": {"network": ["a.example"]}, "config": [], "folder": file.name})
        self.assertEqual(self.host.put_back("io.github.ada.tides", None), "")
        self.assertNotIn("io.github.ada.tides", json.loads(self.host.grants.file.read_text()))

    def test_a_broken_grants_file_is_never_written_over(self):
        self.host.grants.file.write_text("{not json")
        why = self.host.allow({"id": "io.github.ada.x", "permissions": {}},
                              self.skills / "io.github.ada.x.yaml")
        self.assertIn("couldn't read what your skills were allowed", why)
        self.assertEqual(self.host.grants.file.read_text(), "{not json")


@unittest.skipUnless(KEYGEN, "ssh-keygen isn't installed")
class DoctorTests(BrowseCase):
    def test_the_doctor_line(self):
        def line(address):
            return browse.doctor({"registry_url": address}, self.state, self.skills)
        self.assertEqual(line(""), (True, "none set, so Tux fetches nothing"))
        self.assertEqual(line("http://x/"), (False, browse.BAD_ADDRESS))
        self.assertEqual(line(ADDRESS), (True, f"{ADDRESS}, no list taken from it yet"))
        self.accept(self.index())
        self.assertEqual(line(ADDRESS), (True, f"{ADDRESS}, list 1 of 2026-11-20T09:00:00Z"))
        (self.state / registry.STATE).write_text("damaged")
        passed, detail = line(ADDRESS)
        self.assertFalse(passed)
        self.assertIn("damaged", detail)

    def test_install_and_update_are_not_tools(self):
        from tuxistant import skills
        for name in skills.HANDLERS:
            self.assertFalse(name.startswith(("registry", "browse")), name)
        for module in ("skills.py", "planner.py", "recipes.py", "skillhost.py", "gate.py",
                       "desktop.py", "mcp.py"):
            text = (Path(skills.__file__).parent / module).read_text()
            for words in ("from .browse", "from . import browse", "browse.Browse"):
                self.assertNotIn(words, text, module)


def make_certificate(folder: Path) -> tuple[Path, Path]:
    """A key and certificate for 127.0.0.1, made for this test run only."""
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "2", "-subj", "/CN=127.0.0.1", "-addext",
                    "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True,
                   env={**QUIET, "HOME": str(folder)})
    return key, cert


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, *_args):
        pass    # the client hung up, as the tests make it


class Served(http.server.BaseHTTPRequestHandler):
    """What the loopback registry answers, from the server's routes, noting every request."""

    def do_GET(self):
        self.server.seen.append((self.path, list(self.headers.items())))
        status, headers, body, drip = self.server.routes.get(self.path, (404, {}, b"", 0))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for byte in (body if drip else [body]):
            self.wfile.write(bytes([byte]) if drip else byte)
            self.wfile.flush()
            if drip:
                time.sleep(drip)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(OPENSSL, "openssl isn't installed")
class FetchTests(unittest.TestCase):
    """The real fetch, against an HTTPS server on 127.0.0.1 with a certificate made here."""

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="tux-browse-tls-"))
        cls.addClassCleanup(shutil.rmtree, cls.folder, True)
        key, cert = make_certificate(cls.folder)
        cls.server = Server(("127.0.0.1", 0), Served)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.server.seen, cls.server.routes = [], {}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.trust = ssl.create_default_context(cafile=str(cert))
        cls.address = f"https://127.0.0.1:{cls.server.server_address[1]}/registry/"

    def setUp(self):
        self.server.seen.clear()
        self.server.routes.clear()
        self.fetch = Fetcher(self.address, tls=self.trust)

    def route(self, path, status=200, body=b"", headers=None, drip=0):
        self.server.routes[f"/registry/{path}"] = (status, headers or {}, body, drip)

    def test_one_get_with_only_the_three_headers(self):
        self.route("index.json", body=b"{}\n")
        self.assertEqual(self.fetch("index.json", 100), b"{}\n")
        (path, headers), = self.server.seen
        self.assertEqual(path, "/registry/index.json")
        self.assertEqual(sorted(headers), sorted([
            ("Host", f"127.0.0.1:{self.server.server_address[1]}"),
            ("Accept-Encoding", "identity"), ("User-Agent", "Tuxistant")]))

    def test_a_redirect_another_status_or_compression_is_a_failure(self):
        self.route("index.json", 302, headers={"Location": "https://example.org/index.json"})
        self.route("index.json.sig", 404)
        self.route("revocations.json", body=b"x", headers={"Content-Encoding": "gzip"})
        for path in ("index.json", "index.json.sig", "revocations.json"):
            with self.assertRaises(webfetch.PageError):
                self.fetch(path, 100)
        self.assertEqual(len(self.server.seen), 3, "nothing followed the redirect")

    def test_more_than_the_limit_is_refused(self):
        self.route("index.json", body=b"x" * 101)
        with self.assertRaises(Refused):
            self.fetch("index.json", 100)
        self.assertEqual(self.fetch("index.json", 101), b"x" * 101)

    def test_no_other_path_is_fetched(self):
        for path in ("tags.yaml", "../index.json", "recipes/abc.yaml", "index.json?x=1",
                     f"recipes/{'A' * 64}.yaml", "logo.png"):
            with self.assertRaises(ValueError):
                self.fetch(path, 100)
        self.assertEqual(self.server.seen, [])

    def test_the_whole_file_has_its_time(self):
        self.route("index.json", body=b"x" * 20, drip=0.1)
        with mock.patch.object(browse, "FETCH_S", 0.5):
            started = time.monotonic()
            with self.assertRaises(webfetch.PageError):
                self.fetch("index.json", 100)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_certificate_the_system_doesnt_trust_is_refused(self):
        self.route("index.json", body=b"{}\n")
        with self.assertRaises(webfetch.PageError):
            Fetcher(self.address)("index.json", 100)


if __name__ == "__main__":
    unittest.main()
