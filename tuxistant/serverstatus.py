"""What runs on your own server: its docker containers and systemd services, read over ssh.

One fixed command that reads goes to the host of the server in Settings, and nothing the model
wrote goes with it: a name to look for is matched here, once the answer is back. It is made for
a Linux server where systemd runs the services, and runs docker-ce or docker.io as the system's
docker.service, socket-activated or not. docker is asked only while systemd says docker.service
is running, and only at docker's own socket, so the command itself starts nothing there: not a
docker you stopped that its socket would start again, nor podman behind a docker shim, a
rootless docker, whose unit is the user's own docker.service, or another daemon a docker
context or DOCKER_HOST points at. Only a docker stopped between the check and docker ps, which a
slow D-Bus can make up to 8 seconds apart, could still be started by its socket. Anywhere else
the answer says what it couldn't check.
The login opens a session there as any ssh does, which starts your user services if nothing else
keeps them running, and runs what the server runs for any login: its PAM session, such as
Ubuntu's update-motd scripts, the start-up files your shell there reads, such as ~/.zshenv, and
what it runs for that key, such as an authorized_keys command=, a ForceCommand or ~/.ssh/rc,
before the command or in its place. ssh takes the port and user from ~/.ssh/config, and gets no
agent, so the key has to work on its own. What your ssh_config runs itself, such as a
ProxyCommand, still runs.
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
from typing import NamedTuple

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
SAID_NAME_CHARS = 64        # of a container's name, which docker lets be as long as it likes
MAX_NAME_CHARS = 40

# Run by the remote user's own shell, whichever it is, so it hands the work to sh at once.
# Killing ssh here doesn't end what it started there, as there is no terminal to hang up, so a
# command that hangs is given a timeout there too, where one works: an old busybox one fails
# the "true" first and is left out. docker is asked only when systemctl says the system's
# docker.service is running: docker ps on a stopped docker whose docker.socket still listens
# would start it again, with every container set to restart, and a docker that isn't the
# system's docker.service may be podman's shim, which sets up podman for the user, or a rootless
# docker, whose unit is the user's own docker.service. Any answer but active (0) or not (3), as
# from a D-Bus that is down, a timeout or no systemctl, leaves docker unasked too. 300 and 301
# say which, as no exit code is over 255. -H is docker's own socket, over a DOCKER_HOST or docker
# context of the user's, which may be a rootless docker, podman's socket or another machine. A
# docker stopped between the check and docker ps, which a slow D-Bus can make up to 8 seconds
# apart, would still be started by its socket. What docker lists is the system docker's alone,
# so that is what is said. Services are read in the states where they run something or did:
# active, as a oneshot such as ufw or wg-quick@wg0 stays once it has exited, activating, as a
# oneshot such as restic-backup is while its job runs and a unit that keeps crashing is while
# it waits to start again, and reloading. Each line says which. No "!": csh expands it even in
# quotes.
INNER = ("export LC_ALL=C; T=; timeout 8 true >/dev/null 2>&1 && T=\"timeout 8\"; "
         "$T systemctl is-active --quiet docker.service 2>/dev/null; S=$?; echo @docker; "
         "if [ $S = 0 ]; then $T docker -H unix:///var/run/docker.sock ps "
         "--format \"{{.Names}}|{{.Image}}|{{.Status}}\" 2>&1; echo \"@exit $?\"; "
         "elif [ $S = 3 ]; then echo \"@exit 300\"; else echo \"@exit 301\"; fi; "
         "echo @systemd; $T systemctl list-units --type=service "
         "--state=active,activating,reloading --no-legend --plain --no-pager 2>/dev/null; "
         "echo \"@exit $?\"")
REMOTE = "exec sh -c " + shlex.quote(INNER)
# Over what ~/.ssh/config says: a RemoteCommand would replace the command, a ControlMaster
# would leave a connection open for your own sessions to reuse, an IdentityAgent is an agent,
# GSSAPIDelegateCredentials would hand the server your Kerberos ticket, as -a and -x keep the
# agent and X11 from it, ForkAfterAuthentication would leave an ssh that killing its group can't
# reach, SessionType none would wait for the timeout, UpdateHostKeys and CheckHostIP would write
# known_hosts (the second also calls a key changed for the address unknown), and
# ClearAllForwardings leaves a Tunnel. LogLevel ERROR leaves out a long banner but not why ssh
# failed. An ssh built without GSSAPI says it doesn't support that one and goes on. ssh before
# 8.7 doesn't know two of these, hence IgnoreUnknown, first; it is "*" because the first one
# given replaces yours, and an option only your own let through, such as macOS's UseKeychain,
# would stop ssh.
# None of these reach a ProxyJump hop, which is a second ssh that reads only your ssh_config.
SSH_OPTIONS = ("-T", "-x", "-a", "-o", "IgnoreUnknown=*", "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-o", "StrictHostKeyChecking=yes",
               "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
               "-o", "ClearAllForwardings=yes", "-o", "Tunnel=no",
               "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none", "-o", "ControlMaster=no",
               "-o", "ControlPath=none", "-o", "IdentityAgent=none",
               "-o", "GSSAPIDelegateCredentials=no", "-o", "ForkAfterAuthentication=no",
               "-o", "SessionType=default", "-o", "LogLevel=ERROR")
PASSED_ENV = ("PATH", "HOME", "USER", "LANG")
NOT_FOUND = 127             # what sh says for a command that isn't there
NOT_ACTIVE = 300            # docker not asked: systemctl says docker.service isn't running
NO_SYSTEMD_ANSWER = 301     # docker not asked: systemctl couldn't say

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
NEITHER = "I couldn't find systemd on your server, so I couldn't check what's running there."
BAD_NAME = "Say the name of one program or service, in a word or two."
DOCKER_REFUSED = ("Your user on the server isn't allowed to use docker, so I can't list its "
                  "containers.")
DOCKER_NOT_ACTIVE = ("The system's docker.service isn't running on your server, so I didn't ask "
                     "docker.")
DOCKER_NO_SYSTEMD = ("I couldn't ask systemd whether docker is running on your server, so I "
                     "didn't ask docker.")
# Why docker's containers weren't read: said alone, and after an "and" in a no. Each says only
# what is known: DSM's docker or snap's isn't the system's docker.service, and a rootless one is
# the user's own docker.service, but each may be running. "missing" comes only once the system's
# docker.service was found running.
DOCKER_UNREAD = {
    "missing": ("The system's docker.service is running on your server, but I couldn't find the "
                "docker command there.", "I couldn't find the docker command there"),
    "not active": (DOCKER_NOT_ACTIVE, "the system's docker.service isn't running there"),
    "no systemd": (DOCKER_NO_SYSTEMD, "I couldn't ask systemd whether docker is running there"),
    "refused": (DOCKER_REFUSED, "your user isn't allowed to use docker there"),
    "error": ("Docker on your server gave me an error.", "docker gave me an error there"),
    "silent": ("Docker on your server didn't answer me.", "docker didn't answer there"),
}
# Why a list was read only in part, or why the services weren't read, said the same two ways.
# "long" is more lines than are read, or more than the output holds, and "odd" a container line
# whose name doesn't look like one, which may be running all the same.
NOT_WHOLE = {
    ("docker", "long"): ("Docker's list was longer than I could read.",
                         "docker listed more than I could read there"),
    ("docker", "odd"): ("Docker listed some containers in a way I couldn't read.",
                        "docker listed some containers in a way I couldn't read there"),
    ("systemd", "long"): ("Your server's list of services was longer than I could read.",
                          "it listed more services than I could read"),
    # systemctl not found: on that PATH, at least
    ("systemd", "missing"): ("I couldn't find systemd on your server, so I can't list its "
                             "services.", "I couldn't find systemd there"),
    ("systemd", "unread"): ("I couldn't read your server's system services.",
                            "I couldn't read its system services"),
}
OVER_SSH = "Yes, I reached your server over ssh."
SSHD_RUNS = "Yes, I reached your server over ssh, and its sshd service is running."
NO_SSHD = ("I reached your server over ssh, but I don't see an sshd service among its system "
           "services.")
# Where a no looked, by which lists were read whole: docker's and systemd's. Only the system
# docker is asked, and only the system's services are read, not the ssh user's own.
LOOKED = {(True, True): "the system docker's containers or the system services",
          (True, False): "the system docker's containers", (False, True): "the system services"}

# "@" as in wg-quick@wg0, which a service here may be called too
NAME = re.compile(r"[a-z0-9][a-z0-9 ._@-]{0,%d}" % (MAX_NAME_CHARS - 1))
# docker's own grammar, which has no limit: a compose or a k8s_ name may be long
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
IMAGE = re.compile(r"[A-Za-z0-9_./:@-]{1,200}")
STATUS = re.compile(r"[A-Za-z0-9 ().,:-]{1,60}")
UNIT = re.compile(r"([A-Za-z0-9][A-Za-z0-9@._:+-]{0,99})\.service")
EXIT = re.compile(r"@exit ([0-9]{1,3})")
IPV6 = re.compile(r"\[([0-9a-fA-F:.]+)\]")
# The names /etc/hosts gives this computer by convention, and below .localhost, which
# systemd-resolved and glibc send to loopback. tools/eval_config.py uses these too.
LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "localhost4", "localhost6",
                         "localhost4.localdomain4", "localhost6.localdomain6", "ip6-localhost",
                         "ip6-loopback"})
# Words around a name rather than in it, as in "ssh myserver docker ps": with only these
# left, the whole list was asked for. Otherwise they stay in the name, but a match can do
# without them, so "minecraft server" finds minecraft and "sql server" is not just sql.
AROUND = {"server", "servers", "my", "our", "own", "the", "your", "on", "is", "are", "what",
          "which", "ssh", "docker", "ps", "systemctl", "systemd", "containers", "container",
          "services", "service", "all", "everything", "anything", "running", "processes",
          "programs", "apps", "applications", "*"}
# Never part of a name, so not said back either: "my plex" asks after plex.
FILLER = {"my", "our", "own", "the", "your", "on", "is", "are", "what", "which", "all",
          "everything", "anything", "running"}
SERVER_WORDS = {"server", "servers"}
# Before the server's name, as in "my nas" or "the pi".
POSSESSIVES = {"my", "our", "your", "the", "own"}
# Around the server's name and never part of it, as in "my home nas", "the nas server" or
# "my nas box".
AROUND_A_HOST = POSSESSIVES | SERVER_WORDS | {"home", "box", "host"}
# The server that answered over ssh runs an ssh server, whatever it is. sshd is OpenSSH's,
# which may not be: Tailscale SSH or Dropbear may have answered.
SSH_NAMES = {"ssh"}
SSHD_NAMES = {"sshd", "openssh", "opensshserver", "opensshd"}
# OpenSSH's units: Debian's is ssh, and a socket starts one per connection as sshd@ or ssh@.
SSHD_UNITS = ("ssh", "sshd")
# Part of the system itself, so they are counted but not named: nobody asks after getty. One
# ending in - or @ starts a family; any other is a whole name, or one before a -, @ or ., as
# dbus-broker or snapd.apparmor, so an app such as cronicle or sshwifty is named.
PLUMBING = ("systemd-", "getty@", "serial-getty@", "user@", "dbus", "polkit", "rsyslog", "cron",
            "crond", "atd", "udisks2", "irqbalance", "multipathd", "packagekit",
            "accounts-daemon", "networkd-dispatcher", "unattended-upgrades", "upower",
            "rtkit-daemon", "avahi-daemon", "wpa_supplicant", "modemmanager", "networkmanager",
            "thermald", "chrony", "chronyd", "containerd", "lvm2", "qemu-guest-agent", "fwupd",
            "colord", "power-profiles-daemon", "bolt", "kerneloops", "gdm", "gdm3", "snapd",
            "ssh", "sshd")


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
            return (host in LOCAL_NAMES or host.endswith(".localhost")
                    or host in _this_computer())
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_unspecified


def is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _label(host: str) -> str | None:
    """The first label of a name, as zephyr for zephyr.lan. An address has none: 10 is no name
    for 10.0.0.5."""
    host = host.casefold()
    return None if is_address(host) else host.partition(".")[0]


def own_names(host: str) -> set[str]:
    """What the server is called: its host, and the first label of a name."""
    return {host.casefold(), _label(host)} - {None}


def _as_address(text: str):
    """The address text is, as one value, or None for a name. ::ffff:10.0.0.5 is 10.0.0.5."""
    try:
        address = ipaddress.ip_address(text.strip().strip(".,?!").removeprefix("[")
                                       .removesuffix("]"))
    except ValueError:
        return None
    return getattr(address, "ipv4_mapped", None) or address


def _same_name(text: str, name: str) -> bool:
    """The same letters and the same numbers, so web-11 is not web1-1."""
    return (bool(_key(text)) and _key(text) == _key(name)
            and re.findall(r"[0-9]+", text) == re.findall(r"[0-9]+", name))


def is_own_name(text: str, host: str) -> bool:
    """Whether text is one of own_names in any spelling, as "zephyr nas" for zephyr-nas.lan.
    An address only as the same address: 192.0.21.2 has the digits of 192.0.2.12, but is
    another computer."""
    said, own = _as_address(text), _as_address(host)
    if said is not None or own is not None:
        return said == own
    return any(_same_name(text, name) for name in own_names(host))


def bare(word: str) -> str:
    """A word without an 's at its end, as nas in "the nas's docker" and what in "what's"."""
    return re.sub(r"['’]s?(?=[.,?!]*$)", "", word)


