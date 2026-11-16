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

from . import serverstatus
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


def _ip(tailscale: bool = False) -> dict:
    """The address this computer uses, which is the one people mean, not every Docker bridge.
    Tailscale's only when switched on: said aloud on a call or a recording, it maps your tailnet."""
    try:
        route = json.loads(_tool("ip", "-j", "route", "get", "1.1.1.1"))[0]
        found = {"address": route["prefsrc"], "connection": route.get("dev", "")}
    except (ValueError, LookupError, TypeError):
        return {"address": "not connected"}
    if not tailscale:
        return found
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


# Words for this computer in "on", which the model fills in even when nobody named one.
HERE = {"this", "my", "the", "our", "own", "here", "local", "localhost", "computer", "desktop",
        "laptop", "pc", "machine", "system", "device"}
NOTHING = ("", "true", "false", "none", "null")
ONLY_TWO = "I can only check this computer and the server in Settings."
SETTINGS_HERE = "The server in Settings is this computer."


def _settings_host(config) -> str | None:
    try:
        return serverstatus.host_of(config["server"]) if config is not None else None
    except (TypeError, KeyError):
        return None


def _only_two(config) -> str:
    """What else can be checked: with no server in Settings, only this computer."""
    try:
        server = str(config["server"] or "").strip() if config is not None else ""
    except (TypeError, KeyError):
        server = ""
    return ONLY_TWO if server else serverstatus.NO_SERVER


def _server_host(config) -> str | None:
    """The host of the server in Settings, when it is another computer."""
    host = _settings_host(config)
    return None if not host or serverstatus.is_local(host) else host.casefold()


def _server_is_here(config) -> bool:
    """The server in Settings is this computer, as when LocalAI runs here."""
    host = _settings_host(config)
    return bool(host) and serverstatus.is_local(host)


def _said(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text))


def _names_the_server(said: str, config) -> bool:
    """The server's name, or its first label, as zephyr for zephyr.lan, with my, home or server
    around it and in any spelling: "my home nas" for nas.lan, "zephyr nas" for zephyr-nas.lan.
    An address only as itself."""
    host = _server_host(config)
    return bool(host) and serverstatus.names_host(said, host)


def _here(on: str) -> bool:
    """An on that means this computer: none, "my laptop", "localhost", this computer's name."""
    return (on in NOTHING or serverstatus.is_local(on)
            or bool(_words(on)) and _words(on) <= HERE)


def on_server(args: dict, config) -> bool:
    """Asked about the server outright: by an on with "server" in it, such as my home server or
    the servers, or the server's name, or by "server" with only words like these as the name,
    unless on means this computer. The model copies on as it heard it, so "my minecraft server"
    counts too."""
    on, name = _said(args.get("on")), _said(args.get("name"))
    return (bool(_words(on) & serverstatus.SERVER_WORDS) or _names_the_server(on, config)
            or (on in NOTHING and serverstatus.names_a_server(name)))


def _maybe_another(args: dict, config) -> bool:
    """An on with more in it than your server, as "my work server": it may mean another one,
    so the answer names the one in Settings."""
    on = _said(args.get("on"))
    rest = _words(on) - serverstatus.AROUND_A_HOST
    return on not in NOTHING and bool(rest) and not _names_the_server(on, config)


def _whole_list(name: str) -> bool:
    """Nothing in particular, such as "server" or "containers". docker alone is a program."""
    return serverstatus.asks_for_all(name) and name not in ("docker", "ssh", "systemd")


def _from_the_server(args: dict, config) -> bool:
    """on_server, unless the server in Settings is this computer: then a name is looked for
    here, which is the same machine, and only the whole list goes to serverstatus to be said
    it can't be had."""
    return on_server(args, config) and (not _server_is_here(config)
                                        or _whole_list(_said(args.get("name"))))


def _named_server(args: dict, config) -> bool:
    """The server named as the name, as the model does as often as in on, but only with no on:
    "on this computer" means here, even on a server called plex."""
    return _said(args.get("on")) in NOTHING and _names_the_server(_said(args.get("name")), config)


def about_server(args: dict, config) -> bool:
    """Whether running() answers from the server, when nothing here has the name. Named, it is
    looked for here first, as a server called docker.lan or plex is named like a program."""
    return _from_the_server(args, config) or _named_server(args, config)


def _on_the_server(args: dict, config, session, missed_here: str | None = None) -> dict:
    """serverstatus's one fixed command, said by the app. The name is matched here, never sent."""
    return serverstatus.running(args, config, session if session is not None else Session(),
                                missed_here, _maybe_another(args, config))


def _any_dashes(name: str) -> str:
    """A pattern for pgrep that takes name with or without - or _ between its letters, as the
    model says localai for the local-ai process that LocalAI's image runs. systemctl takes only
    the exact name of a unit."""
    letters = [re.sub(r"([.+])", r"\\\1", c) for c in name if c not in "-_"]
    return "[-_]?".join(letters)


def _here_by_name(name: str) -> bool:
    active = any(_tool("systemctl", *scope, "is-active", "--", name).strip() == "active"
                 for scope in ([], ["--user"]))
    return active or bool(_tool("pgrep", "-i", "-x", "--", _any_dashes(name)).strip())


def running(args: dict, config, session=None) -> dict:
    """Whether a service or program is up, here or on the server. Here, a name goes no further
    than these two read-only tools, and this needs the real system, which the shell sandbox
    hides."""
    if _from_the_server(args, config):
        return _on_the_server(args, config, session)
    name, on = _said(args.get("name")), _said(args.get("on"))
    # Here on_server means the server in Settings is this computer, which "my server" means,
    # but "my work server" may be another one.
    settings_here = on_server(args, config)
    if settings_here and not _maybe_another(args, config):
        on = ""
    if not SERVICE_NAME.fullmatch(name):
        if _named_server(args, config):     # such as fd00::5, which no program here is called
            return _on_the_server(args, config, session)
        if not _here(on):
            # and nothing was looked at here
            raise ValueError(f"{ONLY_TWO} {SETTINGS_HERE}" if settings_here else
                             _only_two(config))
        raise ValueError("Say which program or service to check.")
    found = _here_by_name(name)
    if not found and _named_server(args, config):
        # A label such as plex, or docker for docker.lan, may be a program after all, so the
        # answer says it isn't here. A host with a dot or an address is no program's name.
        return _on_the_server(args, config, session, None if "." in name else name)
    if not _here(on):
        # Another computer, which can't be asked: a plain yes or no would be about the wrong
        # one. What this computer runs is said all the same, and raising skips files_read.
        if session is not None:
            session.tainted = True
        if settings_here:
            said = f"{name} is" if found else f"nothing called {name} is"
            raise ValueError(f"{ONLY_TWO} The server in Settings is this computer, and {said} "
                             "running on it.")
        only = _only_two(config)
        raise ValueError(f"{only} On this computer, {name} is running." if found else
                         f"{only} Nothing called {name} is running on this computer.")
    return {"name": name, "running": found}


def _says_tailscale(config) -> bool:
    try:
        return config["say_tailscale_address"] is True   # not "false" from a hand-written file
    except (TypeError, KeyError):
        return False


def info(args: dict, config, _session=None) -> dict:
    about = args.get("about")
    wanted = [about] if about in FACTS else list(FACTS)    # a model that invents a word gets it all
    return {name: _ip(_says_tailscale(config)) if name == "ip" else FACTS[name]()
            for name in wanted}


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
