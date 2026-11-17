"""Recipes from the skills registry, checked here and nowhere else.

Tux trusts a signature it checked itself and a hash it pinned, never the site that served them.
The index is signed by the registry key that ships with Tuxistant, revocations by a moderation
key the index names, and each recipe by its publisher, with a namespace per kind of file so a
signature made for one kind is refused for another. docs/REGISTRY-FORMAT.md is the format, field
by field, and "How Tux checks" there is what this module does.

Every signature is checked by ssh-keygen -Y verify: no shell, a fixed argv, a time limit, and
its input in files in a private folder, with an allowed_signers file written for that one check.
A signature is checked before the file it covers is parsed, so nothing unsigned reaches the
JSON parser.

The moderation key can only add revocations. Every distinct copy of an id Tux accepts is kept and
in effect, so a copy signed first can neither narrow nor hide a later one, and only an index the
registry key signed can lift an id. The index's copies and the moderation keys' are kept apart,
each with its own limit, so moderation copies never stop an index, and an index that retires a
moderation key takes the copies only that key signed, and its sequence number, with it. A state
file Tux can't read refuses everything until it is fixed, as starting again from nothing would
forget the newest sequence numbers and revocations.

No network here: fetch(path, limit) -> bytes is handed in, and tests pass a fake.
"""
import base64
import copy
import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR
from .skills import risk_of

log = logging.getLogger("tuxistant")

SHIPPED_SIGNERS = Path(__file__).resolve().parent / "registry_allowed_signers"
# an empty XDG_STATE_HOME is unset, as in paths.sh
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
STATE_DIR = STATE_HOME / "tuxistant" / "registry"
SKILLS_DIR = DATA_DIR / "skills"
INSTALLED = "registry-installed.json"   # next to the skills folder, as skill-grants.json is
STATE = "state.json"
HELD_INDEX = "index.json"

INDEX_NS, REVOCATIONS_NS, RECIPE_NS = "tuxistant-index", "tuxistant-revocations", "tuxistant-recipe"
REGISTRY, MODERATION = "tuxistant-registry", "tuxistant-moderation"

MAX_INDEX_BYTES = 8 * 1024 * 1024         # the revocations and lifts at their limits leave 3.5 MiB
MAX_REVOCATIONS_BYTES = 5 * 1024 * 1024   # MAX_LIVE entries at their longest are under 4.5 MiB
MAX_RECIPE_BYTES = 64 * 1024
MAX_SIG_BYTES = 4 * 1024
MAX_SHIPPED_BYTES = 4 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_LIVE = 1000                 # revocations in effect, the most an index lists
MAX_LISTED = MAX_LIVE           # copies from the indexes Tux accepted, at most about 6.5 KB each
MAX_MODERATED = 1000            # copies only a moderation key signed, the same size
MAX_LIFTED = 10_000             # lifted ids; all three together fit in MAX_STATE_BYTES
STATE_FIELDS = {"index", "revocations", "listed", "moderated", "lifted"}
VERIFY_S = 10
AHEAD_S = 24 * 3600             # an index dated further ahead of the clock than this is refused
KEYGEN_PATH = "/usr/bin:/bin"   # never the caller's PATH, so a planted ssh-keygen isn't run
MAX_KEY_CHARS = 2000            # an 8192-bit RSA key is about 1070

