"""Refuses ssh, docker and systemctl while switched on, however a test starts them.

An audit hook sees each start before it happens: subprocess.Popen, which subprocess.run and a
Popen imported by name both start through, os.posix_spawn, os.exec*, os.spawn* and os.system.
A start is refused when one of its words is ssh, docker or systemctl, by name or by path, as in
timeout 5 ssh x or env /usr/bin/ssh x, or when a word of a script one of its words holds is, one
level down, as in sh -c "ssh x". ssh reads ~/.ssh/config from the home in the passwd file
whatever HOME says, so a ProxyCommand or Match exec of the developer's would run. An audit hook
can't be taken out again, so this one stays for the whole run and does nothing while off."""
import contextlib
import os
import shlex
import sys

REFUSED = {"ssh", "docker", "systemctl"}
REACHED = []        # what was refused, for the test to check
# Where each event has the program and its words: os.system has only a command line.
WATCHED = {"subprocess.Popen": (0, 1), "os.posix_spawn": (0, 1), "os.exec": (0, 1),
           "os.spawn": (1, 2), "os.system": (None, 0)}
_ON = []


def _texts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, os.PathLike)):
        value = [value]
    return [os.fsdecode(v) if isinstance(v, (str, bytes, os.PathLike)) else str(v)
            for v in value]


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:      # a quote that doesn't close
        return text.split()


def refused(words: list[str]) -> bool:
    """Whether a start with these words runs ssh, docker or systemctl."""
    return any(os.path.basename(piece) in REFUSED
               for word in words for piece in (word, *_split(word)))


def _guard(event: str, args: tuple) -> None:
    if not _ON or event not in WATCHED:
        return
    program, words = WATCHED[event]
    started = _texts(args[words])
    if refused(started + (_texts(args[program]) if program is not None else [])):
        REACHED.append(started)
        raise AssertionError(f"a test started {started[:1]}")


@contextlib.contextmanager
def refusing():
    """Switched on for what runs inside."""
    _ON.append(True)
    try:
        yield REACHED
    finally:
        _ON.pop()


sys.addaudithook(_guard)
