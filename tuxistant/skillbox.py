"""The sandbox a code skill runs in: bubblewrap, shellgate's seccomp filter and memory cap.

A code skill is someone else's program. It sees /usr, a few files from /etc, its own folder read
only at /skill, its own state folder at /state and the skill kit. No home folder, no session bus,
no runtime folder, no display, and no network unless its manifest asks for it. A list of hosts
in the manifest cannot be enforced here, so any network permission shares the whole network.

The network permission shares the computer's own network namespace, which also holds services
meant only for this computer: adb on 127.0.0.1, CUPS, the display's abstract sockets. Landlock,
set up inside the sandbox before the skill's code starts, keeps those out of reach: no abstract
socket from outside the sandbox, and TCP only to the ports a web or name server uses.
"""
import ctypes
import os
from pathlib import Path

from .shellgate import (BWRAP, PRLIMIT, SANDBOX_MEMORY_BYTES, SANDBOX_TMP_BYTES,
                        seccomp_program)

SKILLKIT = Path(__file__).with_name("skillkit.py")
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
STATE_ROOT = STATE_HOME / "tuxistant" / "skills"
SKILL_AT, STATE_AT, KIT_AT = "/skill", "/state", "/run/tuxistant"
PYTHON, NODE = "/usr/bin/python3", "/usr/bin/node"
SYSTEM_LINKS = ("/bin", "/lib", "/lib64", "/sbin")
ETC_ALWAYS = ("/etc/ssl/certs", "/etc/ssl/openssl.cnf", "/etc/ca-certificates",
              "/etc/ca-certificates.conf", "/etc/ld.so.cache")
ETC_NETWORK = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf")
ENV = {"PATH": "/usr/bin:/bin", "HOME": STATE_AT, "LANG": "C.UTF-8",
       "PYTHONPATH": KIT_AT, "PYTHONDONTWRITEBYTECODE": "1"}
# -I ignores PYTHONPATH and leaves out the script's folder, so both are put back by hand, and
# only those two: nothing from the environment or the user's site folder.
PYTHON_START = ("import runpy, sys; sys.argv = sys.argv[1:]; "
                f"sys.path[:0] = [{SKILL_AT!r}, {KIT_AT!r}]; "
                "runpy.run_path(sys.argv[0], run_name='__main__')")

# Landlock's numbers are the same on every architecture.
LANDLOCK_CREATE, LANDLOCK_ADD_RULE, LANDLOCK_RESTRICT = 444, 445, 446
LANDLOCK_SCOPE_ABI = 6          # the first that can shut out abstract sockets (Linux 6.12)
NETWORK_PORTS = (53, 80, 443)   # TCP a skill may connect to; it may listen on none
# Run by trusted code inside the sandbox, after bwrap set no_new_privs and before the skill's own
# code. Landlock can't be undone, and it holds across exec.
LOCK = f"""import ctypes as c, os, struct, sys
libc = c.CDLL(None, use_errno=True)
libc.syscall.restype = c.c_long
ruleset = struct.pack("QQQ", 0, 3, 1)
fd = libc.syscall(c.c_long({LANDLOCK_CREATE}), ruleset, c.c_long(len(ruleset)), c.c_long(0))
if fd < 0:
    sys.exit("The skill's network could not be limited.")
for port in {NETWORK_PORTS!r}:
    rule = struct.pack("QQ", 2, port)
    if libc.syscall(c.c_long({LANDLOCK_ADD_RULE}), c.c_long(fd), c.c_long(2), rule, c.c_long(0)):
        sys.exit("The skill's network could not be limited.")
if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(c.c_long({LANDLOCK_RESTRICT}), c.c_long(fd),
                                               c.c_long(0)):
    sys.exit("The skill's network could not be limited.")
os.close(fd)
"""
EXEC = "os.execv(sys.argv[1], sys.argv[1:])"

NO_RUN = "The skill does not say how to run it."
BAD_PATH = "The skill's run line may only name files inside its own folder."
NO_SCRIPT = "The skill's run line names a file that is not in its folder."
BAD_PERMISSION = "A code skill may only ask for the network."
NO_SANDBOX = "The sandbox for skills is not working on this computer."
OLD_KERNEL = ("This skill needs the network, and this computer's Linux is too old to keep it "
              "away from the rest of the computer. It needs Linux 6.12 or newer.")


def available() -> bool:
    return (os.access(BWRAP, os.X_OK) and os.access(PRLIMIT, os.X_OK)
            and seccomp_program() is not None and SKILLKIT.is_file())


