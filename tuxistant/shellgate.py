"""The shell skill: a strict parser, a read-only sandbox and the approval gate.

A command the parser fully reads, and whose every stage is on the read-only allowlist with exactly
the listed arguments, runs at once, inside bubblewrap. The allowlist only reads intent; the sandbox
is what holds, so it has to hold even if a stage turns out to be hostile, and what runs there is
rebuilt from the parsed words, not the text the model sent. Everything else goes to the approval
card as the exact string. A deletion is counted first and moved to the Trash when it is simple
enough to count, and the card says it is permanent when it is not.

What lies in the home folder is not to be trusted: it may have come out of an archive. Nothing in
there may stop the sandbox from starting, or turn a command the allowlist accepted into an offer
to run it on the host.
"""
import asyncio
import collections
import errno
import functools
import glob
import itertools
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import string
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIR, DATA_DIR
from .gate import MAX_DETAIL_CHARS, Ask, printable, shown

BASH = "/bin/bash"
BWRAP = "/usr/bin/bwrap"
GIO = "/usr/bin/gio"
PKEXEC = "/usr/bin/pkexec"
PRLIMIT = "/usr/bin/prlimit"

MAX_COMMAND_CHARS = 2000
MAX_OUTPUT_CHARS = 3000
MAX_OUTPUT_BYTES = MAX_OUTPUT_CHARS * 4 + 4     # the longest UTF-8 character is 4 bytes
MAX_LIST_BYTES = 8 << 20                        # find's file list for a deletion card
AUTO_TIMEOUT_S = 20
ASK_TIMEOUT_S = 120
MEASURE_S = 5                                   # counting files for the card
MEASURE_MAX_ENTRIES = 200_000
TRASH_BATCH = 200
SANDBOX_TMP_BYTES = 64 << 20
SANDBOX_MEMORY_BYTES = 1536 << 20               # of address space, for each process in there
KILLED = 128 + signal.SIGKILL                   # the exit code of a command the kernel killed

ASK_TITLE = "Run this command? It runs outside the sandbox and can change anything."
NO_SANDBOX_TITLE = "The sandbox is not working. Run this command without it?"
PERMANENT_TITLE = "Delete permanently? This cannot be undone."
ADMIN_TITLE = "Run this command as administrator?"
ADMIN_PERMANENT_TITLE = "Delete permanently as administrator? This cannot be undone."
SUDO_ALONE = ("That needs administrator rights, so ask for it on its own: one plain sudo command "
              "with nothing else in the line.")
TOO_MUCH = "That would delete your home folder or a system folder, so I will not do it."
NOTHING_TO_DELETE = "Nothing matched, so nothing was deleted."
SANDBOX_FAILED = "The sandbox would not start, so that did not run."
MEMORY_KILLED = "That was stopped before it finished, most likely for using too much memory."
SEALED = ("There is too much there to check for secrets or to hide one file at a time, so that "
          "folder stays hidden and that did not run.")

# Some everyday commands are not on the allowlist on purpose. ip, ss and hostname -I would describe
# the sandbox's empty network, ps and top its three processes, who its missing utmp and id its
# unmapped groups: a confident wrong answer, where the approval card gets the right one.
SANDBOX_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/snap/bin"
HIDDEN_TREES = ("/run", "/tmp", "/var/tmp")

# Environment for commands that run outside the sandbox. A list of what passes, not of what to
# remove, so a key in the daemon's environment cannot reach them, nor can SSH_AUTH_SOCK.
HOST_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
            "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_DATA_HOME",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_DIRS")

# What a read-only command never sees. Everything directly in the home folder that starts with a
# dot is hidden unless it is listed here, ~/.config and ~/.local with all they hold, so what a new
# app keeps there is hidden from its first day. That is how the Trash stays out of reach too: a key
# moved there must not come back into view. A masked file is /dev/null on a nodev mount, which
# cannot even be opened, so what a permitted program must read is on this list: git stops dead at
# an unreadable .gitconfig. A question needs nothing else that lives there.
VISIBLE_DOT_ENTRIES = frozenset({".gitconfig"})
HIDDEN_ENTRIES = frozenset({"snap"})                # every snap app's data, logins included
APP_FOLDERS = (str(CONFIG_DIR), str(DATA_DIR))      # wherever XDG_CONFIG_HOME has put them
GIT_CONFIG = "/.git/config"                         # a remote's URL can hold a token

# Secrets are also scattered by name: a .env in every project, a signing key in an app's folder.
SECRET_NAME = re.compile(
    r"(?:^|/)(?:\.env[^/]*|[^/]*\.env|\.netrc|id_(?:rsa|dsa|ecdsa|ed25519)[^/]*|adbkey[^/]*"
    r"|[^/]*\.(?:pem|key|p12|pfx|ppk|kdbx|keystore|jks)|cookies(?:-journal)?"
    r"|login data[^/]*|web data[^/]*|local state|accounts\.json|\.git/config"
    r"|[^/]*(?:credential|secret|token)s?(?:\.[^/]*)?)$", re.IGNORECASE)
SCAN_DEPTH = 4
SCAN_MAX_FOLDERS = 30_000
SCAN_EVERY_S = 300      # the walk takes a fraction of a second, too long for every question
SCAN_SKIP = {"node_modules", "__pycache__", "site-packages", "venv", "target", "dist", "build",
             "vendor", "flatpak", "containers", "pnpm", "uv"}
MAX_SCATTERED = 500         # bwrap's cost grows with the square: 500 mounts take 0.3 seconds
MAX_REBUILT_ENTRIES = 300   # what may be mounted back into folders rebuilt around a link

PLAIN = set(string.ascii_letters + string.digits + "_-./,:=+%@^!")
WILDCARDS = set("*?")


def _home() -> str:
    return str(Path.home())


def _within(path: str, tops) -> bool:
    return any(path == top or path.startswith(top + "/") for top in tops)


# The parser. It accepts plain words, single quotes, double quotes with nothing to expand, and the
# operators |, && and ;. Anything else, it does not read at all.

@dataclass(frozen=True)
class Word:
    text: str                       # what the program receives, quotes gone
    live: frozenset = frozenset()   # positions in text of unquoted * and ?


@dataclass(frozen=True)
class Stage:
    words: tuple
    join: str       # the operator after this stage, "" at the end


def _literal(char: str) -> bool:
    """Inside quotes. Control and bidirectional characters can hide what a word really says."""
    return char in " \t" or char.isprintable()


def _word(chars: list) -> Word | None:
    """chars is (char, quoted) pairs. Only ~ alone or ~/ at the start means the home folder."""
    if chars[:1] == [("~", False)]:
        if chars[1:2] not in ([], [("/", False)]):
            return None
        chars = [(char, True) for char in _home()] + chars[1:]
    return Word("".join(char for char, _ in chars),
                frozenset(i for i, (char, quoted) in enumerate(chars)
                          if not quoted and char in WILDCARDS))


def _double_quoted(command: str, start: int) -> tuple[str, int] | None:
    """The text between the quote at start and its partner, and where the partner is."""
    out, i = [], start + 1
    while i < len(command) and command[i] != '"':
        char = command[i]
        if char in "$`" or not _literal(char):
            return None
        if char == "\\":
            i += 1
            if command[i:i + 1] in ("", '"', "$", "`", "\\", "\n"):
                return None     # bash would change these, so they are not taken as written
            out.append(char)
            char = command[i]
        out.append(char)
        i += 1
    return ("".join(out), i) if i < len(command) else None


