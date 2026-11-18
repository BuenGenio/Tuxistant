"""What runs on your own server, as its docker and systemd list it, read over ssh.

One fixed command that reads goes to the host of the server in Settings, and nothing the model
wrote goes with it: a name to look for is matched here, once the answer is back. What comes back
is relayed, not judged: each row that has the name in it, with its state as docker or systemd
wrote it, how many rows were looked through, whether a list went on past what was read, and why
a list wasn't read, in words that claim only what was seen. Whether an app works is not said.
It is made for a Linux server where systemd runs the services, and runs docker-ce or docker.io as
the system's docker.service, socket-activated or not. docker is asked only while systemctl says
docker.service is active, and only at docker's own socket, so the command itself starts nothing
there: not a docker you stopped that its socket would start again, nor podman behind a docker
shim, a rootless docker, whose unit is the user's own docker.service, or another daemon a docker
context or DOCKER_HOST points at. Only a docker stopped between the check and docker ps, which a
slow D-Bus can make up to 8 seconds apart, could still be started by its socket. Anywhere else
the answer says what it couldn't ask.
The login opens a session there as any ssh does, which starts your user services if nothing else
keeps them running, and runs what the server runs for any login: its PAM session, such as
Ubuntu's update-motd scripts, the start-up files your shell there reads, such as ~/.zshenv, and
what it runs for that key, such as an authorized_keys command=, a ForceCommand or ~/.ssh/rc,
before the command or in its place. ssh takes the port and user from ~/.ssh/config, and gets no
agent, so the key has to work on its own. What your ssh_config runs itself, such as a
ProxyCommand, still runs. With no ~/.ssh, ssh isn't started, as it would make one.
What the server sends is someone else's text. It is capped, each row has to look like one, and
the app says the answer itself, so none of it reaches a model with tools.
"""
import ipaddress
import os
import pwd
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
SAID_ROWS = 4               # of each list, for a name
SAID_CONTAINERS = 6         # for the whole list
SAID_SERVICES = 4
SAID_NAME_CHARS = 64        # of a container's name, image or unit, which may be as long as it likes
MAX_NAME_CHARS = 40
MAX_SAID_CHARS = 2400       # more than the longest answer the caps above allow, so none is cut

# Run by the remote user's own shell, whichever it is, so it hands the work to sh at once.
# Killing ssh here doesn't end what it started there, as there is no terminal to hang up, so a
# command that hangs is given a timeout there too, where one works: an old busybox one fails
# the "true" first and is left out. docker is asked only when systemctl says the system's
# docker.service is active (0): docker ps on a stopped docker whose docker.socket still listens
# would start it again, with every container set to restart, and a docker that isn't the
# system's docker.service may be podman's shim, which sets up podman for the user, or a rootless
# docker, whose unit is the user's own docker.service. Any other answer, as from a D-Bus that is
# down, a timeout or no systemctl, leaves docker unasked too, and what systemctl said, its word
# and exit code, comes back to say why. 300 says docker wasn't asked, as no exit code is over
# 255. -H is docker's own socket, over a DOCKER_HOST or docker context of the user's, which may
# be a rootless docker, podman's socket or another machine. A docker stopped between the check
# and docker ps, which a slow D-Bus can make up to 8 seconds apart, would still be started by
# its socket. Services are listed as list-units lists them with no --state: in every state it
# gives, active and exited, activating, reloading and failed among them, so the rows relayed are
# all it listed. No "!": csh expands it even in quotes.
INNER = ("export LC_ALL=C; T=; timeout 8 true >/dev/null 2>&1 && T=\"timeout 8\"; "
         "echo @docker.service; $T systemctl is-active docker.service 2>/dev/null; S=$?; "
         "echo \"@exit $S\"; echo @docker; "
         "if [ $S = 0 ]; then $T docker -H unix:///var/run/docker.sock ps "
         "--format \"{{.Names}}|{{.Image}}|{{.Status}}\" 2>&1; echo \"@exit $?\"; "
         "else echo \"@exit 300\"; fi; "
         "echo @systemd; $T systemctl list-units --type=service "
         "--no-legend --plain --no-pager 2>/dev/null; echo \"@exit $?\"")
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
CANT_RUN = 126              # and for one it found but couldn't run
NOT_ASKED = 300             # docker not asked: systemctl didn't say docker.service is active
TIMED_OUT_THERE = 124       # what timeout says of a command it stopped
NOT_ACTIVE = 3              # systemctl is-active: not active, and before systemd 253 no such unit
NO_UNIT = 4                 # systemctl is-active from systemd 253 on: no such unit

