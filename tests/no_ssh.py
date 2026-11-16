"""Refuses ssh, docker and systemctl while switched on, started any of the ways listed here,
and the programs that start ssh themselves by its name: scp, sftp, slogin, sshpass and autossh.

An audit hook sees each start before it happens: subprocess.Popen, which subprocess.run and a
Popen imported by name both start through, os.posix_spawn, os.exec* and os.system. A start is
refused when one of its words is ssh, docker or systemctl, by name or by path, as in timeout 5
ssh x or env /usr/bin/ssh x, or when a word of a script one of its words holds is, one level
down, split as a shell splits it at spaces and at ; & | ( ) < > and backquotes, as in
sh -c "cd /&&ssh x" or o=$(ssh x). Double quotes don't keep a script's words together here, as
$(...) still runs inside them, and are taken out of a word as a shell takes them out, so "ssh"
and eval "ssh x" are ssh; single quotes do, so RemoteCommand's exec sh -c '...' is one word,
whose docker is a level further down. os.spawn* and pty.spawn fork first and start the program
in the child, where a refusal only ends the child, so every os.fork and os.forkpty is refused
while it is on. It can't see a script given on stdin, as sh with input=, which is in no word,
or another program that starts ssh itself, such as git or rsync with a host in its words.
ssh reads ~/.ssh/config from the home in the passwd file whatever HOME says, so a ProxyCommand
or Match exec of the developer's would run. An audit hook can't be taken out again, so this one
stays for the whole run and does nothing while off."""
import contextlib
import os
import re
import shlex
import sys

REFUSED = {"ssh", "scp", "sftp", "slogin", "sshpass", "autossh", "docker", "systemctl"}
REACHED = []        # what was refused, for the test to check
# Where each event has the program and its words: os.system has only a command line.
WATCHED = {"subprocess.Popen": (0, 1), "os.posix_spawn": (0, 1), "os.exec": (0, 1),
           "os.system": (None, 0)}
FORKS = {"os.fork", "os.forkpty"}
OPERATORS = "();<>|&`"
_ON = []


def _texts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, os.PathLike)):
        value = [value]
    return [os.fsdecode(v) if isinstance(v, (str, bytes, os.PathLike)) else str(v)
            for v in value]


def _split(text: str) -> list[str]:
    """A script's words as a shell has them, at its operators too: shlex.split keeps true;ssh
    as one word."""
    words = shlex.shlex(text, posix=True, punctuation_chars=OPERATORS)
    words.whitespace_split = True
    words.commenters = ""       # a # inside a word starts no comment in sh
    words.quotes = "'"
    try:
        return list(words)
    except ValueError:      # a quote that doesn't close
        return re.split(rf"[\s'{re.escape(OPERATORS)}]+", text)


def refused(words: list[str]) -> bool:
    """Whether a start with these words runs one of REFUSED. A shell takes double
    quotes out of a word, so "/usr/bin/ssh" and s"s"h are ssh to it."""
    return any(os.path.basename(piece.replace('"', "")) in REFUSED
               for word in words for piece in (word, *_split(word)))


def _guard(event: str, args: tuple) -> None:
    if not _ON:
        return
    if event in FORKS:
        REACHED.append([event])
        raise AssertionError(f"a test called {event}, which os.spawn* and pty.spawn start through")
    if event not in WATCHED:
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