def _without(text: str, words: set) -> str:
    """text without these as words of their own: "my-nas" keeps its my."""
    return " ".join(w for w in (bare(w.strip(".,?!")) for w in text.casefold().split())
                    if w and w not in words)


def names_host(text: str, host: str) -> bool:
    """Whether text names host: as said, or without my, the, home, server or box around it, so
    "my home nas", "nas server" and "my nas box" name nas.lan, and "my-nas" and "home" still name
    my-nas.lan and home.lan, or without the other words around a name, as "ssh nas" and "nas
    containers"."""
    return any(is_own_name(said, host) for said in (
        text, _without(text, POSSESSIVES), _without(text, AROUND_A_HOST), _without(text, AROUND)))


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


def _sections(text: str, full: bool = False) -> dict:
    """{"docker": (exit code, lines, cut), "systemd": ...} for each section the answer finished,
    where cut says it had more than MAX_LINES lines, of which only those were kept. With full,
    the output was cut off at MAX_OUTPUT_BYTES: the section it was cut off in is kept as cut,
    without the line the cut went through. That section only begins once its check has passed,
    and an error is never that long."""
    if full and not text.endswith("\n"):
        text = text.rpartition("\n")[0]
    found, current, lines, cut = {}, None, [], False
    for line in text.splitlines():
        if line in ("@docker", "@systemd"):
            current, lines, cut = line[1:], [], False
        elif current and (code := EXIT.fullmatch(line)):
            found[current] = (int(code[1]), lines, cut)
            current = None
        elif current and len(lines) < MAX_LINES:
            lines.append(line)
        elif current:
            cut = True
    if full and current:
        found[current] = (0, lines, True)
    return found


