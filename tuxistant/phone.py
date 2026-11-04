"""Ringing, battery and sending to a phone through GSConnect.

GSConnect's actions are fire and forget: Activate returns before anything has reached the phone,
and only a desktop notification says how it went. So results say "sending", never "sent".
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from . import files
from . import system
from .gate import MAX_DETAIL_CHARS, Ask, shown

NAME = "org.gnome.Shell.Extensions.GSConnect"
ROOT = "/org/gnome/Shell/Extensions/GSConnect"
DEVICE = f"{NAME}.Device"
THIS_COMPUTER = {"laptop", "this laptop", "my laptop", "computer", "this computer", "my computer",
                 "macbook", "this", "pc", "desktop"}
TIMEOUT_S = 5
PHONES = ("phone", "tablet")
NOT_RUNNING = "GSConnect isn't running, so I can't reach your phone."
CANNOT = {"ring": "can't ring right now", "battery": "doesn't share its battery level",
          "shareFile": "can't receive files right now"}

# What a small model puts around a name, or in place of one when nobody was named.
LEADING = re.compile(r"^(?:(?:my|your|our|the|this|that|a|an|users|user['’]s)\s+)+")
NOT_NAMES = {"phone", "mobile", "cell", "cellphone", "smartphone", "telephone", "handset",
             "device", "android", "default", "none", "null"}
BRANDS = {"samsung": "galaxy", "google": "pixel"}   # people say the maker, devices show the line


def _read(path: str, interface: str, method: str):
    """A call's reply. No auto-start: looking at the device list must not launch GSConnect."""
    command = ["busctl", "--user", "--json=short", "--auto-start=no", f"--timeout={TIMEOUT_S}",
               "call", NAME, path, interface, method]
    try:
        done = subprocess.run(command, capture_output=True, check=True, timeout=TIMEOUT_S + 1)
        return json.loads(done.stdout)["data"][0]
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError) as e:
        raise ValueError(NOT_RUNNING) from e


def _known() -> list[dict]:
    objects = _read(ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
    found = []
    try:
        for path, interfaces in objects.items():
            if DEVICE in interfaces:
                value = {key: entry["data"] for key, entry in interfaces[DEVICE].items()}
                found.append({"id": value["Id"], "name": value["Name"], "type": value["Type"],
                              "connected": value["Connected"], "paired": value["Paired"],
                              "path": path})
    except (AttributeError, KeyError, TypeError) as e:
        raise ValueError(NOT_RUNNING) from e
    return sorted(found, key=lambda device: device["name"].casefold())


def devices() -> list[dict]:
    try:
        return [{key: device[key] for key in ("id", "name", "type", "connected", "paired")}
                for device in _known()]
    except ValueError:
        return []


def _tokens(text: str) -> list[str]:
    """Letters and digits apart, so "s23" and "S 23" are the same thing."""
    return re.findall(r"[^\W\d_]+|\d+", re.sub(r"['’]s\b", "", text.casefold()))


def _fits(device: dict, words: list[str]) -> bool:
    theirs = {*_tokens(device["name"]), device["type"]}
    return all(word in theirs or BRANDS.get(word) in theirs for word in words)


def _meant(known: list[dict], wanted: str) -> list[dict] | None:
    """The devices wanted can refer to, or None if it names nothing: "my phone" doesn't say which.
    Every word has to fit, or an invented "Galaxy S24" would reach a Galaxy S23."""
    exact = [d for d in known if wanted.casefold() in (d["name"].casefold(), d["id"].casefold())]
    words = [word for word in _tokens(LEADING.sub("", " ".join(wanted.casefold().split())))
             if word not in NOT_NAMES]
    if exact or not words:
        return exact or None
    return [device for device in known if _fits(device, words)]


def _ready(device: dict) -> bool:
    """Other people's phones on the same network show up connected, but never paired."""
    return device["connected"] and device["paired"]


def _choices(known: list[dict]) -> str:
    names = [device["name"] for device in known]
    return " or ".join(filter(None, [", ".join(names[:-1]), names[-1]]))


def _pick(args: dict, config) -> dict:
    known = _known()
    found = _meant(known, " ".join(str(args.get("device") or "").split()))
    if found is None:
        found = _meant(known, config["phone_device"])
    if found is None:
        found = [device for device in known if device["type"] in PHONES]
    mine = [device for device in found if device["paired"]] or found
    ready = [device for device in mine if _ready(device)]
    if len(ready) == 1:
        return ready[0]
    if ready:
        raise ValueError(f"Which device do you mean: {_choices(ready)}?")
    if mine:
        state = "connected right now" if mine[0]["paired"] else "paired with this computer"
        raise ValueError(f"{mine[0]['name']} isn't {state}.")
    others = [device for device in known if _ready(device)]
    raise ValueError(f"No device matches. The choices are {_choices(others)}." if others
                     else "No phone is connected.")


def _action(device: dict, name: str) -> list:
    """The action's state. Unpaired devices lack most actions, and GSConnect turns them off when
    a device drops, so this is asked before anything claims to have happened."""
    offered = _read(device["path"], "org.gtk.Actions", "DescribeAll").get(name)
    if not offered or not offered[0]:
        raise ValueError(f"{device['name']} {CANNOT[name]}.")
    return offered[2]


def _activate(device: dict, action: str, parameter: str = "[]") -> None:
    command = ["gdbus", "call", "--session", "--dest", NAME, "--object-path", device["path"],
               "--timeout", str(TIMEOUT_S), "--method", "org.gtk.Actions.Activate",
               action, parameter, "{}"]
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=TIMEOUT_S + 1)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError("GSConnect had a problem, so I can't say if that reached "
                         f"{device['name']}.") from e


