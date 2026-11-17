"""Skills that load, change and go away while the daemon runs.

SkillHost reads the manifests, checks each one on its own, and builds a new Skills from what
passed. A turn takes current() once, so a reload never changes the tools a turn offered halfway
through it. Code skills run as their own processes in a sandbox, behind async handlers that go
through Skills.execute like any other action.

Order is trust: built-in manifests first, then the code skills that ship with Tuxistant, then the
user's. The first to claim an id, an action or a tool name keeps it, so nothing added later can
stand in for a built-in action.

What a skill you added was allowed is kept by its id. A new version that asks for more is not
used until you take the skill out and put it back, and then the data it kept is cleared, so
what an older version stored can't leave through a permission it never had.

A YAML file directly in your skills folder is a recipe: no code, only the requests, windows and
sentences tuxistant/recipes.py knows how to make. Recipes and code skills you added share one
budget of actions, as every tool the small model is offered costs it some of its other answers.
"""
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time
from collections import ChainMap, Counter
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from . import registry
from .config import DATA_DIR
from .gate import printable, shown
from .inotify import ignored
from .mcp import START_TIMEOUT_S
from .skills import BUILTIN, HANDLERS, Skills, risk_of
from .webfetch import HOST

log = logging.getLogger("tuxistant")

BUNDLED = Path(__file__).resolve().parent / "skills_code"
USER_DIR = DATA_DIR / "skills"
MANIFEST = "skill.yaml"
SKILL_CONFIG_KEYS = frozenset({"tapo_email", "tapo_password"})
RESERVED = "org.tuxistant."
DEFAULT_TIMEOUT_S = 15
MAX_TIMEOUT_S = 60
BUSY_MARGIN_S = 5
MAX_MANIFEST_BYTES = 64 * 1024
MAX_YAML_DEPTH = 16             # the deepest manifest that ships is 7
MAX_FOLDER_BYTES = 50 * 1024 * 1024
MAX_FOLDER_FILES = 2000
MAX_ACTIONS = 32
MAX_TEXT = {"name": 100, "description": 1000, "version": 40}
MAX_ACTION_TEXT = 200           # the model reads every action's description on every turn
MAX_PARAMS_CHARS = 2000
MAX_PARAMS_DEPTH = 6
MAX_RESULT_CHARS = 3900         # as the planner's, so a result fits an approval card whole
MAX_RESULT_DEPTH = 8
MAX_ERROR_CHARS = 300
BUNDLED_IDLE_S = 900            # Lights keeps the bulbs it found for 10 minutes
USER_IDLE_S = 300
IGNORED_PARTS = frozenset({"__pycache__"})
GRANTS = "skill-grants.json"    # next to the skills folder, so tests that move it move this too
CODE_DIR = "skill-code"         # the checked copies that skills really run from
LOADED = "skills-loaded.json"   # yours that keep their place, by name, with their counts
MAX_STATE_BYTES = 1 << 20       # the most either of those two files is read up to
CHANGED = "it changed as Tux saved it"
MAX_PASSES = 16                 # how often a reload tries again with names an edit gave up
MAX_REMOVED = 1000              # records kept of skills taken out, the newest, ~150 bytes each
BROKEN_GRANTS = ("Tux couldn't read what your skills were allowed, in {file}, so until that "
                 "file is fixed or taken out it allows no skill you added the network or a "
                 "setting, even one it allowed before. Taking the file out allows every skill of "
                 "yours what it asks for now, edits that were refused included.")
UNSAVED_GRANTS = ("Tux couldn't save what your skills were allowed to {file} ({why}), so a "
                  "skill of yours it hasn't allowed this before can't have it until it can. "
                  "Skills it allowed before carry on, and it tries again at the next reload.")
NOT_ALLOWED = ("Tux couldn't save what that skill is allowed to {file} ({why}), so nothing was "
               "installed.")
NOT_A_SKILL = ("It is no longer a recipe or a skill folder, so nothing of it is in use, but it "
               "keeps its place and its names until you take it out.")
SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+")
ACTION = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
PARAM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
FIELDS = {"id", "name", "description", "version", "run", "permissions", "config", "timeout_s",
          "actions"}
ACTION_FIELDS = {"description", "params", "required"}
REFUSED_PERMISSIONS = ("shell", "files_read", "files_write", "clipboard")
UPDATED = "That skill was just updated. Please ask again."
USER_TOOLS = 6      # actions from skills you added, code and recipes together
GREW = ("This skill now has more actions than when it loaded, and with them Tux would have more "
        "than six from skills you added. Take an action out of it, or take another skill out, "
        "to use the new ones.")
KEPT = "The last version that worked is still in use."
KEPT_PLACE, GROWN, NEW = range(3)  # the order yours are counted in, for the budget
NOT_YAML = "A recipe's file name has to end in .yaml."
TOO_DEEP = "It is nested more than 16 levels deep, which no skill needs."


class Bounded(yaml.SafeLoader):
    """safe_load that stops at the first node nested too deep. PyYAML takes about a millisecond
    for each level of [ or { it holds open, so 64 KB of them held the event loop for a minute,
    and composing is lazy, so stopping here stops the scan too."""
    depth = 0

    def compose_node(self, parent, index):
        self.depth += 1
        try:
            if self.depth > MAX_YAML_DEPTH:
                raise ValueError(TOO_DEEP)
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


class Unaliased(Bounded):
    """A few hundred bytes of aliases grow to gigabytes once anything walks what they built, and
    a cap on depth doesn't see them, as an alias isn't composed again. Refused as each node is
    reached, so nothing reads the file twice. Recipes and the code skills you add use this."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise ValueError("It uses a YAML anchor or alias, & or *, which a skill you add "
                             "can't.")
        return super().compose_node(parent, index)


def load_yaml(text: str, loader=Bounded):
    reader = loader(text)
    try:
        return reader.get_single_data()
    finally:
        reader.dispose()


def _read_yaml(file: Path, loader=Bounded) -> dict:
    if file.is_symlink():
        raise ValueError(f"Its {file.name} is a link. It has to be a real file.")
    try:
        if file.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"Its {file.name} is too big.")
        data = load_yaml(file.read_text(), loader)
    except FileNotFoundError:
        raise ValueError(f"It has no {file.name}.") from None
    except UnicodeDecodeError:
        raise ValueError(f"Its {file.name} is not text.") from None
    except yaml.YAMLError as e:     # its own text could quote the file, so only the line goes out
        mark = getattr(e, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark else ""
        raise ValueError(f"Its {file.name} is not valid YAML{where}.") from None
    except OSError as e:
        raise ValueError(f"Its {file.name} could not be read: {e.strerror or e}.") from None
    if not isinstance(data, dict):
        raise ValueError(f"Its {file.name} has to be a mapping of fields.")
    return data


def _text(manifest: dict, field: str, required: bool = True) -> str:
    value = manifest.get(field)
    if value is None and not required:
        return ""
    text = isinstance(value, (str, int, float)) and not isinstance(value, bool)
    if not text or not str(value).strip():
        raise ValueError(f"Its {field} is missing or is not text.")
    value = " ".join(str(value).split())
    if len(value) > MAX_TEXT[field] or not value.isprintable():
        raise ValueError(f"Its {field} is too long or holds characters that can't be shown.")
    if _bracketed(value):
        raise ValueError(f"Its {field} holds < or >, which a skill can't use.")
    return value


def _bracketed(text: str) -> bool:
    """What a manifest says goes into the tool list on every turn. <|im_start|> would be a real
    control token to the chat template there, and <tool_call> a call to the server's parser, so
    the author is told rather than the text quietly changed."""
    return "<" in text or ">" in text


def _unbracketed(text: str) -> str:
    """A skill's answer loses its angle brackets, for the same reason webfetch.clean removes them."""
    return text.replace("<", " ").replace(">", " ")


def _actions(manifest: dict, bundled: bool) -> dict:
    actions = manifest.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise ValueError("It declares no actions.")
    if len(actions) > MAX_ACTIONS:
        # one of yours is told the limit it really has to meet, not one that would refuse it again
        raise ValueError(f"It declares more than {MAX_ACTIONS} actions." if bundled
                         else over_budget(0, len(actions)))
    checked = {}
    for name, action in actions.items():
        action_name(name)
    distinct_tools(actions)
    for name, action in actions.items():
        if not isinstance(action, dict) or set(action) - ACTION_FIELDS:
            raise ValueError(f"The action {name} has fields it shouldn't have.")
        description = action_description(name, action)
        params = action.get("params") or {}
        required = action.get("required") or []
        if not isinstance(params, dict) or not all(
                isinstance(p, str) and PARAM.fullmatch(p) for p in params):
            raise ValueError(f"The params of {name} have to be a mapping by name.")
        if not _schema_ok(params):
            raise ValueError(f"The params of {name} are too big or hold characters that can't "
                             "be shown.")
        if _bracketed(json.dumps(params)):     # keys and every text in it, as _schema_ok passed
            raise ValueError(f"The params of {name} hold < or >, which a skill can't use.")
        if not isinstance(required, list) or not set(required) <= set(params) or not all(
                isinstance(r, str) for r in required):
            raise ValueError(f"The action {name} requires a parameter it doesn't declare.")
        checked[name] = {"description": description, "params": params,
                         "required": required}
    return checked