def _tokens(command: str) -> list | None:
    """Words and operators, or None at the first character not fully understood."""
    tokens, chars, i = [], None, 0

    def finish() -> bool:
        nonlocal chars
        if chars is None:
            return True
        word, chars = _word(chars), None
        tokens.append(word)
        return word is not None

    while i < len(command):
        char = command[i]
        if char in " \t":
            if not finish():
                return None
            i += 1
        elif char in ";|&":
            if not finish():
                return None
            operator = "&&" if command.startswith("&&", i) else char
            if operator == "&" or command.startswith("||", i):
                return None
            tokens.append(operator)
            i += len(operator)
        elif char in "'\"":
            if chars == [("~", False)]:
                return None     # ~"" is not the home folder to bash, and ""~ is not either
            if char == "'":
                end = command.find("'", i + 1)
                text = command[i + 1:end]
                if end < 0 or not all(map(_literal, text)):
                    return None
            else:
                found = _double_quoted(command, i)
                if found is None:
                    return None
                text, end = found
            chars = (chars or []) + [(c, True) for c in text]
            i = end + 1
        elif char == "~" and chars is not None:
            return None
        elif char in PLAIN or char in WILDCARDS or char == "~" or char.isalnum():
            chars = (chars or []) + [(char, False)]
            i += 1
        else:
            return None
    return tokens if finish() else None


def parse(command: str) -> list[Stage] | None:
    """The stages of command, or None if any part of it is not fully understood."""
    tokens = _tokens(command)
    if not tokens:
        return None
    stages, words = [], []
    for token in tokens:
        if isinstance(token, Word):
            words.append(token)
        elif not words:
            return None         # an operator with nothing before it
        else:
            stages.append(Stage(tuple(words), token))
            words = []
    if words:
        stages.append(Stage(tuple(words), ""))
    elif stages[-1].join != ";":
        return None             # "ls |" and "ls &&" are not finished
    return stages


def _live(word: Word) -> str:
    """A word for bash, wildcards working and all else quoted. The ./ stops a file called -o or
    --compress-program=x from turning into an option when the wildcard expands."""
    runs = itertools.groupby(enumerate(word.text), lambda pair: pair[0] in word.live)
    shell = "".join("".join(c for _, c in run) if wild else shlex.quote("".join(c for _, c in run))
                    for wild, run in runs)
    return shell if word.text.startswith("/") else "./" + shell


# The allowlist.

@dataclass
class Seen:
    operands: list = field(default_factory=list)    # positions in the argument list
    options: dict = field(default_factory=dict)     # name: values


class Rule:
    """What a program may be given. Only what is listed passes and it is matched exactly, so a
    new option, an abbreviation or a bundled flag fails closed. A long option's value counts only
    after =, because reading a word as a value skips checking it, and being wrong about that is
    how an option gets past. A short flag in values does take the next word, as getopt does.

    A program prints the contents of files unless it says otherwise. prints=False is a claim about
    everything it can be told to do here. filters=True is a claim that, with no file named, on a
    pipe, it reads its stdin and nothing else. reaches lists the options that break that."""

    def __init__(self, flags="", values="", long="", *, script=0, script_flags=(), files=True,
                 prints=True, filters=False, reaches=frozenset(), most=None, digits=False, run=(),
                 check=None):
        self.flags, self.values, self.long = flags, values, {}
        for spec in long.split():
            name, takes_value, _ = spec.partition("=")
            self.long[name] = bool(takes_value)
        self.script, self.script_flags = script, script_flags   # operands that are not files
        self.files, self.prints, self.most, self.digits = files, prints, most, digits
        self.filters, self.reaches = filters, reaches
        self.run, self.check = run, check

    def read(self, words) -> Seen | None:
        """Walks the arguments as getopt does, options after operands included."""
        seen, ended, i = Seen(), False, 0
        while i < len(words):
            text = words[i].text
            at, i = i, i + 1
            if ended or text == "-" or not text.startswith("-"):
                seen.operands.append(at)
            elif text == "--":
                ended = True
            elif self.digits and re.fullmatch("-[0-9]+", text):
                continue
            elif text.startswith("--"):
                name, equals, value = text.partition("=")
                if name not in self.long or (equals and not self.long[name]):
                    return None
                seen.options.setdefault(name, []).append(value)
            else:
                i = self._read_short(words, i, text, seen)
                if i is None:
                    return None
        return seen

    def _read_short(self, words, i, text, seen) -> int | None:
        """-rn, -n5 and -n 5. Returns where the next argument is."""
        for at, flag in enumerate(text[1:], 2):
            if flag in self.values:
                value = text[at:]
                if not value:
                    if i == len(words):
                        return None
                    value, i = words[i].text, i + 1
                seen.options.setdefault("-" + flag, []).append(value)
                return i
            if flag not in self.flags:
                return None
            seen.options.setdefault("-" + flag, [])
        return i

    def accepts(self, words, seen: Seen) -> bool:
        if self.most is not None and (len(seen.operands) > self.most
                                      or any(words[i].live for i in seen.operands)):
            return False    # uniq's second operand is an output file: no wildcard may make one
        return self.check is None or self.check(words, seen)

    def _leading(self, seen: Seen) -> int:
        return 0 if any(flag in seen.options for flag in self.script_flags) else self.script

    def argv(self, program: str, words, seen: Seen) -> list[str]:
        files = set(seen.operands[self._leading(seen):]) if self.files else set()
        return [*(self.run or (program,)),
                *(_live(word) if word.live and i in files else shlex.quote(word.text)
                  for i, word in enumerate(words))]

    def reads_files(self, seen: Seen, piped: bool) -> bool:
        if self.reaches & seen.options.keys():
            return True
        passing_along = self.filters and piped and len(seen.operands) <= self._leading(seen)
        return self.prints and not passing_along


SED_ADDRESS = r"(?:[0-9]+|\$|/(?:[^/\\]|\\.)*/I?)"
SED_COMMAND = re.compile(
    rf"\s*(?:{SED_ADDRESS}(?:,{SED_ADDRESS})?\s*!?\s*)?"
    r"(?:[pdq=]|s([^\w\s\\])(?:(?!\1)[^\\]|\\.)*\1(?:(?!\1)[^\\]|\\.)*\1[gpiIm0-9]*)\s*")
AWK_UNSAFE = re.compile(r"system|getline|close|fflush|ENVIRON|PROCINFO|SYMTAB|FUNCTAB|ARGV|ARGC"
                        r"|@|/inet|/dev/")


def _sed_safe(script: str) -> bool:
    """Addresses with p, d, q, = and s///, separated by ;. Not r, w, e or the s flags w and e."""
    at = 0
    while True:
        match = SED_COMMAND.match(script, at)
        if match is None:
            return False
        at = match.end()
        if at == len(script):
            return True
        if script[at] != ";":
            return False
        at += 1
        if at == len(script):
            return True


def _sed_ok(words, seen: Seen) -> bool:
    scripts = seen.options.get("-e", []) + seen.options.get("--expression", [])
    if not scripts and seen.operands:
        scripts = [words[seen.operands[0]].text]
    return bool(scripts) and all(map(_sed_safe, scripts))


