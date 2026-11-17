"""Browse in Settings, the daemon's half: the skills registry's address, fetching from it, the
daily check, and installing, updating and removing through registry.Registry, with a card for
each install and for each update that asks for more. "Skills registry: Browse" in
docs/CONTRACT.md is the interface. registry.py does every check; this decides when, and writes
the card from what it checked.

Registry's calls block on the network and on ssh-keygen, so they run in a thread, one at a time,
under one lock, which isn't held while a card waits. What touches the skills folder and the
grants runs on the event loop under that lock, with no await between the steps, so no reload
sees a grant without its file or a file without its grant. Settings reads a copy made under the
lock after each change, so it never waits for a fetch.
"""
import asyncio
import ipaddress
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import apps, registry, webfetch
from .controller import ALLOWED, DENIED, LOCKED, UNANSWERED
from .gate import Ask, shown
from .registry import Refused
from .skills import Skills, risk_because, risk_of

log = logging.getLogger("tuxistant")

FIRST_CHECK_S = 300
CHECK_EVERY_S = 24 * 3600
RETRY_S = 3600          # after a check that couldn't reach the registry
WAKE_S = 600            # the wall clock is looked at this often, so a suspend delays little
FETCH_S = 30            # for the whole file
MAX_CHECKED = 32        # recipes kept checked for an install, at most 64 KiB each
MAX_ADDRESS_CHARS = 200
MAX_LINE_CHARS = 600
ADDRESS = re.compile(r"https://(?P<host>[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])"
                     r"(?::(?P<port>[0-9]{1,5}))?(?P<path>/(?:[A-Za-z0-9._~!$&'()*+,;=:@%-]+/)*)")
PATHS = re.compile(r"(?:index|revocations)\.json(?:\.sig)?|recipes/[0-9a-f]{64}\.yaml")
REASONS = {"network": "it reaches the network"}

NO_REGISTRY = "No skills registry is set, so Tux fetches nothing."
BAD_ADDRESS = ("The skills registry address in config.json isn't one Tux can use, so Tux fetches "
               "nothing.")
ALREADY = "That skill is already installed. Update it instead."
UP_TO_DATE = "That skill is up to date."
SAME_ID = "You already have a skill with the id {id}, so Tux left it alone."
SCREEN_LOCKED = "The screen is locked, so nothing was installed."
CARD_OPEN = "Another approval card is open. Answer it, then try again."
NO_CARD = ("Tux can't show an approval card on this desktop yet, so nothing was installed. Log "
           "out and back in once, then try again.")
NOT_ALLOWED = "You didn't allow it, so nothing was installed."
TOO_LATE = "The card wasn't answered in time, so nothing was installed."
NOT_WRITTEN = "Tux couldn't write the skill into your skills folder ({why}), so nothing changed."
TOO_BIG = "The skills registry sent a file too big to be what it says, so Tux didn't use it."
INSTALL_TITLE = "Install a skill from the skills registry?"
UPDATE_TITLE = "Update a skill from the skills registry?"
SWITCHES_ON = "It is switched off now, and this switches it back on."
LIFTED = "The skills registry lifted what switched it off."
ANSWERS = {DENIED: NOT_ALLOWED, UNANSWERED: TOO_LATE, LOCKED: SCREEN_LOCKED}


def usable(address) -> str:
    """The registry's address if Tux can use it: https, a host, an optional port and a path
    ending in /, with no user, password, query or fragment, at most 200 characters. Anything
    else is ""."""
    if not isinstance(address, str) or len(address) > MAX_ADDRESS_CHARS:
        return ""
    match = ADDRESS.fullmatch(address)
    if not match or not _host(match["host"]) or not 0 < int(match["port"] or 443) < 65536:
        return ""
    return address


def _host(host: str) -> bool:
    if host.startswith("["):
        try:
            return ipaddress.ip_address(host[1:-1]).version == 6
        except ValueError:
            return False
    return bool(webfetch.HOST.fullmatch(host.rstrip(".")))