def action_name(name) -> str:
    if not isinstance(name, str) or len(name) > 60 or not ACTION.fullmatch(name):
        raise ValueError(f"The action {shown(name, 60)} needs a name like skill.do, in small "
                         "letters.")
    return name


def distinct_tools(names) -> None:
    """Two actions of one skill the model would see as the same tool, such as a.one_x and
    a_one.x, are refused."""
    seen = {}
    for name in names:
        tool = name.replace(".", "_")
        if tool in seen:
            raise ValueError(f"Its actions {shown(seen[tool], 60)} and {shown(name, 60)} would "
                             "have the same tool name. Rename one of them.")
        seen[tool] = name


def action_description(name: str, action: dict) -> str:
    """What the model routes on, so it is held to one short line it can be shown."""
    description = action.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"The action {name} has no description.")
    description = " ".join(description.split())
    if len(description) > MAX_ACTION_TEXT or not description.isprintable():
        raise ValueError(f"The description of {name} is too long or holds characters that "
                         "can't be shown.")
    if _bracketed(description):
        raise ValueError(f"The description of {name} holds < or >, which a skill can't use.")
    return description


def skill_id(manifest: dict, bundled: bool = False) -> str:
    value = manifest.get("id")
    if not isinstance(value, str) or len(value) > 100 or not SKILL_ID.fullmatch(value):
        raise ValueError("Its id has to look like org.example.name, in small letters.")
    if value.startswith(RESERVED) and not bundled:
        raise ValueError(f"Its id starts with {RESERVED}, which only skills that come with "
                         "Tuxistant may use.")
    return value


def _schema_ok(params: dict) -> bool:
    """The params schema goes to the model as it is, so it gets the same limits as a description."""
    def ok(value, depth: int) -> bool:
        if depth > MAX_PARAMS_DEPTH:
            return False
        if isinstance(value, dict):
            return all(isinstance(key, str) and ok(key, depth) and ok(item, depth + 1)
                       for key, item in value.items())
        if isinstance(value, list):
            return all(ok(item, depth + 1) for item in value)
        if isinstance(value, str):
            return len(value) <= MAX_ACTION_TEXT and value.isprintable()
        if isinstance(value, float):
            return math.isfinite(value)
        return value is None or isinstance(value, (bool, int))
    try:
        return ok(params, 0) and len(json.dumps(params)) <= MAX_PARAMS_CHARS
    except (TypeError, ValueError, RecursionError):
        return False


def _run(manifest: dict, folder: Path) -> list[str]:
    run = manifest.get("run")
    if not isinstance(run, list) or not run or len(run) > 32 or not all(
            isinstance(part, str) and part and "\0" not in part for part in run):
        raise ValueError("Its run line has to be a list of words, like python3 main.py.")
    program = run[0]
    if program not in ("python3", "node"):
        path = Path(program)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Its run line may only name files inside its own folder.")
        if not (folder / path).is_file():
            raise ValueError(f"Its run line names {shown(program, 80)}, which is not in its "
                             "folder.")
    return list(run)


def _permissions(manifest: dict) -> dict:
    """The sandbox can give a skill the network or not, but not some hosts and not others. So a
    list of hosts is shown as the whole network, which is what it really gets."""
    permissions = manifest.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise ValueError("Its permissions have to be a mapping.")
    for key, value in permissions.items():
        if key in REFUSED_PERMISSIONS:
            if value:
                raise ValueError(f"It asks for {key}, and skills you add may only ask for the "
                                 "network.")
        elif key != "network":
            raise ValueError(f"It asks for a permission Tuxistant doesn't know: {shown(key, 40)}.")
    network = permissions.get("network") or False
    if isinstance(network, list):
        if not all(isinstance(host, str) and len(host) <= 253 and HOST.fullmatch(host)
                   and host != "home" for host in network):
            raise ValueError("Its network permission has to be true or a list of host names.")
        network = bool(network)
    elif not isinstance(network, bool):
        raise ValueError("Its network permission has to be true or a list of host names.")
    return {"network": True} if network else {}


def _config(manifest: dict, bundled: bool) -> list[str]:
    """The settings there are to hand out are the owner's TP-Link login, which is for the Lights
    that come with Tuxistant and for no skill anyone else wrote."""
    keys = manifest.get("config") or []
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("Its config has to be a list of setting names.")
    for key in keys:
        if key not in SKILL_CONFIG_KEYS:
            raise ValueError(f"It asks for the setting {shown(key, 40)}, which skills can't have.")
        if not bundled:
            raise ValueError(f"It asks for the setting {key}, which only skills that come with "
                             "Tuxistant may have.")
    return sorted(set(keys))


def _timeout(manifest: dict) -> float:
    value = manifest.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            or not 1 <= value <= MAX_TIMEOUT_S):
        raise ValueError(f"Its timeout_s has to be a number of seconds from 1 to {MAX_TIMEOUT_S}.")
    return value


def load_code_manifest(folder: Path, bundled: bool) -> dict:
    """A code skill's skill.yaml, checked. Raises ValueError with a reason fit to be spoken."""
    manifest = _read_yaml(folder / MANIFEST, Bounded if bundled else Unaliased)
    unknown = set(manifest) - FIELDS
    if unknown:
        raise ValueError(f"Its {MANIFEST} has fields Tuxistant doesn't know: "
                         f"{shown(', '.join(sorted(map(str, unknown))), 100)}.")
    return {"id": skill_id(manifest, bundled), "name": _text(manifest, "name"),
            "description": _text(manifest, "description"),
            "version": _text(manifest, "version", required=False),
            "run": _run(manifest, folder), "permissions": _permissions(manifest),
            "config": _config(manifest, bundled), "timeout_s": _timeout(manifest),
            "actions": _actions(manifest, bundled), "source": "bundled" if bundled else "yours"}


def _skipped(name: str, is_dir: bool) -> bool:
    """What the watcher ignores, an editor's swap files and byte code, and hidden folders such as
    .git, are not the skill's code: they don't count, and they aren't copied."""
    return ignored(name) or (is_dir and name.startswith("."))


def _skip_names(root: str, names: list[str]) -> set[str]:
    return {name for name in names if _skipped(name, os.path.isdir(os.path.join(root, name)))}


def _code_files(folder: Path):
    """(path, path relative to the folder) of every file of a skill's code, links included and
    never followed, leaving out what _skipped does."""
    for root, dirs, names in os.walk(folder):
        links = [d for d in dirs if os.path.islink(os.path.join(root, d))]
        dirs[:] = sorted(d for d in dirs if d not in links and not _skipped(d, True))
        for name in sorted(names + links):
            if not _skipped(name, name in links):
                path = Path(root) / name
                yield path, path.relative_to(folder)


def fingerprint(folder: Path) -> str:
    """What the skill's code is: every file but what _skipped leaves out. Links count by where
    they point, and are never followed out of the folder."""
    return fingerprinted(folder)[0]


def fingerprinted(folder: Path) -> tuple[str, list[str]]:
    """The fingerprint, and the names of the files it was made from, in one walk."""
    digest, count, size, names = hashlib.sha256(), 0, 0, []
    for path, relative in _code_files(folder):
        count += 1
        names.append(str(relative))
        digest.update(str(relative).encode() + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + os.readlink(path).encode() + b"\0")
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ValueError(f"Its file {shown(relative, 80)} could not be read: "
                             f"{e.strerror or e}.") from None
        size += len(data)
        if count > MAX_FOLDER_FILES or size > MAX_FOLDER_BYTES:
            raise ValueError("Its folder is too big.")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest(), names


def _as_loaded(file: Path, load):
    """(what load() returns, the text it was loaded from). The file is read again on each side
    of the load, as one saved halfway through would show text that isn't what was loaded."""
    before = _peek(file)
    loaded = load()
    if _peek(file) != before:
        raise ValueError(f"Its {file.name} changed while it was being read.")
    return loaded, before or ""


def _peek(file: Path) -> str | None:
    """A manifest's text as Settings shows it, never through a link, at most the size a
    manifest may be. None when it can't be read; loading says why."""
    try:
        handle = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(handle, "rb") as stream:
        try:
            return stream.read(MAX_MANIFEST_BYTES + 1).decode("utf-8", "replace")
        except OSError:
            return None


def _state_dir(skill_id: str) -> Path:
    home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")   # empty is unset, as in paths.sh
    return home / "tuxistant" / "skills" / skill_id


@dataclass(eq=False)
class _Running:
    """One code skill's process and the code it was started for. code is the checked copy it
    runs from, so a restart after the folder changed still runs the version that was checked."""
    manifest: dict
    folder: Path
    digest: str
    process: object
    retired: bool = False
    code: Path | None = None


def _beyond(manifest: dict, granted: dict) -> list[str]:
    """What a manifest asks for that was never allowed, in words. A recipe names its hosts, so
    one host more is more, though the network was allowed before."""
    more = []
    asked = manifest["permissions"].get("network")
    had = granted.get("permissions", {}).get("network")
    if asked and had is not True:
        new = sorted(set(asked) - set(had or ())) if isinstance(asked, list) else []
        if not isinstance(asked, list) or not isinstance(had, list):
            more.append(f"to reach {', '.join(asked)}" if isinstance(asked, list)
                        else "for the network")
        elif new:
            more.append(f"to reach {', '.join(new)}")
    more += [f"for the setting {key}" for key in manifest.get("config", [])
             if key not in granted.get("config", [])]
    return more