def _awk_safe(program: str) -> bool:
    """Reads the raw text, strings and regexes included, so a false alarm only costs a click.
    Before the first { a > can only be a comparison. After it there is no telling a comparison
    from a redirect without parsing awk, and a brace or a quote in a regex would fool a count."""
    if AWK_UNSAFE.search(program) or "|" in program.replace("||", ""):
        return False
    return ">" not in program.partition("{")[2]


def _awk_ok(words, seen: Seen) -> bool:
    return bool(seen.operands) and _awk_safe(words[seen.operands[0]].text)


def _date_ok(words, seen: Seen) -> bool:
    return all(words[i].text.startswith("+") for i in seen.operands)     # anything else sets it


GREP = Rule(
    "EFGPivwxcLlnHhoqsrRaIbUZz", "eABCm",
    "--extended-regexp --fixed-strings --basic-regexp --perl-regexp --ignore-case "
    "--no-ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches "
    "--files-without-match --line-number --with-filename --no-filename --only-matching --quiet "
    "--silent --no-messages --recursive --dereference-recursive --text --byte-offset --null "
    "--null-data --regexp= --context= --after-context= --before-context= --max-count= --include= "
    "--exclude= --exclude-dir= --label= --color= --colour= --binary-files=",
    script=1, script_flags=("-e", "--regexp"), filters=True, digits=True,
    reaches=frozenset({"-r", "-R", "--recursive", "--dereference-recursive"}))  # the working folder
# Only ever names, sizes and facts about the system. file is not one of these: it quotes what it
# finds inside, a script's first line, a document's title. --check prints the names a file lists.
FACTS = {"prints": False}
SHA = Rule("bct", long="--binary --check --text", reaches=frozenset({"-c", "--check"}), **FACTS)
TEXT = {"files": False, **FACTS}     # never opens a file it is given, so wildcards stay words

RULES = {
    "cd": Rule(most=1, **FACTS),
    "ls": Rule("aAbBcCdfFgGhHiklLmnNopqQrRsStuUvxXZ1", "ITw",
               "--all --almost-all --human-readable --si --recursive --reverse --size --directory "
               "--dereference --classify --inode --numeric-uid-gid --group-directories-first "
               "--no-group --full-time --sort= --time= --time-style= --block-size= --color= "
               "--format= --hide= --ignore=", **FACTS),
    "du": Rule("abchHklLmPsSxD0", "dBt",
               "--all --apparent-size --bytes --total --human-readable --si --summarize "
               "--one-file-system --dereference --inodes --separate-dirs --max-depth= "
               "--block-size= --threshold= --exclude= --time= --time-style=", **FACTS),
    "df": Rule("ahHiklmPTv", "Btx",
               "--all --human-readable --si --inodes --local --portability --print-type --total "
               "--block-size= --type= --exclude-type= --output=", **FACTS),
    "stat": Rule("Lft", "c", "--dereference --file-system --terse --format= --printf=", **FACTS),
    "file": Rule("bhiLk", long="--brief --mime --mime-type --mime-encoding --dereference "
                               "--no-dereference --keep-going"),
    "wc": Rule("clmwL", long="--bytes --chars --lines --words --max-line-length", **FACTS),
    "cat": Rule("AbeEnstTuv", long="--show-all --number --number-nonblank --squeeze-blank "
                                   "--show-ends --show-tabs --show-nonprinting", filters=True),
    "head": Rule("qvz", "nc", "--quiet --silent --verbose --zero-terminated --lines= --bytes=",
                 filters=True, digits=True),
    "tail": Rule("qvz", "nc", "--quiet --silent --verbose --zero-terminated --lines= --bytes=",
                 filters=True, digits=True),
    "grep": GREP, "egrep": GREP, "fgrep": GREP,
    "sort": Rule("bdfgiMhnRrVcCmsuz", "kt",
                 "--ignore-leading-blanks --dictionary-order --ignore-case "
                 "--general-numeric-sort --ignore-nonprinting --month-sort --human-numeric-sort "
                 "--numeric-sort --random-sort --reverse --version-sort --merge --stable --unique "
                 "--zero-terminated --check= --key= --field-separator= --sort=", filters=True),
    "uniq": Rule("cdDiuz", "fsw", "--count --repeated --ignore-case --unique --zero-terminated "
                                  "--all-repeated= --group= --skip-fields= --skip-chars= "
                                  "--check-chars=", filters=True, most=1),
    "cut": Rule("snz", "bcdf", "--complement --only-delimited --zero-terminated --bytes= "
                               "--characters= --delimiter= --fields= --output-delimiter=",
                filters=True),
    "tr": Rule("cCdst", long="--complement --delete --squeeze-repeats --truncate-set1",
               most=2, **TEXT),
    # --sandbox refuses r, w and e in sed and system() and redirects in gawk, should a script
    # get past the checks above.
    "sed": Rule("nErsuz", "e", "--quiet --silent --regexp-extended --separate --unbuffered "
                               "--null-data --expression=",
                script=1, script_flags=("-e", "--expression"), filters=True,
                run=("sed", "--sandbox"), check=_sed_ok),
    "awk": Rule(values="Fv", long="--field-separator= --assign=", script=1, filters=True,
                run=("gawk", "--sandbox") if shutil.which("gawk", path=SANDBOX_PATH) else (),
                check=_awk_ok),
    "diff": Rule("uqrywiBbNs", long="--brief --unified= --recursive --ignore-case "
                                    "--ignore-all-space --side-by-side"),
    "tree": Rule("adfghlpsuDFx", "LIP", "--dirsfirst --du --noreport", **FACTS),
    "which": Rule("a", **TEXT),
    "basename": Rule("az", "s", **TEXT),
    "dirname": Rule("z", **TEXT),
    "realpath": Rule("emLPqsz", **FACTS),
    "readlink": Rule("efmnqsvz", **FACTS),
    "echo": Rule("neE", **TEXT),
    "pwd": Rule(most=0, **FACTS),
    "whoami": Rule(most=0, **FACTS),
    "hostname": Rule(most=0, **FACTS),
    "arch": Rule(most=0, **FACTS),
    "nproc": Rule(long="--all", most=0, **FACTS),
    "lscpu": Rule("J", most=0, **FACTS),
    "lsusb": Rule("t", most=0, **FACTS),
    "lspci": Rule("knv", most=0, **FACTS),
    "lsmod": Rule(most=0, **FACTS),
    "lsb_release": Rule("acdirs", long="--all --short", most=0, **FACTS),
    "dpkg": Rule("lLsS", **TEXT),
    "date": Rule("uR", "d", "--utc --universal --rfc-email --date= --iso-8601= --rfc-3339=",
                 most=1, check=_date_ok, **TEXT),
    "uptime": Rule("ps", long="--pretty --since", most=0, **FACTS),
    "free": Rule("bkmghltvw", long="--bytes --kilo --mega --giga --human --si --lohi --total "
                                   "--wide", most=0, **FACTS),
    "uname": Rule("asnrvmpio", long="--all --kernel-name --nodename --kernel-release "
                                    "--kernel-version --machine --processor "
                                    "--hardware-platform --operating-system", most=0, **FACTS),
    "lsblk": Rule("abdDfiJlmnOprstSz", "eIoxE",
                  "--all --bytes --nodeps --discard --fs --ascii --json --list --perms "
                  "--noheadings --paths --pairs --raw --scsi --topology --output= --exclude= "
                  "--include= --sort=", **FACTS),
    "md5sum": SHA, "sha1sum": SHA, "sha256sum": SHA, "sha512sum": SHA,
}