def _containers(lines: list[str]) -> list[dict]:
    """A line without a | is no container, as the template has two: docker's own message."""
    found = []
    for line in lines:
        name, _, rest = line.partition("|")
        image, _, status = rest.partition("|")
        if "|" in line and CONTAINER.fullmatch(name):
            found.append({"name": name, "image": image if IMAGE.fullmatch(image) else "",
                          "status": status if STATUS.fullmatch(status) else ""})
    return found


def _dropped(lines: list[str]) -> bool:
    """A container whose name doesn't look like one: it may be running all the same."""
    return any("|" in line and not CONTAINER.fullmatch(line.partition("|")[0]) for line in lines)


RUNNING, STARTING, RESTARTING, DONE = "running", "starting", "restarting", "done"
# As said after a unit's name. One that is done is still active, with nothing left running.
UNIT_STATUS = {RUNNING: "", STARTING: ", starting", RESTARTING: ", restarting again and again",
               DONE: ", active"}


class Unit(NamedTuple):
    name: str
    state: str      # RUNNING, STARTING, RESTARTING or DONE


def _state(active: str, sub: str) -> str:
    """Activating is a job that runs, as a oneshot's backup does, unless the unit waits to start
    again after it crashed. Active and exited is done, as ufw is once it has set up."""
    if active == "activating":
        return RESTARTING if sub.startswith("auto-restart") else STARTING
    return DONE if sub == "exited" else RUNNING


