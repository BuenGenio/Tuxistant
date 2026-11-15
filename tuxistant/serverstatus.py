"""What runs on your own server: its docker containers and systemd services, read over ssh.

One fixed, read-only command goes to the host of the server in Settings, and nothing the model
wrote goes with it: a name to look for is matched here, once the answer is back. ssh takes the
port and user from ~/.ssh/config, and gets no agent, so the key has to work on its own. What
your ssh_config runs itself, such as a ProxyCommand, still runs.
What the server sends is someone else's text. It is capped, each name has to look like a name,
and the app says the answer itself, so none of it reaches a model with tools.
"""
import ipaddress
import os
import re
import selectors
import shlex
import signal
import socket
import subprocess
import time

from .gate import APP_SAYS, Speak, shown
from .servers import SERVER, SERVER_NAME_RE

CONNECT_TIMEOUT_S = 5
TIMEOUT_S = 10              # someone is waiting to hear the answer
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ERROR_BYTES = 4096
MAX_LINES = 300             # per section
SAID_CONTAINERS = 8
SAID_SERVICES = 6
SAID_MATCHES = 4
MAX_NAME_CHARS = 40

# Run by the remote user's own shell, whichever it is, so it hands the work to sh at once.
# Killing ssh here doesn't end what it started there, as there is no terminal to hang up, so a
# docker that hangs is given a timeout there too, where one works: an old busybox one fails
# the "true" first and is left out.
INNER = ("export LC_ALL=C; T=; timeout 8 true >/dev/null 2>&1 && T=\"timeout 8\"; "
         "echo @docker; $T docker ps --format \"{{.Names}}|{{.Image}}|{{.Status}}\" 2>&1; "
         "echo \"@exit $?\"; "
         "echo @systemd; $T systemctl list-units --type=service --state=running --no-legend "
         "--plain --no-pager 2>/dev/null; echo \"@exit $?\"")
REMOTE = "exec sh -c " + shlex.quote(INNER)
# Over what ~/.ssh/config says: a RemoteCommand would replace the command, a ControlMaster
# would leave a connection open for your own sessions to reuse, an IdentityAgent is an agent,
# ForkAfterAuthentication would leave an ssh that killing its group can't reach, SessionType
# none would wait for the timeout, UpdateHostKeys would write known_hosts, and ClearAllForwardings
# leaves a Tunnel. LogLevel ERROR leaves out a long banner but not why ssh failed. ssh before 8.7
# doesn't know two of these, hence IgnoreUnknown, first; it is "*" because the first one given
# replaces yours, and an option only your own let through, such as macOS's UseKeychain, would
# stop ssh.
SSH_OPTIONS = ("-T", "-x", "-a", "-o", "IgnoreUnknown=*", "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-o", "StrictHostKeyChecking=yes",
               "-o", "UpdateHostKeys=no", "-o", "ClearAllForwardings=yes", "-o", "Tunnel=no",
               "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none", "-o", "ControlMaster=no",
               "-o", "ControlPath=none", "-o", "IdentityAgent=none",
               "-o", "ForkAfterAuthentication=no", "-o", "SessionType=default",
               "-o", "LogLevel=ERROR")
PASSED_ENV = ("PATH", "HOME", "USER", "LANG")
NOT_FOUND = 127             # what sh says for a command that isn't there

NO_SERVER = "There's no server set in Settings, so I don't know which one to check."
ON_THIS_COMPUTER = ("The server in Settings is this computer. I can't list what runs here, but "
                    "I can check one program or service by its name.")
BAD_HOST = "The server address in Settings isn't one I can reach over ssh."
NO_SSH = "ssh isn't installed on this computer, so I can't reach your server."
NO_KEY = ("Your server turned down this computer's ssh key. I can't type a passphrase or use the "
          "ssh agent, so I need a key that works on its own.")
HOST_KEY = ("This computer doesn't know your server's ssh host key yet. Connect to it once with "
            "ssh in a terminal, then ask me again.")
HOST_KEY_CHANGED = ("Your server's ssh host key has changed since this computer last saw it, which "
                    "can mean someone is in the way. Check it on the server before you trust the "
                    "new one.")