GIT_LOG = ("--oneline --stat --shortstat --numstat --name-only --name-status --graph --all "
           "--decorate --no-decorate --abbrev-commit --no-merges --merges --first-parent "
           "--reverse --follow --patch --no-patch --no-color --cached --staged --since= --until= "
           "--after= --before= --author= --grep= --max-count= --format= --pretty= --date=")
# A repository is as hostile as the archive it came in, and git runs what its config names. Every
# git stage counts as printing files, as whatever such a program writes comes back with the answer.
# These turn off what can be turned off by name. Diff drivers and filters take any name a repository
# likes, so the first are off by flag, in the rules below, and the second need GIT_PRELUDE.
GIT_OFF = ("core.fsmonitor=false", "core.hooksPath=/dev/null", "core.pager=cat",
           "credential.helper=", "log.showSignature=false", "gpg.program=/bin/false",
           "gpg.openpgp.program=/bin/false", "gpg.x509.program=/bin/false",
           "gpg.ssh.program=/bin/false")
GIT_NO_DRIVERS = ("--no-ext-diff", "--no-textconv")
GIT_ONE_REPO = "--ignore-submodules=all"    # or git asks each submodule, with its own config
GIT_PRELUDE = r"""safe_git() {
    local place=() name count=0
    while [ "$1" != -- ]; do place+=("$1"); shift; done
    shift
    while IFS= read -r -d '' name; do
        export "GIT_CONFIG_KEY_$count=$name" "GIT_CONFIG_VALUE_$count="
        count=$((count + 1))
    done < <(git "${place[@]}" config --null --name-only --get-regexp '^filter\.' 2>/dev/null)
    GIT_CONFIG_COUNT=$count GIT_NO_LAZY_FETCH=1 GIT_ALLOW_PROTOCOL=none git "${place[@]}" "$@"
}
"""
GIT = {
    "status": Rule("sb", long="--short --branch --long --porcelain= --untracked-files=",
                   run=("status", GIT_ONE_REPO)),
    "log": Rule("p", "n", GIT_LOG, digits=True, run=("log", *GIT_NO_DRIVERS)),
    "show": Rule("p", "n", GIT_LOG, digits=True, run=("show", *GIT_NO_DRIVERS)),
    "diff": Rule("p", "n", GIT_LOG, digits=True, run=("diff", *GIT_NO_DRIVERS, GIT_ONE_REPO)),
    "blame": Rule("swe", "L", run=("blame", "--no-textconv")),
    "grep": Rule("inlwcvE", "e", "--ignore-case --line-number --count", script=1,
                 script_flags=("-e",)),
    "branch": Rule("avr", long="--all --list --remotes --show-current --verbose", most=0),
    "tag": Rule("l", long="--list", most=0),
    "rev-parse": Rule(long="--short= --abbrev-ref --show-toplevel --is-inside-work-tree "
                           "--git-dir"),
    "ls-files": Rule(long="--cached --others --modified --deleted --exclude-standard"),
    "describe": Rule(long="--tags --always --long"),
    "shortlog": Rule("nse", long="--numbered --summary --email"),
}

FIND_WITH_VALUE = frozenset((
    "-name -iname -path -ipath -wholename -iwholename -regex -iregex -regextype -type -xtype "
    "-size -mtime -atime -ctime -mmin -amin -cmin -newer -anewer -cnewer -newermt -user -group "
    "-uid -gid -perm -links -inum -samefile -lname -ilname -fstype -used -maxdepth -mindepth "
    "-printf").split())
FIND_ALONE = frozenset((
    "-a -and -o -or -not ! , ( ) -depth -xdev -mount -daystart -noleaf -empty -executable "
    "-readable -writable -nouser -nogroup -true -false -print -print0 -ls -prune -quit "
    "-follow").split())
FIND_NOT_WITH_DELETE = frozenset("-print -print0 -printf -ls -prune -quit -follow -L -H".split())
PDF_ALONE = frozenset("-layout -raw -nopgbrk -q".split())
PDF_WITH_VALUE = frozenset("-f -l".split())


def _find_paths(words) -> set | None:
    """Positions of find's starting points, or None if anything in the expression is unlisted."""
    i = 0
    while i < len(words) and words[i].text in ("-H", "-L", "-P"):
        i += 1
    paths = set()
    while i < len(words) and not (words[i].text in ("(", ")", "!", ",")
                                  or (words[i].text.startswith("-") and words[i].text != "-")):
        paths.add(i)
        i += 1
    while i < len(words):
        if words[i].text in FIND_ALONE:
            i += 1
        elif words[i].text in FIND_WITH_VALUE and i + 1 < len(words):
            i += 2
        else:
            return None
    return paths


def _find(words) -> tuple[list[str], bool] | None:
    paths = _find_paths(words)
    if paths is None:
        return None
    return ["find", *(_live(word) if word.live and i in paths else shlex.quote(word.text)
                      for i, word in enumerate(words))], False


def _git(words) -> tuple[list[str], bool] | None:
    out, i = ["safe_git", "--no-pager"], 0
    for setting in GIT_OFF:
        out += ["-c", shlex.quote(setting)]
    while i < len(words) and words[i].text in ("--no-pager", "-C"):
        if words[i].text == "-C":
            if i + 1 == len(words):
                return None
            out += ["-C", shlex.quote(words[i + 1].text)]
            i += 1
        i += 1
    rule = GIT.get(words[i].text) if i < len(words) else None
    if rule is None:
        return None
    seen = rule.read(words[i + 1:])
    if seen is None or not rule.accepts(words[i + 1:], seen):
        return None
    return [*out, "--", *rule.argv(words[i].text, words[i + 1:], seen)], True


def _pdftotext(words) -> tuple[list[str], bool] | None:
    """Only to stdout. Without the - it writes a .txt beside the PDF, so that one is an ask."""
    operands, i = [], 0
    while i < len(words):
        text = words[i].text
        if text in PDF_WITH_VALUE and i + 1 < len(words):
            i += 2
        elif text in PDF_ALONE:
            i += 1
        elif text.startswith("-") and text != "-":
            return None
        else:
            operands.append(text)
            i += 1
    if len(operands) != 2 or operands[1] != "-":
        return None
    return ["pdftotext", *(shlex.quote(word.text) for word in words)], True


def _stage(words, piped: bool) -> tuple[list[str], bool] | None:
    """The argv to run for one stage and whether it prints file contents, if it is allowed."""
    program, args = words[0].text, words[1:]
    if words[0].live:
        return None
    if program in ("find", "git", "pdftotext"):
        return {"find": _find, "git": _git, "pdftotext": _pdftotext}[program](args)
    rule = RULES.get(program)
    seen = rule.read(args) if rule else None
    if seen is None or not rule.accepts(args, seen):
        return None
    argv = rule.argv(program, args, seen)
    if program == "df" and not seen.operands and not {"-a", "--all", "-t", "--type", "-x",
                                                      "--exclude-type"} & seen.options.keys():
        argv += ["-x", "tmpfs"]     # or every mask below shows up as a disk
    return argv, rule.reads_files(seen, piped)