class Fetcher:
    """Registry's fetch(path, limit): the address followed by one of the paths it takes, one
    HTTPS GET each with nothing in it about you. tls is for tests, with a key of their own."""

    def __init__(self, address: str, tls=None):
        match = ADDRESS.fullmatch(address)
        self.host = match["host"].strip("[]")
        self.port = int(match["port"] or 443)
        self.prefix = match["path"]
        self.tls = tls

    def __call__(self, path: str, limit: int) -> bytes:
        if not isinstance(path, str) or not PATHS.fullmatch(path):
            raise ValueError("not a path Tux fetches from the skills registry")
        data = webfetch.get_file(self.host, self.port, self.prefix + path, limit, FETCH_S,
                                 self.tls)
        if len(data) > limit:
            raise Refused(TOO_BIG)
        return data


async def _no_notice(_summary: str, _body: str) -> None:
    pass


class Browse:
    """What the daemon holds of the registry, and the D-Bus methods behind Browse. host is the
    daemon's SkillHost, and controller puts up the card."""

    def __init__(self, config, host, controller, emit=None, state: Path | None = None,
                 signers: Path | None = None, fetch=None, clock=time.time):
        self.wanted = config["registry_url"]
        self.address = usable(self.wanted)
        self.host, self.controller = host, controller
        self.emit = emit or (lambda *_: None)
        self.notify = _no_notice    # (summary, body), set once there is a bus
        self.clock = clock
        if self.address and fetch is None:
            fetch = Fetcher(self.address)
        self.registry = registry.Registry(
            state or registry.STATE_DIR, host.user, fetch=fetch if self.address else None,
            signers=signers or registry.SHIPPED_SIGNERS, clock=clock)
        self.lock = asyncio.Lock()
        self.checked: dict[tuple[str, str], registry.Checked] = {}
        self.updates: dict[str, dict] = {}      # by id: the update Browse offers, as checked
        self.started = clock()
        self.last: tuple[float, bool] | None = None    # the last check's end, and if it reached
        self.view, self.pages = self._view(self._problems(), None, {}), {}

    # What Settings reads

    def listing(self) -> dict:
        return self.view

    def page(self, skill_id: str) -> dict:
        page = self.pages.get(skill_id)
        if page is None:
            raise Refused(registry.NOT_LISTED if self.view["index"] else registry.NO_INDEX)
        return page

    async def start(self) -> None:
        async with self.lock:
            await self._new_view()

    async def _new_view(self) -> None:
        self.view, self.pages = await asyncio.to_thread(self._snapshot)

    def _snapshot(self) -> tuple[dict, dict]:
        problems = self._problems()
        try:
            index, effect = self.registry.index(), self.registry.in_effect()
        except Refused as e:
            problems["state"] = str(e)
            index, effect = None, []
        records = registry.installed(self.host.user)
        pages, shown_ids = {}, []
        for skill in index["skills"] if index else ():
            pages[skill["id"]], hidden = self._page(skill, effect, records)
            if not hidden:
                shown_ids.append(skill["id"])
        return self._view(problems, index, {key: pages[key] for key in shown_ids}), pages

    def _view(self, problems: dict, index: dict | None, pages: dict) -> dict:
        return {"address": self.address, "index": self._held(index), "problems": problems,
                "categories": index["categories"] if index else [],
                "skills": [{key: value for key, value in page.items() if key != "versions"}
                           for page in pages.values()]}

    def _problems(self) -> dict:
        address = {"address": BAD_ADDRESS} if self.wanted != "" and not self.address else {}
        return {**address, **self.registry.problems}

    def _held(self, index: dict | None) -> dict | None:
        if index is None:
            return None
        try:
            accepted = _stamp((self.registry.state / registry.HELD_INDEX).stat().st_mtime)
        except OSError:
            accepted = None
        return {"seq": index["seq"], "date": index["date"], "accepted": accepted}

    def _page(self, skill: dict, effect: list, records: dict) -> tuple[dict, bool]:
        """One skill's page, and whether the list leaves it out: every version revoked, and
        not installed."""
        versions = [{"version": entry["version"], "date": entry["date"],
                     "revoked": _revoked(effect, skill["id"], entry)}
                    for entry in skill["versions"]]
        offered = next((entry for entry, shown_as in zip(reversed(skill["versions"]),
                                                          reversed(versions))
                        if not shown_as["revoked"]), None)
        name, record = self._record(skill["id"], records)
        update = None
        if record and offered and _after(skill, offered["version"], record["version"]):
            known = self.updates.get(skill["id"])
            update = known if known and known["version"] == offered["version"] else {
                "version": offered["version"], "new": None, "problem": ""}
        permissions = offered["permissions"] if offered else None
        page = {key: skill[key] for key in ("id", "author", "name", "summary", "tags", "licence",
                                            "links")}
        page.update({
            "version": offered and offered["version"], "date": offered and offered["date"],
            "hosts": None if permissions is None else permissions.get("network", []),
            "risk": offered and offered["risk"],
            "risk_because": None if permissions is None else risk_because(permissions),
            "installed": record and self._installed(name, record), "update": update,
            "versions": versions})
        return page, offered is None and not record

    def _installed(self, name: str, record: dict) -> dict:
        off = record.get("off")
        return {"version": record["version"], "file": str(self.host.user / name),
                "edited": self.registry.edited(name, record),
                "off": off and {"kind": off["kind"], "reason": off["reason"]}}

    def _record(self, skill_id: str, records: dict | None = None) -> tuple[str, dict | None]:
        """The file name for an id, and what Tux recorded installing there while that file is
        still there."""
        name = f"{skill_id}.yaml"
        if records is None:
            records = registry.installed(self.host.user)
        record = records.get(name)
        if record and record["id"] == skill_id and os.path.lexists(self.host.user / name):
            return name, record
        return name, None

    # The daily check

    async def run(self) -> None:
        """The daily check, while the daemon runs. With no usable address there is none."""
        await self.start()
        if not self.address:
            return
        while True:
            wait = self._due() - self.clock()
            if wait > 0:
                await asyncio.sleep(min(wait, WAKE_S))
            else:
                await self.check()

    def _due(self) -> float:
        if self.last:
            when, reached = self.last
            return when + (CHECK_EVERY_S if reached else RETRY_S)
        soon = self.started + FIRST_CHECK_S
        try:
            accepted = (self.registry.state / registry.HELD_INDEX).stat().st_mtime
        except OSError:
            return soon
        return max(soon, accepted + CHECK_EVERY_S)

    async def check(self) -> dict:
        """The daily check now: the index and the revocations, what they switch off or back on,
        then the updates. Returns what BrowseSkills shows, and never fails."""
        if not self.address:
            async with self.lock:
                await self._new_view()
            return self.view
        changes = []
        try:
            async with self.lock:
                changes = await self._refresh()
                if changes:     # switched off at once, before anything is said
                    self.host.reload()
                await self._updates()
                await self._new_view()
                problems = self.registry.problems.values()
                self.last = (self.clock(), registry.NOT_REACHED not in problems)
        except Exception:
            log.exception("the check of the skills registry failed")
            self.last = (self.clock(), False)
        for change in changes:
            await self._notify(change)
        self.emit("RegistryChanged")
        return self.view

    async def _refresh(self) -> list[dict]:
        held = self._held_digest()
        changes = await asyncio.to_thread(self.registry.refresh)
        if self._held_digest() != held:     # what was checked against the old index goes
            self.checked.clear()
            self.updates.clear()
        return changes

    def _held_digest(self):
        try:
            return self.registry.kept().get("index")
        except Refused:
            return None

    async def _updates(self) -> None:
        """Each installed skill's update, fetched and checked. One that asks for nothing new is
        installed; one that asks for more, or is switched off, waits for Update and a card."""
        try:
            index = self.registry.index()
        except Refused:
            return
        listed = {skill["id"]: skill for skill in index["skills"]} if index else {}
        for name, record in registry.installed(self.host.user).items():
            skill = listed.get(record["id"])
            if skill and name == f"{record['id']}.yaml" and os.path.lexists(self.host.user / name):
                await self._update_one(skill, name, record)

    async def _update_one(self, skill: dict, name: str, record: dict) -> None:
        skill_id = record["id"]
        try:
            offered = self._offered(skill)
        except Refused:
            offered = None
        if not offered or not _after(skill, offered["version"], record["version"]):
            self.updates.pop(skill_id, None)
            return
        update = self.updates[skill_id] = {"version": offered["version"], "new": None,
                                           "problem": ""}
        if self.registry.edited(name, record):
            return
        try:
            checked = await self._check(skill_id, offered["version"])
            update["new"] = self._new(checked, name, record)
            if not update["new"] and not record.get("off"):
                self._put(checked, allow=False)
                self.updates.pop(skill_id, None)
        except Refused as e:
            update["problem"] = str(e)

    async def _notify(self, change: dict) -> None:
        if change["off"]:
            summary = f"Tuxistant switched off {change['id']}"
            body = registry.switched_off_because(change["kind"], change["reason"])
        else:
            summary, body = f"Tuxistant switched {change['id']} back on", LIFTED
        try:
            await self.notify(summary, body.replace("&", "&amp;"))
        except Exception as e:      # a desktop with nothing to show it is not an error
            log.info("could not show the notification about %s: %s", change["id"], e)

    # Fetching, installing, updating, removing

    async def fetch(self, skill_id: str) -> dict:
        """The version Browse offers, fetched and checked, for "Show the recipe"."""
        async with self.lock:
            skill = self._listed(skill_id)
            checked = await self._check(skill_id, self._offered(skill)["version"])
        skills = Skills(manifests=[checked.manifest], handlers={})
        return {"id": skill_id, "version": checked.version,
                "text": checked.data.decode("utf-8", "replace"),
                "actions": [{"name": name, "tool": skills.tool(name)}
                            for name in checked.manifest["actions"]]}

    async def install(self, skill_id: str) -> dict:
        async with self.lock:
            offered = self._may_install(skill_id)
            checked = await self._check(skill_id, offered["version"])
        await self._approve(card(checked))
        async with self.lock:
            self._may_install(skill_id)     # as it stands after the card
            return await self._written(checked, allow=True)

    async def update(self, skill_id: str) -> dict:
        async with self.lock:
            offered, name, record = self._may_update(skill_id)
            checked = await self._check(skill_id, offered["version"])
            new, off = self._new(checked, name, record), bool(record.get("off"))
            if not new and not off:
                return await self._written(checked, allow=False)
        await self._approve(card(checked, new, off))
        async with self.lock:
            self._may_update(skill_id, checked.version)
            return await self._written(checked, allow=True)

    async def remove(self, skill_id: str) -> dict:
        """No card: taking a skill away is the safe direction."""
        if not registry.is_skill_id(skill_id):
            raise Refused(registry.NOT_OURS)
        async with self.lock:
            file = self.registry.remove(skill_id)
            self.host.reload()
            self.updates.pop(skill_id, None)
            await self._new_view()
        self.emit("RegistryChanged")
        return {"id": skill_id, "file": str(file)}

    def _listed(self, skill_id: str) -> dict:
        if not self.address:
            raise Refused(NO_REGISTRY)
        index = self.registry.index()
        if index is None:
            raise Refused(registry.NO_INDEX)
        skill = next((s for s in index["skills"] if s["id"] == skill_id), None)
        if skill is None:
            raise Refused(registry.NOT_LISTED)
        return skill

    def _offered(self, skill: dict) -> dict:
        """The newest version no revocation in effect covers, or Refused saying what covers the
        newest one."""
        for entry in reversed(skill["versions"]):
            if not self.registry.covering(skill["id"], entry["version"], entry["key"]):
                return entry
        newest = skill["versions"][-1]
        hit = self.registry.covering(skill["id"], newest["version"], newest["key"])
        raise Refused(registry.switched_off_because(hit["kind"], hit["reason"]))

    def _may_install(self, skill_id: str) -> dict:
        skill = self._listed(skill_id)
        name, record = self._record(skill_id)
        if record:
            raise Refused(ALREADY)
        if os.path.lexists(self.host.user / name):
            raise Refused(registry.YOURS.format(name=name))
        self._not_yours(skill_id, name)
        return self._offered(skill)

    def _may_update(self, skill_id: str, version: str | None = None) -> tuple:
        """The version to update to, the file name and its record, or Refused saying why not.
        version: the one a card was allowed for, which has to be an update still."""
        if not self.address:
            raise Refused(NO_REGISTRY)
        name, record = self._record(skill_id) if registry.is_skill_id(skill_id) else ("", None)
        if not record:
            raise Refused(registry.NOT_OURS)
        if self.registry.edited(name, record):
            raise Refused(registry.CHANGED.format(name=name))
        skill = self._listed(skill_id)
        self._not_yours(skill_id, name)
        offered = self._offered(skill)
        if not _after(skill, version or offered["version"], record["version"]):
            raise Refused(UP_TO_DATE)
        return offered, name, record

    def _not_yours(self, skill_id: str, name: str) -> None:
        """Refused when a skill of yours in another file or folder has this id."""
        mine = str(self.host.user / name)
        seen = [key for key, (seen_id, _) in self.host.seen_ids.items() if seen_id == skill_id]
        seen += [key for key, found in self.host.last_offered.items()
                 if found.manifest["id"] == skill_id]
        if any(key != mine for key in seen):
            raise Refused(SAME_ID.format(id=skill_id))

    async def _check(self, skill_id: str, version: str) -> registry.Checked:
        """registry.check() in a thread, kept so an install can use what "Show the recipe"
        fetched. Install still refuses one a revocation covers by then."""
        key = (skill_id, version)
        if key not in self.checked:
            checked = await asyncio.to_thread(self.registry.check, skill_id, version)
            while len(self.checked) >= MAX_CHECKED:
                self.checked.pop(next(iter(self.checked)))
            self.checked[key] = checked
        return self.checked[key]

    def _new(self, checked: registry.Checked, name: str, record: dict) -> list[str]:
        """What a checked update asks for that its grant record or the installed version
        doesn't have, in words. A file that no longer loads counts as having none of it."""
        granted = self.host.granted(checked.skill_id)
        allowed = (granted or {}).get("permissions", {}).get("network") if (
            granted and not granted.get("removed")) else None
        manifest = checked.manifest
        new = [f"to reach {host}" for host in manifest["permissions"].get("network", [])
               if allowed is not True and host not in (allowed or ())]
        had = self.registry.load_installed(name, record) or {"actions": {}, "desktop": []}
        new += [f"the action {action}" for action in manifest["actions"]
                if action not in had["actions"]]
        new += [f"the desktop action {entry}" for entry in manifest["desktop"]
                if entry not in had["desktop"]]
        opened = _opens(had)
        return new + [f"to open {target}" for target in _opens(manifest) if target not in opened]

    async def _approve(self, ask: Ask) -> None:
        """The shell's card, with no turn behind it. Refused unless Allow was clicked."""
        ctl = self.controller
        if ctl.locked:
            raise Refused(SCREEN_LOCKED)
        if ctl.approval:
            raise Refused(CARD_OPEN)
        if not ctl.face:
            raise Refused(NO_CARD)
        answer = await ctl.card(ask, alone=True)
        if answer != ALLOWED:
            raise Refused(ANSWERS.get(answer, CARD_OPEN))

    async def _written(self, checked: registry.Checked, allow: bool) -> dict:
        problem = self._put(checked, allow)
        self.updates.pop(checked.skill_id, None)
        await self._new_view()
        self.emit("RegistryChanged")
        return {"id": checked.skill_id, "version": checked.version,
                "file": str(self.host.user / f"{checked.skill_id}.yaml"), "problem": problem}

    def _put(self, checked: registry.Checked, allow: bool) -> str:
        """allow(), install() and the reload, in that order, with no await between them, so no
        reload sees one without the other. A write that fails puts the grant back. Returns the
        reload's problem for the file, or ""."""
        file = self.host.user / f"{checked.skill_id}.yaml"
        before = self.host.granted(checked.skill_id)
        if allow:
            why = self.host.allow(checked.manifest, file)
            if why:
                raise Refused(why)
        try:
            self.registry.install(checked)
        except (Refused, OSError) as e:
            if allow and (why := self.host.put_back(checked.skill_id, before)):
                log.warning("could not put back what %s was allowed: %s", checked.skill_id, why)
            if isinstance(e, OSError):
                raise Refused(NOT_WRITTEN.format(why=e.strerror or e)) from None
            raise
        return self.host.reload().get(str(file), "")