CANT_CONNECT = "I couldn't connect to your server over ssh. It may be off, or out of reach from here."
TIMED_OUT = "Your server took too long to answer, so I stopped."
UNREADABLE = "Your server answered, but not in a way I could read."
NEITHER = "Your server has neither docker nor systemd, so I can't tell what's running there."
BAD_NAME = "Say the name of one program or service, in a word or two."
DOCKER_REFUSED = ("Your user on the server isn't allowed to use docker, so I can't list its "
                  "containers.")

NAME = re.compile(r"[a-z0-9][a-z0-9 ._-]{0,%d}" % (MAX_NAME_CHARS - 1))
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IMAGE = re.compile(r"[A-Za-z0-9_./:@-]{1,200}")
STATUS = re.compile(r"[A-Za-z0-9 ().,:-]{1,60}")
UNIT = re.compile(r"([A-Za-z0-9][A-Za-z0-9@._:+-]{0,99})\.service")
EXIT = re.compile(r"@exit ([0-9]{1,3})")
IPV6 = re.compile(r"\[([0-9a-fA-F:.]+)\]")
LOCAL = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback")
# Words around a name rather than in it, as in "ssh myserver docker ps": with only these
# left, the whole list was asked for.
AROUND = {"server", "servers", "my", "our", "own", "the", "your", "on", "is", "are", "what",
          "which", "ssh", "docker", "ps", "systemctl", "systemd", "containers", "container",
          "services", "service", "all", "everything", "anything", "running"}
SERVER_WORDS = {"server", "servers"}
# Part of the system itself, so they are counted but not named: nobody asks after getty.
PLUMBING = ("systemd-", "getty@", "serial-getty@", "user@", "dbus", "polkit", "rsyslog", "cron",
            "atd", "udisks2", "irqbalance", "multipathd", "packagekit", "accounts-daemon",
            "networkd-dispatcher", "unattended-upgrades", "upower", "rtkit-daemon",
            "avahi-daemon", "wpa_supplicant", "modemmanager", "networkmanager", "thermald",
            "chrony", "containerd", "lvm2", "qemu-guest-agent", "fwupd", "colord",
            "power-profiles-daemon", "bolt", "kerneloops", "gdm", "snapd", "ssh", "sshd")


class Unanswered(Exception):
    """Why the server can't be read, as a sentence to say."""


def host_of(server) -> str | None:
    """The host part of the server in Settings, or None for one ssh can't be given safely.
    It is passed as one argument, so it must not be able to look like an option."""
    rest = str(server or "").strip().rstrip("/").rpartition("://")[2]
    match = SERVER.fullmatch(rest)
    if not match:
        return None
    host = match[1]
    if bracketed := IPV6.fullmatch(host):
        return bracketed[1]
    if host.startswith("-") or not SERVER_NAME_RE.fullmatch(host):
        return None
    return host


def _this_computer() -> set[str]:
    """This computer's own name, with and without its domain and as .local, which Debian and
    Ubuntu map to 127.0.1.1 and mDNS answers: ssh to it comes back here."""
    name = socket.gethostname().rstrip(".").casefold()
    short = name.partition(".")[0]
    return {name, short, f"{short}.local"} - {"", ".local"}


def is_local(host: str) -> bool:
    """Short forms count, as ssh reads "0" and "127.1" as addresses here too."""
    host = host.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except (OSError, ValueError):       # ValueError: a NUL in what the model wrote
            return host in LOCAL or host in _this_computer()
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_unspecified


def is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _label(host: str) -> str | None:
    """The first label of a name, as stormy for stormy.lan. An address has none: 10 is no name
    for 10.0.0.5."""
    host = host.casefold()
    return None if is_address(host) else host.partition(".")[0]


def own_names(host: str) -> set[str]:
    """What the server is called: its host, and the first label of a name."""
    return {host.casefold(), _label(host)} - {None}