@dataclass(frozen=True)
class Plan:
    script: str     # rebuilt from the parsed words: only what was classified can run
    prints: bool    # the output may hold the contents of files


def _lands_in(text: str, places) -> bool:
    """Whether a word is a path into places, by its letters or through the links on the way."""
    full = os.path.join(_home(), text)
    return _within(os.path.normpath(full), places) or _within(os.path.realpath(full), places)


def _dot_wildcard(word: Word) -> bool:
    """~/.* is everything that is hidden, and in there all of it would look empty. Only a live
    wildcard counts: '.*' in quotes is what a grep pattern looks like."""
    if not word.live:
        return False
    first = os.path.relpath(os.path.join(_home(), word.text), _home()).split("/")[0]
    return first.startswith(".") and first != ".."


def _names_hidden(word: Word, hidden) -> bool:
    return (bool(SECRET_NAME.search(word.text)) or _lands_in(word.text, hidden)
            or _dot_wildcard(word))


def _rebuilt(stages: list[Stage]) -> Plan | None:
    """The line from its parsed words, if every stage of it is on the allowlist."""
    parts, prints = [], False
    for index, stage in enumerate(stages):
        checked = _stage(stage.words, index > 0 and stages[index - 1].join == "|")
        if checked is None:
            return None
        parts += [*checked[0], stage.join]
        prints = prints or checked[1]
    script = " ".join(part for part in parts if part)
    if any(stage.words[0].text == "git" for stage in stages):
        script = GIT_PRELUDE + script
    return Plan(script, prints)


def classify(stages: list[Stage], hidden) -> Plan | None:
    """A Plan if the whole line may run in the sandbox. A word naming something the sandbox hides
    is an ask, because there the answer would be a false "no such file"."""
    if any(_names_hidden(word, hidden) for stage in stages for word in stage.words[1:]):
        return None
    return _rebuilt(stages)


# The sandbox.

def _walk_for_secrets(home: str) -> tuple[list[str], list[str]]:
    """What is named like a secret, and the folders left over if the walk had to stop. Planted
    folders can use it up, and whatever it never reached must not be taken for clean."""
    found, folders = [], 0
    pending = [(home, 0)]
    while pending and folders < SCAN_MAX_FOLDERS:
        folder, depth = pending.pop()
        folders += 1
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name == ".git":
                        if entry.is_dir(follow_symlinks=False):
                            found.append(entry.path + "/config")
                    elif SECRET_NAME.search(entry.name):
                        found.append(entry.path)
                    elif (depth < SCAN_DEPTH and entry.name[0] != "."
                          and entry.name not in SCAN_SKIP
                          and (depth or entry.name not in HIDDEN_ENTRIES)
                          and entry.is_dir(follow_symlinks=False)):
                        pending.append((entry.path, depth + 1))
        except OSError:
            continue
    return found, [folder for folder, _ in pending]


def _folder_at(path: str, level: int) -> str:
    return "/".join(path.split("/")[:level + 1])


def _gather(found: list[str], home: str) -> tuple[list[str], list[str]]:
    """What to mask, and which of that are folders hidden whole. More than bwrap can mount is a
    matter of one unpacked SDK, and leaving some out would show them, so the folders holding the
    most are hidden whole, those nearest the files first and never the home folder itself. The
    files in them chose that, so what a word naming such a folder gets is a refusal: a card would
    offer to run a planted repository's config on the host."""
    sealed = []
    deepest = max((path.count("/") for path in found), default=0)
    for level in range(deepest - 1, home.count("/"), -1):
        excess = len(found) - MAX_SCATTERED
        if excess <= 0:
            break
        below = [path for path in found if path.count("/") > level]
        chosen = set()
        for folder, count in collections.Counter(_folder_at(path, level)
                                                 for path in below).most_common():
            if excess <= 0 or count < 2:
                break
            chosen.add(folder)
            excess -= count - 1
        found = [path for path in found
                 if path.count("/") <= level or _folder_at(path, level) not in chosen]
        found += sorted(chosen)
        sealed += chosen
    return found, [folder for folder in sealed if folder in found]


_scanned: tuple = (float("-inf"), "", [], [])


def _scattered_secrets(home: str) -> list[str]:
    global _scanned
    at, scanned_home, found, sealed = _scanned
    if time.monotonic() - at > SCAN_EVERY_S or scanned_home != home:
        found, unwalked = _walk_for_secrets(home)
        found, sealed = _gather(found + unwalked, home)
        sealed += [folder for folder in unwalked if folder in found and folder not in sealed]
        _scanned = (time.monotonic(), home, found, sealed)
    return found


def _sockets() -> list[str]:
    """Docker, LXD and CUPS keep sockets outside /run, and connect() works on a read-only mount.
    For someone in the docker or lxd group, a reachable socket is as good as root."""
    try:
        with open("/proc/net/unix") as table:
            rows = [line.rstrip("\n").split(None, 7) for line in table]
    except OSError:
        return []
    return [row[7] for row in rows if len(row) == 8 and row[7].startswith("/")]


def _home_entries(home: str) -> list[str]:
    with os.scandir(home) as entries:
        return [entry.path for entry in entries if entry.name in HIDDEN_ENTRIES
                or (entry.name[0] == "." and entry.name not in VISIBLE_DOT_ENTRIES)]


def _mask_for(path: str, home: str, follow_folders: bool) -> str:
    """Where the mask for an entry in the home folder goes. A mount on a link lands on whatever the
    link points at, so a file called deploy.key could mask /bin/bash, the home folder or /, and
    stop every command. The real path is covered only if it stays inside the home folder and is
    not what must stay readable, and any other link is itself what gets hidden: see _rebuilds.
    A name match does not get to hide a whole folder of the user's, or a link planted in a
    repository could hide that repository and turn every question about it into a card."""
    real = os.path.realpath(path)
    if real == path:
        return path
    visible = {os.path.join(home, name) for name in VISIBLE_DOT_ENTRIES}
    if (not real.startswith(home + "/") or real in visible
            or (os.path.isdir(real) and not follow_folders)):
        return path
    return real


def mask_paths() -> list[str]:
    """Everything a word may not name and the sandbox must cover, parents first. A path that does
    not exist needs no mask, and bwrap would refuse to start over it. Sockets and the app's own
    folders are not the home folder's doing, so they are covered wherever they really are."""
    home = os.path.realpath(_home())
    found = {_mask_for(path, home, True) for path in _home_entries(home)
             if os.path.exists(path)}
    found |= {_mask_for(path, home, False) for path in _scattered_secrets(home)
              if os.path.exists(path)}
    found |= {os.path.realpath(path) for path in (*APP_FOLDERS, *_sockets())
              if os.path.exists(path)}
    masks = []
    for path in sorted(found, key=lambda p: p.split("/")):
        if not _within(path, HIDDEN_TREES) and not (masks and _within(path, masks[-1:])):
            masks.append(path)
    return masks