def card(checked: registry.Checked, new: list | None = None, off: bool = False) -> Ask:
    """The card for installing a checked recipe, or with new, what an update asks for that its
    installed version doesn't, for updating to it. Written from what was checked and will be
    written: never the publisher's words, so no name, summary or description."""
    manifest = checked.manifest
    hosts = manifest["permissions"].get("network", [])
    lines = [f"{checked.skill_id}, version {checked.version}, from the GitHub account "
             f"{checked.skill_id.split('.')[2]}.",
             f"It reaches {_listed(hosts)}." if hosts else "It reaches nothing on the network.",
             _risk(manifest["permissions"]),
             f"Its actions: {', '.join(manifest['actions'])}."]
    if _opens(manifest):
        lines.append(f"It can open {_listed(_opens(manifest))}.")
    if manifest["desktop"]:
        lines.append(f"Its desktop actions: {', '.join(manifest['desktop'])}.")
    if new:
        lines.append(f"New in this version: {_listed(new)}.")
    if off:
        lines.append(SWITCHES_ON)
    return Ask(INSTALL_TITLE if new is None else UPDATE_TITLE,
               "\n".join(shown(line, MAX_LINE_CHARS) for line in lines), dict)


def _risk(permissions: dict) -> str:
    because = [REASONS.get(name, name) for name in risk_because(permissions)]
    risk = risk_of(permissions)
    return f"Risk: {risk}, because {_listed(because)}." if because else f"Risk: {risk}."