NO_SERVER = "There's no server set in Settings, so I don't know which one to check."
ON_THIS_COMPUTER = ("The server in Settings is this computer. I can't list what runs here, but "
                    "I can check one program or service by its name.")
BAD_HOST = "The server address in Settings isn't one I can reach over ssh."
NO_SSH = "ssh isn't installed on this computer, so I can't reach your server."
NO_SSH_FOLDER = ("There's no .ssh folder in your home folder, and ssh would make one, so I didn't "
                 "start it. Connect to your server once with ssh in a terminal, then ask me again.")
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
NEITHER = ("On your server, I couldn't find systemctl, so I didn't ask docker or list the "
           "services.")
BAD_NAME = "Say the name of one program or service, in a word or two."
OVER_SSH = "I reached your server over ssh."
# Why docker's list wasn't read, from what systemctl is-active said of docker.service, or from
# what docker did once asked. Each says only what was seen: a NAS's docker, snap's or a rootless
# one isn't the system's docker.service, but may be running all the same.
NO_SYSTEMCTL_DOCKER = "I couldn't find systemctl, so I didn't ask docker."
NO_DOCKER_UNIT = "systemctl says there's no docker.service unit, so I didn't ask docker."
SYSTEMCTL_SLOW_DOCKER = ("systemctl took too long to say whether docker.service is active, so I "
                         "didn't ask docker.")
SYSTEMCTL_UNREAD_DOCKER = ("I couldn't read systemctl's answer about docker.service, so I didn't "
                           "ask docker.")
# What sh said of docker, as 127 or 126, after what systemctl said of docker.service.
DOCKER_MISSING = {127: "I couldn't find a docker command.", 126: "I couldn't run docker."}
DOCKER_SLOW = "docker took too long to answer."
DOCKER_REFUSED = "docker said permission denied."
DOCKER_UNREAD = "I couldn't read docker's answer."
# Why systemd's list wasn't read.
SYSTEMCTL_SLOW = "systemctl took too long to list the services."
NO_SYSTEMCTL = "I couldn't find systemctl, so I couldn't list the services."
SYSTEMCTL_UNREAD = "I couldn't read systemctl's list of services."
# Which is which, as said: docker's rows are containers and systemd's are services.
PARTS = ("docker", "systemd")
WHAT = {"docker": "container", "systemd": "service"}
IN = {"docker": "name or image", "systemd": "name"}     # where a name is looked for in each

# "@" as in wg-quick@wg0, which a service here may be called too
NAME = re.compile(r"[a-z0-9][a-z0-9 ._@-]{0,%d}" % (MAX_NAME_CHARS - 1))
# docker's own grammar, which has no limit: a compose or a k8s_ name may be long
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
IMAGE = re.compile(r"[A-Za-z0-9_./:@-]{1,255}")
STATUS = re.compile(r"[A-Za-z0-9 ().,:-]{1,60}")
# systemd's grammar for a unit's name, with its \x2d escapes, and the states it lists
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:+\\-]{0,240}\.service")
STATE = re.compile(r"[a-z][a-z-]{0,31}")
EXIT = re.compile(r"@exit ([0-9]{1,3})")
IPV6 = re.compile(r"\[([0-9a-fA-F:.]+)\]")
# The circle or cross a failed or missing unit gets where list-units shows one: never a unit.
GLYPHS = ("●", "×", "*", "x")
# The names /etc/hosts gives this computer by convention, and below .localhost, which
# systemd-resolved and glibc send to loopback. tools/eval_config.py uses these too.
LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "localhost4", "localhost6",
                         "localhost4.localdomain4", "localhost6.localdomain6", "ip6-localhost",
                         "ip6-loopback"})
