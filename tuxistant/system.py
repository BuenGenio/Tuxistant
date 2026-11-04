"""Facts about this computer and its folders. These read the system directly: the shell sandbox
hides the network and other processes, so it would give confident wrong answers here."""
import json
import os
import platform
import re
import stat
import subprocess
import time
from pathlib import Path

from .files import _size, _tilde, resolve
from .gate import Session, shown

TOOL_TIMEOUT_S = 5
SCAN_LIMIT_S = 12       # someone is waiting to hear the answer
SCAN_BATCH = 200        # folders read before the next one gets a turn
TOP_FOLDERS = 5
TOP_PROCESSES = 5
POWER = Path("/sys/class/power_supply")
SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,63}")
STOPPED_EARLY = ("Counting stopped after a few seconds, so the real numbers are higher and the "
                 "order may change.")


def _tool(*command: str) -> str:
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TOOL_TIMEOUT_S).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    words = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts if n]
    return " ".join(words[:2]) or "less than a minute"


def _uptime() -> str:
    return _span(float(Path("/proc/uptime").read_text().split()[0]))


def _ip() -> dict:
    """The address this computer uses, which is the one people mean, not every Docker bridge."""
    try:
        route = json.loads(_tool("ip", "-j", "route", "get", "1.1.1.1"))[0]
        found = {"address": route["prefsrc"], "connection": route.get("dev", "")}
    except (ValueError, LookupError, TypeError):
        return {"address": "not connected"}
    try:
        for link in json.loads(_tool("ip", "-j", "-4", "addr", "show", "tailscale0")):
            found["tailscale"] = link["addr_info"][0]["local"]
    except (ValueError, LookupError, TypeError):
        pass
    return found


def _memory() -> str:
    fields = dict(re.findall(r"^(\w+):\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M))
    total, free = int(fields["MemTotal"]) * 1024, int(fields["MemAvailable"]) * 1024
    return f"{_size(total - free)} of {_size(total)} in use"


def _cpu() -> str:
    load = float(Path("/proc/loadavg").read_text().split()[0])
    return f"about {min(100, round(100 * load / (os.cpu_count() or 1)))} percent busy"


def _battery() -> str:
    for supply in sorted(POWER.glob("*")) if POWER.is_dir() else []:
        try:
            if (supply / "type").read_text().strip() == "Battery":
                level = (supply / "capacity").read_text().strip()
                return f"{level} percent, {(supply / 'status').read_text().strip().lower()}"
        except OSError:
            continue
    return "no battery found"


def _wifi() -> str:
    for line in _tool("nmcli", "-t", "-f", "active,ssid", "dev", "wifi").splitlines():
        if line.startswith("yes:"):
            return shown(line[4:])
    return "not on Wi-Fi"


def _busiest() -> list[str]:
    lines = _tool("ps", "-eo", "comm", "--sort=-pcpu", "--no-headers").splitlines()
    return [shown(line.strip()) for line in lines if line.strip()][:TOP_PROCESSES]


def _os() -> str:
    release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', Path("/etc/os-release").read_text(), re.M))
    return f"{release.get('PRETTY_NAME', 'Linux')}, kernel {platform.release()}"


FACTS = {"uptime": _uptime, "ip": _ip, "memory": _memory, "cpu": _cpu, "battery": _battery,
         "wifi": _wifi, "busiest": _busiest, "os": _os}


def running(args: dict, _config, _session=None) -> dict:
    """Whether a service or program is up. A name goes no further than these two read-only tools,
    and this needs the real system, which the shell sandbox hides."""
    name = str(args.get("name") or "").strip().casefold()
    if not SERVICE_NAME.fullmatch(name):
        raise ValueError("Say which program or service to check.")
    active = any(_tool("systemctl", *scope, "is-active", "--", name).strip() == "active"
                 for scope in ([], ["--user"]))
    return {"name": name, "running": active or bool(_tool("pgrep", "-i", "-x", "--", name).strip())}


def info(args: dict, _config, _session=None) -> dict:
    about = args.get("about")
    wanted = [about] if about in FACTS else list(FACTS)    # a model that invents a word gets it all
    return {name: FACTS[name]() for name in wanted}


class _Tally:
    def __init__(self, path: str):
        self.pending = [path]
        self.files = self.size = 0

    def step(self, device: int):
        """Adds up one folder without following links or leaving the disk."""
        try:
            entries = list(os.scandir(self.pending.pop()))
        except OSError:
            return
        for entry in entries:
            try:
                found = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(found.st_mode):
                if found.st_dev == device:
                    self.pending.append(entry.path)
            else:
                self.files += 1
                self.size += found.st_size


def _scan(tallies: list[_Tally], device: int, deadline: float) -> bool:
    """Takes turns between the folders: done one at a time, a slow one early in the alphabet would
    use up the time and the biggest folder might never be counted. False if time ran out."""
    while any(tally.pending for tally in tallies):
        for tally in tallies:
            if time.monotonic() > deadline:
                return False
            for _ in range(min(SCAN_BATCH, len(tally.pending))):
                tally.step(device)
    return True


def _target(value, session) -> Path:
    if not value or str(value).strip().casefold() in ("~", "home", "my home", "home folder"):
        return Path.home()
    path = resolve(value, session)
    if not path.is_dir():
        raise ValueError(f"{path.name} isn't a folder.")
    return path


def folders(args: dict, _config, session=None) -> dict:
    folder = _target(args.get("folder"), session or Session())
    device = folder.stat().st_dev
    loose, tallies = _Tally(str(folder)), {}
    loose.pending = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        raise RuntimeError("I can't read that folder.") from None
    for entry in entries:
        try:
            found = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(found.st_mode):
            loose.files += 1
            loose.size += found.st_size
        elif found.st_dev == device:
            tallies[entry.name] = _Tally(entry.path)
    finished = _scan(list(tallies.values()), device, time.monotonic() + SCAN_LIMIT_S)
    biggest = sorted(tallies.items(), key=lambda item: item[1].size, reverse=True)[:TOP_FOLDERS]
    # In the key itself: the model repeats what it is given and drops a caveat beside it.
    result = {"folder": shown(_tilde(folder)),
              "files": loose.files + sum(t.files for t in tallies.values()),
              "size": _size(loose.size + sum(t.size for t in tallies.values())),
              "biggest" if finished else "biggest_so_far": [
                  {"name": shown(name), "size": _size(t.size), "files": t.files}
                  for name, t in biggest]}
    if not finished:
        result["note"] = STOPPED_EARLY
    return result


HANDLERS = {"system.info": info, "system.running": running, "disk.folders": folders}
