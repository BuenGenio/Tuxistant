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
INNER = ("export LC_ALL=C; "
         "echo @docker; docker ps --format \"{{.Names}}|{{.Image}}|{{.Status}}\" 2>&1; "
         "echo \"@exit $?\"; "
         "echo @systemd; systemctl list-units --type=service --state=running --no-legend --plain "
         "--no-pager 2>/dev/null; echo \"@exit $?\"")
REMOTE = "exec sh -c " + shlex.quote(INNER)
# Over what ~/.ssh/config says: a RemoteCommand would replace the command, a ControlMaster
# would leave a connection open for your own sessions to reuse, and an IdentityAgent is an agent.
SSH_OPTIONS = ("-T", "-x", "-a", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
               "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
               "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none", "-o", "ControlMaster=no",
               "-o", "ControlPath=none", "-o", "IdentityAgent=none")
PASSED_ENV = ("PATH", "HOME", "USER", "LANG")
NOT_FOUND = 127             # what sh says for a command that isn't there

NO_SERVER = "There's no server set in Settings, so I don't know which one to check."
ON_THIS_COMPUTER = ("The server in Settings is this computer. Ask me what's running on this "
                    "computer instead.")
BAD_HOST = "The server address in Settings isn't one I can reach over ssh."
NO_SSH = "ssh isn't installed on this computer, so I can't reach your server."
NO_KEY = ("Your server turned down this computer's ssh key. I can't type a passphrase or use the "
          "ssh agent, so I need a key that works on its own.")
HOST_KEY = ("This computer doesn't know your server's ssh host key yet. Connect to it once with "
            "ssh in a terminal, then ask me again.")
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
AROUND = {"server", "servers", "my", "the", "your", "on", "is", "are", "what", "which", "ssh",
          "docker", "ps", "systemctl", "systemd", "containers", "container", "services",
          "service", "all", "everything", "anything", "running"}
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


def is_local(host: str) -> bool:
    """Short forms count, as ssh reads "0" and "127.1" as addresses here too."""
    host = host.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return host in LOCAL
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_unspecified


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
            buffer += chunk[:cap - len(buffer)]
            if key.fileobj is process.stdout and len(buffer) >= cap:
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


def wanted(args: dict, host: str) -> str | None:
    """The one thing asked after, checked, or None for the whole list. Never sent anywhere.
    The host's first label stays: on localai.lan, "localai" is what is asked after."""
    around = AROUND | {host.casefold()}
    name = " ".join(w for w in str(args.get("name") or "").casefold().split() if w not in around)
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


def _everything(found: dict) -> str:
    said = []
    containers = found["docker"]
    if containers is None:
        said.append(_docker_unread(found))
    elif not containers:
        said.append("No docker containers are running on your server.")
    else:
        up = [c["name"] for c in containers if not _restarting(c)]
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
    services = found["systemd"]
    if services is None:
        said.append("Your server doesn't use systemd, so I can't list its services."
                    if found["systemd_missing"] else "I couldn't read its system services.")
    elif services:
        own = [s for s in services if not s.casefold().startswith(PLUMBING)]
        count = f"There {_is(len(services))} {_plural(len(services), 'system service')} running"
        said.append(f"{count}, among them {_listed(own, SAID_SERVICES)}."
                    if own else f"{count}, all part of the system itself.")
    return " ".join(said)


def _keys(name: str) -> set[str]:
    """The whole name, and each word of it long enough not to match everything."""
    return {_key(name)} | {key for key in map(_key, name.split()) if len(key) >= 3}


def _matches(keys: set[str], *texts: str) -> bool:
    return any(key in _key(text) for key in keys for text in texts)


def _seen(found: dict, name: str) -> tuple[list[str], bool]:
    """What on the server matches name, as said, and whether any of it is really up."""
    keys, seen, up = _keys(name), [], False
    for c in found["docker"] or []:
        if _matches(keys, c["name"], c["image"].rpartition("/")[2].partition(":")[0]):
            status = c["status"]
            seen.append(f"the docker container {c['name']}"
                        + (f", {status[0].lower()}{status[1:]}" if status else ""))
            up = up or not _restarting(c)
    services = [s for s in found["systemd"] or [] if _matches(keys, s)]
    return seen + [f"the service {s}" for s in services], up or bool(services)


def _one(found: dict, name: str) -> str:
    seen, up = _seen(found, name)
    if seen:
        start = "Yes, on your server I see" if up else "Not really: on your server I see"
        return f"{start} {_listed(seen, SAID_MATCHES)}."
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
    if name == host.casefold().partition(".")[0] and not _seen(found, name)[0]:
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

