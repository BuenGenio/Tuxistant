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
import hashlib
import json
import logging
import math
import os
import re
import shutil
import tempfile
from collections import ChainMap, Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

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
LOADED = "skills-loaded.json"   # yours that were loaded, by name, with how many actions each had
SKILL_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+")
ACTION = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
PARAM = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
FIELDS = {"id", "name", "description", "version", "run", "permissions", "config", "timeout_s",
          "actions"}
ACTION_FIELDS = {"description", "params", "required"}
REFUSED_PERMISSIONS = ("shell", "files_read", "files_write", "clipboard")
UPDATED = "That skill was just updated. Please ask again."
USER_TOOLS = 6      # actions from skills you added, code and recipes together
OVER_BUDGET = ("Tux already has six actions from skills you added, and more would make it worse "
               "at choosing the right one. Take one out to use this one.")
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


def _actions(manifest: dict) -> dict:
    actions = manifest.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise ValueError("It declares no actions.")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"It declares more than {MAX_ACTIONS} actions.")
    checked = {}
    for name, action in actions.items():
        action_name(name)
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
            "actions": _actions(manifest), "source": "bundled" if bundled else "yours"}


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
        self._records: dict | None = None
        self._dirty = False

    @property
    def records(self) -> dict:
        if self._records is None:
            self._records = self._read()
        return self._records

    def _read(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("could not read %s, so skills are allowed again from now", self.file)
            return {}
        return {key: value for key, value in data.items() if isinstance(value, dict)} \
            if isinstance(data, dict) else {}

    def problem(self, manifest: dict, folder: Path) -> str:
        record = self.records.get(manifest["id"])
        if not record or record.get("removed"):
            return ""
        more = _beyond(manifest, record)
        if not more:
            return ""
        if record.get("folder") != folder.name:   # it stays refused while this folder is there
            record["folder"], self._dirty = folder.name, True
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
        fresh = {"permissions": dict(manifest["permissions"]),
                 "config": list(manifest.get("config", [])), "folder": folder.name}
        if record is None:
            self.records[manifest["id"]], self._dirty = fresh, True
            return False
        if record.get("removed"):
            more = _beyond(manifest, record)
            self.records[manifest["id"]] = fresh if more else {
                **record, "removed": False, "folder": folder.name}
            self._dirty = True
            return bool(more)
        return False

    def gone(self, user: Path) -> None:
        for record in self.records.values():
            if not record.get("removed") and not (user / str(record.get("folder"))).exists():
                record["removed"], self._dirty = True, True

    def save(self) -> None:
        if not self._dirty or not self.writable:
            return
        try:
            _write_json(self.file, self.records)
            self._dirty = False
        except OSError as e:
            log.warning("could not save what skills were allowed: %s", e)


def _write_json(file: Path, data) -> None:
    """Whole or not at all, so a crash halfway leaves the last copy."""
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{file.stem}-", dir=file.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=1, sort_keys=True)
        os.replace(temp, file)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_loaded(file: Path) -> dict[str, int | None]:
    """Names and the number of actions each was offered with. A list, as an earlier 0.5 build
    kept, gives names alone, counted as they are now."""
    try:
        data = json.loads(file.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("could not read %s, so the skills you added load in name order", file)
        return {}
    if isinstance(data, list):
        return {name: None for name in data if _plain_name(name)}
    if not isinstance(data, dict):
        return {}
    return {name: count for name, count in data.items() if _plain_name(name)
            and type(count) is int and 0 < count <= USER_TOOLS}


def _plain_name(name) -> bool:
    return isinstance(name, str) and name not in ("", ".", "..") and "/" not in name


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
        # what keeps its place in the budget, by key, with how many actions it was offered with:
        # what the last reload loaded, or at start yours that loaded before the restart
        self.offered: dict[str, int | None] = {
            str(self.user / name): count for name, count in self.saved.items()}
        self.last_offered: dict[str, _Found] = {}   # by key, as the last reload offered them
        self.running: dict[str, _Running] = {}      # by folder
        self.last_good: dict[str, _Found] = {}      # by folder or file
        self.stopping: set[asyncio.Task] = set()
        self.retired: list[_Running] = []           # retired with no loop to stop them on
        self.watcher = None
        self.grants = _Grants(self.user.parent / GRANTS)
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
            self.watcher = Watcher([self.builtin, self.bundled, self.user], self._changed)
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

    def _changed(self) -> None:
        problems = self.reload()
        if problems:
            log.info("skill problems: %s", problems)

    def reload(self) -> dict[str, str]:
        """Builds a new Skills from what is on disk and swaps it in. A skill that fails keeps its
        last version that worked, if it has one. Never raises: a reload runs on every save."""
        problems: dict[str, str] = {}
        try:
            found = self._scan(problems)
            sandbox_problem = self._sandbox_problem()
            chosen = self._resolve(found, problems, sandbox_problem)
            self._grant(chosen)
            skills, running, offered = self._build(chosen, problems, sandbox_problem)
        except Exception as e:
            log.exception("reloading the skills failed")
            problems["skills"] = f"The skills could not be reloaded: {e}"
            self.problems = problems
            return dict(problems)
        self._swap(skills, running)
        self.problems, self.last_offered = problems, offered
        self.offered = {key: len(item.manifest["actions"]) for key, item in offered.items()}
        self._save_loaded()
        return dict(problems)

    def _save_loaded(self) -> None:
        """The order of the budget has to outlast a restart, or a skill refused for being one too
        many would take the place of one that worked, at the next login."""
        names = {Path(key).name: count for key, count in self.offered.items()
                 if Path(key).parent == self.user}
        if names == self.saved or not self.grants.writable:
            return
        try:
            _write_json(self.loaded, names)
            self.saved = names
        except OSError as e:
            log.warning("could not save which skills were loaded: %s", e)

    # Reading what is on disk

    def _scan(self, problems: dict) -> list[_Found]:
        found = []
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
        live = {item.key for item in found} | set(problems)
        self.last_good = {key: item for key, item in self.last_good.items() if key in live}
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
        except Exception as e:
            problem = str(e) if isinstance(e, ValueError) else f"It could not be loaded: {e}"
            last = self.last_good.get(key)
            if last and Path(key).exists():
                problems[key] = f"{problem} The last version that worked is still in use."
                return [last]
            problems[key] = problem
            return []
        self.last_good[key] = item
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
        if problem:
            raise ValueError(problem)
        digest, files = fingerprinted(folder)
        return _Found(str(folder), manifest, folder.resolve(), digest, "code", text,
                      tuple(sorted(files)))

    def _recipe(self, file: Path) -> _Found:
        from . import recipes
        manifest, text = _as_loaded(file, lambda: recipes.load(file))
        problem = self.grants.problem(manifest, file)
        if problem:
            raise ValueError(problem)
        return _Found(str(file), manifest, kind="recipe", text=text)

    def _grant(self, chosen: list[_Found]) -> None:
        for item in chosen:
            if item.kind != "builtin" and item.manifest["source"] == "yours" and (
                    self.grants.grant(item.manifest, Path(item.key))) and item.kind == "code":
                self._clear_state(item.manifest["id"])  # a recipe keeps nothing
        self.grants.gone(self.user)
        self.grants.save()

    def _clear_state(self, skill_id: str) -> None:
        """Only the default launcher gives a skill this folder; a test's launcher has none."""
        if self.launcher == self._launch and self.grants.writable:
            shutil.rmtree(_state_dir(skill_id), ignore_errors=True)

    # Deciding who gets what

    def _resolve(self, found: list[_Found], problems: dict, sandbox_problem: str) -> list[_Found]:
        """Yours that were loaded keep their place, ahead of new ones, so a file dropped in is
        the one refused rather than one that was working. One that has more actions than it was
        offered with comes after those that don't, so what it added is refused, never another
        skill, and the version offered stays in use while there is one. Room is held for that
        version of each one still to come, so one's growth never takes another's place."""
        ids, actions, tools = set(), set(), set()
        builtin_ids = {item.manifest["id"] for item in found if item.kind == "builtin"}
        ours = {_namespace(name) for name in HANDLERS} | {
            _namespace(name) for item in found if item.manifest["source"] != "yours"
            for name in item.manifest["actions"]}
        yours = 0
        chosen = []
        held = {item.key: len(self.last_offered[item.key].manifest["actions"])
                for item in found
                if self._place(item) == GROWN and item.key in self.last_offered}

        def refusal(item: _Found) -> str:
            mine = item.manifest["source"] == "yours"
            unrunnable = mine and item.kind == "code" and sandbox_problem
            needed = yours + len(item.manifest["actions"]) + sum(held.values())
            return unrunnable or self._clash(item, ids, actions, tools, builtin_ids) or (
                mine and _lookalike(item.manifest, ours)) or (
                OVER_BUDGET if mine and needed > USER_TOOLS else "")

        for item in sorted(found, key=self._place):
            held.pop(item.key, None)
            problem = refusal(item)
            if problem == OVER_BUDGET and self._place(item) == GROWN:
                problem = GREW
                before = self.last_offered.get(item.key)
                if before is not None and not refusal(before):
                    # a broken edit's own reason, which says the same of the version kept
                    problems.setdefault(item.key, f"{GREW} {KEPT}")
                    item, problem = before, ""
            if problem:
                problems[item.key] = problem
                continue
            yours += len(item.manifest["actions"]) if item.manifest["source"] == "yours" else 0
            ids.add(item.manifest["id"])
            actions.update(item.manifest["actions"])
            tools.update(name.replace(".", "_") for name in item.manifest["actions"])
            chosen.append(item)
        return chosen

    def _place(self, item: _Found) -> int:
        """Where it is counted: built-in and bundled ones, and yours as loaded or smaller, keep
        the order they were found in; then yours that grew; then new ones."""
        if item.manifest["source"] != "yours":
            return KEPT_PLACE
        if item.key not in self.offered:
            return NEW
        before = self.offered[item.key]
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
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.exception("telling others the skills changed failed")

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