def landlock_abi() -> int:
    """0 where Landlock is missing or switched off."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        version = libc.syscall(ctypes.c_long(LANDLOCK_CREATE), None, ctypes.c_long(0),
                               ctypes.c_long(1))
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def state_dir(skill_id: str) -> Path:
    if not skill_id or "/" in skill_id or skill_id in (".", "..") or "\0" in skill_id:
        raise ValueError("That skill id cannot name a folder.")
    return STATE_ROOT / skill_id


def _launch_class():
    from .mcp import Launch     # written alongside; imported late so this module loads alone
    return Launch


def _inside(folder: Path, name: str) -> Path:
    """A path in the run line, checked against the real folder, links followed."""
    if not isinstance(name, str) or not name or "\0" in name:
        raise ValueError(BAD_PATH)
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(BAD_PATH)
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise ValueError(NO_SCRIPT)
    return path


def command(folder: Path, run: list[str], network: bool = False) -> list[str]:
    """The argv inside the sandbox. Every word is held to the folder, not just the script, so an
    argument cannot point the interpreter somewhere else either. With the network, Landlock goes
    on first: inside Python's own start for a Python skill, or Python then exec for the rest."""
    inner = _command(folder, run)
    if not network:
        return inner
    if inner[0] == PYTHON:
        return [*inner[:4], LOCK + inner[4], *inner[5:]]
    return [PYTHON, "-I", "-B", "-c", LOCK + EXEC, *inner]


def _command(folder: Path, run: list[str]) -> list[str]:
    if not isinstance(run, list) or not run:
        raise ValueError(NO_RUN)
    for word in run[1:]:
        if not isinstance(word, str) or "\0" in word or word.startswith("/") \
                or ".." in word.split("/"):
            raise ValueError(BAD_PATH)
    first, rest = run[0], run[1:]
    if first in ("python3", "node"):
        if not rest:
            raise ValueError(NO_RUN)
        script = _inside(folder, rest[0]).relative_to(folder).as_posix()
        if first == "node":
            return [NODE, "--", f"{SKILL_AT}/{script}", *rest[1:]]
        return [PYTHON, "-I", "-B", "-c", PYTHON_START, f"{SKILL_AT}/{script}", *rest[1:]]
    program = _inside(folder, first).relative_to(folder).as_posix()
    return [f"{SKILL_AT}/{program}", *rest]


def _wants_network(permissions: dict) -> bool:
    if not isinstance(permissions, dict):
        raise ValueError(BAD_PERMISSION)
    if any(value for name, value in permissions.items() if name != "network"):
        raise ValueError(BAD_PERMISSION)
    return bool(permissions.get("network"))


def _system(network: bool) -> list[str]:
    args = ["--ro-bind", "/usr", "/usr"]
    for path in SYSTEM_LINKS:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            args += ["--ro-bind", path, path]
    for path in ETC_ALWAYS + (ETC_NETWORK if network else ()):
        args += ["--ro-bind-try", path, path]
    return args


def _env() -> list[str]:
    return [word for name, value in ENV.items() for word in ("--setenv", name, value)]


def bwrap_args(folder: Path, state: Path, network: bool) -> list[str]:
    """--unshare-all takes the network away; --share-net gives it back. The root is left read
    only, so /state and /tmp are the only places the skill can write."""
    return ["--unshare-all", "--unshare-user", "--disable-userns",
            *(["--share-net"] if network else []),
            "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv", *_env(),
            *_system(network), "--proc", "/proc", "--dev", "/dev",
            "--size", str(SANDBOX_TMP_BYTES), "--tmpfs", "/tmp",
            "--ro-bind", str(folder), SKILL_AT, "--bind", str(state), STATE_AT,
            "--ro-bind", str(SKILLKIT), f"{KIT_AT}/skillkit.py",
            "--remount-ro", "/", "--chdir", SKILL_AT]


def _make_state(state: Path) -> Path:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    return state.resolve()


def _filter_fd(program: bytes) -> int:
    """The filter goes through a pipe, as bwrap reads it from a descriptor. It fits the pipe's
    buffer, so writing it all before bwrap starts cannot block."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, program)
    except OSError:
        os.close(read_end)
        raise
    finally:
        os.close(write_end)
    return read_end


def prepare(folder: Path, run: list[str], permissions: dict, state_dir: Path):
    """An mcp.Launch for one start of the skill. Its pass_fds hold the filter's descriptor,
    which Launch.close() closes once the process has been spawned."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(NO_SCRIPT)
    network = _wants_network(permissions)
    inner = command(folder, run, network)
    program = seccomp_program()
    if program is None or not available():
        raise ValueError(NO_SANDBOX)
    if network and landlock_abi() < LANDLOCK_SCOPE_ABI:
        raise ValueError(OLD_KERNEL)
    state = _make_state(Path(state_dir))
    launch_class = _launch_class()
    fd = _filter_fd(program)
    argv = [PRLIMIT, f"--as={SANDBOX_MEMORY_BYTES}", BWRAP, "--seccomp", str(fd),
            *bwrap_args(folder, state, network), "--", *inner]
    return launch_class(argv=argv, pass_fds=(fd,), cwd="/", env={})