def _opens(manifest: dict) -> list[str]:
    """The apps and Settings pages a recipe's actions open, as the card names them."""
    plans = [action["plan"] for action in manifest["actions"].values() if "plan" in action]
    return sorted({plan.app or _page_title(plan.page) for plan in plans if plan.effect == "open"})


def _page_title(stem: str) -> str:
    title = next((panel.title for panel in apps.PANELS
                  if panel.desktop == f"gnome-{stem}-panel.desktop"), stem)
    return f"{title} in Settings"


def _listed(things) -> str:
    things = list(things)
    return things[0] if len(things) == 1 else ", ".join(things[:-1]) + " and " + things[-1]


def _after(skill: dict, version: str, installed: str) -> bool:
    """Whether version was merged after the installed one. An installed version the index
    doesn't list can't be placed, so nothing is offered over it."""
    order = [entry["version"] for entry in skill["versions"]]
    return installed in order and version in order and order.index(version) > order.index(
        installed)


def _revoked(effect: list, skill_id: str, entry: dict) -> dict | None:
    hits = [hit for hit in effect if registry.covers(hit, skill_id, entry["version"], entry["key"])]
    hit = min(hits, key=lambda hit: (hit["kind"] != "ban", hit["id"]), default=None)
    return hit and {"kind": hit["kind"], "reason": hit["reason"]}


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def doctor(config, state: Path, skills: Path) -> tuple[bool, str]:
    """The skills registry's line in tuxistant doctor, read from the files here: no fetch."""
    wanted = config["registry_url"]
    address = usable(wanted)
    if not address:
        return wanted == "", "none set, so Tux fetches nothing" if wanted == "" else BAD_ADDRESS
    if not registry.keygen():
        return False, registry.NO_KEYGEN
    try:
        index = registry.Registry(state, skills).index()
    except Refused as e:
        return False, str(e)
    held = f"list {index['seq']} of {index['date']}" if index else "no list taken from it yet"
    off = registry.switched_off(skills)
    return True, f"{address}, {held}" + (f", {len(off)} of its skills switched off" if off else "")