def _stat(path: Path) -> os.stat_result:
    try:
        return path.stat()
    except OSError as e:
        raise ValueError(f"I can't read {path.name}.") from e


def _stamp(info: os.stat_result) -> tuple:
    """Same stamp, same file. ctime because, unlike mtime, nothing can set it back."""
    return info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns


def _size(count: int) -> str:
    if count < 1000:
        return f"{count} byte{'' if count == 1 else 's'}"
    for unit in ("KB", "MB", "GB"):
        count /= 1000
        if count < 1000:
            return f"{count:.1f} {unit}"
    return f"{count / 1000:.1f} TB"


def send(args: dict, config, session) -> Ask:
    device = _pick(args, config)
    _action(device, "shareFile")
    path = files.resolve(args.get("file"), session)     # the real file, links followed
    info = _stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"I can only send a single file, and {path.name} isn't one.")
    if not os.access(path, os.R_OK):
        raise ValueError(f"I can't read {path.name}.")
    name = device["name"]

    def run() -> dict:
        if _stamp(_stat(path)) != _stamp(info):
            raise ValueError(f"{path.name} changed after the question was asked, so I didn't "
                             "send it.")
        _action(device, "shareFile")
        # A URI is plain ASCII, so a quote in a file name can't break out of the GVariant text.
        _activate(device, "shareFile", f"[<('{path.as_uri()}', false)>]")
        return {"sending": path.name, "device": name}

    # Short lines first: a very long path is what gets cut if the card runs out of room.
    return Ask(f'Send "{shown(path.name)}" to {shown(name)}?',
               f"To: {shown(name)}\nSize: {_size(info.st_size)}\nFile: {shown(path, MAX_DETAIL_CHARS)}",
               run)


def ring(args: dict, config, _session=None) -> dict:
    device = _pick(args, config)
    _action(device, "ring")
    _activate(device, "ring")
    return {"ringing": device["name"]}


def battery(args: dict, config, _session=None) -> dict:
    # The model reaches for this whatever the device: "this laptop" means this computer.
    if str(args.get("device") or "").casefold() in THIS_COMPUTER:
        return {"device": "this computer", "battery": system.FACTS["battery"]()}
    device = _pick(args, config)
    state = _action(device, "battery")
    charging, _icon, level, _seconds = state[0]["data"] if state else (False, "", -1, 0)
    if level < 0:   # what GSConnect holds until the phone first reports
        raise ValueError(f"{device['name']} hasn't reported its battery level yet.")
    return {"device": device["name"],
            "battery": f"{level} percent, {'charging' if charging else 'not charging'}"}


HANDLERS = {"phone.send": send, "phone.ring": ring, "phone.battery": battery}