def _rebuilds(links: set) -> list[str]:
    """bwrap cannot put anything over a link, so the folder it is in is made again without it: an
    empty one with everything else mounted back. Past the limit a link just stays. It leads out of
    the home folder, to what the sandbox shows at that path anyway, and a planted crowd of them
    must not be able to take the sandbox down."""
    args, room = [], MAX_REBUILT_ENTRIES
    for folder in sorted({os.path.dirname(link) for link in links}, key=lambda p: p.split("/")):
        try:
            with os.scandir(folder) as found:
                entries = list(found)
            if len(entries) > room:
                continue
            kept = [("--symlink", os.readlink(entry.path), entry.path) if entry.is_symlink()
                    else ("--ro-bind-try", entry.path, entry.path)
                    for entry in entries if entry.path not in links]
        except OSError:
            continue
        room -= len(entries)
        args += ["--tmpfs", folder, *itertools.chain.from_iterable(kept), "--remount-ro", folder]
    return args


def sandbox_args() -> list[str]:
    home = _home()
    args = ["--unshare-user", "--disable-userns", "--unshare-pid", "--unshare-ipc",
            "--unshare-net", "--unshare-uts", "--unshare-cgroup-try", "--new-session",
            "--die-with-parent", "--cap-drop", "ALL", "--clearenv",
            "--setenv", "PATH", SANDBOX_PATH, "--setenv", "HOME", home,
            "--setenv", "LANG", os.environ.get("LANG") or "C.UTF-8",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/run", "--remount-ro", "/run",
            "--size", str(SANDBOX_TMP_BYTES), "--tmpfs", "/tmp",
            "--tmpfs", "/var/tmp", "--remount-ro", "/var/tmp"]
    masks = mask_paths()
    links = {path for path in masks if os.path.islink(path)}
    args += _rebuilds(links)
    for path in masks:
        if path in links:
            continue
        if os.path.isdir(path):
            args += ["--tmpfs", path, "--remount-ro", path]
        elif path.endswith(GIT_CONFIG):
            args += ["--dev-bind", "/dev/null", path]   # git must be able to open it
        else:
            args += ["--ro-bind", "/dev/null", path]
    return args + ["--chdir", home]


# The seccomp filter, in classic BPF, as bwrap wants it. --unshare-net does not stop AF_VSOCK, and
# the keyring and io_uring have no namespace to shut them in.
BPF_LOAD, BPF_JEQ, BPF_JGE, BPF_RET = 0x20, 0x15, 0x35, 0x06
RET_ALLOW, RET_ERRNO, RET_KILL = 0x7FFF0000, 0x00050000, 0x80000000
ARCH_AT, NUMBER_AT, FIRST_ARGUMENT_AT = 4, 0, 16    # in struct seccomp_data
SYSCALL_LIMIT = 0x40000000                          # above it are x32's numbers, on x86_64
SOCKET_FAMILIES = (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6, socket.AF_NETLINK)
SECCOMP_ARCHES = {      # uname: audit arch, socket, socketpair, those that get EPERM
    # keyctl, add_key, request_key, io_uring_setup, io_uring_enter, io_uring_register
    "aarch64": (0xC00000B7, 198, 199, (219, 217, 218, 425, 426, 427)),
    "x86_64": (0xC000003E, 41, 53, (250, 248, 249, 425, 426, 427)),
}


def _bpf(code: int, value: int, if_true: int = 0, if_false: int = 0) -> bytes:
    return struct.pack("=HBBI", code, if_true, if_false, value)


def seccomp_program() -> bytes | None:
    """None on a machine it has no numbers for, and then the sandbox does not start. A process of
    another architecture than this one is killed, as its numbers mean something else. socketpair
    is held to the same families as socket, or it would load the same modules."""
    known = SECCOMP_ARCHES.get(os.uname().machine)
    if known is None:
        return None
    arch, socket_call, socketpair_call, refused = known
    kill = _bpf(BPF_RET, RET_KILL)
    families = len(SOCKET_FAMILIES)
    return b"".join((
        _bpf(BPF_LOAD, ARCH_AT),
        _bpf(BPF_JEQ, arch, 1, 0), kill,
        _bpf(BPF_LOAD, NUMBER_AT),
        _bpf(BPF_JGE, SYSCALL_LIMIT, 0, 1), kill,
        *(step for number in refused for step in (_bpf(BPF_JEQ, number, 0, 1),
                                                  _bpf(BPF_RET, RET_ERRNO | errno.EPERM))),
        _bpf(BPF_JEQ, socket_call, 1, 0),
        _bpf(BPF_JEQ, socketpair_call, 0, families + 2),
        _bpf(BPF_LOAD, FIRST_ARGUMENT_AT),
        *(_bpf(BPF_JEQ, family, families - i, 0) for i, family in enumerate(SOCKET_FAMILIES)),
        _bpf(BPF_RET, RET_ERRNO | errno.EAFNOSUPPORT),
        _bpf(BPF_RET, RET_ALLOW)))


@dataclass(frozen=True)
class Finished:
    output: bytes
    exit_code: int
    timed_out: bool = False
    cut: bool = False       # there was more output than was kept


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass                # already gone, or root's after pkexec
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        pass


def capture(argv, env, seconds, keep=MAX_OUTPUT_BYTES, stop_when_full=False, pass_fds=()):
    """Runs argv in its own process group with no stdin, keeping at most keep bytes of output.
    Reads as it goes, as an endless command would fill memory. Only a read-only command may be
    stopped for talking too much: one that changes things must be left to finish."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=_home(), pass_fds=pass_fds,
                            start_new_session=True)
    deadline = time.monotonic() + seconds
    data, cut, timed_out = bytearray(), False, False
    output = proc.stdout.fileno()
    reader = select.poll()
    reader.register(output, select.POLLIN)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        if reader.poll(min(left, 0.2) * 1000):
            chunk = os.read(output, 1 << 16)
            if not chunk:
                break
            room = keep - len(data)
            data += chunk[:room]
            cut = cut or len(chunk) > room
            if cut and stop_when_full:
                break
        elif proc.poll() is not None:
            break           # a job left in the background may hold the pipe open for hours
    stopped = cut and stop_when_full
    if not (timed_out or stopped):
        try:
            proc.wait(max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            timed_out = True
    if timed_out or stopped:
        _kill_group(proc)
    proc.stdout.close()
    # Cut off for length is not a failure, whatever the kill did to the exit code.
    code = 0 if stopped else -1 if proc.returncode is None else proc.returncode
    return Finished(bytes(data), code, timed_out, cut)


def _run_once(script: str, seconds, keep) -> Finished | None:
    """Memory is limited here as the daemon shares its cgroup with what runs in there. If bwrap
    cannot load the filter it stops before anything has run."""
    program = seccomp_program()
    if program is None:
        return None
    args = sandbox_args()
    status_read, status_write = os.pipe()
    filter_read, filter_write = os.pipe()
    os.write(filter_write, program)
    os.close(filter_write)
    argv = [PRLIMIT, f"--as={SANDBOX_MEMORY_BYTES}", BWRAP, "--seccomp", str(filter_read),
            "--json-status-fd", str(status_write), *args, "--",
            BASH, "--noprofile", "--norc", "-c", script]
    try:
        done = capture(argv, {}, seconds, keep, True, (status_write, filter_read))
    except OSError:
        os.close(status_read)
        return None
    finally:
        os.close(status_write)
        os.close(filter_read)
    os.set_blocking(status_read, False)
    try:
        status = os.read(status_read, 4096)
    except BlockingIOError:
        status = b""
    finally:
        os.close(status_read)
    # bwrap reports an exit code only for a command it really started. Its own failures exit 1
    # too, so the exit status cannot say which it was.
    return done if b'"exit-code"' in status or done.timed_out or done.cut else None


def run_sandboxed(script: str, seconds=AUTO_TIMEOUT_S, keep=MAX_OUTPUT_BYTES) -> Finished | None:
    """Runs script in the sandbox. None means it never started, so nothing ran. The second try is
    for a socket or a temporary file that vanished between looking and mounting over it."""
    return _run_once(script, seconds, keep) or _run_once(script, seconds, keep)


# Results.

def _host_env() -> dict:
    return {name: os.environ[name] for name in HOST_ENV if name in os.environ}


def _result(done: Finished, sandboxed: bool, prints: bool) -> dict:
    text = done.output.decode("utf-8", "replace").strip()
    result = {"output": text[:MAX_OUTPUT_CHARS], "exit_code": done.exit_code,
              "sandboxed": sandboxed}
    if done.cut or len(text) > MAX_OUTPUT_CHARS:
        result["truncated"] = True
    if prints and text:
        result["file_contents"] = True
    return result


def _auto_result(done: Finished, plan: Plan) -> dict:
    if done.timed_out:
        return {"error": f"That took longer than {AUTO_TIMEOUT_S} seconds, so it was stopped.",
                "sandboxed": True}
    if done.exit_code == KILLED:
        return {"error": MEMORY_KILLED, "sandboxed": True}
    return _result(done, True, plan.prints)


def _on_host(command: str) -> dict:
    """Outside the sandbox, after a click. Nobody can say what it prints, so assume files."""
    done = capture([BASH, "--noprofile", "--norc", "-c", command], _host_env(), ASK_TIMEOUT_S)
    if done.timed_out:
        return {"error": f"That ran for {ASK_TIMEOUT_S} seconds and was stopped. "
                         "It may have done part of its work.", "sandboxed": False}
    return _result(done, False, True)


def _ask(command: str, title: str, stages=None) -> Ask:
    """A card for command. If that is read-only git, the card means what it says on the host too:
    the words shown, and not whatever a repository's config would run along with them."""
    plan = _rebuilt(stages) if stages else None
    script = plan.script if plan and plan.script.startswith(GIT_PRELUDE) else command
    return Ask(title, command, functools.partial(_on_host, script), timeout_s=ASK_TIMEOUT_S)