class _Grants:
    """What each skill you added has been allowed, by id. Its first version is allowed what it
    asks for. A later one that asks for more is refused until the skill is taken out of the
    folder, seen gone, and put back."""

    def __init__(self, file: Path):
        self.file = file
        self.writable = True
        self.broken = ""        # why the file is there but can't be trusted, if it is
        self._records: dict | None = None
        self._seen = None       # the identity of the file as it was read, or None
        self._hold = False      # hold() for the next reload
        self.unsaved = ""       # why what was allowed couldn't be saved, for this reload
        self.held_back: dict[str, list] = {}    # ids not counted taken out, and why
        self._dirty = False

    @property
    def records(self) -> dict:
        if self._records is None:
            self._seen = _identity(self.file)
            self._records = self._read()
        return self._records

    def refresh(self) -> None:
        """Read the file again at the next use if it is broken or changed since it was read,
        as after a fix, a hand edit or damage while Tux runs, or once it is taken out."""
        if self._hold:      # hold() is for the one reload it was made for
            self._hold = False
            return
        self.unsaved = ""
        if self._records is not None and (self.broken or _identity(self.file) != self._seen):
            self._records, self.broken, self._dirty = None, "", False

    def _read(self) -> dict:
        """What was allowed. A file that is there but can't be read as one of these is left as
        it is, and while it is, nothing a skill of yours asks for is allowed. A record of the
        wrong shape counts as one that allowed nothing."""
        data = self._data()
        try:
            if isinstance(data, dict):
                return _grant_records(data)
        except Exception as e:      # whatever a hand edit holds, it never stops a reload
            data = e
        log.warning("could not read %s, so skills you added are allowed nothing they ask for "
                    "until it is fixed or taken out: %s", self.file, data)
        self.broken = BROKEN_GRANTS.format(file=self.file)
        return {}

    def _data(self):
        """What the file holds, {} when there is none, or the error. A link that leads nowhere,
        such as onto a drive that isn't there, or out of the folder the file is in, where Tux
        won't write, is an error, not a missing file."""
        if self.file.is_symlink() and not _beside(self.file):
            return OSError(f"{self.file} is a link to a file in another folder")
        try:
            return _read_json(self.file)
        except FileNotFoundError as e:
            return e if os.path.lexists(self.file) else {}
        except (OSError, ValueError, RecursionError) as e:
            return e

    def as_seen(self) -> bool:
        """Whether the file is as Tux last read or wrote it, so a change to it is Tux's own."""
        return self._records is not None and not self.broken and (
            _identity(self.file) == self._seen)

    def hold(self, why: str) -> None:
        """For one reload, after what was allowed couldn't be saved: the records are read from
        the file again, as they are kept, and nothing new is allowed, so nothing unkept is."""
        self._records, self._dirty, self._hold = None, False, True
        self.unsaved = UNSAVED_GRANTS.format(file=self.file, why=why)
        log.warning("could not save what skills were allowed to %s: %s", self.file, why)

    def denies(self, manifest: dict) -> str:
        """Why what a manifest asks for isn't allowed now, as for a version kept in use after
        the file was damaged or a hand edit took a host away; "" when it is."""
        record = self.records.get(manifest["id"])
        if self.broken:
            return self.broken if _beyond(manifest, {}) else ""
        if record is None or record.get("removed"):
            return self.unsaved if self.unsaved and _asks(manifest) else ""
        if not _beyond(manifest, record):
            return ""
        return f"It asks {' and '.join(_beyond(manifest, record))}, which it isn't allowed now."

    def problem(self, manifest: dict, folder: Path) -> str:
        record = self.records.get(manifest["id"])   # reads the file, which may find it broken
        if self.broken:
            return self.broken if _beyond(manifest, {}) else ""
        if record is None or record.get("removed"):
            return self.unsaved if self.unsaved and _asks(manifest) else ""
        more = _beyond(manifest, record)
        if not more:
            return ""
        if record.get("folder") != folder.name and _there(folder.parent, record) is False:
            record["folder"], self._dirty = folder.name, True   # moved: it stays refused here
        waiting = self.held_back.get(manifest["id"])
        if waiting:
            return (f"It now asks {' and '.join(more)}, which it wasn't allowed before, so the "
                    "new version isn't used. It isn't counted as taken out while "
                    f"{', '.join(shown(name, 60) for name in waiting)} in your skills folder "
                    "can't be read, as that could be it under another name. Fix or take that "
                    "out first.")
        if manifest.get("recipe"):
            return (f"It now asks {' and '.join(more)}, which it wasn't allowed before, so the "
                    "new version isn't used. To allow it, take the file out of your skills "
                    "folder, wait a moment, and put it back.")
        return (f"It now asks {' and '.join(more)}, which it wasn't allowed before, so the "
                "new version isn't used. To allow it, take the skill out of your skills folder, "
                "wait a moment, and put it back. What it saved is then cleared.")

    def grant(self, manifest: dict, folder: Path) -> bool:
        """True when what the skill saved has to be cleared: it is back with more than before."""
        record = self.records.get(manifest["id"])
        if self.broken or (self.unsaved and (record is None or record.get("removed"))):
            return False
        fresh = {"permissions": dict(manifest["permissions"]),
                 "config": list(manifest.get("config", [])), "folder": folder.name}
        if record is None:      # new, so it starts with nothing an earlier one saved
            self.records[manifest["id"]], self._dirty = fresh, True
            return True
        if record.get("removed"):
            more = _beyond(manifest, record)
            self.records[manifest["id"]] = fresh if more else {
                **record, "removed": False, "folder": folder.name}
            self._dirty = True
            return bool(more)
        if record.get("folder") != folder.name and _there(folder.parent, record) is False:
            record["folder"], self._dirty = folder.name, True   # renamed, or the copy that
        return False                                            # is left of two

    def gone(self, user: Path, here: set[str], held_back=lambda skill_id: []) -> set[str]:
        """Marks removed each record whose file or folder is really not there and whose id no
        skill found in this reload has, and dates those with none. Only call it when the folder
        could be listed. One held_back names files for isn't marked, as one of them may be it
        renamed and broken; held_back says so. Returns the ids marked or dated."""
        self.held_back = {}
        marked, now = set(), int(time.time())
        for key, record in self.records.items():
            if record.get("removed"):
                if "removed_at" not in record:      # first seen now, so not pruned now either
                    record["removed_at"], self._dirty = now, True
                    marked.add(key)
                continue
            if key in here or _there(user, record) is not False:
                continue
            blocking = held_back(key)
            if blocking:
                self.held_back[key] = blocking
                continue
            record["removed"], record["removed_at"], self._dirty = True, now, True
            marked.add(key)
        return marked

    def prune(self, user: Path, keep: set[str]) -> list[str]:
        """The ids of skills taken out longest ago, past the newest MAX_REMOVED, whose records
        are dropped, so the file stays small. One of those that comes back is new. Never one in
        keep, one of Tuxistant's own, one marked removed in this same reload, or one whose
        file or folder is there."""
        now = time.time()
        removed = sorted((min(record.get("removed_at", now), now), key)
                         for key, record in self.records.items() if record.get("removed"))
        dropped = [key for _, key in removed[:max(len(removed) - MAX_REMOVED, 0)]
                   if key not in keep and not key.startswith(RESERVED)
                   and _there(user, self.records[key]) is False]
        for key in dropped:
            del self.records[key]
            self._dirty = True
        return dropped

    def save(self) -> str:
        """Writes the records, whole, over the file they were read from, and says why what was
        allowed in this reload isn't kept, if it isn't: CHANGED when the file changed since it
        was read, which is read again for the reload to be made again, or the error. Through a
        link only onto the file it led to when read, beside it. An edit in the moment between
        that check and the rename is written over; one just after is seen at the next reload."""
        if not self._dirty or not self.writable or self.broken or self.unsaved:
            return ""
        target = self._target()
        if target is None:
            self._records, self._dirty = None, False
            return CHANGED
        try:
            written = _write_json(target, self.records)
        except OSError as e:
            self._records, self._dirty = None, False
            return e.strerror or str(e)
        now = _identity(self.file)      # ours, unless an edit came just after the rename
        self._seen = now if isinstance(now, tuple) and isinstance(written, tuple) and (
            now[:4] == written[:4]) else None
        self._dirty = False
        return ""

    def _target(self) -> Path | None:
        """Where to write: the file, or the one a link to it leads to, resolved once, as long
        as it is still the file that was read, and next to the link."""
        if _identity(self.file) != self._seen:
            log.warning("%s changed while Tux ran, so it is read again rather than written "
                        "over", self.file)
            return None
        if not self.file.is_symlink():
            return self.file
        target = Path(os.path.realpath(self.file))
        if not _beside(self.file) or _identity(target) != self._seen:
            log.warning("%s leads elsewhere, so it isn't written", self.file)
            return None
        return target


def _beside(link: Path) -> bool:
    """Whether a link leads to a file in the folder it is in."""
    return Path(os.path.realpath(link)).parent == Path(os.path.realpath(link.parent))


def _asks(manifest: dict) -> bool:
    """Whether a skill with no record needs one noted to load: one that asks for anything, or
    has code, which would start on whatever an earlier one with its id saved."""
    return bool(_beyond(manifest, {})) or not manifest.get("recipe")