def _services(lines: list[str]) -> list[Unit]:
    """UNIT LOAD ACTIVE SUB DESCRIPTION, each line."""
    found = []
    for line in lines:
        words = line.split()
        if words and (unit := UNIT.fullmatch(words[0])):
            found.append(Unit(unit[1], _state(*(words + ["", "", ""])[2:4])))
    return found


def _runs(unit: Unit) -> bool:
    """Runs something now: is counted, and is up."""
    return unit.state in (RUNNING, STARTING)


def read(host: str) -> dict:
    """What runs on host, or Unanswered. "docker" and "systemd" are lists, or None when that
    one wasn't read, with why in "docker_why", a key of DOCKER_UNREAD, and in "systemd_missing"
    for a systemctl sh didn't find. "docker_cut" and "systemd_cut" mark a list read only in part,
    with why: "long" for one with more than was read, "odd" for a container that was dropped."""
    code, output, errors = _run(command(host), _env())
    if code == 255:
        raise Unanswered(_ssh_failed(errors))
    sections = _sections(output.decode("utf-8", "replace"), len(output) >= MAX_OUTPUT_BYTES)
    if not sections:
        raise Unanswered(UNREADABLE)
    result = {}
    for part, parse in (("docker", _containers), ("systemd", _services)):
        status, lines, cut = sections.get(part, (None, [], False))
        result[part] = parse(lines) if status == 0 else None
        partly = "long" if cut else "odd" if part == "docker" and _dropped(lines) else None
        result[f"{part}_cut"] = partly if status == 0 else None
    status, lines, _ = sections.get("docker", (None, [], False))
    result["docker_why"] = None if status == 0 else _docker_why(status, lines)
    result["systemd_missing"] = sections.get("systemd", (None,))[0] == NOT_FOUND
    return result


def _docker_why(status: int | None, lines: list[str]) -> str:
    """Why docker's list wasn't read. docker's own complaint is read only to pick a sentence."""
    if why := {NOT_FOUND: "missing", NOT_ACTIVE: "not active",
               NO_SYSTEMD_ANSWER: "no systemd"}.get(status):
        return why
    said = " ".join(lines).casefold()
    if "permission denied" in said:
        return "refused"
    return "error" if "error response from daemon" in said else "silent"