# Deletions.

DELETES = re.compile(
    r"(?:^|[\s;&|(/=`])(?:rm|rmdir|shred|unlink|truncate|srm|wipe|wipefs|mkfs(?:\.\w+)?|dd)"
    r"(?=$|[\s;&|)`])"
    r"|\s-delete(?=$|[\s;&|)])"
    r"|\bgit\s+(?:clean|rm)\b|\bgit\s+reset\s+--hard\b|\bgio\s+(?:remove|trash)\b")
ELEVATES = re.compile(r"(?:^|[\s;&|(/=`])(?:sudo|su|doas|pkexec|run0)(?=$|[\s;&|)`])")
RM = Rule("rRfv", long="--recursive --force --verbose")
RECURSIVE = {"-r", "-R", "--recursive"}


def _flat(command: str) -> str:
    """Quotes and backslashes out of the way: r'm' and r\\m are rm, whatever the parser made of
    them. Only ever used to warn more, never to allow."""
    return re.sub(r"\\\n|[\\'\"]", "", command)


@dataclass(frozen=True)
class Trash:
    paths: tuple            # moved as they are, what is inside them included
    folders: tuple = ()     # moved only if the paths left them empty, as find -delete would


def _target(path: str) -> str | None:
    """Where path really is, not following the last step: rm removes a link, not its target."""
    parent, name = os.path.split(os.path.join(_home(), path).rstrip("/") or "/")
    if name in (".", ".."):
        return None
    return os.path.join(os.path.realpath(parent), name)


def _too_much(path: str) -> bool:
    home = _home()
    return path.count("/") < 2 or path == home or home.startswith(path + "/")


def _expand(word: Word) -> list[str]:
    """One look at the disk, with bash's rules for * and ?. What this finds is what the card
    counts and what gets moved, so a file that appears later is not swept up."""
    if not word.live:
        return [word.text]
    pattern = "".join(c if i in word.live else glob.escape(c) for i, c in enumerate(word.text))
    return sorted(glob.glob(pattern, root_dir=_home()))


def _rm_plan(words) -> Trash | dict | None:
    """A Trash, a spoken answer, or None if this rm is more than plain paths and flags."""
    seen = RM.read(words[1:])
    if seen is None or not seen.operands:
        return None
    targets = [_target(path) for i in seen.operands for path in _expand(words[1 + i])]
    if None in targets:
        return None
    if any(map(_too_much, targets)):
        return {"error": TOO_MUCH}
    targets = list(dict.fromkeys(path for path in targets if os.path.lexists(path)))
    if not RECURSIVE & seen.options.keys() and any(
            os.path.isdir(path) and not os.path.islink(path) for path in targets):
        return {"error": "That is a folder and the command does not say to remove folders, "
                         "so nothing was deleted."}
    if not targets:
        return {"output": NOTHING_TO_DELETE, "exit_code": 0}      # nothing ran, anywhere
    return Trash(tuple(targets))


def _find_plan(words) -> Trash | dict | None:
    """The real list comes from find itself, in the sandbox, with -print0 where -delete was. It
    sits in the same place in the expression, so it selects exactly what -delete would. A word
    naming what the sandbox hides makes it complicated: find could not see what it asks for."""
    body = words[1:-1]
    hidden = (*HIDDEN_TREES, *mask_paths())
    if (_find_paths(body) is None or {word.text for word in body} & FIND_NOT_WITH_DELETE
            or any(_names_hidden(word, hidden) for word in body)):
        return None
    argv, _ = _find((*body, Word("-print0")))
    done = run_sandboxed(" ".join(argv) + " 2>/dev/null", keep=MAX_LIST_BYTES)
    if done is None or done.timed_out or done.cut:
        return None
    found = [_target(os.fsdecode(path)) for path in done.output.split(b"\0") if path]
    found = [path for path in found if path and os.path.lexists(path)]
    if any(map(_too_much, found)):
        return {"error": TOO_MUCH}
    if not found:
        return {"output": NOTHING_TO_DELETE, "exit_code": 0, "sandboxed": True}
    folders = {path for path in found if os.path.isdir(path) and not os.path.islink(path)}
    return Trash(tuple(path for path in found if path not in folders), tuple(sorted(folders)))


def _measure(paths) -> tuple[int, int, int, bool]:
    """Files, folders and bytes under paths, and whether that is all of them."""
    files = folders = size = 0
    whole = True
    pending = list(paths)
    deadline = time.monotonic() + MEASURE_S
    while pending:
        if files + folders >= MEASURE_MAX_ENTRIES or time.monotonic() > deadline:
            return files, folders, size, False
        path = pending.pop()
        try:
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                folders += 1
                with os.scandir(path) as entries:
                    pending += [entry.path for entry in entries]
            else:
                files += 1
                size += info.st_size
        except OSError:
            whole = False
    return files, folders, size, whole


def _size(count: float) -> str:
    if count < 1000:
        return f"{count:.0f} bytes"
    for unit in ("KB", "MB", "GB"):
        count /= 1000
        if count < 1000:
            return f"{count:.1f} {unit}"
    return f"{count / 1000:.1f} TB"