def _identity(file: Path):
    """What tells whether a file changed: device, inode, size and times, following a link;
    "missing" or "dangling" when there is nothing to follow."""
    try:
        info = os.stat(file)
    except FileNotFoundError:
        return "dangling" if os.path.lexists(file) else "missing"
    except OSError as e:
        return f"error {e.errno}"
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            stat.S_IFMT(info.st_mode))


def _there(user: Path, record: dict) -> bool | None:
    """Whether the file or folder a record names is there: None when that can't be told now,
    False for no plain name at all."""
    folder = record.get("folder")
    if not _plain_name(folder):
        return False
    try:
        (user / folder).lstat()
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):   # UnicodeError is a ValueError
        return None


def _grant_records(data: dict) -> dict:
    """The records of a grants file, by skill id. One that isn't a mapping of ids to mappings,
    such as another app's settings behind a link, isn't a grants file at all."""
    if not all(isinstance(key, str) and SKILL_ID.fullmatch(key) and isinstance(value, dict)
               for key, value in data.items()):
        raise ValueError("it isn't a list of skills and what each was allowed")
    return {key: _grant_record(value) for key, value in data.items()}


def _grant_record(value: dict) -> dict:
    """A record as _beyond and the rest read it, or one that allowed nothing."""
    allowed_nothing = {"permissions": {}, "config": [], "folder": ""}
    permissions, config = value.get("permissions", {}), value.get("config", [])
    network = permissions.get("network") if isinstance(permissions, dict) else None
    shaped = (isinstance(permissions, dict)
              and (network is None or isinstance(network, bool) or (
                  isinstance(network, list) and all(isinstance(h, str) for h in network)))
              and isinstance(config, list) and all(isinstance(key, str) for key in config)
              and isinstance(value.get("removed", False), bool)
              and isinstance(value.get("folder", ""), str))
    if shaped:
        if not _a_time(value.get("removed_at", 0)):
            value = {key: field for key, field in value.items() if key != "removed_at"}
        return value
    folder = value.get("folder")
    return {**allowed_nothing, "folder": folder if isinstance(folder, str) else ""}


def _a_time(value) -> bool:
    """A number of seconds a JSON file can hold sensibly: no bool, NaN or infinity, and no
    integer too big for a float, which math would refuse."""
    if type(value) is int:
        return abs(value) < 2 ** 53
    return type(value) is float and math.isfinite(value)


def _read_json(file: Path):
    """A small JSON file of Tux's own. Anything but a regular file, such as a pipe that would
    never end, or one past MAX_STATE_BYTES, is refused with an OSError unread."""
    fd = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{file} is not a file")
        if info.st_size > MAX_STATE_BYTES:
            raise OSError(f"{file} is larger than {MAX_STATE_BYTES} bytes")
        text = handle.read(MAX_STATE_BYTES + 1)
    if len(text) > MAX_STATE_BYTES:
        raise OSError(f"{file} is larger than {MAX_STATE_BYTES} bytes")
    return json.loads(text)     # RecursionError for one nested too deep


def _write_json(file: Path, data) -> tuple:
    """Whole or not at all, so a crash halfway leaves the last copy. Returns the device, inode,
    size and time of what was written."""
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{file.stem}-", dir=file.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=1, sort_keys=True)
            out.flush()
            info = os.fstat(out.fileno())
        os.replace(temp, file)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class _Place:
    """What one of yours holds in the budget: the count it was offered with, and the fingerprint
    of that manifest, its id and its actions, so no other skill takes those while it is refused
    or broken, and a restart still knows whether it was edited since."""
    count: int | None           # None: a name alone, from an earlier build, counted as it is now
    digest: str = ""            # "" when not known, which counts as edited
    skill_id: str = ""
    actions: tuple = ()

    @classmethod
    def of(cls, item: "_Found") -> "_Place":
        return cls(len(item.manifest["actions"]), _text_digest(item.text), item.manifest["id"],
                   tuple(sorted(item.manifest["actions"])))

    def saved(self) -> dict:
        return {"count": self.count, "digest": self.digest, "id": self.skill_id,
                "actions": list(self.actions)}


