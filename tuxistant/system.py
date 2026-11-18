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
from .gate import APP_SAYS, Session, Speak, shown

TOOL_TIMEOUT_S = 5
SCAN_LIMIT_S = 12       # someone is waiting to hear the answer
SCAN_BATCH = 200        # folders read before the next one gets a turn
TOP_FOLDERS = 5
TOP_PROCESSES = 5
POWER = Path("/sys/class/power_supply")
SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,63}")
STOPPED_EARLY = ("Counting stopped after a few seconds, so the real numbers are higher and the "
                 "order may change.")
SAID_HERE = 4           # services and processes said, of each
LIST_UNITS = ("list-units", "--type=service", "--no-legend", "--plain", "--no-pager")
PROCESS_LINE = re.compile(r"([0-9]+) (.{1,15})")     # a pid and a name of up to 15 characters
# A pid and a kernel thread's name, which Linux keeps whole, up to 63 characters, where it keeps
# 15 of a user process's: kworker/R-nvme-wq or kworker/u8:3-events_power_efficient.
KERNEL_THREAD_LINE = re.compile(r"([0-9]+) (.{16,63})")
PROC = Path("/proc")
PF_KTHREAD = 0x00200000     # the flag in /proc/<pid>/stat that Linux sets on a kernel thread


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
    """As said, without an 's after a word: "the nas's docker" is the nas docker."""
    return " ".join(w for w in map(serverstatus.bare, str(value or "").casefold().split()) if w)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text))


def _names_the_server(said: str, config) -> bool:
    """The server's name, or its first label, as zephyr for zephyr.lan, with my, home or server
    around it and in any spelling: "my home nas" for nas.lan, "zephyr nas" for zephyr-nas.lan.
    An address only as itself. The server as Settings has it, with its port or scheme, as
    zephyr.lan:8080 or http://zephyr.lan, is it too."""
    host = _server_host(config)
    if not host:
        return False
    given = serverstatus.host_of(said)
    return (serverstatus.names_host(said, host)
            or given is not None and serverstatus.is_own_name(given, host))