# Words around a name rather than in it, as in "ssh myserver docker ps": with only these
# left, the whole list was asked for. Otherwise they are left out of the name, so "the minecraft
# server" is minecraft and "the plex service" is plex.
AROUND = {"server", "servers", "my", "our", "own", "the", "your", "on", "is", "are", "what",
          "which", "ssh", "docker", "ps", "systemctl", "systemd", "containers", "container",
          "services", "service", "all", "everything", "anything", "running", "processes",
          "programs", "apps", "applications", "*"}
# With docker or dockerd, docker's own daemon, as "dockerd", "the docker daemon" or "docker
# engine": the whole list, which starts with what systemctl says of docker.service, with the
# rows that have docker in them first.
DOCKERD = {"dockerd", "daemon", "engine"}
# Never part of a name, so not said back either: "my plex" asks after plex.
FILLER = {"my", "our", "own", "the", "your", "on", "is", "are", "what", "which", "all",
          "everything", "anything", "running"}
SERVER_WORDS = {"server", "servers"}
# Before the server's name, as in "my nas" or "the pi".
POSSESSIVES = {"my", "our", "your", "the", "own"}
# Around the server's name and never part of it, as in "my home nas", "the nas server" or
# "my nas box".
AROUND_A_HOST = POSSESSIVES | SERVER_WORDS | {"home", "box", "host"}
# Asked after ssh: the answer came over ssh, whichever ssh server gave it, so that is said, and
# then the services with ssh in their name, as systemd lists them.
SSH_NAMES = {"ssh", "sshd", "openssh", "opensshserver", "opensshd"}


class Unanswered(Exception):
    """Why the server can't be read, as a sentence to say."""


class Container(NamedTuple):
    name: str
    image: str
    status: str     # as docker gave it, such as "Up 3 hours" or "Restarting (1) 5 seconds ago"


class Unit(NamedTuple):
    name: str       # with its .service, as systemd gave it
    active: str     # its ACTIVE and SUB, such as active and exited
    sub: str


class Listing(NamedTuple):
    rows: list      # each Container or Unit read
    odd: int        # rows it had that couldn't be read
    cut: bool       # it went on past what was read


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