def _inode(key: str):
    try:
        st = os.lstat(key)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read_loaded(file: Path) -> dict[str, _Place]:
    """Names and what each holds. Counts alone, or a list of names, as earlier 0.5 builds kept,
    give no fingerprint or names, and a list no counts either. Counts that add up past the
    budget, which Tux never writes, are not trusted at all."""
    try:
        data = _read_json(file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        log.warning("could not read %s, so the skills you added load in name order", file)
        return {}
    if isinstance(data, list):
        return {name: _Place(None) for name in data if _plain_name(name)}
    if not isinstance(data, dict):
        return {}
    places = {}
    for name, value in data.items():
        place = _place_from(value)
        if _plain_name(name) and place is not None:
            places[name] = place
    if sum(place.count or 0 for place in places.values()) > USER_TOOLS:
        log.warning("%s holds more than %d actions, so the skills you added load in name order",
                    file, USER_TOOLS)
        return {}
    return _unshared(places)


def _unshared(places: dict[str, _Place]) -> dict[str, _Place]:
    """An id, action or tool name that two entries claim, which Tux never writes, is left to
    neither, so a hand edit can't keep a skill from another's names."""
    ids = Counter(place.skill_id for place in places.values() if place.skill_id)
    tools = Counter(tool for place in places.values()       # each entry counted once
                    for tool in {name.replace(".", "_") for name in place.actions})
    return {name: replace(place, skill_id=place.skill_id if ids[place.skill_id] == 1 else "",
                          actions=tuple(action for action in place.actions
                                        if tools[action.replace(".", "_")] == 1))
            for name, place in places.items()}


def _place_from(value) -> _Place | None:
    if type(value) is int:
        return _Place(value) if 0 < value <= USER_TOOLS else None
    if not isinstance(value, dict):
        return None
    count, digest = value.get("count"), value.get("digest")
    skill_id, actions = value.get("id"), value.get("actions")
    if type(count) is not int or not 0 < count <= USER_TOOLS:
        return None
    if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
        digest = ""
    if not (isinstance(skill_id, str) and SKILL_ID.fullmatch(skill_id)):
        skill_id = ""
    if not (isinstance(actions, list) and len(actions) <= count and all(
            isinstance(name, str) and ACTION.fullmatch(name) for name in actions)):
        actions = []
    return _Place(count, digest, skill_id, tuple(sorted(set(actions))))


def _plain_name(name) -> bool:
    """A name of one file in a folder, that the system can be asked about: not empty, . or ..,
    with no / or NUL, and one os.fsencode can encode, so no lone surrogate either."""
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
        return False
    try:
        os.fsencode(name)
    except UnicodeError:
        return False
    return True


@dataclass
class _Found:
    key: str
    manifest: dict
    folder: Path | None = None      # code skills only
    digest: str = ""
    kind: str = "builtin"           # "builtin", "code" or "recipe"
    text: str = ""                  # the manifest's text, as it was loaded
    files: tuple = ()               # a code skill's files, as fingerprinted

    def origin(self) -> dict:
        """Where it came from, as Skills.details hands it to Settings."""
        if self.kind == "code":
            return {"file": str(self.folder / MANIFEST), "folder": str(self.folder),
                    "files": list(self.files), "manifest": self.text}
        return {"file": self.key, "manifest": self.text}


class SkillHost:
    def __init__(self, config, builtin=BUILTIN, bundled=BUNDLED, user=USER_DIR,
                 launcher=None, on_change=None, recipes=None):
        if recipes is None:
            from . import recipes as recipe_module
            recipes = recipe_module.handlers
        self.recipe_handlers = recipes      # a recipe's manifest -> its async handlers
        self.config = config
        self.builtin, self.bundled, self.user = Path(builtin), Path(bundled), Path(user)
        self.launcher = launcher or self._launch
        self.on_change = on_change
        self.problems: dict[str, str] = {}
        self.loaded = self.user.parent / LOADED
        self.saved = _read_loaded(self.loaded)
        # what keeps its place in the budget, by key, with what it holds: what the last reload
        # loaded or kept a place for, or at start yours that kept one before
        self.offered: dict[str, _Place] = {
            str(self.user / name): place for name, place in self.saved.items()}
        self.last_offered: dict[str, _Found] = {}   # by key, as the last reload offered them
        self.running: dict[str, _Running] = {}      # by folder
        self.failed: dict[str, str] = {}            # by key, why it didn't load, last reload
        self.unknown_since: dict[str, tuple] = {}   # yours that won't read, unknown: since, as
        self.missing_since: dict[str, int] = {}     # ids whose record's folder is gone, since
        self._count = 0                             # reloads that listed your folder
        self.seen_ids: dict[str, tuple] = {}        # yours by key: (id, digest) last read with
        self.stopping: set[asyncio.Task] = set()
        self.retired: list[_Running] = []           # retired with no loop to stop them on
        self.watcher = None
        self.grants = _Grants(self.user.parent / GRANTS)
        self.switched_off: dict[str, tuple[str, str]] = {}  # recipes the registry revoked
        self.code_root = self.user.parent / CODE_DIR
        self.using: Counter = Counter()             # checked copies by the processes using them
        self._current = Skills(config=config, manifests=[])

    def current(self) -> Skills:
        return self._current

    async def start(self) -> None:
        self.reload()
        self._prune_code()
        try:
            self.user.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("could not make the skills folder: %s", e)
        try:
            from .inotify import Watcher
            self.watcher = Watcher([self.builtin, self.bundled, self.user], self._changed,
                                   files=[self.grants.file], unchanged=self.grants.as_seen)
            await self.watcher.start()
        except Exception:   # the skills still work; only changes need a ReloadSkills
            log.exception("could not watch the skills folders")
            self.watcher = None

    async def stop(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        everything, self.running = list(self.running.values()), {}
        everything += self.retired
        self.retired = []
        for entry in everything:
            entry.retired = True
        await asyncio.gather(*(self._stop_one(entry) for entry in everything),
                             *self.stopping, return_exceptions=True)

    # Browse's approval card, the one other way what a skill is allowed changes

    def granted(self, skill_id: str) -> dict | None:
        """A copy of the record of what a skill of yours is allowed, from the grants file as it
        is now, or None: no record, or a file that can't be read."""
        self.grants.refresh()
        record = self.grants.records.get(skill_id)
        return None if self.grants.broken or record is None else copy.deepcopy(record)

    def allow(self, manifest: dict, file: Path) -> str:
        """After Allow on Browse's card and before the file is written: the record of the
        checked manifest's id becomes what it asks for, and is saved, so the reload after the
        write loads it. Nothing else changes. "" or why it couldn't be saved."""
        return self._set_grant(manifest["id"], {
            "permissions": copy.deepcopy(manifest["permissions"]), "config": [],
            "folder": file.name})

    def put_back(self, skill_id: str, record: dict | None) -> str:
        """The record as granted() gave it before allow(), after the file couldn't be written."""
        return self._set_grant(skill_id, record)

    def _set_grant(self, skill_id: str, record: dict | None) -> str:
        grants = self.grants
        grants.refresh()
        records = grants.records    # reads the file, which may find it broken
        if grants.broken:
            return grants.broken
        if not grants.writable:
            return NOT_ALLOWED.format(file=grants.file, why="nothing is saved here")
        if record is None:
            records.pop(skill_id, None)
        else:
            records[skill_id] = record
        grants._dirty = True
        why = grants.save()
        return NOT_ALLOWED.format(file=grants.file, why=why) if why else ""

    def _changed(self) -> None:
        problems = self.reload()
        if problems:
            log.info("skill problems: %s", problems)

    def reload(self, again: int = 2) -> dict[str, str]:
        """Builds a new Skills from what is on disk and swaps it in. A skill that fails keeps its
        last version that worked, if it has one. Never raises: a reload runs on every save.
        When what is noted of yours changed while some of yours were refused, as when an edit
        that gave a name up loaded too late in _settle for another to take it, it reloads again,
        up to again times, so a reload with nothing changed changes nothing. Others are told
        once, after the last."""
        swapped = False
        for _ in range(again + 1):
            problems, done, more, unsaved = self._reload_once()
            swapped = swapped or done
            if not more:
                break
        if unsaved:     # what it allowed couldn't be kept: allow nothing new this time
            self.grants.hold(unsaved)
            problems, done, _, _ = self._reload_once()
            swapped = swapped or done
        if swapped and self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("telling others the skills changed failed")
        return problems

    def _reload_once(self) -> tuple[dict[str, str], bool, bool, str]:
        """The problems, whether a new Skills was swapped in, whether another look could load
        more: a name was freed too late in _settle, or what is noted of yours changed while
        some of yours were refused, or the grants file changed under the save; and why what
        was allowed couldn't be saved, if it couldn't."""
        problems: dict[str, str] = {}
        try:
            self.grants.refresh()
            found = self._scan(problems)
            sandbox_problem = self._sandbox_problem()
            chosen, places, problems, late = self._settle(found, problems, sandbox_problem)
            unsaved = self._grant(chosen, found, listed=str(self.user) not in problems)
            if unsaved:     # made again from the file as it is now, if that could help
                return dict(problems), False, unsaved == CHANGED, unsaved
            skills, running, offered = self._build(chosen, problems, sandbox_problem)
        except Exception as e:
            log.exception("reloading the skills failed")
            problems["skills"] = f"The skills could not be reloaded: {e}"
            self.problems = problems
            # nothing half noted stays, and a file fixed meanwhile counts at the next read
            self.grants._records, self.grants.broken, self.grants._dirty = None, "", False
            return dict(problems), False, False, ""
        self._swap(skills, running)
        before, last = self.offered, self.last_offered
        self.problems, self.last_offered = problems, offered
        self.offered = {key: _Place.of(item) for key, item in offered.items()}
        self.offered.update(places)
        if str(self.user) in problems:      # none of yours was seen, so none of them changed
            self.offered.update((key, place) for key, place in before.items()
                                if Path(key).parent == self.user)
            self.last_offered.update((key, item) for key, item in last.items()
                                     if Path(key).parent == self.user)
        else:
            self._save_loaded()
        mine = {key: place for key, place in self.offered.items()
                if Path(key).parent == self.user}
        moved = mine != {key: place for key, place in before.items()
                         if Path(key).parent == self.user}
        refused = any(Path(key).parent == self.user for key in problems)
        return dict(problems), True, late or (moved and refused), ""

    def _save_loaded(self) -> None:
        """The order of the budget has to outlast a restart, or a skill refused for being one too
        many would take the place of one that worked, at the next login."""
        names = {Path(key).name: place for key, place in self.offered.items()
                 if Path(key).parent == self.user and place.count is not None}
        if names == self.saved or not self.grants.writable:
            return
        try:
            _write_json(self.loaded, {name: place.saved() for name, place in names.items()})
            self.saved = names
        except OSError as e:
            log.warning("could not save which skills were loaded: %s", e)

    # Reading what is on disk

    def _scan(self, problems: dict) -> list[_Found]:
        self.failed: dict[str, str] = {}    # why each that didn't load failed, this reload
        found = []
        self.switched_off = registry.switched_off(self.user)
        for file in self._listing(self.builtin, "*.yaml", problems):
            found += self._attempt(str(file), problems, lambda f=file: self._builtin(f))
        for root, bundled in ((self.bundled, True), (self.user, False)):
            for entry in self._listing(root, "*", problems):
                if entry.name.startswith(".") or entry.name in IGNORED_PARTS:
                    continue
                if entry.is_dir():
                    found += self._attempt(str(entry), problems,
                                           lambda f=entry, b=bundled: self._code(f, b))
                elif not bundled and _a_recipe(entry):
                    found += self._attempt(str(entry), problems, lambda f=entry: self._recipe(f))
                elif not bundled and _almost_a_recipe(entry):
                    problems[str(entry)] = NOT_YAML
        return found

    @staticmethod
    def _listing(root: Path, pattern: str, problems: dict) -> list[Path]:
        # iterdir, not glob: since Python 3.14 glob finds nothing in a folder it can't read and
        # says nothing, which would hide every skill there without a word.
        try:
            return sorted(p for p in root.iterdir() if p.match(pattern)) if root.is_dir() else []
        except OSError as e:
            problems[str(root)] = f"This folder could not be read: {e.strerror or e}."
            return []

    def _attempt(self, key: str, problems: dict, load) -> list[_Found]:
        try:
            item = load()
        except registry.SwitchedOff as e:     # never the version kept in use, either
            self.failed[key] = problems[key] = str(e)
            return []
        except Exception as e:
            problem = str(e) if isinstance(e, ValueError) else f"It could not be loaded: {e}"
            self.failed[key] = problems[key] = problem
            last = self.last_offered.get(key)
            if last and Path(key).exists():     # what is in use stays, if it is still allowed
                problems[key] = f"{problem} {KEPT}"
                return [last]
            return []
        return [item]

    @staticmethod
    def _builtin(file: Path) -> _Found:
        manifest, text = _as_loaded(file, lambda: _read_yaml(file))
        actions = manifest.get("actions")
        if not isinstance(manifest.get("id"), str) or not isinstance(actions, dict) or not actions:
            raise ValueError("It needs an id and at least one action.")
        for field in ("name", "description"):
            if not isinstance(manifest.get(field), str):
                raise ValueError(f"Its {field} is missing or is not text.")
        for name, action in actions.items():
            if not isinstance(action, dict) or not isinstance(action.get("description"), str):
                raise ValueError(f"The action {shown(name, 60)} has no description.")
        missing = [name for name in actions if name not in HANDLERS]
        if missing:     # Python handlers only load at start
            raise ValueError(f"The action {shown(missing[0], 60)} has no code in the daemon yet. "
                             "Restart Tuxistant to load it.")
        manifest = {**manifest, "source": "built-in"}
        manifest["risk"] = risk_of(manifest.get("permissions") or {})
        return _Found(str(file), manifest, text=text)

    def _code(self, folder: Path, bundled: bool) -> _Found:
        manifest, text = _as_loaded(folder / MANIFEST, lambda: load_code_manifest(folder, bundled))
        problem = "" if bundled else self.grants.problem(manifest, folder)
        if problem:     # it read, so its id is known, though it doesn't load
            self.seen_ids[str(folder)] = (manifest["id"], _text_digest(text))
            raise ValueError(problem)
        digest, files = fingerprinted(folder)
        return _Found(str(folder), manifest, folder.resolve(), digest, "code", text,
                      tuple(sorted(files)))

    def _recipe(self, file: Path) -> _Found:
        from . import recipes
        manifest, text = _as_loaded(file, lambda: recipes.load(file))
        off = self.switched_off.get(file.name)
        if off and off[0] == manifest["id"]:
            self.seen_ids[str(file)] = (manifest["id"], _text_digest(text))
            raise registry.SwitchedOff(off[1])
        problem = self.grants.problem(manifest, file)
        if problem:     # it read, so its id is known, though it doesn't load
            self.seen_ids[str(file)] = (manifest["id"], _text_digest(text))
            raise ValueError(problem)
        return _Found(str(file), manifest, kind="recipe", text=text)

    def _grant(self, chosen: list[_Found], found: list[_Found], listed: bool) -> str:
        """Notes what is allowed, and clears the state of those new, back with more or pruned,
        once that is kept. Why it couldn't be kept, if it couldn't."""
        clear = [item.manifest["id"] for item in chosen
                 if item.kind != "builtin" and item.manifest["source"] == "yours"
                 and self.grants.grant(item.manifest, Path(item.key)) and item.kind == "code"]
        if listed:      # a folder that couldn't be read shows nothing gone
            for item in found:
                if item.manifest["source"] == "yours":
                    self.seen_ids[item.key] = (item.manifest["id"], _text_digest(item.text))
            self.seen_ids = {key: seen for key, seen in self.seen_ids.items()
                             if os.path.lexists(key)}
            found_ids = {item.manifest["id"] for item in found}
            known = {key: self._known_id(key) for key in set(self.seen_ids) | {
                key for key in self.offered if Path(key).parent == self.user}}
            # a file there that doesn't load now still has the id it last read with, as long
            # as it is the same file; one that changed since may be another renamed onto it
            present = {skill_id for skill_id in known.values() if skill_id}
            self._count += 1
            # by what it holds and which entry it is: a folder renamed onto an empty one that
            # was already failing is another, so its clock starts again
            unknown = {key: (_text_digest(self._peek_key(key) or ""), _inode(key))
                       for key in self.failed
                       if Path(key).parent == self.user and not self._known_id(key)}
            # since when each has failed as it is now: written anew, it starts again
            self.unknown_since = {
                key: self.unknown_since[key] if self.unknown_since.get(key, (0, ""))[1] == digest
                else (self._count, digest) for key, digest in unknown.items()}
            marked = self.grants.gone(self.user, found_ids | present, self._held_back)
            self.missing_since = {skill_id: began for skill_id, began
                                  in self.missing_since.items()
                                  if skill_id in self.grants.held_back
                                  or skill_id in found_ids | present}
            # kept while a copy stands in, so taking the copy out later still holds it back
            here = found_ids | marked | {
                place.skill_id for place in self.offered.values() if place.skill_id}
            # one pruned is new when it comes back, so it keeps nothing it saved
            clear += [skill_id for skill_id in self.grants.prune(self.user, here)
                      if SKILL_ID.fullmatch(skill_id)]  # a key from the file may be anything
        unsaved = self.grants.save()
        if unsaved:
            return unsaved
        if not self.grants.unsaved:     # held, nothing was saved, so nothing is cleared
            for skill_id in clear:      # a recipe keeps nothing, so only code is cleared
                self._clear_state(skill_id)
        return ""

    @staticmethod
    def _peek_key(key: str) -> str | None:
        return _peek(Path(key) / MANIFEST if Path(key).is_dir() else Path(key))

    def _known_id(self, key: str) -> str:
        """The id a file of yours had when last read, if it is the same file now, by what it
        holds: from this run, or the place it keeps from before."""
        digest = _text_digest(self._peek_key(key) or "")
        seen = self.seen_ids.get(key)
        if seen and seen[1] == digest:
            return seen[0]
        place = self.offered.get(key)
        return place.skill_id if place and place.digest == digest else ""

    def _held_back(self, skill_id: str) -> list[str]:
        """The files of yours that won't read, have no known id and began so no earlier than
        the record's folder went missing, as a rename into one would: while there are, it
        isn't marked taken out. One there from before, such as a draft, holds nothing."""
        since = self.missing_since.setdefault(skill_id, self._count)
        return sorted(Path(key).name for key, (began, _digest) in self.unknown_since.items()
                      if began >= since)

    def _clear_state(self, skill_id: str) -> None:
        """Only the default launcher gives a skill this folder; a test's launcher has none."""
        if self.launcher == self._launch and self.grants.writable:
            shutil.rmtree(_state_dir(skill_id), ignore_errors=True)

    # Deciding who gets what

    def _settle(self, found: list[_Found], scanned: dict, sandbox_problem: str):
        """_resolve, then again with the names an edit gives up freed while that edit is
        otherwise allowed, as when skills swap actions, widening the skills freed so to every
        edited one that blocks another, as long as needed. A pass counts only if every skill
        whose names were freed then loads that edit; otherwise the last that counted stands,
        so a name is never taken from a skill that ends up on its kept version or its place."""
        current = {item.key: item for item in found}
        trying, rejected = frozenset(), set()
        best = None     # the first pass frees nothing, so it always counts
        for _ in range(MAX_PASSES):
            result = self._resolve(found, dict(scanned), sandbox_problem, trying)
            chosen, _, _, blockers, waiting, _, _ = result
            loads = {item.key for item in chosen if current.get(item.key) is item}
            failed = trying - loads
            if not failed:
                best = result
            more = {key for key in blockers - trying - rejected
                    if key in current and self._edited(current[key])
                    and (key in loads or key in waiting)}
            if more:
                trying |= more
            elif failed:        # nothing more to free: let those go, and try without them
                rejected |= failed
                trying -= failed
            else:
                break
        chosen, places, problems, _, _, held_by, named = best
        loads = {item.key for item in chosen if current.get(item.key) is item}
        self._name_reasons(current, chosen, places, problems, named)
        late = False
        for key, holder in held_by.items():     # an edit that gave the name up loaded after all
            if holder in loads and key in problems and not _holders(
                    current[key], {holder: _still_held(self.offered[holder], current[holder])}):
                reason = _held_reason(current[key], self.offered[holder], holder, freed=True)
                problems[key] = f"{reason} {KEPT}" if problems[key].endswith(KEPT) else reason
                late = True
        return chosen, places, problems, late

    def _name_reasons(self, current: dict, chosen: list[_Found], places: dict, problems: dict,
                      named: set[str]) -> None:
        """Why each refused for a name, a lookalike or the budget, as the reload ends
        rather than as it went, so the same skills give the same reason whatever order they
        were counted in. A name one of yours has is said with its file."""
        builtin_ids = {item.manifest["id"] for item in chosen if item.kind == "builtin"}
        ours = {_namespace(name) for name in HANDLERS} | {
            _namespace(name) for item in current.values() if item.manifest["source"] != "yours"
            for name in item.manifest["actions"]}
        by_key = {item.key: item for item in chosen}
        owner: dict[str, str] = {}
        for item in chosen:
            owner.update(dict.fromkeys(_names(item.manifest), item.key))
            owner.update(dict.fromkeys((f"action:{name}" for name in item.manifest["actions"]),
                                       item.key))
        for key in sorted(named - set(self.failed)):
            item = current.get(key)
            if item is None or key not in problems:
                continue
            theirs = {name for name, holder in owner.items() if holder != key}
            reason = self._clash(
                item, {name[3:] for name in theirs if name.startswith("id:")},
                {name[7:] for name in theirs if name.startswith("action:")},
                {name for name in theirs if ":" not in name}, builtin_ids)
            holder = next((owner[name] for name in [f"id:{item.manifest['id']}"] + [
                n for action in item.manifest["actions"]
                for n in (f"action:{action}", action.replace(".", "_"))] if name in theirs), "")
            if reason and holder and by_key[holder].manifest["source"] == "yours":
                reason = _held_reason(item, _Place.of(by_key[holder]), holder)
            if not reason and self._edited(item):
                holders = _holders(item, {k: place for k, place in places.items() if k != key})
                holder = min(holders, default="")
                reason = holder and _held_reason(item, places[holder], holder)
            if not reason:
                reason = _lookalike(item.manifest, ours) or self._over(item, key, chosen, places)
            if reason:
                problems[key] = f"{reason} {KEPT}" if problems[key].endswith(KEPT) else reason

    def _over(self, item: _Found, key: str, chosen: list[_Found], places: dict) -> str:
        """The budget's reason for one, counting what else of yours loads or keeps a place."""
        taken = sum(len(other.manifest["actions"]) for other in chosen
                    if other.manifest["source"] == "yours" and other.key != key)
        taken += sum(place.count for k, place in places.items() if k != key)
        count = len(item.manifest["actions"])
        if taken + count <= USER_TOOLS:
            return ""
        if self._place(item) == GROWN and count <= USER_TOOLS:
            return GREW
        return over_budget(taken, count)

    def _resolve(self, found: list[_Found], problems: dict, sandbox_problem: str,
                 released: frozenset = frozenset()):
        """What loads, and the places kept for yours that were offered and aren't now.
        Yours that were loaded keep their place, ahead of new ones, so a file dropped in is
        the one refused rather than one that was working. Of those, the ones unchanged since
        they were last offered come first. One that has more actions than it was offered with
        comes after those that don't, so what it added is refused, never another skill. Each
        one of yours that was offered holds the id and actions it was last offered with,
        whatever an edit to it says, so one that is new or edited can't take them, whatever the
        order; what an edit gives up is free once that edit has loaded, from the next reload.
        One of yours refused for any reason keeps the version last offered while that is still
        allowed; with none, as after a restart, it keeps its place and those names, as one
        whose file is still there but failed to load does, so no other skill takes that room
        or those names. Room for each one that grew and is still to come is held the same way.
        A code skill that can't run for want of a sandbox keeps no place."""
        ids, actions, tools = set(), set(), set()
        builtin_ids = {item.manifest["id"] for item in found if item.kind == "builtin"}
        ours = {_namespace(name) for name in HANDLERS} | {
            _namespace(name) for item in found if item.manifest["source"] != "yours"
            for name in item.manifest["actions"]}
        chosen = []
        places = self._unloaded(found, problems, sandbox_problem)
        yours = sum(place.count for place in places.values())
        held = {item.key: self.offered[item.key].count for item in found
                if self._place(item) == GROWN}
        reserved = dict(places) | {
            item.key: _still_held(self.offered[item.key], item) if item.key in released
            else self.offered[item.key] for item in found
            if item.manifest["source"] == "yours" and item.key in self.offered
            and not (item.kind == "code" and sandbox_problem)}
        blockers, waiting = set(), set()    # who holds names refused to others; who waits
        held_by: dict[str, str] = {}        # one refused for a held name, to its holder
        named: set[str] = set()             # those refused for a name another has
        owners: dict[str, str] = {}         # an id or tool name of yours chosen, to its key

        def budget(item: _Found) -> str:
            count, taken = len(item.manifest["actions"]), yours + sum(held.values())
            if taken + count <= USER_TOOLS:
                return ""
            if self._place(item) == GROWN and count <= USER_TOOLS:
                return GREW
            return over_budget(taken, count)

        def refusal(item: _Found, note=False) -> str:
            mine = item.manifest["source"] == "yours"
            # nor may a kept version ask for what the grants don't allow now
            unrunnable = mine and (item.kind == "code" and sandbox_problem
                                   or self.grants.denies(item.manifest))
            problem = unrunnable or self._clash(item, ids, actions, tools, builtin_ids)
            if problem:     # ones of yours that load may have it, maybe as a kept version
                holders = set() if unrunnable or not mine else _owners(item, owners)
                if note and mine and not unrunnable:
                    named.add(item.key)
            elif mine and self._edited(item):
                holders = _holders(item, reserved)
                holder = min(holders, default="")
                problem = holder and _held_reason(item, reserved[holder], holder)
                if holder and note:
                    held_by[item.key] = holder
                    named.add(item.key)
            else:
                holders = set()
            if holders and note:
                blockers.update(holders)
                waiting.add(item.key)
            if problem or not mine:
                return problem
            problem = _lookalike(item.manifest, ours) or budget(item)
            if problem and note:
                named.add(item.key)     # said as the reload ends, as a clash may come first
            return problem

        for item in sorted(found, key=self._order):
            mine = item.manifest["source"] == "yours"
            held.pop(item.key, None)
            problem = refusal(item, note=True)
            before = self.last_offered.get(item.key) if problem and mine else None
            if before is not None and before is not item and not refusal(before):
                # a broken edit's own reason, which says the same of the version kept
                problems.setdefault(item.key, f"{problem} {KEPT}")
                item, problem = before, ""
            if problem:
                problems[item.key] = self.failed.get(item.key, problem)
                place = reserved.get(item.key) if mine else None
                if place is not None and place.count is not None and not (
                        item.kind == "code" and sandbox_problem):
                    places[item.key] = place
                    yours += place.count
                continue
            yours += len(item.manifest["actions"]) if mine else 0
            if mine:
                owners.update(dict.fromkeys(_names(item.manifest), item.key))
            ids.add(item.manifest["id"])
            actions.update(item.manifest["actions"])
            tools.update(name.replace(".", "_") for name in item.manifest["actions"])
            chosen.append(item)
        return chosen, places, problems, blockers, waiting, held_by, named

    def _unloaded(self, found: list[_Found], problems: dict,
                  sandbox_problem: str) -> dict[str, _Place]:
        """Yours that were offered and whose file or folder is still there, but didn't load, as
        after an edit that broke one while Tux was off or grants a new host refuses. One taken
        out frees its place, and one that is there but no longer a skill is said."""
        loaded = {item.key for item in found}
        places = {}
        for key, place in self.offered.items():
            path = Path(key)
            if key in loaded or place.count is None or path.parent != self.user:
                continue
            if os.path.lexists(path) and not (sandbox_problem and path.is_dir()):
                places[key] = place
                problems.setdefault(key, NOT_A_SKILL)
        return places

    def _order(self, item: _Found) -> tuple[int, bool]:
        """Where it is counted, and among yours that keep their place, those edited since they
        were last offered after the others."""
        return self._place(item), self._edited(item)

    def _edited(self, item: _Found) -> bool:
        """One of yours that is new, or edited since it was last offered, as far as is known."""
        place = self.offered.get(item.key)
        return item.manifest["source"] == "yours" and (
            place is None or not place.digest or place.digest != _text_digest(item.text))

    def _place(self, item: _Found) -> int:
        """Where it is counted: built-in and bundled ones, and yours as loaded or smaller, keep
        the order they were found in; then yours that grew; then new ones."""
        if item.manifest["source"] != "yours":
            return KEPT_PLACE
        if item.key not in self.offered:
            return NEW
        before = self.offered[item.key].count
        return GROWN if before is not None and len(item.manifest["actions"]) > before \
            else KEPT_PLACE

    @staticmethod
    def _clash(item: _Found, ids, actions, tools, builtin_ids) -> str:
        manifest = item.manifest
        skill_id = manifest["id"]
        added = item.kind != "builtin"
        if added and skill_id in builtin_ids:
            return f"The id {skill_id} belongs to a skill built into Tuxistant."
        if skill_id in ids:
            return f"Another skill already has the id {shown(skill_id, 100)}."
        for name in manifest["actions"]:
            if name in actions or (added and name in HANDLERS):
                return f"Another skill already has the action {shown(name, 60)}."
            if name.replace(".", "_") in tools:
                return f"The action {shown(name, 60)} would have the same tool name as another."
        return ""

    # Building and swapping

    def _build(self, chosen: list[_Found], problems: dict, sandbox_problem: str):
        """The new Skills, the processes it uses and the keys of what keeps its place in the
        budget. Nothing is swapped yet."""
        running, proxies, origins = {}, {}, {}
        manifests, offered = [], {}
        for item in chosen:
            if item.kind == "recipe":
                proxies.update(self.recipe_handlers(item.manifest))
            elif item.kind == "code":
                if sandbox_problem:
                    problems[item.key] = sandbox_problem
                    continue
                try:
                    entry = self._process_for(item)
                except Exception as e:     # it still counts, so it keeps its place too
                    problems[item.key] = f"It could not be started: {e}"
                    offered[item.key] = item
                    continue
                running[item.key] = entry
                for name in item.manifest["actions"]:
                    proxies[name] = _proxy(entry, name)
            manifests.append(item.manifest)
            origins[item.manifest["id"]] = item.origin()
            offered[item.key] = item
        handlers = ChainMap(proxies, HANDLERS)  # HANDLERS read live, so tests can patch it
        try:
            skills = Skills(config=self.config, manifests=manifests, handlers=handlers,
                            origins=origins)
        except Exception:
            self._drop_unused(running)
            raise
        return skills, running, offered

    def _sandbox_problem(self) -> str:
        if self.launcher != self._launch:
            return ""
        try:
            from . import skillbox
            if skillbox.available():
                return ""
        except Exception:
            log.exception("the skill sandbox could not be checked")
        return "Skills with code of their own need bubblewrap, and it isn't working here."

    def _process_for(self, item: _Found) -> _Running:
        old = self.running.get(item.key)
        if old and old.digest == item.digest and old.manifest == item.manifest:
            return old
        if self.launcher != self._launch:
            return _Running(item.manifest, item.folder, item.digest,
                            self.launcher(item.manifest, item.folder))
        code = self._snapshot(item)
        entry = _Running(item.manifest, item.folder, item.digest,
                         self.launcher(item.manifest, code), code=code)
        self.using[code] += 1
        return entry

    def _snapshot(self, item: _Found) -> Path:
        """A copy of the folder as it was checked. The sandbox binds the live folder otherwise,
        so a restart after an idle stop or a crash would run whatever is there now, under the
        manifest of the version that passed."""
        final = self.code_root / item.digest
        if final.is_dir():
            return final
        self.code_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".new-", dir=self.code_root))
        try:
            shutil.copytree(item.folder, temp / "code", symlinks=True, ignore=_skip_names)
            if fingerprint(temp / "code") != item.digest:
                raise ValueError("It changed while it was loading. It loads again once the "
                                 "changes stop.")
            try:
                os.rename(temp / "code", final)
            except OSError:
                if not final.is_dir():
                    raise
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        return final

    def _release(self, entry: _Running) -> None:
        if entry.code is None:
            return
        self.using[entry.code] -= 1
        if self.using[entry.code] <= 0:
            del self.using[entry.code]
            shutil.rmtree(entry.code, ignore_errors=True)

    def _prune_code(self) -> None:
        """Copies left from before this start, which no process uses."""
        try:
            leftovers = [path for path in self.code_root.iterdir() if path not in self.using]
        except OSError:
            return
        for path in leftovers:
            shutil.rmtree(path, ignore_errors=True)

    def _drop_unused(self, running: dict):
        for key, entry in running.items():
            if self.running.get(key) is not entry:
                self._retire(entry)

    def _swap(self, skills: Skills, running: dict):
        old, self.running = self.running, running
        self._current = skills
        kept = set(map(id, running.values()))
        for entry in old.values():
            if id(entry) not in kept:
                self._retire(entry)

    def _retire(self, entry: _Running):
        """A turn still holding the old Skills gets a plain answer rather than the old code.
        Calls already running finish first."""
        entry.retired = True
        try:
            task = asyncio.get_running_loop().create_task(self._stop_one(entry))
        except RuntimeError:
            self.retired.append(entry)
            return
        self.stopping.add(task)
        task.add_done_callback(self.stopping.discard)

    async def _stop_one(self, entry: _Running):
        try:
            await entry.process.stop(graceful=True)
        except Exception:
            log.exception("stopping the skill %s failed", entry.manifest["id"])
        self._release(entry)

    # Processes and their settings

    def _values(self, manifest: dict) -> dict:
        if manifest.get("source") != "bundled":     # load_code_manifest refuses these already
            return {}
        return {key: self.config[key] for key in manifest["config"] if key in SKILL_CONFIG_KEYS}

    def _launch(self, manifest: dict, folder: Path):
        from . import mcp, skillbox
        state = _state_dir(manifest["id"])

        def launch():
            return skillbox.prepare(folder, manifest["run"], manifest["permissions"], state)
        # The name is what errors say out loud, so it is the skill's name rather than its id.
        idle = BUNDLED_IDLE_S if manifest["source"] == "bundled" else USER_IDLE_S
        return mcp.SkillProcess(manifest["name"], launch, self._values(manifest),
                                call_timeout_s=manifest["timeout_s"], idle_s=idle)

    async def config_changed(self, key: str) -> None:
        """A setting a skill was given has changed, so its process hears the new value."""
        if key not in SKILL_CONFIG_KEYS:
            return
        for entry in list(self.running.values()):
            if key in entry.manifest["config"] and not entry.retired:
                try:
                    await entry.process.set_config(self._values(entry.manifest))
                except Exception:
                    log.exception("passing settings to %s failed", entry.manifest["id"])