def _trash_title(plan: Trash) -> str:
    files, folders, size, whole = _measure(plan.paths)
    folders += len(plan.folders)
    more = "" if whole else "more than "
    if files:
        what = f"{more}{files} file{'s' * (files != 1)} ({_size(size)})"
    else:
        what = f"{more}{folders} folder{'s' * (folders != 1)}"
    return f"Move {what} to the Trash?"


def _trash_works(paths) -> bool:
    """Whether a Trash exists depends on the place, so one file from each folder will do."""
    samples = list({os.path.dirname(path): path for path in paths}.values())[:TRASH_BATCH]
    try:
        done = capture([GIO, "info", "-n", "-a", "access::can-trash", *samples], _host_env(), 10,
                       keep=MAX_LIST_BYTES)
    except OSError:
        return False
    return done.exit_code == 0 and b"can-trash: FALSE" not in done.output


def _trash(paths, deadline) -> None:
    for start in range(0, len(paths), TRASH_BATCH):
        capture([GIO, "trash", "--", *paths[start:start + TRASH_BATCH]], _host_env(),
                max(deadline - time.monotonic(), 1))


def _move_to_trash(plan: Trash) -> dict:
    """Exactly the paths on the card. A folder goes only if nothing is left in it, so a file that
    was not listed is never carried along, and nothing falls back to a real delete."""
    deadline = time.monotonic() + ASK_TIMEOUT_S
    _trash(plan.paths, deadline)
    for folder in sorted(plan.folders, key=len, reverse=True):
        try:
            if not os.listdir(folder):
                _trash([folder], deadline)
        except OSError:
            pass
    stuck = [path for path in plan.paths if os.path.lexists(path)]
    if not stuck:
        return {"output": "Moved to the Trash.", "exit_code": 0, "sandboxed": False}
    moved = len(plan.paths) - len(stuck)
    return {"output": f"Moved {moved} of {len(plan.paths)} to the Trash. Nothing else changed.",
            "not_moved": [shown(path) for path in stuck[:5]], "exit_code": 1, "sandboxed": False}


def _trash_ask(command: str, plan: Trash) -> Ask:
    listing = "\n".join(map(shown, (*plan.paths, *plan.folders)))
    return Ask(_trash_title(plan), f"{command}\n\nMoves to the Trash:\n{listing}",
               functools.partial(_move_to_trash, plan), timeout_s=ASK_TIMEOUT_S)


def _deletion(command: str, stages) -> Ask | dict:
    """The Trash for the simple cases. Anything else keeps its words and says what it means."""
    plan = None
    if stages and len(stages) == 1:
        words = stages[0].words
        if words[0].text == "rm":
            plan = _rm_plan(words)
        elif words[0].text == "find" and words[-1].text == "-delete":
            plan = _find_plan(words)
    if isinstance(plan, dict):
        return plan
    if plan and _trash_works(plan.paths or plan.folders):
        return _trash_ask(command, plan)
    return _ask(command, PERMANENT_TITLE, stages)


# Administrator commands: pkexec, as there is no terminal for sudo to ask a password on.

SUDO_FLAGS = {"-n", "--non-interactive", "-H", "--set-home", "--user=root"}


def _after_sudo(words) -> list | None:
    """The command without sudo's options, or None at an option that is not fully understood."""
    i = 0
    while i < len(words) and words[i].text.startswith("-"):
        text = words[i].text
        if text == "--":
            return list(words[i + 1:])
        if text == "-u" and i + 1 < len(words) and words[i + 1].text == "root":
            i += 2
        elif text in SUDO_FLAGS:
            i += 1
        else:
            return None
    return list(words[i:])


def _as_admin(argv: list[str]) -> dict:
    done = capture([PKEXEC, "--keep-cwd", *argv], _host_env(), ASK_TIMEOUT_S)
    if done.timed_out:
        return {"error": f"That ran for {ASK_TIMEOUT_S} seconds and was stopped. "
                         "It may have done part of its work.", "sandboxed": False}
    if done.exit_code in (126, 127):
        return {"error": "Nothing was run. The password prompt was closed or refused, "
                         "or that program does not exist."}
    return _result(done, False, True)


def _admin(words) -> Ask | dict:
    rest = _after_sudo(words)
    if rest is None:
        return {"error": "I do not know that sudo option, so nothing was run."}
    if not rest or re.match(r"-|[A-Za-z_][A-Za-z0-9_]*=", rest[0].text):
        return {"error": "That sudo command has nothing to run, so nothing was done."}
    if rest[0].text in ("sudo", "su", "doas", "pkexec", "run0"):
        return {"error": SUDO_ALONE}
    if any(word.live for word in rest):
        return {"error": "A sudo command cannot use wildcards. Name the files one by one."}
    argv = [word.text for word in rest]
    line = shlex.join(argv)
    problem = _problem(line)        # quoting can make it longer than what was asked for
    if problem:
        return {"error": problem}
    title = ADMIN_PERMANENT_TITLE if DELETES.search(line) else ADMIN_TITLE
    return Ask(title, line, functools.partial(_as_admin, argv), level="admin",
               timeout_s=ASK_TIMEOUT_S)


# The action.

def _problem(command) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return "No command was given."
    if len(command) > MAX_COMMAND_CHARS:
        return f"That command is too long. The limit is {MAX_COMMAND_CHARS} characters."
    if "\0" in command:
        return "That command has a NUL character in it, so it was not run."
    if len(printable(command, MAX_DETAIL_CHARS * 8)) > MAX_DETAIL_CHARS:
        return "That command is too long to show on the approval card, so it was not run."
    return None


def _run_plan(command: str, stages, plan: Plan, sealed) -> dict | Ask:
    """Only a sandbox that is not installed may become an offer to run on the host. Anything else
    that stops it may have been planted, so it is a spoken failure and nothing runs."""
    if any(_lands_in(word.text, sealed) for stage in stages for word in stage.words[1:]):
        return {"error": SEALED}
    done = run_sandboxed(plan.script)
    if done is not None:
        return _auto_result(done, plan)
    if os.access(BWRAP, os.X_OK):
        return {"error": SANDBOX_FAILED}
    return _ask(command, NO_SANDBOX_TITLE, stages)


def decide(command) -> dict | Ask:
    problem = _problem(command)
    if problem:
        return {"error": problem}
    stages = parse(command)
    if stages and len(stages) == 1 and stages[0].words[0].text == "sudo":
        return _admin(stages[0].words[1:])
    plan, sealed = None, ()
    if stages:
        masks = mask_paths()
        sealed = set(_scanned[3]) & set(masks)
        plan = classify(stages, (*HIDDEN_TREES, *(mask for mask in masks if mask not in sealed)))
    if plan:
        return _run_plan(command, stages, plan, sealed)
    flat = _flat(command)
    if ELEVATES.search(flat):
        return {"error": SUDO_ALONE}
    if DELETES.search(flat):
        return _deletion(command, stages)
    return _ask(command, ASK_TITLE, stages)


async def run(args: dict, _config, session) -> dict | Ask:
    session.busy(AUTO_TIMEOUT_S + MEASURE_S + 5)
    return await asyncio.to_thread(decide, args.get("command"))


HANDLERS = {"shell.run": run}