def _neither(found: dict) -> bool:
    """Nothing read, and no systemctl: docker is asked only where systemd runs it."""
    return found["docker"] is None and found["systemd_missing"]


def _not_whole(found: dict, part: str) -> tuple[str, str] | None:
    """Why part wasn't read whole, said alone and after an "and", or None when it was."""
    if found[part] is None:
        if part == "docker":
            return DOCKER_UNREAD[found["docker_why"]]
        return NOT_WHOLE["systemd", "missing" if found["systemd_missing"] else "unread"]
    return NOT_WHOLE.get((part, found[f"{part}_cut"]))


def _read(found: dict, part: str) -> bool:
    """Read whole: a list cut short may leave out what was asked after."""
    return _not_whole(found, part) is None


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _words(name) -> list[str]:
    """Its words, without "home" before server: a home server is a server. A unit's ".service"
    is left out, as the units listed have none."""
    words = [bare(w.strip(".,?!")).removesuffix(".service")
             for w in str(name or "").casefold().split()]
    return [w for w, after in zip(words, words[1:] + [""])
            if w and not (w == "home" and after in SERVER_WORDS)]


def asks_for_all(name) -> bool:
    """Only words around a name, as in "my home server" or "docker containers"."""
    return all(w in AROUND for w in _words(name))


def names_a_server(name) -> bool:
    """A name that is "server" with words around it, such as "the servers" or "our home server"."""
    return asks_for_all(name) and any(w in SERVER_WORDS for w in _words(name))


def wanted(args: dict, host: str) -> str | None:
    """The one thing asked after, as said and checked, or None for the whole list. Never sent
    anywhere. A host with a dot, or an address, is no program's name and is left out; a single
    label is kept, as on a server called plex or localai.lan, plex or localai is what is asked
    after. An address in any spelling, as [fd00::12] or fd00:0::12, is left out too."""
    dropped = FILLER | ({host.casefold()} - {_label(host)})
    words = [w for w in _words(args.get("name"))
             if w not in dropped and not (is_address(host) and is_own_name(w, host))]
    if all(w in AROUND for w in words):
        return None
    name = " ".join(words)
    if not NAME.fullmatch(name) or len(_key(name)) < 2:
        raise Unanswered(BAD_NAME)
    return name


def _needed(name: str) -> list[str]:
    """The words of name a match can't do without."""
    return [key for key in (_key(w) for w in name.split() if w not in AROUND) if key]


def _asks_after_ssh(args: dict) -> str | None:
    """"ssh" for an ssh server, which the answer came over, "sshd" for OpenSSH's, or None."""
    words = [w for w in _words(args.get("name"))
             if w not in FILLER | SERVER_WORDS | {"service", "daemon"}]
    key = _key(" ".join(words))
    return "ssh" if key in SSH_NAMES else "sshd" if key in SSHD_NAMES else None


def _sshd(found: dict) -> str:
    """Whether OpenSSH's sshd runs there: the answer came over ssh, but Tailscale SSH or
    Dropbear may have given it."""
    if any(_runs(s) and s.name.split("@")[0] in SSHD_UNITS for s in found["systemd"] or []):
        return SSHD_RUNS
    if not _read(found, "systemd"):
        return f"I reached your server over ssh. {_not_whole(found, 'systemd')[0]}"
    return NO_SSHD


def _listed(names: list[str], limit: int) -> str:
    """Said with commas and "and", or with semicolons when a name has its status after a comma
    of its own, so what is a name and what its status can still be heard."""
    said = names[:limit]
    if len(names) > limit:
        said.append(f"{len(names) - limit} more")
    if len(said) == 1:
        return said[0]
    if any(", " in item for item in said):
        return "; ".join(said[:-1]) + "; and " + said[-1]
    return ", ".join(said[:-1]) + " and " + said[-1]


def _is(count: int) -> str:
    return "is" if count == 1 else "are"


def _plural(count: int, one: str) -> str:
    return f"{count} {one}" + ("" if count == 1 else "s")


def _restarting(container: dict) -> bool:
    """docker ps lists a container that keeps crashing too, as "Restarting (1) 5 seconds ago"."""
    return container["status"].casefold().startswith("restarting")


def _paused(container: dict) -> bool:
    """And a paused one, as "Up 2 hours (Paused)"."""
    return "(paused)" in container["status"].casefold()


def _up(container: dict) -> bool:
    return not _restarting(container) and not _paused(container)


def _status(container: dict) -> str:
    """As said after its name. The time in "Restarting (1) 5 seconds ago" is when it last
    stopped, so it isn't said."""
    if _paused(container):
        return ", paused"
    if _restarting(container):
        return ", restarting again and again"
    status = re.sub(r"\b(About|Less than)\b", lambda word: word[0].lower(), container["status"])
    return f", {status[0].lower()}{status[1:]}" if status else ""