class Offline:
    """Stands in for a code skill's process where none may run: tuxistant doctor and the live
    evaluations, which must never reach a light or a server through a skill."""

    def __init__(self, manifest: dict, _folder=None):
        self.name = manifest["name"]

    async def list_tools(self) -> list[dict]:
        return []

    async def call(self, tool: str, arguments: dict, timeout=None) -> dict:
        return {"error": f"{self.name} is not running here."}

    async def set_config(self, values: dict) -> None:
        pass

    async def stop(self, graceful: bool = True) -> None:
        pass


def offline(config, **folders) -> SkillHost:
    """The skills the daemon would load, checked and listed, with no code skill started and no
    recipe sending anything."""
    from . import recipes
    host = SkillHost(config, launcher=Offline, recipes=recipes.offline_handlers, **folders)
    host.grants.writable = False    # a check allows nothing; the daemon does that
    host.reload()
    return host


def _a_recipe(entry: Path) -> bool:
    """A YAML file at the top of your skills folder. A link is let through to be refused with a
    reason, rather than skipped without a word."""
    return entry.suffix == ".yaml" and not ignored(entry.name) and (
        entry.is_symlink() or entry.is_file())


def _almost_a_recipe(entry: Path) -> bool:
    """coins.yml or coins.json would otherwise be skipped without a word."""
    return entry.suffix != ".yaml" and entry.suffix.casefold() in (".yaml", ".yml", ".json") and (
        not ignored(entry.name))