def target(config) -> str:
    """The host to ask, or Unanswered."""
    server = str((config["server"] if config is not None else "") or "").strip()
    if not server:
        raise Unanswered(NO_SERVER)
    host = host_of(server)
    if host is None:
        raise Unanswered(BAD_HOST)
    if is_local(host):
        raise Unanswered(ON_THIS_COMPUTER)
    return host


def command(host: str) -> list[str]:
    return ["ssh", *SSH_OPTIONS, "--", host, REMOTE]


def _env() -> dict:
    """No agent socket, no display, nothing else of the daemon's."""
    env = {name: os.environ[name] for name in PASSED_ENV if os.environ.get(name)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _run(argv: list[str], env: dict) -> tuple[int, bytes, bytes]:
    """(exit code, output, errors), each capped. A server that sends too much is cut off there.
    Done once ssh has exited and its output has ended, even if something ssh started, such as a
    ProxyCommand, still holds its error pipe: that is killed with the rest of the group."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError:
        raise Unanswered(NO_SSH) from None
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, (output, MAX_OUTPUT_BYTES))
            selector.register(process.stderr, selectors.EVENT_READ, (errors, MAX_ERROR_BYTES))
            if _drain(selector, process, deadline):     # enough to answer from
                _kill(process)
        return process.wait(max(0.1, deadline - time.monotonic())), bytes(output), bytes(errors)
    except subprocess.TimeoutExpired:
        raise Unanswered(TIMED_OUT) from None
    finally:
        _kill(process)
        process.stdout.close()
        process.stderr.close()


def _drain(selector, process, deadline: float) -> bool:
    """Reads both pipes until the output ends and ssh has exited. True if the output was full."""
    ended = False
    while selector.get_map() and not (ended and process.poll() is not None):
        left = deadline - time.monotonic()
        if left <= 0:
            raise Unanswered(TIMED_OUT)
        for key, _ in selector.select(min(left, 0.05) if ended else left):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                ended = ended or key.fileobj is process.stdout
                continue
            buffer, cap = key.data
            if key.fileobj is not process.stdout:
                buffer += chunk
                del buffer[:-cap]       # the end, where ssh says why it gave up
                continue
            buffer += chunk[:cap - len(buffer)]
            if len(buffer) >= cap:
                return True
    return False


def _kill(process) -> None:
    """The whole group, as what ssh started may outlive it, then ssh itself if that failed."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    if process.poll() is None:
        try:
            process.kill()
            process.wait(2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _ssh_failed(errors: bytes) -> str:
    """ssh's own complaint, read only to pick a sentence: none of it is said."""
    text = errors.decode("utf-8", "replace")
    if "Permission denied" in text or "Too many authentication failures" in text:
        return NO_KEY
    # Before the next one: ssh ends this warning with "Host key verification failed." too.
    if ("REMOTE HOST IDENTIFICATION HAS CHANGED" in text
            or "has changed and you have requested strict checking" in text):
        return HOST_KEY_CHANGED
    if "Host key verification failed" in text or "host key is known" in text:
        return HOST_KEY
    return CANT_CONNECT


def _sections(text: str) -> dict:
    """{"docker": (exit code, lines), "systemd": ...} for each section the answer finished."""
    found, current, lines = {}, None, []
    for line in text.splitlines():
        if line in ("@docker", "@systemd"):
            current, lines = line[1:], []
        elif current and (code := EXIT.fullmatch(line)):
            found[current] = (int(code[1]), lines)
            current = None
        elif current and len(lines) < MAX_LINES:
            lines.append(line)
    return found


def _containers(lines: list[str]) -> list[dict]:
    found = []
    for line in lines:
        name, _, rest = line.partition("|")
        image, _, status = rest.partition("|")
        if CONTAINER.fullmatch(name):
            found.append({"name": name, "image": image if IMAGE.fullmatch(image) else "",
                          "status": status if STATUS.fullmatch(status) else ""})
    return found


def _services(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        words = line.split()
        if words and (unit := UNIT.fullmatch(words[0])):
            found.append(unit[1])
    return found


def read(host: str) -> dict:
    """What runs on host, or Unanswered. "docker" and "systemd" are lists, or None when that
    one isn't there, with why in "docker_missing" or "systemd_missing"."""
    code, output, errors = _run(command(host), _env())
    if code == 255:
        raise Unanswered(_ssh_failed(errors))
    sections = _sections(output.decode("utf-8", "replace"))
    if not sections:
        raise Unanswered(UNREADABLE)
    result = {}
    for part, parse in (("docker", _containers), ("systemd", _services)):
        status, lines = sections.get(part, (None, []))
        result[part] = parse(lines) if status == 0 else None
        result[f"{part}_missing"] = status == NOT_FOUND
    # docker's own complaint, read only to pick a sentence
    result["docker_refused"] = result["docker"] is None and any(
        "permission denied" in line.casefold() for line in sections.get("docker", (0, []))[1])
    if result["docker"] is None and result["systemd"] is None and (
            result["docker_missing"] and result["systemd_missing"]):
        raise Unanswered(NEITHER)
    return result


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _words(name) -> list[str]:
    """Its words, without "home" before server: a home server is a server."""
    words = [w.strip(".,?!") for w in str(name or "").casefold().split()]
    return [w for w, after in zip(words, words[1:] + [""])
            if w and not (w == "home" and after in SERVER_WORDS)]


def asks_for_all(name) -> bool:
    """Only words around a name, as in "my home server" or "docker containers"."""
    return all(w in AROUND for w in _words(name))


def names_a_server(name) -> bool:
    """A name that is "server" with words around it, such as "the servers" or "our home server"."""
    return asks_for_all(name) and any(w in SERVER_WORDS for w in _words(name))


def wanted(args: dict, host: str) -> str | None:
    """The one thing asked after, checked, or None for the whole list. Never sent anywhere.
    A host with a dot, or an address, is no program's name and is left out; a single label is
    kept, as on a server called plex or localai.lan, plex or localai is what is asked after."""
    around = AROUND | ({host.casefold()} - {_label(host)})
    name = " ".join(w for w in _words(args.get("name")) if w not in around)
    if not name:
        return None
    if not NAME.fullmatch(name) or len(_key(name)) < 2:
        raise Unanswered(BAD_NAME)
    return name


def _listed(names: list[str], limit: int) -> str:
    said = names[:limit]
    if len(names) > limit:
        said.append(f"{len(names) - limit} more")
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]


def _is(count: int) -> str:
    return "is" if count == 1 else "are"


def _plural(count: int, one: str) -> str:
    return f"{count} {one}" + ("" if count == 1 else "s")


def _docker_unread(found: dict) -> str:
    if found["docker_missing"]:
        return "Docker isn't installed on your server."
    return DOCKER_REFUSED if found["docker_refused"] else "Docker on your server didn't answer me."


def _restarting(container: dict) -> bool:
    """docker ps lists a container that keeps crashing too, as "Restarting (1) 5 seconds ago"."""
    return container["status"].casefold().startswith("restarting")


def _paused(container: dict) -> bool:
    """And a paused one, as "Up 2 hours (Paused)"."""
    return "(paused)" in container["status"].casefold()


def _up(container: dict) -> bool:
    return not _restarting(container) and not _paused(container)


def _status(container: dict) -> str:
    """As said after its name: not the exit code in "Restarting (1) 5 seconds ago"."""
    if _paused(container):
        return ", paused"
    status = re.sub(r" ?\([0-9]+\)", "", container["status"])
    return f", {status[0].lower()}{status[1:]}" if status else ""


def _everything(found: dict) -> str:
    said = []
    containers = found["docker"]
    if containers is None:
        said.append(_docker_unread(found))
    elif not containers:
        said.append("No docker containers are running on your server.")
    else:
        up = [c["name"] for c in containers if _up(c)]
        if up:
            said.append(f"On your server, {_plural(len(up), 'docker container')} "
                        f"{_is(len(up))} running: {_listed(up, SAID_CONTAINERS)}.")
        else:
            said.append("No docker containers are really up on your server.")
        sick = [c["name"] for c in containers if "unhealthy" in c["status"].casefold()]
        if sick:
            said.append(f"Unhealthy: {_listed(sick, SAID_CONTAINERS)}.")
        if again := [c["name"] for c in containers if _restarting(c)]:
            said.append(f"Restarting again and again, so not really up: "
                        f"{_listed(again, SAID_CONTAINERS)}.")
        if paused := [c["name"] for c in containers if _paused(c)]:
            said.append(f"Paused, so not running: {_listed(paused, SAID_CONTAINERS)}.")
    services = found["systemd"]
    if services is None:
        said.append("Your server doesn't use systemd, so I can't list its services."
                    if found["systemd_missing"] else "I couldn't read your server's system "
                    "services.")
    elif services:
        own = [s for s in services if not s.casefold().startswith(PLUMBING)]
        count = f"There {_is(len(services))} {_plural(len(services), 'system service')} running"
        plumbing = "which is" if len(services) == 1 else "all"
        said.append(f"{count}, among them {_listed(own, SAID_SERVICES)}."
                    if own else f"{count}, {plumbing} part of the system itself.")
    return " ".join(said)


def _in(key: str, text: str) -> bool:
    """A short key only as a word of its own: "ai" is not in localai, though it is in open-ai."""
    if len(key) >= 3:
        return key in _key(text)
    return key in re.split(r"[^a-z0-9]+", text.casefold())


def _match(name: str, *texts: str) -> str | None:
    """How name is in any of texts: "whole" for the whole name or every word of it, "part" for
    only some of its words of three letters or more, as homepage is for home assistant."""
    words = [key for key in map(_key, name.split()) if key]
    if any(_in(_key(name), text) for text in texts) or (
            len(words) > 1 and all(any(_in(key, text) for text in texts) for key in words)):
        return "whole"
    if any(_in(key, text) for key in words if len(key) >= 3 for text in texts):
        return "part"
    return None


def _seen(found: dict, name: str) -> tuple[list[str], bool, bool]:
    """What on the server matches name, as said, whether any of it is really up, and whether
    it matched the whole name: if nothing does, what matched some of its words."""
    seen, up = {"whole": [], "part": []}, {"whole": False, "part": False}
    for c in found["docker"] or []:
        if how := _match(name, c["name"], c["image"].rpartition("/")[2].partition(":")[0]):
            seen[how].append(f"the docker container {c['name']}{_status(c)}")
            up[how] = up[how] or _up(c)
    for s in found["systemd"] or []:
        if how := _match(name, s):
            seen[how].append(f"the service {s}")
            up[how] = True
    how = "whole" if seen["whole"] else "part"
    return seen[how], up[how], how == "whole"


def _one(found: dict, name: str) -> str:
    seen, up, whole = _seen(found, name)
    if seen and whole:
        start = "Yes, on your server I see" if up else "Not really: on your server I see"
        return f"{start} {_listed(seen, SAID_MATCHES)}."
    if seen:
        return f"I don't see {name} on your server, but I see {_listed(seen, SAID_MATCHES)}."
    if found["docker"] is None and found["systemd"] is None:
        return f"I couldn't check for {name} on your server. {_everything(found)}"
    if found["docker"] is None:
        return (f"I don't see {name} among the services running on your server, and I couldn't "
                "check docker there.")
    if found["systemd"] is None:
        return f"I don't see {name} among the docker containers running on your server."
    return f"I don't see {name} among the docker containers or services running on your server."


def answer(args: dict, config, session=None) -> str:
    """The sentence to say, from daemon code alone. Marks the conversation as having read
    something private once the server has answered: what you run there is yours."""
    host = target(config)
    name = wanted(args, host)
    found = read(host)
    if session is not None:
        session.tainted = True
    if name in own_names(host) and not _seen(found, name)[0]:
        name = None     # the server's own name, with nothing there called that
    return shown(_one(found, name) if name else _everything(found), 600)


def running(args: dict, config, session) -> dict:
    """system.running on the server: the answer, or why there is none, said by the app."""
    try:
        text = answer(args, config, session)
    except Unanswered as why:
        text = str(why)
    session.after.append(Speak(text, config["voice"]))
    return {APP_SAYS: True}