def _said_name(name: str) -> str:
    """A container's name as said: docker lets it be as long as it likes, as a k8s_ one is, so
    a long one is cut after a whole part of it, with no count of what is left to read out."""
    if len(name) <= SAID_NAME_CHARS:
        return name
    cut = name[:SAID_NAME_CHARS]
    end = max(cut.rfind(mark) for mark in "-_.")
    return f"{cut[:end] if end > 0 else cut}…"


def _plumbing(unit: str) -> bool:
    name = unit.casefold()
    return any(name.startswith(part) if part[-1] in "-@" else
               name == part or name.startswith((f"{part}-", f"{part}@", f"{part}."))
               for part in PLUMBING)


def _everything(found: dict, first: set = frozenset()) -> str:
    """What runs there, a few names in each part, with the names in first before the rest, so
    the cut can't drop what matched the name asked after. A list read only in part is counted
    as "at least", and says why after its part. Only the system docker is asked, so that is
    what is said: a rootless docker of the ssh user's may run more."""
    said = []
    containers, partly = found["docker"], _not_whole(found, "docker")
    least = "at least " if partly else ""
    if containers is None or not containers and partly:
        said.append(partly[0])      # none read, so none is said to be up or down
    elif not containers:
        said.append("On your server, the system docker has no containers running.")
    else:
        def named(which) -> list[str]:
            return [_said_name(n) for n in sorted((c["name"] for c in containers if which(c)),
                                                   key=lambda n: n not in first)]
        if up := named(_up):
            said.append(f"On your server, the system docker has {least}"
                        f"{_plural(len(up), 'container')} running: {_listed(up, SAID_CONTAINERS)}.")
        else:
            said.append("On your server, none of the system docker's containers I could read is "
                        "really up." if partly else
                        "On your server, the system docker has no containers really up.")
        if sick := named(lambda c: "unhealthy" in c["status"].casefold()):
            said.append(f"Unhealthy: {_listed(sick, SAID_CONTAINERS)}.")
        if again := named(_restarting):
            said.append(f"Restarting again and again, so not really up: "
                        f"{_listed(again, SAID_CONTAINERS)}.")
        if paused := named(_paused):
            said.append(f"Paused, so not running: {_listed(paused, SAID_CONTAINERS)}.")
        if partly:
            said.append(partly[0])
    services, partly = found["systemd"], _not_whole(found, "systemd")
    least = "at least " if partly else ""
    # Only those that run something are counted, a oneshot's job included: one that is done, as
    # ufw, or waits to start again after a crash runs nothing.
    running = [s.name for s in services or [] if _runs(s)]
    if running:
        own = sorted((s for s in running if not _plumbing(s)), key=lambda s: s not in first)
        count = (f"There {_is(len(running))} {least}"
                 f"{_plural(len(running), 'system service')} running")
        plumbing = "which is" if len(running) == 1 else "all"
        if not own:
            said.append(f"{count}, {plumbing} part of the system itself.")
        elif len(own) == len(running):
            said.append(f"{count}: {_listed(own, SAID_SERVICES)}.")
        else:
            said.append(f"{count}, among them {_listed(own, SAID_SERVICES)}.")
    if partly:
        said.append(partly[0])
    return " ".join(said)


def _in(key: str, text: str) -> str | None:
    """"whole" where key starts text or one of its words, as postgres does postgresql and api
    does localai-api-1, "part" where it is only inside one, as sql is in postgresql. A key
    under three letters only as a word of its own: "ai" is not in localai, though it is in
    open-ai."""
    words = [w for w in re.split(r"[^a-z0-9]+", text.casefold()) if w]
    if len(key) < 3:
        return "whole" if key in words else None
    joined, starts, at = "".join(words), set(), 0
    for word in words:
        starts.add(at)
        at += len(word)
    found = {i for i in range(len(joined)) if joined.startswith(key, i)}
    if found & starts:
        return "whole"
    return "part" if found else None


def _match(name: str, *texts: str) -> str | None:
    """How name is in any of texts: "whole" for the whole name or every word of it but the
    ones around a name, such as server, "part" for only some of its words of three letters or
    more, as homepage is for home assistant."""
    needed = _needed(name)
    if any(_in(_key(name), text) == "whole" for text in texts) or (
            needed and all(any(_in(key, text) == "whole" for text in texts) for key in needed)):
        return "whole"
    if any(_in(key, text) for key in [_key(name), *needed] if len(key) >= 3 for text in texts):
        return "part"
    return None