def _namespace(action: str) -> str:
    """The first word of the tool name the model sees, plural or not: files.delete and
    file_trash.now are file_delete and file_trash_now to it, beside file_find and file_trash."""
    word = re.split(r"[._]", action, maxsplit=1)[0].casefold()
    return word[:-1] if word.endswith("s") and len(word) > 1 else word


def over_budget(yours: int, count: int) -> str:
    """Why a skill with count actions doesn't fit beside the yours already counted. One with
    more than the budget never fits as it is, whatever else is taken out, so it is told how many
    it may keep beside the rest instead."""
    if count > USER_TOOLS:
        alone = (f"It has {count} actions, and the skills you add may bring {USER_TOOLS} at most "
                 "between them, as more would make Tux worse at choosing the right one.")
        if not yours:
            return f"{alone} Take some out of it, leaving {USER_TOOLS} or fewer, to use it."
        if yours < USER_TOOLS:
            return (f"{alone} Skills you added already bring {_counted(yours)}, so take some "
                    f"out of it, leaving {USER_TOOLS - yours} or fewer, to use it beside them.")
        return (f"{alone} Skills you added already bring {_counted(yours)}, so to use it, take "
                "one of them out as well as some of its own.")
    return (f"Skills you added already bring {_counted(yours)}, and its {_counted(count)} would "
            f"take them past {USER_TOOLS}, which would make Tux worse at choosing the right one. "
            "Take one of them out to use this one.")


