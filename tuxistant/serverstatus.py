"""What runs on your own server: its docker containers and systemd services, read over ssh.

One fixed, read-only command goes to the host of the server in Settings, and nothing the model
wrote goes with it: a name to look for is matched here, once the answer is back. ssh takes the
port and user from ~/.ssh/config, and gets no agent, so the key has to work on its own.
What the server sends is someone else's text. It is capped, each name has to look like a name,
and the app says the answer itself, so none of it reaches a model with tools.
"""
import os
import re
import selectors
import shlex
import signal
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
         "echo @docker; docker ps --format \"{{.Names}}|{{.Image}}|{{.Status}}\" 2>/dev/null; "
         "echo \"@exit $?\"; "
         "echo @systemd; systemctl list-units --type=service --state=running --no-legend --plain "
         "--no-pager 2>/dev/null; echo \"@exit $?\"")
REMOTE = "exec sh -c " + shlex.quote(INNER)
SSH_OPTIONS = ("-T", "-x", "-a", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
               "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no")
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

NAME = re.compile(r"[a-z0-9][a-z0-9 ._-]{0,%d}" % (MAX_NAME_CHARS - 1))
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
IMAGE = re.compile(r"[A-Za-z0-9_./:@-]{1,200}")
STATUS = re.compile(r"[A-Za-z0-9 ().,:-]{1,60}")
UNIT = re.compile(r"([A-Za-z0-9][A-Za-z0-9@._:+-]{0,99})\.service")
EXIT = re.compile(r"@exit (\d{1,3})")
IPV6 = re.compile(r"\[([0-9a-fA-F:.]+)\]")
LOCAL = ("localhost", "localhost.localdomain", "ip6-localhost", "::1", "0.0.0.0")
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
    return host.casefold() in LOCAL or host.startswith("127.")


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
    """(exit code, output, errors), each capped. A server that sends too much is cut off there."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError:
        raise Unanswered(NO_SSH) from None
    caps = {process.stdout: (bytearray(), MAX_OUTPUT_BYTES),
            process.stderr: (bytearray(), MAX_ERROR_BYTES)}
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in caps:
                selector.register(pipe, selectors.EVENT_READ)
            full = False
            while selector.get_map() and not full:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Unanswered(TIMED_OUT)
                for key, _ in selector.select(left):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer, cap = caps[key.fileobj]
                    buffer += chunk[:cap - len(buffer)]
                    full = full or (key.fileobj is process.stdout and len(buffer) >= cap)
        if full:    # enough to answer from: the rest is never read
            os.killpg(process.pid, signal.SIGKILL)
        code = process.wait(max(0.1, deadline - time.monotonic()))
        return code, bytes(caps[process.stdout][0]), bytes(caps[process.stderr][0])
    except subprocess.TimeoutExpired:
        raise Unanswered(TIMED_OUT) from None
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _ssh_failed(errors: bytes) -> str:
    """ssh's own complaint, read only to pick a sentence: none of it is said."""
    text = errors.decode("utf-8", "replace")
    if "Permission denied" in text:
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
    if result["docker"] is None and result["systemd"] is None and (
            result["docker_missing"] and result["systemd_missing"]):
        raise Unanswered(NEITHER)
    return result


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def wanted(args: dict, host: str) -> str | None:
    """The one thing asked after, checked, or None for the whole list. Never sent anywhere."""
    host = host.casefold()
    around = AROUND | {host, host.partition(".")[0]}
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


def _plural(count: int, one: str) -> str:
    return f"{count} {one}" + ("" if count == 1 else "s")


def _docker_unread(found: dict) -> str:
    return ("Docker isn't installed on your server." if found["docker_missing"]
            else "Docker on your server didn't answer me.")


def _everything(found: dict) -> str:
    said = []
    containers = found["docker"]
    if containers is None:
        said.append(_docker_unread(found))
    elif not containers:
        said.append("No docker containers are running on your server.")
    else:
        verb = "is" if len(containers) == 1 else "are"
        said.append(f"On your server, {_plural(len(containers), 'docker container')} {verb} "
                    f"running: {_listed([c['name'] for c in containers], SAID_CONTAINERS)}.")
        sick = [c["name"] for c in containers if "unhealthy" in c["status"].casefold()]
        if sick:
            said.append(f"Unhealthy: {_listed(sick, SAID_CONTAINERS)}.")
    services = found["systemd"]
    if services is None:
        said.append("Your server doesn't use systemd, so I can't list its services."
                    if found["systemd_missing"] else "I couldn't read its system services.")
    elif services:
        own = [s for s in services if not s.casefold().startswith(PLUMBING)]
        count = _plural(len(services), "system service")
        said.append(f"There are {count} running, among them {_listed(own, SAID_SERVICES)}."
                    if own else f"There are {count} running, all part of the system itself.")
    return " ".join(said)


def _one(found: dict, name: str) -> str:
    key = _key(name)
    seen = []
    for c in found["docker"] or []:
        if key in _key(c["name"]) or key in _key(c["image"].rpartition("/")[2].partition(":")[0]):
            seen.append(f"the docker container {c['name']}"
                        + (f", {c['status'][0].lower()}{c['status'][1:]}" if c["status"] else ""))
    seen += [f"the service {s}" for s in found["systemd"] or [] if key in _key(s)]
    if seen:
        return f"Yes, on your server I see {_listed(seen, SAID_MATCHES)}."
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
    return shown(_one(found, name) if name else _everything(found), 600)


def running(args: dict, config, session) -> dict:
    try:
        text = answer(args, config, session)
    except Unanswered as why:
        text = str(why)
    session.after.append(Speak(text, config["voice"]))
    return {APP_SAYS: True}


HANDLERS = {"server.running": running}