class Match(NamedTuple):
    said: str       # as said, with its status
    exact: bool     # called the name exactly, once the words around a name are gone: by its
                    # own name, a container's image or a unit's name
    named: bool     # called it by its own name, a container's or a unit's that runs something
                    # or keeps trying to: only that decides
    up: bool        # really up: not restarting or paused, and for a unit, running something
    name: str       # the container's or the unit's own name


def _seen(found: dict, name: str) -> tuple[list[Match], bool]:
    """What on the server matches name, and whether they match the whole name: if nothing does,
    what matched some of its words. "the plex service" is called plex, but nas-backup isn't
    called nas, and "my home nas" is homenas. What is called the name exactly comes first, by its
    own name before its image, then what isn't up, and when some of that isn't up, the first
    that is up comes second, so what decides how the answer starts is among the few said."""
    exactly = _without(name, AROUND)
    seen = {"whole": [], "part": []}
    for c in found["docker"] or []:
        image = c["image"].rpartition("/")[2].partition(":")[0]
        named = _same_name(exactly, c["name"])
        exact = named or _same_name(exactly, image)
        if how := "whole" if exact else _match(name, c["name"], image):
            seen[how].append(Match(f"the docker container {_said_name(c['name'])}{_status(c)}",
                                   exact, named, _up(c), c["name"]))
    for s in found["systemd"] or []:
        exact = _same_name(exactly, s.name)
        if how := "whole" if exact else _match(name, s.name):
            # One that is done, as ufw once it has set up, or a compose wrapper such as
            # immich.service once it has run docker compose up, runs nothing, so it says nothing
            # of the app, and decides nothing.
            seen[how].append(Match(f"the service {s.name}{UNIT_STATUS[s.state]}", exact,
                                   exact and s.state != DONE, _runs(s), s.name))
    how = "whole" if seen["whole"] else "part"
    matches = sorted(seen[how], key=lambda m: (not m.named, not m.exact, m.up))
    deciding = _deciding(matches)
    first = deciding or [m for m in matches if m.exact]
    # With all that decides down, what else is up decides there is no "Not really".
    if not all(m.up for m in first) and (
            up := next((m for m in (matches if deciding else first) if m.up), None)):
        matches.remove(up)
        matches.insert(1, up)
    return matches, how == "whole"


def _deciding(matches: list[Match]) -> list[Match]:
    """What decides whether it is up: what is called the name exactly by its own name. An app
    that keeps restarting is not up beside its database that is, and a nextcloud-db that is up
    says nothing of a nextcloud container that isn't there. Nor does a container that only runs
    the image of that name, as a nextcloud-cron-1 of the nextcloud image or mastodon-sidekiq of
    mastodon's, which may be a helper beside an app that is down, or a unit of that name that is
    done, as a wrapper that ran docker compose up."""
    return [m for m in matches if m.named]


def _start(matches: list[Match], plainly: bool = False) -> str:
    """With nothing called the name exactly, or plainly, for a list not read whole, where more
    may be called that, there is no "Yes", "Partly" or "Not really". Nor is there a "Not really"
    while something else that matches the whole name is up: a native plexmediaserver may be the
    Plex asked after, beside a plex container that can't have its port."""
    deciding = _deciding(matches)
    if plainly or not deciding:
        return "On your server I see"
    if all(m.up for m in deciding):
        return "Yes, on your server I see"
    if any(m.up for m in deciding):
        return "Partly: on your server I see"
    if any(m.up for m in matches):
        return "On your server I see"
    return "Not really: on your server I see"


def _no_docker(found: dict) -> str:
    """Why docker's containers weren't all read, after an "and"."""
    return _not_whole(found, "docker")[1]


def _no_systemd(found: dict) -> str:
    return _not_whole(found, "systemd")[1]


def _one(found: dict, name: str, matches: list[Match], whole: bool) -> str:
    """Whether name runs there. A no says where it was looked for and what couldn't be, which
    may have it. Only the system docker and the system's services are read, not the ssh user's
    own, so a no says so. What matches only some of the name gets no no, as it may be what was
    asked after under another name, as mssql-server is SQL Server: only that nothing is called
    it exactly."""
    seen = [m.said for m in matches]
    unread = [why[0] for why in (_not_whole(found, "docker"), _not_whole(found, "systemd")) if why]
    if seen and whole:
        # What wasn't read whole may hold more that is called that, up or not, so it decides
        # nothing, and is said.
        return " ".join([f"{_start(matches, bool(unread))} {_listed(seen, SAID_MATCHES)}.",
                         *unread])
    if found["docker"] is None and found["systemd"] is None:
        return f"I couldn't check for {name} on your server. {_everything(found)}"
    docker, systemd = _read(found, "docker"), _read(found, "systemd")
    if not docker and not systemd:
        no = (f"I couldn't check all of your server for {name}, as {_no_docker(found)} and "
              f"{_no_systemd(found)}")
    else:
        where = f"{LOOKED[docker, systemd]} running on your server"
        why = "" if docker and systemd else (
            f", and {_no_systemd(found) if docker else _no_docker(found)}")
        no = (f"Nothing among {where} is called {name} exactly{why}" if seen
              else f"I don't see {name} among {where}{why}")
    return f"{no}, but I see {_listed(seen, SAID_MATCHES)}." if seen else f"{no}."