def _counted(count: int) -> str:
    return f"{count} action" if count == 1 else f"{count} actions"


def _still_held(place: _Place, item: _Found) -> _Place:
    """What one of yours holds once its edit is allowed: the id and actions it was last offered
    with that the edit still has."""
    manifest = item.manifest
    return replace(place, skill_id=place.skill_id if place.skill_id == manifest["id"] else "",
                   actions=tuple(name for name in place.actions if name in manifest["actions"]))


def _holders(item: _Found, reserved: dict[str, _Place]) -> set[str]:
    """The keys of others of yours that hold the id or an action of this one, whether they
    load or keep their place."""
    tools = {name.replace(".", "_") for name in item.manifest["actions"]}
    return {key for key, place in reserved.items()
            if key != item.key and (item.manifest["id"] == place.skill_id or tools & {
                name.replace(".", "_") for name in place.actions})}


def _names(manifest: dict) -> set:
    """The id and the tool names a skill takes, which no other may have."""
    return {f"id:{manifest['id']}"} | {name.replace(".", "_") for name in manifest["actions"]}


def _owners(item: _Found, owners: dict[str, str]) -> set[str]:
    return {owners[name] for name in _names(item.manifest)
            if owners.get(name, item.key) != item.key}


def _held_reason(item: _Found, place: _Place, holder: str, freed=False) -> str:
    """Which name the skill in holder has, and until when; freed, when an edit to it without
    that has loaded, in a pass of _settle that couldn't free it for this one at once."""
    until = ("which an edit to it that has just loaded gives up, so it is free from the next "
             "time Tux reads your skills folder, which it does again at once." if freed else
             "which it keeps until it is taken out of your skills folder, or an edit to it "
             "without that loads.")
    where = shown(Path(holder).name, 60)
    if item.manifest["id"] == place.skill_id:
        return f"The skill in {where} has the id {shown(place.skill_id, 100)}, {until}"
    theirs = {name.replace(".", "_"): name for name in place.actions}
    name = next(name for name in item.manifest["actions"] if name.replace(".", "_") in theirs)
    if name in place.actions:
        return f"The skill in {where} has the action {shown(name, 60)}, {until}"
    return (f"The action {shown(name, 60)} would have the same tool name as "
            f"{shown(theirs[name.replace('.', '_')], 60)} in {where}, {until}")


def _lookalike(manifest: dict, ours: set) -> str:
    for name in manifest["actions"]:
        if _namespace(name) in ours:
            return (f"Its action {shown(name, 60)} starts like one built into Tuxistant, so the "
                    "model could take it for one.")
    return ""


def _proxy(entry: _Running, tool: str):
    """An action of a code skill, as a handler. Skills.execute has already applied its gates."""
    timeout = entry.manifest["timeout_s"]

    async def call(args: dict, _config, session) -> dict:
        if entry.retired:
            return {"error": UPDATED}
        session.busy(START_TIMEOUT_S + timeout + BUSY_MARGIN_S)    # the first call starts it
        result = await entry.process.call(tool, args, timeout)
        return _shaped(entry.manifest["name"], result)
    return call


def _shaped(name: str, result) -> dict:
    """What a skill answers goes to the model and the history, so it is held to what a
    description is: visible characters, no angle brackets, a short error, and a size the model
    can take."""
    unusable = {"error": f"{name} gave an answer Tuxistant can't use."}
    if not isinstance(result, dict):
        return unusable
    try:
        cleaned = _cleaned(result, 0)
        if "error" in result:
            error = result["error"]
            error = " ".join(_unbracketed(error).split()) if isinstance(error, str) else ""
            cleaned["error"] = printable(error, MAX_ERROR_CHARS) or f"{name} could not do that."
        size = len(json.dumps(cleaned, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return unusable
    if size > MAX_RESULT_CHARS:
        return {"error": f"{name} gave an answer too long to use."}
    return cleaned


def _cleaned(value, depth: int):
    if depth > MAX_RESULT_DEPTH:
        raise ValueError("too deep")
    if isinstance(value, dict):
        return {_cleaned(str(key), depth): _cleaned(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_cleaned(item, depth + 1) for item in value]
    if isinstance(value, str):
        return printable(_unbracketed(value), MAX_RESULT_CHARS)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValueError("not JSON")