def _here(on: str) -> bool:
    """An on that means this computer: none, "my laptop", "localhost", this computer's name,
    with "my" or "box" around it as the server's may have."""
    return (on in NOTHING or serverstatus.is_local(on)
            or serverstatus.is_local(serverstatus._without(on, serverstatus.AROUND_A_HOST))
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


def _on_the_server(args: dict, config, session, here: str | None = None) -> dict:
    """serverstatus's one fixed command, said by the app. The name is matched here, never sent.
    here is what this computer lists, when it was asked first."""
    return serverstatus.running(args, config, session, here, _maybe_another(args, config))


def _read(*command: str) -> tuple[int | None, str]:
    """A tool that reads: its exit code and output, or None for one that couldn't run or took
    too long. Unlike _tool, it tells an empty list from no list. The output is decoded here, not
    with text=True, whose universal newlines would make a carriage return a line break."""
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _pattern(name: str) -> str:
    """name as pgrep's extended regular expression, under serverstatus's match rule: with any of
    -, _, . or a space between its letters, so localai finds the local-ai process that LocalAI's
    image runs. pgrep -i takes any case. A + is itself; nothing else in a name is special."""
    return "[-_. ]*".join("[+]" if c == "+" else c for c in serverstatus.squeezed(name))


def _services_here(name: str) -> tuple[list, int, bool]:
    """The services systemd lists here, the system's and yours, that have name in theirs, how
    many of the two lists were read, and whether both were read whole."""
    found, read, whole = [], 0, True
    for scope in ([], ["--user"]):
        code, output = _read("systemctl", *scope, *LIST_UNITS)
        if code != 0:
            whole = False
            continue
        read += 1
        listing = serverstatus.units(output.splitlines(), False)
        found += [unit for unit in listing.rows if serverstatus.matches(name, unit)]
        whole = whole and not listing.odd
    return found, read, whole


def _kernel_thread(pid: str) -> bool:
    """Whether Linux flags pid as a kernel thread in its /proc/<pid>/stat: False for one that is
    gone or can't be read. Only the flags are read, after the name in parentheses, which may
    hold a ) itself, and never the command line, which a kernel thread doesn't have."""
    try:
        status = (PROC / pid / "stat").read_bytes()
        return bool(int(status[status.rindex(b")") + 1:].split()[6]) & PF_KTHREAD)
    except (OSError, ValueError, IndexError):
        return False


def _pid_and_name(line: str) -> bool:
    """Whether a line of pgrep's is a pid and a process's own name as Linux keeps it: up to 15
    characters for a user process's, or up to 63 for a kernel thread's, which Linux keeps whole
    and which has no command line, so no argument can be in it."""
    if PROCESS_LINE.fullmatch(line):
        return True
    thread = KERNEL_THREAD_LINE.fullmatch(line)
    return thread is not None and _kernel_thread(thread[1])


def _processes_here(name: str) -> tuple[list[str], int] | None:
    """Each process pgrep -f matches: by its command line, or by its own name where it has none,
    as a kernel thread or a zombie. Each is said by its pid and its own name as pgrep -l gives
    them, with how many lines weren't that, or None when pgrep didn't list them. Never the
    command line itself: another program's arguments may hold a password or a token, as in
    mysql -pS3cret, and what Tux says is heard, sent to the speech server and kept in History.
    A line is read only as a pid and a name of up to 15 characters, as long as Linux keeps a
    user process's own name, or of up to 63 for a process Linux flags as a kernel thread, whose
    name it keeps whole. So a pgrep whose -l gives more for any other process, such as its whole
    command line, gets that line counted, not said. Lines are split at newlines only, as pgrep
    ends each with one, and any other line break in a name is said as an escape."""
    code, output = _read("pgrep", "-l", "-i", "-f", "--", _pattern(name))
    if code not in (0, 1):
        return None
    lines = [line for line in output.split("\n") if line]
    rows = [line for line in lines if _pid_and_name(line)]
    return rows, len(lines) - len(rows)


def _processes(count: int) -> str:
    return f"{count} process" + ("" if count == 1 else "es")


def here_said(name: str) -> tuple[bool, str]:
    """What this computer lists with name in it, relayed: the services as systemd gives them and
    the processes by pid and name as pgrep does, and whether there was any."""
    units, read, whole = _services_here(name)
    found = _processes_here(name)
    processes, odd = found if found is not None else (None, 0)
    if not units and whole and processes == [] and not odd:
        return False, (f"No service systemd lists here, and no process's command line, has {name} "
                       "in it.")
    said = []
    if units:
        said.append("systemd lists " + serverstatus.said_rows(
            [serverstatus.said_row(unit) for unit in units], SAID_HERE))
    elif whole:
        said.append(f"no service systemd lists has {name} in its name")
    if processes:
        said.append(f"these processes have {name} in their command line, or in their name where "
                    "they have none: " + serverstatus.said_rows(processes, SAID_HERE))
    elif processes is None:
        said.append("pgrep didn't list the processes")
    elif not odd:
        said.append(f"no process's command line has {name} in it")
    if odd:
        said.append(f"pgrep listed {_processes(odd)} in a way I couldn't read")
    text = f"On this computer, {', and '.join(said)}."
    if not read:
        text += " I couldn't read systemd's lists here."
    elif not whole:
        text += " I couldn't read all of systemd's list here."
    return bool(units or processes or odd), text


def running(args: dict, config, session=None) -> dict:
    """What lists a service or program by that name, here or on the server, relayed and said by
    the app: a process's name is any program's text. Here, a name goes no further than these
    read-only tools, and this needs the real system, which the shell sandbox hides."""
    session = session if session is not None else Session()
    if _from_the_server(args, config):
        return _on_the_server(args, config, session)
    name, on = _said(args.get("name")), _said(args.get("on"))
    # Here on_server means the server in Settings is this computer, which "my server" means,
    # but "my work server" may be another one.
    settings_here = on_server(args, config)
    if settings_here and not _maybe_another(args, config):
        on = ""
    name = name.removesuffix(".service")
    if not SERVICE_NAME.fullmatch(name) or len(serverstatus.squeezed(name)) < 2:
        if _named_server(args, config):     # such as fd00::5, which no program here is called
            return _on_the_server(args, config, session)
        if not _here(on):
            # and nothing was looked at here
            raise ValueError(f"{ONLY_TWO} {SETTINGS_HERE}" if settings_here else
                             _only_two(config))
        raise ValueError("Say which program or service to check.")
    found, said = here_said(name)
    session.tainted = True      # what runs here is yours, whatever comes after
    if not found and _named_server(args, config):
        # A label such as plex, or docker for docker.lan, may be a program after all, so the
        # answer says what is here first. A host with a dot or an address is no program's name.
        return _on_the_server(args, config, session, None if "." in name else said)
    if not _here(on):
        # Another computer, which can't be asked: what this computer lists is said all the same,
        # after a sentence that says it is this one.
        only = f"{ONLY_TWO} {SETTINGS_HERE}" if settings_here else _only_two(config)
        said = f"{only} {said}"
    session.after.append(Speak(shown(said, serverstatus.MAX_SAID_CHARS),
                               serverstatus.voice(config)))
    return {APP_SAYS: True}


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