def _on_a_server(args: dict, host: str) -> bool:
    """An on that says server without naming this one, as "my server"."""
    on = " ".join(_words(args.get("on")))
    return any(w in SERVER_WORDS for w in on.split()) and not names_host(on, host)


def _as_a_program(args: dict, host: str, name: str, missed_here: str | None) -> bool:
    """The server's label alone, asked after as a program: once this computer was looked at for
    it, or with an on that says server, as "Is Plex running on my server?" on plex.lan."""
    return is_own_name(name, host) and bool(missed_here or _on_a_server(args, host))


def _the_list_answers(found: dict, name: str, host: str, matches: list[Match], whole: bool,
                      program: bool) -> bool:
    """The server's own name: what runs there, unless something there is what was asked after.
    Asked after as a program, anything that matches any of it is, as myplex may be Plex, and
    the list, which then says no first, answers only once docker and systemd were both read
    whole: otherwise the answer says what it couldn't check, as for any name. Said as the
    server, as "my nas" beside nas-backup, only something called exactly that is."""
    if not names_host(name, host):
        return False
    if program:
        return not matches and _read(found, "docker") and _read(found, "systemd")
    return not any(m.exact for m in matches)


def _not_here(name: str) -> str:
    """What this computer was looked at for: a service or a process of that name."""
    return f"no service or process called {name} is running on this computer"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def answer(args: dict, config, session=None, missed_here: str | None = None,
           say_which: bool = False) -> str:
    """The sentence to say, from daemon code alone. Marks the conversation as having read
    something private once the server has answered: what you run there is yours.
    missed_here is a name this computer was asked for first and doesn't have, and say_which
    names the server, for an on that may mean another one."""
    host = target(config)
    name = wanted(args, host)
    found = read(host)
    ssh = _asks_after_ssh(args)
    if not ssh and _neither(found):
        raise Unanswered(NEITHER)
    if session is not None:
        session.tainted = True
    matches, whole = _seen(found, name) if name else ([], False)
    program = bool(name) and _as_a_program(args, host, name, missed_here)
    # Asked "Is the NAS up?", a no, or what isn't called nas, as nas-backup, would sound as if
    # the server were down, so the answer says it answered. Nothing there is called the name
    # by its own name, so it is never a yes.
    up_there = program and bool(missed_here) and not _deciding(matches)
    both = f"{LOOKED[True, True]} running"
    if ssh == "ssh":
        text = OVER_SSH     # whatever ssh server it is, it let me in
    elif ssh:
        text = _sshd(found)
    elif name and _the_list_answers(found, name, host, matches, whole, program):
        text = _everything(found, {m.name for m in matches} if whole else frozenset())
        if up_there:
            text = (f"Your server answered. I don't see {name} among {both} there, and "
                    f"{_not_here(name)}. {text}")
        elif program:
            text = f"I don't see {name} among {both} on your server. {text}"
    else:
        text = _one(found, name, matches, whole) if name else _everything(found)
        if up_there:
            text = f"Your server answered, and {_not_here(missed_here)}. {text}"
        elif missed_here and not whole:
            # This computer was looked at first, and only a yes says where it was found.
            text = f"{_sentence(_not_here(missed_here))}. {text}"
    if say_which:
        text = f"I checked the server in Settings, {_label(host) or host}. {text}"
    return shown(text, 600)


def running(args: dict, config, session, missed_here: str | None = None,
            say_which: bool = False) -> dict:
    """system.running on the server: the answer, or why there is none, said by the app."""
    try:
        text = answer(args, config, session, missed_here, say_which)
    except Unanswered as why:
        text = _unanswered(str(why), config, missed_here, say_which)
    session.after.append(Speak(text, config["voice"]))
    return {APP_SAYS: True}


def _unanswered(why: str, config, missed_here: str | None, say_which: bool) -> str:
    """Why the server didn't answer, still after what this computer was found not to have, and
    which server was tried once one was: a sleeping server must not hide the no from here."""
    if say_which and why not in (NO_SERVER, BAD_HOST, ON_THIS_COMPUTER, BAD_NAME, NO_SSH):
        try:
            host = target(config)
            why = f"I tried the server in Settings, {_label(host) or host}. {why}"
        except Unanswered:
            pass
    if missed_here:
        why = f"{_sentence(_not_here(missed_here))}. {why}"
    return shown(why, 600)