KEY_TYPES = frozenset({"ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256",
                       "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                       "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa"})
MIN_RSA_BITS = 2048
LOGIN = re.compile(r"(?=[a-z0-9-]{1,39}$)[a-z0-9]+(?:-[a-z0-9]+)*")
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TAG = re.compile(r"(?=[a-z0-9-]{2,24}$)[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,39}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
REPORT = re.compile(r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/issues/"
                    r"[1-9][0-9]{0,9}")
ARMOR = re.compile(rb"-----BEGIN SSH SIGNATURE-----\n(?:[A-Za-z0-9+/=]{1,76}\n){1,60}"
                   rb"-----END SSH SIGNATURE-----\n?")
SHIPPED_LINE = re.compile(rf'{REGISTRY} namespaces="{INDEX_NS}" (ssh-ed25519 [A-Za-z0-9+/=]+)')
HIDDEN = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
LICENCES = ("MIT", "CC0-1.0")

INDEX_FIELDS = {"format", "seq", "date", "commit", "moderation_keys", "categories", "authors",
                "skills", "revocations", "lifted"}
REVOCATIONS_FIELDS = {"format", "seq", "date", "revocations"}
REVOCATION_FIELDS = {"id", "kind", "scope", "reason", "date", "actor", "report"}
AUTHOR_FIELDS = {"status", "keys"}
AUTHOR_KEY_FIELDS = {"key", "fingerprint", "added", "removed"}
CATEGORY_FIELDS = {"id", "name", "tags"}
SKILL_FIELDS = {"id", "author", "name", "summary", "tags", "licence", "links", "versions"}
VERSION_FIELDS = {"version", "date", "sha256", "size", "key", "sig", "permissions", "risk"}
LOGO_FIELDS = {"type", "sha256", "size", "width", "height", "frames", "duration_ms", "key", "sig",
               "still_sha256"}
LOGO_TYPES = ("png", "webp", "avif", "gif")
LOGO_SIDE = (128, 512)
MAX_LOGO_BYTES = {True: 256 * 1024, False: 512 * 1024}     # still or not
MAX_LOGO_FRAMES = 120
MAX_LOGO_LOOP_MS = 8000
MIN_FRAME_MS = 20
SCOPES = ({"skill"}, {"skill", "versions"}, {"author"}, {"key"})

NO_KEYGEN = ("ssh-keygen isn't installed, so Tux can't check what the skills registry signed. "
             "It comes with OpenSSH.")
NOT_REACHED = "Tux couldn't reach the skills registry."
NO_INDEX = "Tux has no list of skills from the registry yet."
NEWER_FORMAT = "The skills registry's list needs a newer Tuxistant."
SHIPPED_DAMAGED = "The registry key that came with Tuxistant is damaged, so nothing can be checked."
SUSPENDED = "The skills registry has switched it off: {reason}"
BANNED = "The skills registry has switched it off for good, and you can remove it: {reason}"
STATE_DAMAGED = ("Tux's record of the skills registry is damaged, so it won't take anything from "
                 "the registry until it is fixed. Removing state.json in {folder} starts it "
                 "again.")
TOO_MANY = ("The skills registry has more revoked skills than Tux can keep track of, so Tux kept "
            "the list it had.")
NOT_LISTED = "The skills registry doesn't list that skill."
NOT_OURS = "Tux didn't install that skill from the registry, so it left it alone."
CHANGED = "You've changed {name}, so Tux won't update it."
CHANGED_LEFT = ("You've changed {name}, so Tux left it. Take it out of your skills folder yourself "
                "if you want it gone.")
YOURS = "You already have a skill file called {name}, so Tux left it alone."
DROPPED = "The skills registry's list no longer has that version, so Tux didn't install it."
UNSAVED = ("Tux couldn't save what it took from the skills registry in {folder} ({why}). Tux "
           "tries again within an hour.")
NOT_RECORDED = ("Tux couldn't write {file} ({why}). What the skills registry switched off is off "
                "all the same, but what it switched back on stays off until Tux can write it. Tux "
                "tries again within an hour.")
ONLY_TILL_RESTART = ("Tux couldn't save what switched off {ids} either, so {it_is} off only "
                     "until Tux restarts.")
RECORD_FIELDS = ("id", "version", "key", "sha256")

# Switch-offs apply() couldn't write to registry-installed.json, by that file, then by file
# name: installed() reads them as if written while the daemon runs, and apply() writes them
# when it can. Only the safe direction is held: a lift waits for the write.
_UNWRITTEN: dict[Path, dict[str, dict]] = {}


class Refused(ValueError):
    """Something from the registry isn't used. The message is a sentence for Settings."""


class SwitchedOff(ValueError):
    """A recipe from the registry that a revocation has switched off. Raised by the loader, and
    never answered with the last version that worked, as that would keep it running."""


# Checking a signature

def keygen() -> str | None:
    return shutil.which("ssh-keygen", path=KEYGEN_PATH)


def verify(data: bytes, signature: bytes, signers: list[str], principal: str, namespace: str,
           what: str, work: Path | None = None) -> None:
    """Raises Refused unless signature is principal's, in namespace, over data's exact bytes,
    with a key in signers: allowed_signers lines this function writes for this one check."""
    if not isinstance(signature, bytes) or len(signature) > MAX_SIG_BYTES or (
            not ARMOR.fullmatch(signature)):
        raise Refused(f"{what} came without a signature Tux can read, so it wasn't used.")
    program = keygen()
    if not program:
        raise Refused(NO_KEYGEN)
    if work is not None:
        _private_folder(work)
    with tempfile.TemporaryDirectory(prefix="verify-", dir=work) as folder:
        folder = Path(folder)       # mkdtemp makes it 0700
        _write_new(folder / "allowed_signers", "".join(f"{line}\n" for line in signers).encode())
        _write_new(folder / "signed.sig", signature)
        _write_new(folder / "signed", data)
        argv = [program, "-Y", "verify", "-f", str(folder / "allowed_signers"), "-I", principal,
                "-n", namespace, "-s", str(folder / "signed.sig")]
        try:
            with open(folder / "signed", "rb") as signed:
                result = subprocess.run(argv, stdin=signed, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, cwd=folder, timeout=VERIFY_S,
                                        env={"PATH": KEYGEN_PATH, "HOME": str(folder),
                                             "LC_ALL": "C"})
        except subprocess.TimeoutExpired:
            raise Refused(f"Checking the signature on {what.lower()} took too long, so it "
                          "wasn't used.") from None
        except OSError as e:
            raise Refused(f"ssh-keygen couldn't be run: {e.strerror or e}.") from None
    if result.returncode != 0:
        raise Refused(f"{what} isn't signed by the key it has to be, so it wasn't used.")


def _write_new(file: Path, data: bytes) -> None:
    handle = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
    with os.fdopen(handle, "wb") as out:
        out.write(data)


def _private_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)


def shipped_signers(file: Path = SHIPPED_SIGNERS) -> list[str]:
    """The registry key lines that ship with Tux: one, or two while a new key comes in."""
    try:
        if file.stat().st_size > MAX_SHIPPED_BYTES:
            raise Refused(SHIPPED_DAMAGED)
        lines = [line.strip() for line in file.read_text().splitlines()]
    except (OSError, UnicodeDecodeError):
        raise Refused(SHIPPED_DAMAGED) from None
    lines = [line for line in lines if line and not line.startswith("#")]
    if not 1 <= len(lines) <= 2:
        raise Refused(SHIPPED_DAMAGED)
    for line in lines:
        match = SHIPPED_LINE.fullmatch(line)
        if not match:
            raise Refused(SHIPPED_DAMAGED)
        try:
            key_fingerprint(match.group(1), "the shipped key", {"ssh-ed25519"})
        except Refused:
            raise Refused(SHIPPED_DAMAGED) from None
    return lines


# Keys

def key_fingerprint(key, where: str, types=KEY_TYPES) -> str:
    """The SHA256 fingerprint of an SSH public key written as "<type> <base64>", after checking
    the blob holds that type and, for RSA, at least 2048 bits."""
    parts = key.split(" ") if isinstance(key, str) and len(key) <= MAX_KEY_CHARS else []
    if len(parts) != 2 or parts[0] not in types or not BASE64.fullmatch(parts[1]):
        raise _bad(where)
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise _bad(where) from None
    fields = _ssh_strings(blob)
    if not fields or fields[0] != parts[0].encode():
        raise _bad(where)
    if parts[0] == "ssh-rsa" and (
            len(fields) != 3 or int.from_bytes(fields[2], "big").bit_length() < MIN_RSA_BITS):
        raise _bad(where)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _ssh_strings(blob: bytes) -> list[bytes] | None:
    """A key blob as its length-prefixed fields, or None if anything is left over or short."""
    fields, at = [], 0
    while at < len(blob):
        if at + 4 > len(blob):
            return None
        size = int.from_bytes(blob[at:at + 4], "big")
        if at + 4 + size > len(blob):
            return None
        fields.append(blob[at + 4:at + 4 + size])
        at += 4 + size
    return fields


# Reading the files

def _bad(where: str) -> Refused:
    return Refused(f"A file from the skills registry wasn't used: its {where} isn't in the "
                   "registry's format.")


def _json(data: bytes, what: str):
    def no_repeats(pairs):
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("a field twice")
        return dict(pairs)

    def no_constant(_name):
        raise ValueError("not a JSON number")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_repeats,
                          parse_constant=no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(f"{what} isn't JSON Tux can read, so it wasn't used.") from None


def _exact(value, fields: set, where: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise _bad(where)
    return value


def _int(value, where: str, low: int = 1, high: int = 2 ** 53) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _bad(where)
    return value


def _list(value, where: str, low: int, high: int) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise _bad(where)
    return value


def _text(value, longest: int, where: str) -> str:
    """text(n): one line, nothing hidden, no angle brackets, no space at either end."""
    if (not isinstance(value, str) or not 1 <= len(value) <= longest or value != value.strip()
            or any(unicodedata.category(c) in HIDDEN or c in "<>" for c in value)):
        raise _bad(where)
    return value


def _matching(pattern: re.Pattern, value, where: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _bad(where)
    return value


def _time(value, where: str) -> float:
    _matching(TIMESTAMP, value, where)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise _bad(where) from None


def _day(value, where: str) -> str:
    _matching(DAY, value, where)
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise _bad(where) from None
    return value


def is_skill_id(value) -> bool:
    try:
        _skill_id(value, "skill id")
        return True
    except Refused:
        return False


def _skill_id(value, where: str) -> str:
    """io.github.<login>.<skill name>, returning the login."""
    parts = value.split(".") if isinstance(value, str) else []
    if (len(parts) != 4 or parts[:2] != ["io", "github"] or not LOGIN.fullmatch(parts[2])
            or not SKILL_NAME.fullmatch(parts[3])):
        raise _bad(where)
    return parts[2]


def _report(value, where: str) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > 200
                              or not REPORT.fullmatch(value)):
        raise _bad(where)


def revocation(entry, where: str = "revocation") -> dict:
    """One revocation entry, checked."""
    _exact(entry, REVOCATION_FIELDS, where)
    _int(entry["id"], f"{where} id")
    if entry["kind"] not in ("suspend", "ban"):
        raise _bad(f"{where} kind")
    scope = entry["scope"]
    if not isinstance(scope, dict) or set(scope) not in SCOPES:
        raise _bad(f"{where} scope")
    if "skill" in scope:
        if entry["kind"] == "ban":      # a ban is for an author or a key
            raise _bad(f"{where} scope")
        _skill_id(scope["skill"], f"{where} scope")
        if "versions" in scope:
            for version in _list(scope["versions"], f"{where} versions", 1, 50):
                _matching(VERSION, version, f"{where} versions")
    if "author" in scope:
        _matching(LOGIN, scope["author"], f"{where} scope")
    if "key" in scope:
        _matching(FINGERPRINT, scope["key"], f"{where} scope")
    _text(entry["reason"], 300, f"{where} reason")
    _time(entry["date"], f"{where} date")
    _matching(LOGIN, entry["actor"], f"{where} actor")
    _report(entry["report"], f"{where} report")
    return entry


def _by_id(entries: list, where: str) -> None:
    ids = [entry["id"] for entry in entries]
    if ids != sorted(set(ids)):
        raise _bad(f"{where} order")


def parse_index(data: bytes, now: float | None) -> dict:
    """index.json, every field checked. now=None skips the date, for the copy Tux already held."""
    index = _json(data, "The skills registry's list")
    if not isinstance(index, dict) or index.get("format") != 1:
        raise Refused(NEWER_FORMAT)
    _exact(index, INDEX_FIELDS, "list")
    _int(index["seq"], "sequence number")
    if _time(index["date"], "date") > (math.inf if now is None else now + AHEAD_S):
        raise Refused("The skills registry's list is dated in the future, so it wasn't used. "
                      "Check this computer's clock.")
    _matching(COMMIT, index["commit"], "commit")
    moderation = _list(index["moderation_keys"], "moderation keys", 1, 3)
    for key in moderation:
        key_fingerprint(key, "moderation keys", {"ssh-ed25519"})
    if len(set(moderation)) != len(moderation):
        raise _bad("moderation keys")
    tags = _categories(index["categories"])
    # the registry's own rules, so it can never publish an index every Tux would have to refuse:
    # only revocations in effect, and lifts as bare ids, which at their limits fit in the index
    listed = _list(index["revocations"], "revocations", 0, MAX_LIVE)
    revocations = [revocation(entry, f"revocation {n + 1}") for n, entry in enumerate(listed)]
    _by_id(revocations, "revocations")
    lifted = [_int(id, "lifts") for id in _list(index["lifted"], "lifts", 0, MAX_LIFTED)]
    if lifted != sorted(set(lifted)):
        raise _bad("lifts order")
    if not set(lifted).isdisjoint(entry["id"] for entry in revocations):
        raise _bad("revocations")
    _authors(index["authors"], revocations)
    _skills(index["skills"], index["authors"], tags)
    return index


def _categories(categories) -> set:
    seen_ids, tags = set(), []
    for n, category in enumerate(_list(categories, "categories", 1, 30)):
        where = f"category {n + 1}"
        _exact(category, CATEGORY_FIELDS, where)
        seen_ids.add(_matching(TAG, category["id"], f"{where} id"))
        _text(category["name"], 30, f"{where} name")
        tags += [_matching(TAG, tag, f"{where} tags")
                 for tag in _list(category["tags"], f"{where} tags", 1, 30)]
    if len(seen_ids) != len(categories) or len(set(tags)) != len(tags):
        raise _bad("categories")
    return set(tags)


def _authors(authors, revocations: list) -> None:
    if not isinstance(authors, dict):
        raise _bad("authors")
    for login, author in authors.items():
        where = f"author {login[:40]}"
        _matching(LOGIN, login, where)
        _exact(author, AUTHOR_FIELDS, where)
        if author["status"] != _status(login, revocations):
            raise _bad(f"{where} status")
        for key in _list(author["keys"], f"{where} keys", 1, 50):
            _exact(key, AUTHOR_KEY_FIELDS, f"{where} keys")
            if key_fingerprint(key["key"], f"{where} keys") != key["fingerprint"]:
                raise _bad(f"{where} fingerprint")
            _day(key["added"], f"{where} keys")
            if key["removed"] is not None:
                _day(key["removed"], f"{where} keys")


def _status(login: str, revocations: list) -> str:
    kinds = {entry["kind"] for entry in revocations if entry["scope"].get("author") == login}
    return "banned" if "ban" in kinds else "suspended" if "suspend" in kinds else "active"


def _skills(skills, authors: dict, tags: set) -> None:
    ids = []
    for n, skill in enumerate(_list(skills, "skills", 0, 10 ** 5)):
        where = f"skill {n + 1}"
        _exact(skill, SKILL_FIELDS, where)
        login = _skill_id(skill["id"], f"{where} id")
        if skill["author"] != login or login not in authors:
            raise _bad(f"{where} author")
        ids.append(skill["id"])
        _text(skill["name"], 100, f"{where} name")
        _text(skill["summary"], 140, f"{where} summary")
        chosen = _list(skill["tags"], f"{where} tags", 1, 4)
        if len(set(map(str, chosen))) != len(chosen) or not set(map(str, chosen)) <= tags:
            raise _bad(f"{where} tags")
        if skill["licence"] not in LICENCES:
            raise _bad(f"{where} licence")
        for link in _list(skill["links"], f"{where} links", 0, 3):
            _exact(link, {"title", "url"}, f"{where} links")
            _text(link["title"], 30, f"{where} links")
            _link(link["url"], f"{where} links")
        keys = {key["fingerprint"] for key in authors[login]["keys"]}
        versions = [_version(entry, keys, f"{where} version {m + 1}")
                    for m, entry in enumerate(_list(skill["versions"], f"{where} versions",
                                                    1, 10 ** 4))]
        if len(set(versions)) != len(versions):
            raise _bad(f"{where} versions")
    if ids != sorted(set(ids)):
        raise _bad("skills order")


def _link(url, where: str) -> None:
    if (not isinstance(url, str) or len(url) > 200 or not url.startswith("https://")
            or any(c.isspace() for c in url)):
        raise _bad(where)
    authority = url[len("https://"):].split("/", 1)[0]
    if not authority or "@" in authority:
        raise _bad(where)


def _version(entry, keys: set, where: str) -> str:
    # the signed example predates logos, so a version may leave the field out
    optional = {"logo"} & set(entry) if isinstance(entry, dict) else set()
    _exact(entry, VERSION_FIELDS | optional, where)
    _matching(VERSION, entry["version"], where)
    _time(entry["date"], f"{where} date")
    _matching(SHA256, entry["sha256"], f"{where} hash")
    _int(entry["size"], f"{where} size", 1, MAX_RECIPE_BYTES)
    if entry["key"] not in keys:
        raise _bad(f"{where} key")
    if not isinstance(entry["sig"], str) or not ARMOR.fullmatch(entry["sig"].encode()):
        raise _bad(f"{where} signature")
    _permissions(entry["permissions"], f"{where} permissions")
    if entry["risk"] != risk_of(entry["permissions"]):
        raise _bad(f"{where} risk")
    _logo(entry.get("logo"), keys, f"{where} logo")
    return entry["version"]


def _logo(logo, keys: set, where: str) -> None:
    """A logo's entry, checked as the format gives it. Tux shows no logo yet, so nothing here
    fetches or decodes one: these are numbers and hashes, never image bytes."""
    if logo is None:
        return
    _exact(logo, LOGO_FIELDS, where)
    if logo["type"] not in LOGO_TYPES:
        raise _bad(where)
    for field in ("sha256", "still_sha256"):
        _matching(SHA256, logo[field], where)
    still = _int(logo["frames"], where, 1, MAX_LOGO_FRAMES) == 1
    _int(logo["size"], where, 1, MAX_LOGO_BYTES[still])
    if _int(logo["width"], where, *LOGO_SIDE) != _int(logo["height"], where, *LOGO_SIDE):
        raise _bad(where)
    if still:
        _int(logo["duration_ms"], where, 0, 0)
    else:
        _int(logo["duration_ms"], where, MIN_FRAME_MS * logo["frames"], MAX_LOGO_LOOP_MS)
    if logo["key"] not in keys:
        raise _bad(where)
    if not isinstance(logo["sig"], str) or not ARMOR.fullmatch(logo["sig"].encode()):
        raise _bad(where)


def _permissions(permissions, where: str) -> None:
    from .recipes import _host     # recipes imports skillhost, which imports this module
    if permissions == {}:
        return
    _exact(permissions, {"network"}, where)
    hosts = _list(permissions["network"], where, 1, 4)
    for host in hosts:
        try:
            if _host(host) != host:
                raise _bad(where)
        except ValueError:
            raise _bad(where) from None
    if len(set(hosts)) != len(hosts):
        raise _bad(where)


def parse_revocations(data: bytes) -> dict:
    """revocations.json: exactly four fields, and one bad entry refuses the whole file."""
    found = _json(data, "The registry's list of revoked skills")
    if not isinstance(found, dict) or found.get("format") != 1:
        raise Refused(NEWER_FORMAT)
    _exact(found, REVOCATIONS_FIELDS, "list of revoked skills")
    _int(found["seq"], "sequence number")
    _time(found["date"], "date")
    entries = [revocation(entry, f"revocation {n + 1}")
               for n, entry in enumerate(_list(found["revocations"], "revocations", 0, 10 ** 5))]
    _by_id(entries, "revocations")
    return found


def switched_off_because(kind: str, reason: str) -> str:
    return (BANNED if kind == "ban" else SUSPENDED).format(reason=reason)


def _copy_key(entry: dict) -> str:
    """Where one copy of a revocation is kept: its id and the hash of exactly what it says, so
    two copies of an id that differ are both kept."""
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{entry['id']}:{hashlib.sha256(text.encode()).hexdigest()}"


def _held_copies(kept: dict) -> dict:
    """Every copy Tux holds, from the indexes and from the moderation keys, by copy key."""
    held = dict(kept["listed"])
    for copies in kept["moderated"].values():
        held.update(copies)
    return held


def _with_index(kept: dict, index: dict) -> dict:
    """kept with an index's revocations and lifts added. A lifted id's copies are dropped, as a
    lifted id stays lifted. Moderation copies never refuse an index: they only shrink here, by
    lifts, by the index listing the same copy, and by the index no longer naming the key that
    signed them, which also ends that key's sequence number, so a leaked key that filled its
    share or raised the number past reach is undone by the index that retires it."""
    lifted = set(kept["lifted"]) | set(index["lifted"])
    listed = {key: entry for key, entry in kept["listed"].items() if entry["id"] not in lifted}
    listed.update((_copy_key(entry), entry) for entry in index["revocations"]
                  if entry["id"] not in lifted)
    if len(listed) > MAX_LISTED or len(lifted) > MAX_LIFTED:
        raise Refused(TOO_MANY)
    named = set(index["moderation_keys"])
    moderated = {}
    for signer, copies in kept["moderated"].items():
        copies = {key: entry for key, entry in copies.items()
                  if entry["id"] not in lifted and key not in listed}
        if signer in named and copies:
            moderated[signer] = copies
    held = kept["revocations"]
    return {**kept, "listed": listed, "moderated": moderated, "lifted": sorted(lifted),
            "revocations": held if held and held["key"] in named else None}


def _with_moderation(kept: dict, signer: str, entries: list) -> dict:
    """kept with the copies one moderation key signed added. Only these count against
    MAX_MODERATED, so filling it refuses revocations.json and never an index."""
    lifted = set(kept["lifted"])
    copies = dict(kept["moderated"].get(signer, {}))
    copies.update((_copy_key(entry), entry) for entry in entries if entry["id"] not in lifted)
    copies = {key: entry for key, entry in copies.items() if key not in kept["listed"]}
    moderated = {**kept["moderated"], signer: copies}
    if not copies:
        del moderated[signer]
    if sum(map(len, moderated.values())) > MAX_MODERATED:
        raise Refused(TOO_MANY)
    return {**kept, "moderated": moderated}


def covers(entry: dict, skill_id: str, version: str, key: str) -> bool:
    scope = entry["scope"]
    if "skill" in scope:
        return scope["skill"] == skill_id and version in scope.get("versions", [version])
    if "author" in scope:
        return skill_id.startswith(f"io.github.{scope['author']}.")
    return scope["key"] == key


def _in_effect(kept: dict) -> list[dict]:
    lifted = set(kept["lifted"])
    return sorted((entry for entry in _held_copies(kept).values() if entry["id"] not in lifted),
                  key=lambda entry: entry["id"])


def _covering(kept: dict, skill_id: str, version: str, key: str) -> dict | None:
    hits = [entry for entry in _in_effect(kept) if covers(entry, skill_id, version, key)]
    return min(hits, key=lambda entry: (entry["kind"] != "ban", entry["id"]), default=None)


def _known(off: dict, kept: dict) -> bool:
    """Whether kept tells how a switch-off stands: its id was lifted, or it holds the copy that
    made it. A record without the copy is from before Tux kept it, and is held on."""
    return off["revocation"] in kept["lifted"] or off.get("copy") in _held_copies(kept)


# What Tux keeps

@dataclass(frozen=True)
class Checked:
    """A recipe's exact bytes, after every check in "A recipe" passed."""
    skill_id: str
    version: str
    key: str
    sha256: str
    data: bytes
    manifest: dict


class Registry:
    """The index Tux accepted, the revocations it has seen, and the recipes it installed.

    state holds the sequence numbers, the accepted index and every revocation seen; it is
    private and written whole or not at all. What was installed from the registry is kept
    next to the skills folder, so the loader can find what is switched off."""

    def __init__(self, state: Path = STATE_DIR, skills: Path = SKILLS_DIR, fetch=None,
                 signers: Path = SHIPPED_SIGNERS, clock=time.time, on_change=None):
        self.state, self.skills = Path(state), Path(skills)
        self.fetch = fetch or no_network
        self.signers = Path(signers)
        self.clock = clock
        self.on_change = on_change
        self.problems: dict[str, str] = {}      # "index", "revocations" or "state": why it failed
        self.unsaved: set[str] = set()          # those of them taken but not saved
        self._kept: dict | None = None
        self._index: dict | None = None

    # Fetching

    def refresh(self) -> list[dict]:
        """Fetch and check the index, then the revocations, and switch off what they cover.
        A file that fails keeps the one Tux had, and problems says why, as it does for a disk
        that is full or a state folder that can't be made; unsaved then names it too. A list of
        revocations checked but not saved is used all the same while the daemon runs."""
        for name, step in (("index", self._refresh_index), ("revocations", self._refresh_revoked)):
            try:
                step()
                self._problem(name, "")
            except Refused as e:
                self._problem(name, str(e))
            except OSError as e:
                self._problem(name, UNSAVED.format(folder=_shown(self.state),
                                                   why=e.strerror or e), unsaved=True)
        try:
            return self.apply()
        except Refused as e:
            self._problem("state", str(e))
        return []

    def _problem(self, name: str, problem: str, unsaved: bool = False) -> None:
        if problem:
            self.problems[name] = problem
        else:
            self.problems.pop(name, None)
        (self.unsaved.add if unsaved else self.unsaved.discard)(name)

    def _refresh_index(self) -> None:
        data = self._fetched("index.json", MAX_INDEX_BYTES)
        self.accept_index(data, self._fetched("index.json.sig", MAX_SIG_BYTES))

    def _refresh_revoked(self) -> None:
        data = self._fetched("revocations.json", MAX_REVOCATIONS_BYTES)
        self.accept_revocations(data, self._fetched("revocations.json.sig", MAX_SIG_BYTES))

    def _fetched(self, path: str, limit: int) -> bytes:
        try:
            data = self.fetch(path, limit)
        except Refused:
            raise
        except Exception as e:      # whatever the fetch hit, the file Tux had is kept
            log.info("could not fetch %s from the skills registry: %s", path, e)
            raise Refused(NOT_REACHED) from None
        if not isinstance(data, bytes):
            raise Refused(NOT_REACHED)
        if len(data) > limit:
            raise Refused("The skills registry sent a file too big to be what it says, so Tux "
                          "didn't use it.")
        return data

    # The index

    def accept_index(self, data: bytes, signature: bytes) -> dict:
        if len(data) > MAX_INDEX_BYTES:
            raise Refused("The skills registry's list is too big, so it wasn't used.")
        verify(data, signature, shipped_signers(self.signers), REGISTRY, INDEX_NS,
               "The skills registry's list", self._work())
        index = parse_index(data, self.clock())
        kept = copy.deepcopy(self.kept())   # kept in memory only once it is on disk
        digest = hashlib.sha256(data).hexdigest()
        _not_older(kept.get("index"), index["seq"], digest, "list of skills")
        kept = _with_index(kept, index)
        kept["index"] = {"seq": index["seq"], "sha256": digest}
        state = _state_bytes(kept)
        _write(self.state / HELD_INDEX, data)       # before the state that names it
        self._save(kept, state)
        self._index = index
        return index

    def index(self) -> dict | None:
        """The index accepted last, or None. Its copy is used only if it is the one the state
        names, so a crash between writing the two leaves no index rather than a wrong one."""
        if self._index is None:
            held = self.kept().get("index")
            try:
                data = (self.state / HELD_INDEX).read_bytes()
            except OSError:
                return None
            if held and hashlib.sha256(data).hexdigest() == held["sha256"]:
                try:
                    self._index = parse_index(data, None)
                except Refused:
                    log.warning("the registry list Tux kept can't be read")
        return self._index

    # Revocations

    def accept_revocations(self, data: bytes, signature: bytes) -> dict:
        index = self.index()
        if index is None:
            raise Refused(NO_INDEX)
        if len(data) > MAX_REVOCATIONS_BYTES:
            raise Refused("The registry's list of revoked skills is too big, so it wasn't used.")
        signer = self._moderation_key(data, signature, index["moderation_keys"])
        found = parse_revocations(data)
        kept = copy.deepcopy(self.kept())
        digest = hashlib.sha256(data).hexdigest()
        _not_older(kept.get("revocations"), found["seq"], digest, "list of revoked skills")
        kept = _with_moderation(kept, signer, found["revocations"])
        kept["revocations"] = {"seq": found["seq"], "sha256": digest, "key": signer}
        state = _state_bytes(kept)
        try:
            self._save(kept, state)
        except OSError:
            # checked, and it only adds revocations, so it is used all the same while the daemon
            # runs, and the next save of the state writes it with the rest
            self._kept = kept
            raise
        return found

    def _moderation_key(self, data: bytes, signature: bytes, keys: list) -> str:
        """The moderation key that signed it, one key per check so Tux knows which, as an index
        that retires a key takes the copies it signed with it."""
        for n, key in enumerate(keys):
            try:
                verify(data, signature, [f'{MODERATION} namespaces="{REVOCATIONS_NS}" {key}'],
                       MODERATION, REVOCATIONS_NS, "The registry's list of revoked skills",
                       self._work())
                return key
            except Refused:
                if n == len(keys) - 1:
                    raise

    def in_effect(self) -> list[dict]:
        return _in_effect(self.kept())

    def covering(self, skill_id: str, version: str, key: str) -> dict | None:
        """The revocation in effect that covers this version, a ban before a suspension."""
        return _covering(self.kept(), skill_id, version, key)

    # A recipe

    def check(self, skill_id: str, version: str) -> Checked:
        """Download one version and check it as "A recipe" says. Writes nothing."""
        index = self.index()
        if index is None:
            raise Refused(NO_INDEX)
        skill = next((s for s in index["skills"] if s["id"] == skill_id), None)
        entry = next((v for v in skill["versions"] if v["version"] == version), None) \
            if skill else None
        if entry is None:
            raise Refused(NOT_LISTED)
        revoked = self.covering(skill_id, version, entry["key"])
        if revoked:
            raise Refused(switched_off_because(revoked["kind"], revoked["reason"]))
        data = self._fetched(f"recipes/{entry['sha256']}.yaml", entry["size"])
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise Refused("The file the skills registry sent isn't the one its list names, so "
                          "Tux didn't use it.")
        author = skill["author"]
        key = next((k["key"] for k in index["authors"][author]["keys"]
                    if k["fingerprint"] == entry["key"]), None)
        if key is None:
            raise Refused("The skills registry doesn't list the key that signed it.")
        verify(data, entry["sig"].encode(), [f'io.github.{author} namespaces="{RECIPE_NS}" {key}'],
               f"io.github.{author}", RECIPE_NS, "That skill", self._work())
        manifest = self._loaded(data, skill_id)
        if (manifest["id"] != skill_id or manifest["version"] != version
                or manifest["permissions"] != entry["permissions"]
                or risk_of(manifest["permissions"]) != entry["risk"]):
            raise Refused("That skill isn't what the skills registry's list says it is, so Tux "
                          "didn't use it.")
        return Checked(skill_id, version, entry["key"], entry["sha256"], data, manifest)

    def _loaded(self, data: bytes, skill_id: str) -> dict:
        """Tux's own loader, the one the daemon runs, on a private copy of the bytes."""
        from . import recipes
        with tempfile.TemporaryDirectory(prefix="recipe-", dir=self._work()) as folder:
            file = Path(folder) / f"{skill_id}.yaml"
            _write_new(file, data)
            try:
                return recipes.load(file)
            except ValueError as e:
                raise Refused(f"Tux can't load that skill: {e}") from None

    def install(self, checked: Checked) -> Path:
        """Write a checked recipe into the skills folder, where the loader picks it up. Browse
        calls this only after its card is allowed. A file of the same name that isn't one Tux
        installed, or that was changed since, is left alone."""
        name = f"{checked.skill_id}.yaml"
        records = installed(self.skills)
        record = records.get(name)
        self._may_write(checked, name, record)
        new = {"id": checked.skill_id, "version": checked.version, "key": checked.key,
               "sha256": checked.sha256}
        target = self.skills / name
        ledger = self.skills.parent / INSTALLED
        was = _kept_bytes(ledger)
        # the record first, so a crash before the file is written never leaves a file from the
        # registry that revocations can't reach. Over a file Tux installed, the record names the
        # version on disk as well until the new one is there, and installed() reads the one whose
        # bytes are there, switch-off and all
        old = record if record and os.path.lexists(target) else None
        records[name] = {**new, "was": {k: v for k, v in old.items() if k != "id"}} if old else new
        _write(ledger, _dumps(records))
        try:
            self.skills.mkdir(parents=True, exist_ok=True)
            _write(target, checked.data, 0o644)
        except OSError:     # the record goes back to what is still on disk
            _put_back(ledger, was)
            raise
        if old:
            records[name] = new
            try:
                _write(ledger, _dumps(records))
            except OSError as e:    # the record naming both still reads as what is on disk
                log.warning("could not finish the record of %s: %s", name, e)
        return target

    def _may_write(self, checked: Checked, name: str, record: dict | None) -> None:
        """Refused unless the index Tux holds now still lists exactly this version, no
        revocation covers it, and the file there, if any, is the one Tux installed. Both can
        change while a card is up."""
        index = self.index()
        skill = next((s for s in index["skills"] if s["id"] == checked.skill_id), None) if (
            index) else None
        if not skill or not any(entry["version"] == checked.version and entry["key"] == checked.key
                                and entry["sha256"] == checked.sha256
                                for entry in skill["versions"]):
            raise Refused(DROPPED)
        revoked = self.covering(checked.skill_id, checked.version, checked.key)
        if revoked:
            raise Refused(switched_off_because(revoked["kind"], revoked["reason"]))
        # an update ends a switch-off only if Tux holds the very copy that made it, which the
        # check above found doesn't cover the new version. Another copy of the same id, maybe
        # narrower, isn't enough, and after a lost state Tux holds none: only a lift ends it
        off = self._still_off(record)
        if off:
            raise Refused(switched_off_because(off["kind"], off["reason"]))
        target = self.skills / name
        if os.path.lexists(target):
            if not record or record["id"] != checked.skill_id or target.is_symlink():
                raise Refused(YOURS.format(name=name))
            if _ours(target, record) is None:
                raise Refused(CHANGED.format(name=name))

    def remove(self, skill_id: str) -> Path:
        """Take out a file Tux installed, then its record, so a crash between the two leaves a
        record of nothing. A file changed since it was installed is left, and so is its record.
        A switch-off Tux can't tell has ended keeps its record, with no file, so installing the
        skill again waits for a lift, as it does after the file is deleted by hand."""
        name = f"{skill_id}.yaml"
        records = installed(self.skills)
        record = records.get(name)
        if not record or record["id"] != skill_id:
            raise Refused(NOT_OURS)
        target = self.skills / name
        if os.path.lexists(target):
            if _ours(target, record) is None:
                raise Refused(CHANGED_LEFT.format(name=name))
            target.unlink()
        if not self._still_off(record):
            del records[name]
            _write(self.skills.parent / INSTALLED, _dumps(records))
        return target

    def held_off(self, skill_id: str) -> dict | None:
        """The switch-off of a skill Tux installed, its file there or not, that only a lift can
        end now, or None. Browse refuses an install over it before any card."""
        record = installed(self.skills).get(f"{skill_id}.yaml")
        return self._still_off(record) if record and record["id"] == skill_id else None

    def _still_off(self, record: dict | None) -> dict | None:
        off = record.get("off") if record else None
        try:
            return off if off and not self._ended_or_held(off) else None
        except Refused:     # a state Tux can't read can't say it ended
            return off

    def edited(self, name: str, record: dict) -> bool:
        """Whether a file Tux installed is there but no longer the bytes it wrote."""
        target = self.skills / name
        return os.path.lexists(target) and _ours(target, record) is None

    def load_installed(self, name: str, record: dict) -> dict | None:
        """What a file Tux installed asks for, read from its bytes once they are the ones it
        wrote, or None: changed, gone, or no longer one Tux's loader takes."""
        data = _ours(self.skills / name, record)
        if data is None:
            return None
        try:
            return self._loaded(data, record["id"])
        except Refused:
            return None

    # Switching off

    def apply(self) -> list[dict]:
        """Switch off each installed recipe a revocation in effect covers, and back on each one
        whose revocation was lifted. Returns what changed, for a notification. When
        registry-installed.json can't be written, problems says so, and each switch-off holds
        in memory all the same until a later apply() writes it (_UNWRITTEN), while a lift waits
        for that write and is returned then. A switch-off a restart would lose, as state.json
        on disk doesn't have its revocation either, is said to be off only until Tux restarts,
        in problems and as until_restart in its change."""
        self.kept()     # a damaged state is refused here too, with nothing installed
        ledger = self.skills.parent / INSTALLED
        records = installed(self.skills)    # with what couldn't be written before
        changes, dirty, held, lost = [], ledger in _UNWRITTEN, {}, set()
        for name, record in records.items():
            was = record.get("off")
            if was:
                held[name] = dict(record)
            # a switch-off whose copy Tux no longer holds, after a lost state or a retired
            # moderation key, outlasts it and isn't swapped for another copy: only a lift ends it
            if was and not self._ended_or_held(was):
                continue
            hit = self.covering(record["id"], record["version"], record["key"])
            now = ({"revocation": hit["id"], "kind": hit["kind"], "reason": hit["reason"],
                    "copy": _copy_key(hit)} if hit else None)
            if now == was:
                continue
            if now:
                record["off"] = now
                held[name] = dict(record)
            else:
                record.pop("off", None)     # held as it was until this is written
            dirty = True
            if os.path.lexists(self.skills / name):     # one taken out has nothing to switch
                changes.append({"file": name, "id": record["id"], "off": bool(now),
                                "kind": (now or was)["kind"], "reason": (now or was)["reason"]})
        try:
            if dirty:
                _write(ledger, _dumps(records))
            _UNWRITTEN.pop(ledger, None)
            self._problem("state", "")
        except OSError as e:
            _UNWRITTEN[ledger] = held
            changes = [change for change in changes if change["off"]]
            lost = self._lost_at_restart(held)
            problem = NOT_RECORDED.format(file=_shown(ledger), why=e.strerror or e)
            if lost:
                ids = [held[name]["id"] for name in sorted(lost)]
                problem += " " + ONLY_TILL_RESTART.format(
                    ids=_named(ids), it_is="it is" if len(ids) == 1 else "they are")
            self._problem("state", problem, unsaved=True)
        for change in changes:
            change["until_restart"] = change["file"] in lost
        if changes and self.on_change:
            self.on_change()
        return changes

    def _ended_or_held(self, off: dict) -> bool:
        return _known(off, self.kept())

    def _lost_at_restart(self, held: dict) -> set[str]:
        """The files whose switch-off is in memory alone, which a restart's apply() wouldn't
        make again: the record on disk doesn't have it, and state.json on disk doesn't cover it,
        as Tux couldn't save the revocation either."""
        try:
            saved = _read_state(self.state / STATE)
        except Refused:     # then apply() refuses at a start, and only the record holds
            saved = None
        written = installed(self.skills, unwritten=False)
        lost = set()
        for name, record in held.items():
            on_disk = written.get(name)
            off = on_disk.get("off") if on_disk and all(
                on_disk[field] == record[field] for field in RECORD_FIELDS) else None
            if off and (saved is None or not _known(off, saved)):
                continue
            if saved is not None and _covering(saved, record["id"], record["version"],
                                               record["key"]):
                continue
            if os.path.lexists(self.skills / name):
                lost.add(name)
        return lost

    # The state folder

    def kept(self) -> dict:
        """What the state file holds. Raises Refused if it is there but can't be read, and is
        read again next time, so removing it or putting it right is noticed."""
        if self._kept is None:
            self._kept = _read_state(self.state / STATE)
        return self._kept

    def _save(self, kept: dict, data: bytes) -> None:
        _private_folder(self.state)
        _write(self.state / STATE, data)
        self._kept = kept

    def _work(self) -> Path:
        _private_folder(self.state)
        return self.state


def _not_older(held: dict | None, seq: int, digest: str, what: str) -> None:
    """A lower seq would hide newer revocations, and the same seq must be the same bytes."""
    if not held:
        return
    if seq < held["seq"]:
        raise Refused(f"The skills registry sent an older {what} than the one Tux has, which "
                      "could hide a revoked skill, so Tux kept the newer one.")
    if seq == held["seq"] and digest != held["sha256"]:
        raise Refused(f"The skills registry sent a different {what} with the same number as "
                      "the one Tux has, so Tux kept its own.")


def _ours(target: Path, record: dict) -> bytes | None:
    """The bytes of a file Tux installed, if they are still the ones it recorded. A link, a
    pipe or a file too big to be a recipe is not, and is never read."""
    try:
        handle = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECIPE_BYTES:
            return None
        data = file.read(MAX_RECIPE_BYTES + 1)
    return data if hashlib.sha256(data).hexdigest() == record["sha256"] else None


def _kept_bytes(file: Path) -> bytes | None:
    try:
        return file.read_bytes()
    except FileNotFoundError:
        return None


def _put_back(file: Path, data: bytes | None) -> None:
    try:
        if data is None:
            file.unlink(missing_ok=True)
        else:
            _write(file, data)
    except OSError as e:
        log.warning("could not put back %s: %s", file, e)


def no_network(_path: str, _limit: int) -> bytes:
    raise Refused("Tux can't fetch from the skills registry yet.")


def _dumps(data) -> bytes:
    return (json.dumps(data, indent=1, sort_keys=True) + "\n").encode()


def _write(file: Path, data: bytes, mode: int = 0o600) -> None:
    """Whole or not at all, so a crash halfway leaves the last copy."""
    file.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{file.name}-", dir=file.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, file)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_json(file: Path, limit: int):
    try:
        if file.stat().st_size > limit:
            return None
        return json.loads(file.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        log.warning("could not read %s", file)
        return None


def _state_bytes(kept: dict) -> bytes:
    """The state as written, refused if it is more than _read_state would read back."""
    data = _dumps(kept)
    if len(data) > MAX_STATE_BYTES:
        raise Refused(TOO_MANY)
    return data


def _read_state(file: Path) -> dict:
    """The state Tux wrote, or an empty one if there is none. Anything else, too big, not JSON
    or not in the shape Tux writes, is refused rather than read as empty."""
    damaged = Refused(STATE_DAMAGED.format(folder=_shown(file.parent)))
    try:
        if file.stat().st_size > MAX_STATE_BYTES:
            raise damaged
        data = json.loads(file.read_bytes())
    except FileNotFoundError:
        return {"index": None, "revocations": None, "listed": {}, "moderated": {}, "lifted": []}
    except (OSError, ValueError, RecursionError):
        log.warning("could not read %s", file)
        raise damaged from None
    try:
        return _state(data)
    except (Refused, KeyError, TypeError):
        log.warning("%s isn't in the shape Tux writes", file)
        raise damaged from None


def _shown(folder: Path) -> str:
    """The folder as the user would type it, so the sentence names the real one."""
    try:
        return f"~/{folder.relative_to(Path.home())}"
    except (ValueError, RuntimeError):
        return str(folder)


def _state(data) -> dict:
    _exact(data, STATE_FIELDS, "state")
    lifted = _list(data["lifted"], "lifted", 0, MAX_LIFTED)
    if any(type(n) is not int or n < 1 for n in lifted) or lifted != sorted(set(lifted)):
        raise _bad("lifted")
    listed = _copies(data["listed"], set(lifted), MAX_LISTED)
    moderated = data["moderated"]
    if not isinstance(moderated, dict):
        raise _bad("moderated")
    for signer, copies in moderated.items():
        key_fingerprint(signer, "state", {"ssh-ed25519"})
        if not copies or set(_copies(copies, set(lifted), MAX_MODERATED)) & set(listed):
            raise _bad("moderated")
    if sum(map(len, moderated.values())) > MAX_MODERATED:
        raise _bad("moderated")
    revoked = _held(data["revocations"], {"key"})
    if revoked:
        key_fingerprint(revoked["key"], "state", {"ssh-ed25519"})
    return {"index": _held(data["index"]), "revocations": revoked, "listed": listed,
            "moderated": moderated, "lifted": lifted}


def _copies(copies, lifted: set, most: int) -> dict:
    if not isinstance(copies, dict) or len(copies) > most:
        raise _bad("state")
    for key, entry in copies.items():
        if key != _copy_key(revocation(entry)) or entry["id"] in lifted:
            raise _bad("state")
    return copies


def _held(value, more: set = frozenset()) -> dict | None:
    if value is None:
        return None
    _exact(value, {"seq", "sha256"} | more, "state")
    _int(value["seq"], "state")
    _matching(SHA256, value["sha256"], "state")
    return value


def _named(things: list[str]) -> str:
    return things[0] if len(things) == 1 else ", ".join(things[:-1]) + " and " + things[-1]


def installed(skills: Path, unwritten: bool = True) -> dict:
    """What Tux installed from the registry, by file name in the skills folder, with each
    switch-off apply() couldn't write while the daemon runs, on the version it was made for,
    unless unwritten is False: then only what the file says."""
    data = _read_json(skills.parent / INSTALLED, MAX_STATE_BYTES)
    records = {}
    for name, record in (data.items() if isinstance(data, dict) else ()):
        if (isinstance(name, str) and "/" not in name and name.endswith(".yaml")
                and _record_ok(record, RECORD_FIELDS)
                and (record.get("was") is None
                     or _record_ok(record["was"], RECORD_FIELDS[1:]))):
            records[name] = _on_disk(skills / name, record)
    for name, held in _UNWRITTEN.get(skills.parent / INSTALLED, {}).items() if unwritten else ():
        record = records.get(name)
        if record and all(record[field] == held[field] for field in RECORD_FIELDS):
            records[name] = {**record, "off": held["off"]}
    return records


def _record_ok(record, fields: tuple) -> bool:
    return (isinstance(record, dict) and all(isinstance(record.get(f), str) for f in fields)
            and _off_ok(record.get("off")))


def _on_disk(file: Path, record: dict) -> dict:
    """The record of the version on disk. During an update the record names the one it replaces
    as "was" until the new file is written, so after a crash between the two, the one whose
    bytes are there."""
    record = dict(record)
    was = record.pop("was", None)
    if was is not None and _ours(file, was) is not None:
        return {"id": record["id"], **was}
    return record


def _off_ok(off) -> bool:
    # "copy" may be missing, from before Tux recorded it: such a switch-off is held until lifted
    return off is None or (isinstance(off, dict) and type(off.get("revocation")) is int
                           and off.get("kind") in ("suspend", "ban")
                           and isinstance(off.get("reason"), str)
                           and isinstance(off.get("copy", ""), str))


def switched_off(skills: Path) -> dict[str, tuple[str, str]]:
    """{file name: (the id it was installed as, why it is off)} for the loader."""
    return {name: (record["id"], switched_off_because(record["off"]["kind"],
                                                      record["off"]["reason"]))
            for name, record in installed(skills).items() if record.get("off")}