def _has_ssh_folder() -> bool:
    """ssh without -F makes ~/.ssh in the home folder passwd gives, not $HOME, when there is
    none, once it has connected: so with none it isn't started, and nothing is made."""
    try:
        return os.path.isdir(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh"))
    except (KeyError, OSError):
        return False


def _run(argv: list[str], env: dict) -> tuple[int, bytes, bytes]:
    """(exit code, output, errors), each capped. A server that sends too much is cut off there.
    Done once ssh has exited and its output has ended, even if something ssh started, such as a
    ProxyCommand, still holds its error pipe: that is killed with the rest of the group."""
    if not _has_ssh_folder():
        raise Unanswered(NO_SSH_FOLDER)
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
    # ssh's own refusal lists the methods it tried, as "Permission denied (publickey).": a
    # connect() the system refused says "port 22: Permission denied" and never reached the server.
    if "Permission denied (" in text or "Too many authentication failures" in text:
        return NO_KEY
    # Before the next one: ssh ends this warning with "Host key verification failed." too.
    if ("REMOTE HOST IDENTIFICATION HAS CHANGED" in text
            or "has changed and you have requested strict checking" in text):
        return HOST_KEY_CHANGED
    if "Host key verification failed" in text or "host key is known" in text:
        return HOST_KEY
    return CANT_CONNECT


def _sections(text: str, full: bool = False) -> dict:
    """{"docker.service": (exit code, lines, cut), "docker": ..., "systemd": ...} for each
    section the answer finished, where cut says it had more than MAX_LINES lines, of which only
    those were kept. With full, the output was cut off at MAX_OUTPUT_BYTES: the section it was
    cut off in is kept as cut, without the line the cut went through. That section only begins
    once its check has passed, and an error is never that long."""
    if full and not text.endswith("\n"):
        text = text.rpartition("\n")[0]
    found, current, lines, cut = {}, None, [], False
    for line in text.splitlines():
        if line in ("@docker.service", "@docker", "@systemd"):
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


def _containers(lines: list[str], cut: bool) -> Listing:
    """A line without a | is docker's own message, as the template has two. One with a | whose
    name, image or status doesn't look like one is counted, not read."""
    rows, odd = [], 0
    for line in lines:
        if "|" not in line:
            continue
        name, _, rest = line.partition("|")
        image, _, status = rest.partition("|")
        if CONTAINER.fullmatch(name) and IMAGE.fullmatch(image) and STATUS.fullmatch(status):
            rows.append(Container(name, image, status))
        else:
            odd += 1
    return Listing(rows, odd, cut)


def units(lines: list[str], cut: bool) -> Listing:
    """UNIT LOAD ACTIVE SUB DESCRIPTION, each line, after a circle or cross where systemd puts
    one. A line that isn't that is counted, not read."""
    rows, odd = [], 0
    for line in lines:
        words = line.split()
        if words and words[0] in GLYPHS and not UNIT.fullmatch(words[0]):
            words = words[1:]
        if not words:
            continue
        if len(words) >= 4 and UNIT.fullmatch(words[0]) and all(map(STATE.fullmatch, words[2:4])):
            rows.append(Unit(words[0], words[2], words[3]))
        else:
            odd += 1
    return Listing(rows, odd, cut)


def read(host: str) -> dict:
    """What the server lists, or Unanswered. "docker" and "systemd" are Listings, or None when
    that one wasn't read, with why in "docker_why" or "systemd_why", a sentence. "docker_service"
    is what systemctl is-active said of docker.service: its exit code and its word. "docker_asked"
    is why docker, once asked, didn't list its containers, without what systemctl said."""
    code, output, errors = _run(command(host), _env())
    if code == 255:
        raise Unanswered(_ssh_failed(errors))
    sections = _sections(output.decode("utf-8", "replace"), len(output) >= MAX_OUTPUT_BYTES)
    if not sections:
        raise Unanswered(UNREADABLE)
    unit_code, unit_lines, _ = sections.get("docker.service", (None, [], False))
    word = unit_lines[0].strip() if len(unit_lines) == 1 else ""
    found = {"docker_service": (unit_code, word if STATE.fullmatch(word) else None)}
    code, lines, cut = sections.get("docker", (None, [], False))
    found["docker"] = _containers(lines, cut) if code == 0 else None
    found["docker_asked"] = None if code in (0, NOT_ASKED) else _asked_why(code, lines)
    found["docker_why"] = None if code == 0 else _docker_why(code, lines, *found["docker_service"])
    code, lines, cut = sections.get("systemd", (None, [], False))
    found["systemd"] = units(lines, cut) if code == 0 else None
    found["systemd_why"] = None if code == 0 else _systemd_why(code)
    return found


def _docker_why(code: int | None, lines: list[str], unit_code: int | None,
                word: str | None) -> str:
    """Why docker's list wasn't read."""
    if code == NOT_ASKED:
        return _unasked(unit_code, word)
    if code in (NOT_FOUND, CANT_RUN):   # sh's word, not docker's: so what systemctl said first
        return f"{_says_of_docker(word)}, but {_asked_why(code, lines)}"
    return _asked_why(code, lines)


def _asked_why(code: int | None, lines: list[str]) -> str:
    """Why docker, once asked, didn't list its containers. Its own complaint is read only to pick
    a sentence."""
    if code in (NOT_FOUND, CANT_RUN):
        return DOCKER_MISSING[code]
    if code == TIMED_OUT_THERE:
        return DOCKER_SLOW
    if code is None:
        return DOCKER_UNREAD
    if "permission denied" in " ".join(lines).casefold():
        return DOCKER_REFUSED
    return f"docker gave an error (exit status {code})."


def _says_of_docker(word: str | None) -> str:
    """What systemctl is-active said of docker.service when it exited with 0, with its word."""
    return f"systemctl says docker.service is {word or 'active'}"


def _unasked(code: int | None, word: str | None) -> str:
    """Why docker wasn't asked: what systemctl is-active said of docker.service."""
    if code == NOT_ACTIVE:
        state = f"is {word}" if word else "isn't active"
        return f"systemctl says docker.service {state}, so I didn't ask docker."
    if code == NO_UNIT:
        return NO_DOCKER_UNIT
    if code == TIMED_OUT_THERE:
        return SYSTEMCTL_SLOW_DOCKER
    if code == NOT_FOUND:
        return NO_SYSTEMCTL_DOCKER
    if code is None or code == 0:       # 0, though docker wasn't asked: not as the command says
        return SYSTEMCTL_UNREAD_DOCKER
    return (f"systemctl didn't say whether docker.service is active (exit status {code}), so I "
            "didn't ask docker.")


def _systemd_why(code: int | None) -> str:
    if code == TIMED_OUT_THERE:
        return SYSTEMCTL_SLOW
    if code == NOT_FOUND:
        return NO_SYSTEMCTL
    if code is None:
        return SYSTEMCTL_UNREAD
    return f"systemctl didn't list the services (exit status {code})."


def _neither(found: dict) -> bool:
    """No systemctl, so neither list: docker is asked only where systemctl says it is active."""
    return found["docker_why"] == NO_SYSTEMCTL_DOCKER and found["systemd_why"] == NO_SYSTEMCTL


def squeezed(text: str) -> str:
    """The match rule's form of a name: in lower case, without -, _, . or spaces."""
    return re.sub(r"[-_. ]", "", text.casefold())


def matches(name: str, row) -> bool:
    """The one match rule: name, squeezed, inside a container's name or image as docker lists
    it, or inside a unit's name before its .service, squeezed too."""
    wanted_ = squeezed(name)
    if isinstance(row, Container):
        return wanted_ in squeezed(row.name) or wanted_ in squeezed(row.image)
    return wanted_ in squeezed(row.name.removesuffix(".service"))


def _said_name(name: str) -> str:
    """A name as said: docker lets a container's be as long as it likes, as a k8s_ one is, so a
    long one is cut after a whole part of it and marked, with no count of what is left."""
    if len(name) <= SAID_NAME_CHARS:
        return name
    cut = name[:SAID_NAME_CHARS]
    end = max(cut.rfind(mark) for mark in "-_.")
    return f"{cut[:end] if end > 0 else cut}…"


def said_row(row, name: str | None = None) -> str:
    """A row as its list gave it: a container's name and status, with its image when only that
    has name in it, or a unit's name with ACTIVE (SUB)."""
    if isinstance(row, Unit):
        return f"{_said_name(row.name)}, {row.active} ({row.sub})"
    if name and squeezed(name) not in squeezed(row.name):
        return f"{_said_name(row.name)}, image {_said_name(row.image)}, {row.status}"
    return f"{_said_name(row.name)}, {row.status}"


def said_rows(rows: list[str], limit: int) -> str:
    """With semicolons, as each has its state after a comma, and how many more there are."""
    said = rows[:limit]
    if len(rows) > limit:
        said.append(f"{len(rows) - limit} more")
    if len(said) == 1:
        return said[0]
    return "; ".join(said[:-1]) + "; and " + said[-1]


def plural(count: int, one: str) -> str:
    return f"{count} {one}" + ("" if count == 1 else "s")


def _notes(part: str, listing: Listing) -> list[str]:
    """What of a list wasn't read: rows that couldn't be, and more than was read."""
    notes = []
    if listing.odd:
        notes.append(f"{part} listed {plural(listing.odd, WHAT[part])} in a way I couldn't read.")
    if listing.cut:
        notes.append(f"{part}'s list went on past what I could read.")
    return notes


def _those(part: str, listing: Listing) -> str:
    """The rows looked through, as "the 31 containers docker lists", or "the first 300" of a
    list that went on past them."""
    count = plural(len(listing.rows), WHAT[part])
    if listing.odd:
        return f"the {count} I could read of {part}'s list"
    return f"the {'first ' if listing.cut else ''}{count} {part} lists"


def _on_your_server(sentences: list[str]) -> str:
    """The first one says where: every list here is the server's."""
    first = sentences[0]
    first = "n" + first[1:] if first.startswith("None") else first
    return " ".join([f"On your server, {first}", *sentences[1:]])


def about(found: dict, name: str, parts: tuple = PARTS) -> str:
    """Each row that has name in it, as its list gave it, or how many rows were looked through,
    and why a list wasn't read."""
    chunks = []
    for part in parts:
        listing = found[part]
        if listing is None:
            chunks.append(("why", found[f"{part}_why"], []))
            continue
        hits = [said_row(row, name) for row in listing.rows if matches(name, row)]
        if hits:
            chunks.append(("rows", f"{part} lists {said_rows(hits, SAID_ROWS)}.",
                           _notes(part, listing)))
        elif listing.rows:
            chunks.append(("none", _those(part, listing), _notes(part, listing)))
        else:
            chunks.append(("rows", _no_rows(part, listing), _notes(part, listing)))
    if [kind for kind, _, _ in chunks] == ["none", "none"]:
        looked = " or ".join(text for _, text, _ in chunks)
        return _on_your_server([f"None of {looked} has {name} in its {IN['docker']}.",
                                *(note for _, _, notes in chunks for note in notes)])
    sentences = []
    for (kind, text, notes), part in zip(chunks, parts):
        if kind == "none":
            text = f"None of {text} has {name} in its {IN[part]}."
        sentences += [text, *notes]
    return _on_your_server(sentences)


def _no_rows(part: str, listing: Listing) -> str:
    partly = " I could read" if listing.odd or listing.cut else ""
    return f"{part} lists no {WHAT[part]}s{partly}."


def everything(found: dict, first: str | None = None, docker_state: bool = False) -> str:
    """What each list has, a few rows of each with their state, and how many more, with any that
    have first in them before the rest, so the cap can't leave those out. With docker_state, what
    systemctl said of docker.service first whenever it said active, and so docker was asked: with
    why docker's list then wasn't read in the same sentence. Otherwise why docker wasn't asked
    comes first, which is what systemctl said or why it said nothing."""
    sentences = []
    code, word = found["docker_service"]
    asked = found["docker_asked"]
    said_of_docker = docker_state and code == 0 and (found["docker"] is not None or bool(asked))
    if said_of_docker:
        sentences.append(f"{_says_of_docker(word)}, but {asked}" if asked else
                         f"{_says_of_docker(word)}.")
    for part, limit in (("docker", SAID_CONTAINERS), ("systemd", SAID_SERVICES)):
        listing = found[part]
        if listing is None:
            if not (part == "docker" and said_of_docker):
                sentences.append(found[f"{part}_why"])
            continue
        if not listing.rows:
            sentences += [_no_rows(part, listing), *_notes(part, listing)]
            continue
        rows = sorted(listing.rows, key=lambda row: not (first and matches(first, row)))
        count = plural(len(rows), WHAT[part])
        count = (f"{count} I could read" if listing.odd else
                 f"at least {count}" if listing.cut else count)
        sentences += [f"{part} lists {count}: {said_rows([said_row(r) for r in rows], limit)}.",
                      *_notes(part, listing)]
    return _on_your_server(sentences)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _words(name) -> list[str]:
    """Its words, without "home" before server: a home server is a server. A unit's ".service"
    is left out, as a service is matched by the name before it."""
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


def _kept(args: dict, host: str) -> list[str]:
    """The words of the name that may be part of it. A host with a dot, or an address, is no
    program's name and is left out; a single label is kept, as on a server called plex or
    localai.lan, plex or localai is what is asked after. An address in any spelling, as
    [fd00::12] or fd00:0::12, is left out too."""
    dropped = FILLER | ({host.casefold()} - {_label(host)})
    return [w for w in _words(args.get("name"))
            if w not in dropped and not (is_address(host) and is_own_name(w, host))]


def _asks_after_docker(words: list[str]) -> bool:
    """docker's own daemon, as "the docker daemon", or docker with words around a name."""
    return bool({"docker", "dockerd"} & set(words)) and all(w in AROUND | DOCKERD for w in words)


def wanted(args: dict, host: str) -> str | None:
    """The one thing asked after, as said and matched, or None for the whole list. Never sent
    anywhere. Words around a name are left out of it."""
    words = _kept(args, host)
    if all(w in AROUND for w in words) or _asks_after_docker(words):
        return None
    if not NAME.fullmatch(" ".join(words)):
        raise Unanswered(BAD_NAME)
    name = " ".join(w for w in words if w not in AROUND)
    if len(squeezed(name)) < 2:
        raise Unanswered(BAD_NAME)
    return name


def _asks_after_ssh(args: dict) -> bool:
    """ssh, sshd or OpenSSH: the answer came over ssh."""
    words = [w for w in _words(args.get("name"))
             if w not in FILLER | SERVER_WORDS | {"service", "daemon"}]
    return _key(" ".join(words)) in SSH_NAMES


def _on_a_server(args: dict, host: str) -> bool:
    """An on that says server without naming this one, as "my server"."""
    on = " ".join(_words(args.get("on")))
    return any(w in SERVER_WORDS for w in on.split()) and not names_host(on, host)


def _as_a_program(args: dict, host: str, name: str, here: str | None) -> bool:
    """The server's label alone, asked after as a program: once this computer was looked at for
    it, or with an on that says server, as "Is Plex running on my server?" on plex.lan."""
    return is_own_name(name, host) and bool(here or _on_a_server(args, host))


def answer(args: dict, config, session=None, here: str | None = None,
           say_which: bool = False) -> str:
    """The sentence to say, from daemon code alone. Marks the conversation as having read
    something private once a list was read: what you run there is yours. here is what this
    computer was found to have, when it was asked first, and say_which names the server, for an
    on that may mean another one."""
    host = target(config)
    name = wanted(args, host)
    found = read(host)
    if _neither(found):
        raise Unanswered(NEITHER)
    if session is not None and (found["docker"] is not None or found["systemd"] is not None):
        session.tainted = True
    if _asks_after_ssh(args):
        text = f"{OVER_SSH} {about(found, 'ssh', ('systemd',))}"
    elif name is None:
        docker = _asks_after_docker(_kept(args, host))
        text = everything(found, "docker" if docker else None, docker)
    elif names_host(name, host) and not _as_a_program(args, host, name, here):
        text = everything(found, first=_label(host))
    else:
        text = about(found, name)
    if here:
        text = f"{here} {text}"
    if say_which:
        text = f"I checked the server in Settings, {_label(host) or host}. {text}"
    return shown(text, MAX_SAID_CHARS)


def voice(config) -> str:
    try:
        return config["voice"]
    except (TypeError, KeyError):
        return ""


def running(args: dict, config, session, here: str | None = None,
            say_which: bool = False) -> dict:
    """system.running on the server: the answer, or why there is none, said by the app."""
    try:
        text = answer(args, config, session, here, say_which)
    except Unanswered as why:
        text = _unanswered(str(why), config, here, say_which)
    session.after.append(Speak(text, voice(config)))
    return {APP_SAYS: True}


def _unanswered(why: str, config, here: str | None, say_which: bool) -> str:
    """Why the server didn't answer, still after what this computer was found to have, and
    which server was tried once one was: a sleeping server must not hide what is here."""
    if say_which and why not in (NO_SERVER, BAD_HOST, ON_THIS_COMPUTER, BAD_NAME, NO_SSH,
                                 NO_SSH_FOLDER):
        try:
            host = target(config)
            why = f"I tried the server in Settings, {_label(host) or host}. {why}"
        except Unanswered:
            pass
    if here:
        why = f"{here} {why}"
    return shown(why, MAX_SAID_CHARS)
