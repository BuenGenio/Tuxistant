"""file.find, file.open and file.trash.

Finding reads the desktop's search index, which is full of things nobody means: firmware dumps,
package caches. So hits are ranked here, and only a handful reach the model.

Everything that names a file goes through resolve(), which holds the line whatever the model
asked for and wherever a symlink points: inside the home folder, outside hidden folders.
Trashing returns an Ask carrying a list made once, before the card: the click is for that list.
"""
import functools
import os
import re
import stat
import subprocess
import time
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .gate import Ask, printable, shown

SEARCH_TIMEOUT_S = 8
OPEN_TIMEOUT_S = 15
BATCH_TIMEOUT_S = 30
MAX_HITS = 100_000      # the index answers in path order, so a low cap would lose the late letters
MAX_RESULTS = 5
MAX_QUERY_WORDS = 6
MAX_NAME_CHARS = 120
MIN_PARTIAL_NAME = 3
MAX_LISTED = 15
MAX_LINE_CHARS = 200    # 15 lines of this fit on the card, so "and 297 more" is never cut off
MAX_TRASHED = 50_000
TRASH_BATCH = 100
TRASH_FILES_PER_S = 100     # gio does 5000 on an SSD; this is the slow disk's allowance

WORD = re.compile(r"[^\W_]+")
YEAR = re.compile(r"(?:19|20)\d\d")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
AGE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(day|week|month|year)?s?\s*")
FOLDER_WORDS = re.compile(r"^(?:(?:my|the)\s+)+|\s+(?:folder|directory)$", re.I)

# The model pads a query with these, and the index wants every word it is given.
STOP_WORDS = {"a", "about", "all", "an", "and", "at", "called", "document", "documents", "file",
              "files", "find", "folder", "for", "from", "i", "in", "look", "me", "my", "named",
              "of", "on", "our", "please", "search", "some", "that", "the", "this", "to", "with",
              "your"}
UNITS = {"day": 1, "week": 7, "month": 30, "year": 365}

KINDS = {
    "document": {".csv", ".doc", ".docx", ".epub", ".htm", ".html", ".key", ".md", ".mobi",
                 ".numbers", ".odp", ".ods", ".odt", ".pages", ".pdf", ".ppt", ".pptx", ".rtf",
                 ".tex", ".txt", ".xls", ".xlsx"},
    "image": {".arw", ".avif", ".bmp", ".cr2", ".dng", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
              ".nef", ".png", ".svg", ".tif", ".tiff", ".webp"},
    "video": {".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm",
              ".wmv"},
    "audio": {".aac", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma"},
}
KEPT_SUFFIXES = set().union(*KINDS.values())
KIND_WORDS = {"doc": "document", "pdf": "document", "text": "document", "photo": "image",
              "picture": "image", "screenshot": "image", "movie": "video", "clip": "video",
              "music": "audio", "song": "audio", "recording": "audio", "sound": "audio",
              **{kind: kind for kind in KINDS}}

XDG_FOLDERS = {"DESKTOP": "Desktop", "DOWNLOAD": "Downloads", "TEMPLATES": "Templates",
               "PUBLICSHARE": "Public", "DOCUMENTS": "Documents", "MUSIC": "Music",
               "PICTURES": "Pictures", "VIDEOS": "Videos"}
FIRST_LOOK_FOLDERS = ("DOCUMENTS", "DOWNLOAD", "DESKTOP")
# Folders of other people's files. A name in here is never the one you were thinking of.
JUNK_FOLDERS = {"__pycache__", "bin", "build", "dist", "dist-packages", "etc", "include", "lib",
                "lib64", "node_modules", "obj", "opt", "pkg", "sbin", "sdk", "share",
                "site-packages", "snap", "target", "third_party", "usr", "var", "vendor", "venv"}
FIRST_LOOK, ELSEWHERE, JUNK = 0, 1, 2

ORDINALS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3,
            "three": 3, "fourth": 4, "4th": 4, "four": 4, "fifth": 5, "5th": 5, "five": 5}
PICKING_WORDS = {"document", "file", "item", "no", "number", "result", "the"}

# What gio open may be handed. A list of what is safe, as whatever is not listed may run something.
SHOWABLE_FAMILIES = ("audio/", "image/", "video/", "application/vnd.oasis.opendocument.",
                     "application/vnd.openxmlformats-officedocument.")
SHOWABLE_TYPES = {"application/epub+zip", "application/json", "application/msword",
                  "application/pdf", "application/rtf", "application/vnd.ms-excel",
                  "application/vnd.ms-powerpoint", "application/x-mobipocket-ebook",
                  "application/yaml", "text/csv", "text/markdown", "text/plain", "text/rtf",
                  "text/tab-separated-values", "text/x-log"}
SCRIPTED_TYPES = {"image/svg+xml"}      # an image family member, but a browser runs its scripts
# Sniffed as plain text when they hold text, whatever they do when run.
RUNNABLE_SUFFIXES = {".bat", ".bin", ".cmd", ".com", ".command", ".desktop", ".exe", ".run", ".sh"}

SAY_WHICH = "Say which file you mean."
NOT_FOUND = "I can't find that file. Search for it first."
OUTSIDE = "I can only reach files inside your home folder."
HIDDEN = "That is in a hidden folder, so I am leaving it alone."
WONT_SHOW = ("I only open documents, pictures, audio and video. "
             "That file could run something, so I left it closed.")


@functools.cache
def _real(home: str) -> Path:
    return Path(home).resolve()


def _home() -> Path:
    """Asked once per hit, and following links is a system call per folder."""
    return _real(os.path.expanduser("~"))


def _inside(path: Path) -> tuple[str, ...] | None:
    """The parts of path below the home folder: empty for the folder itself, None outside it."""
    for home in (_home(), Path.home()):
        if path.is_relative_to(home):
            return path.relative_to(home).parts
    return None


def _is_hidden(parts: Iterable[str]) -> bool:
    return any(part.startswith(".") for part in parts)


def _xdg_folders() -> dict[str, Path]:
    home = _home()
    folders = {key: home / name for key, name in XDG_FOLDERS.items()}
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        chosen = re.findall(r'^XDG_(\w+)_DIR="(.*)"$', config.read_text(), re.M)
    except (OSError, UnicodeDecodeError):
        return folders
    for key, value in chosen:
        path = Path(value.replace("$HOME", str(home)))
        if key in folders and path != home:     # a folder set to the home folder means "not used"
            folders[key] = path
    return folders


def _tilde(path: Path) -> str:
    parts = _inside(path)
    if parts is None:
        return str(path)
    return "/".join(("~", *parts))


def _shown(text: str, limit: int = MAX_LINE_CHARS) -> str:
    return shown(text, limit)


def _size(count: float) -> str:
    if count < 1000:
        return f"{int(count)} bytes"
    for unit in ("KB", "MB", "GB"):
        count /= 1000
        if count < 1000:
            return f"{count:.1f} {unit}"
    return f"{count / 1000:.1f} TB"


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _given(value) -> bool:
    """The model fills optional arguments it has no use for with all of these."""
    return value not in (None, "", 0, [], {}, "none", "null")


def _days(value) -> float | None:
    """A number of days from 30, "30" or "30 days". Anything else is None and never a guess."""
    match = AGE.fullmatch(str(value).casefold())
    return float(match[1]) * UNITS[match[2] or "day"] if match else None


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


# Naming a file

def _position(text: str, count: int) -> int | None:
    """The result number in "2", "the second one" or "last", if that is what text is."""
    words = [w for w in re.findall(r"[\w-]+", text.casefold()) if w not in PICKING_WORDS]
    if len(words) > 1 and words[-1] == "one":
        words.pop()
    if len(words) != 1:
        return None
    word = words[0]
    if word == "last":
        return count
    return int(word) if word.isdecimal() and len(word) < 5 else ORDINALS.get(word)


def _numbered(number: int, found: list[str]) -> Path:
    if not found:
        raise ValueError("I have no list of files yet. Search for the file first.")
    if not 1 <= number <= len(found):
        raise ValueError(f"There {'is' if len(found) == 1 else 'are'} only "
                         f"{_plural(len(found), 'result')}.")
    return Path(found[number - 1])


def _called(text: str, found: list[str]) -> Path | None:
    """The one result the model meant, however much of the name survived the trip."""
    wanted = text.casefold()
    matches = [f for f in found if wanted in (Path(f).name.casefold(), Path(f).stem.casefold())]
    if not matches and len(wanted) >= MIN_PARTIAL_NAME:
        matches = [f for f in found if wanted in Path(f).name.casefold()]
    if len(matches) > 1:
        raise ValueError(f"{_plural(len(matches), 'result')} match that. Say the number instead.")
    return Path(matches[0]) if matches else None


def _path(text: str) -> Path:
    """Never expanduser: ~root would walk out of the home folder before anything could look."""
    if text.startswith("file://"):
        text = os.fsdecode(urllib.parse.unquote_to_bytes(text.removeprefix("file://")))
    if text == "~" or text.startswith("~/"):
        text = text[2:]
    return Path(os.path.normpath(_home() / text))    # an absolute text replaces the home folder


def _fit_case(path: Path) -> Path:
    """Speech has no capital letters, so ~/documents can only mean ~/Documents."""
    parts = _inside(path)
    if not parts or os.path.lexists(path):
        return path
    fitted = _home()
    for part in parts:
        if not os.path.lexists(fitted / part):
            try:
                same = [c for c in os.listdir(fitted) if c.casefold() == part.casefold()]
            except OSError:
                return path
            if len(same) != 1:
                return path
            part = same[0]
        fitted = fitted / part
    return fitted


def _named(value, session) -> Path:
    """The file value points at, as named: nothing has been checked and links are not followed."""
    found = session.found
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return _numbered(value, found)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(SAY_WHICH)
    text = value.strip()
    number = _position(text, len(found))
    if number is not None:
        return _numbered(number, found)
    return _called(text, found) or _fit_case(_path(text))


def _checked(path: Path) -> Path:
    """path with every link followed, if the file itself and the way to it are both allowed."""
    try:
        real = path.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise ValueError(NOT_FOUND) from e
    for candidate in (path, real):
        parts = _inside(candidate)
        if not parts:
            raise ValueError(OUTSIDE)
        if _is_hidden(parts):
            raise ValueError(HIDDEN)
    if not real.exists():
        raise ValueError(NOT_FOUND)
    return real


def resolve(value, session) -> Path:
    """The real file behind a result number, an ordinal word, a result's name or a path under the
    home folder. Anything else raises ValueError with a sentence that can be spoken."""
    return _checked(_named(value, session))


def _folder(value) -> Path:
    text = FOLDER_WORDS.sub("", str(value).strip()).strip()
    if text.casefold() in ("", "~", "home"):
        return _home()
    path = _checked(_fit_case(_path(text)))
    if not path.is_dir():
        raise ValueError("That is not a folder.")
    return path


# file.find

def _terms(query) -> tuple[list[str], int | None]:
    """The words worth searching for, and a year if the model left one in with them."""
    words = [w for w in WORD.findall(str(query or "").casefold()) if w not in STOP_WORDS]
    words = words[:MAX_QUERY_WORDS]
    years = [w for w in words if YEAR.fullmatch(w)]
    others = [w for w in words if w not in years]
    return (others, int(years[0])) if others and years else (words, None)


def _year(value) -> int | None:
    text = str(value or "").strip().casefold()
    named = YEAR.search(text)
    if named:
        return int(named[0])
    back = {"this year": 0, "last year": 1}.get(text)
    return None if back is None else time.localtime().tm_year - back


def _kind(value) -> set[str] | None:
    word = str(value or "").strip().casefold()
    kind = KIND_WORDS.get(word) or KIND_WORDS.get(word.removesuffix("s"))
    return KINDS.get(kind)


def _localsearch(words: list[str]) -> list[Path] | None:
    """Names and contents. None if it could not be asked, [] if it found nothing. Its -i, -v and
    --audio only look at embedded metadata and -f and -t cannot be combined, so no flag is best:
    that covers both, and the kind is checked on the hits."""
    result = _run(["localsearch", "search", "-l", str(MAX_HITS), "--", *words], SEARCH_TIMEOUT_S)
    if result is None or result.returncode:
        return None
    lines = (ANSI.sub("", line).strip() for line in os.fsdecode(result.stdout).splitlines())
    return [Path(os.fsdecode(urllib.parse.unquote_to_bytes(line.removeprefix("file://"))))
            for line in lines if line.startswith("file://")]


def _plocate(words: list[str]) -> list[Path] | None:
    """Names only, and as old as last night's update. It exits 1 for no matches as well."""
    result = _run(["plocate", "-i", "-b", "-0", "-l", str(MAX_HITS), "--", *words],
                  SEARCH_TIMEOUT_S)
    if result is None or (result.returncode and result.stderr):
        return None
    return [Path(os.fsdecode(hit)) for hit in result.stdout.split(b"\0") if hit]


def _files_in(folder: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Regular files below folder, skipping dot-names and never following a symlink."""
    for root, folders, names in os.walk(folder, followlinks=False):
        folders[:] = sorted(f for f in folders if not f.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(root, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield path, info


@dataclass(frozen=True)
class _Wanted:
    suffixes: set[str] | None = None
    year: int | None = None
    since: float | None = None
    base: Path | None = None        # below this folder
    part: str | None = None         # or below any folder of this name

    def path(self, path: Path) -> bool:
        parts = _inside(path)
        return (bool(parts) and not _is_hidden(parts)
                and (self.suffixes is None or path.suffix.casefold() in self.suffixes)
                and (self.base is None or path.is_relative_to(self.base))
                and (self.part is None or self.part in map(str.casefold, parts[:-1])))

    def info(self, path: Path, info: os.stat_result) -> bool:
        if not stat.S_ISREG(info.st_mode) or (self.since and info.st_mtime < self.since):
            return False
        return (self.year is None
                or time.localtime(info.st_mtime).tm_year == self.year
                # A date such as IMG_20240101 counts; a build stamp like 20240729143630 doesn't.
                or bool(re.search(rf"(?<!\d){self.year}(\d{{4}})?(?!\d)",
                                  "/".join(_inside(path)))))


def _kept(paths: Iterable[Path], wanted: _Wanted) -> list[tuple[Path, os.stat_result]]:
    kept = []
    for path in dict.fromkeys(paths):
        if wanted.path(path):
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if wanted.info(path, info):
                kept.append((path, info))
    return kept


def _listed(folders: Iterable[Path], wanted: _Wanted) -> list[tuple[Path, os.stat_result]]:
    """No words to search for: what is simply there, for "what did I download this week"."""
    return [(path, info) for folder in folders
            for path, info in islice(_files_in(folder), MAX_HITS)
            if wanted.path(path) and wanted.info(path, info)]


def _dumps(home: Path) -> set[str]:
    """Top-level folders that are a copy of a whole system, such as an unpacked firmware image."""
    try:
        return {f.name for f in os.scandir(home) if f.is_dir(follow_symlinks=False)
                and (Path(f.path) / "etc").is_dir() and (Path(f.path) / "usr").is_dir()}
    except OSError:
        return set()


def _name_match(name: str, words: list[str]) -> int:
    """0: the name holds every word whole. 1: it holds them inside longer words. 2: it doesn't."""
    folded = name.casefold()
    if all(word in WORD.findall(folded) for word in words):
        return 0
    return 1 if all(word in folded for word in words) else 2


def _ranked(hits: list[tuple[Path, os.stat_result]],
            words: list[str]) -> list[tuple[Path, os.stat_result]]:
    folders = _xdg_folders()
    first = [folders[key] for key in FIRST_LOOK_FOLDERS]
    dumps = _dumps(_home())

    def place(path: Path) -> int:
        parts = _inside(path)
        if parts[0] in dumps or not JUNK_FOLDERS.isdisjoint(map(str.casefold, parts[:-1])):
            return JUNK
        return FIRST_LOOK if any(path.is_relative_to(f) for f in first) else ELSEWHERE

    def rank(hit: tuple[Path, os.stat_result]) -> tuple:
        path, info = hit
        where, match = place(path), _name_match(path.name, words)
        # The index also finds every passing mention, and those crowd out a document named for
        # what you asked. Code named for it is no better than a mention, though.
        called = bool(words) and match < 2 and path.suffix.casefold() in KEPT_SUFFIXES
        return where == JUNK, not called, where, match, -info.st_mtime

    return sorted(hits, key=rank)


def _where(value) -> tuple[Path | None, str | None]:
    """A folder to search: a real one when it can be found from the home folder, otherwise, for a
    bare name such as Tax, any folder of that name."""
    if not _given(value):
        return None, None
    try:
        return _folder(value), None
    except ValueError:
        text = FOLDER_WORDS.sub("", str(value).strip()).strip()
        if "/" in text or text.startswith("~"):
            raise
        return None, text.casefold()


def _searched(words: list[str], wanted: _Wanted) -> list[tuple[Path, os.stat_result]]:
    """The index first. A failed or empty answer falls to plocate, which the index can miss."""
    answers = [_localsearch(words)]
    kept = _kept(answers[0] or [], wanted)
    if not kept:
        answers.append(_plocate(words))
        kept = _kept(answers[1] or [], wanted)
    if all(answer is None for answer in answers):
        raise RuntimeError("File search isn't working on this computer right now.")
    return kept


def find(args: dict, _config, session) -> dict:
    words, year_in_query = _terms(args.get("query"))
    days = _days(args.get("days")) if _given(args.get("days")) else None
    base, part = _where(args.get("folder"))
    wanted = _Wanted(_kind(args.get("kind")), _year(args.get("year")) or year_in_query,
                     time.time() - days * 86400 if days else None, base, part)
    if words:
        hits = _searched(words, wanted)
    elif any(v is not None for v in (wanted.suffixes, wanted.year, wanted.since, base, part)):
        hits = _listed([base] if base else _xdg_folders().values(), wanted)
    else:
        raise ValueError("Say what to look for.")

    ranked = _ranked(hits, words)
    results, session.found = [], []
    for path, info in ranked:
        try:
            session.found.append(str(_checked(path)))
        except ValueError:
            continue
        results.append({
            "n": len(results) + 1, "name": _shown(path.name, MAX_NAME_CHARS),
            "folder": _shown(_tilde(path.parent), MAX_NAME_CHARS),
            "modified": time.strftime("%-d %B %Y", time.localtime(info.st_mtime)),
            "size": _size(info.st_size)})
        if len(results) == MAX_RESULTS:
            break
    return {"count": len(ranked), "results": results}


# file.open

def _content_type(path: Path) -> str:
    """What gio open will go by. Nothing printed before the attributes may be believed, as a file
    name can hold a line break."""
    result = _run(["gio", "info", "-a", "standard::content-type", "--", str(path)],
                  SEARCH_TIMEOUT_S)
    text = os.fsdecode(result.stdout) if result and not result.returncode else ""
    types = re.findall(r"^  standard::content-type: (\S+)$", text, re.M)
    return types[-1] if types else ""


def _showable(path: Path) -> bool:
    if path.is_dir():
        return True
    if os.access(path, os.X_OK) or path.suffix.casefold() in RUNNABLE_SUFFIXES:
        return False
    content = _content_type(path)
    return content not in SCRIPTED_TYPES and (content in SHOWABLE_TYPES
                                              or content.startswith(SHOWABLE_FAMILIES))


def _launch(path: Path) -> bool:
    """No pipes: the app inherits them and stays open, and a read would wait for it to quit."""
    try:
        return subprocess.run(["gio", "open", "--", str(path)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=OPEN_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_file(args: dict, _config, session) -> dict:
    path = resolve(args.get("file"), session)
    if not _showable(path):
        raise ValueError(WONT_SHOW)
    if not _launch(path):
        raise RuntimeError("That file wouldn't open.")
    return {"opened": _shown(path.name, MAX_NAME_CHARS)}


# file.trash

@dataclass(frozen=True)
class _Target:
    path: Path
    identity: tuple[int, int] | None    # so a new file taking this name is not moved instead
    size: int
    files: int = 1                      # a folder goes in one move, so the card counts its files
    is_folder: bool = False

    def line(self) -> str:
        if not self.is_folder:
            return _shown(_tilde(self.path))
        return f"{_shown(_tilde(self.path))}/ ({_plural(self.files, 'file')}, {_size(self.size)})"


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _too_many() -> ValueError:
    return ValueError(f"That is more than {MAX_TRASHED:,} files, "
                      "which is too many for me. Pick something smaller.")


def _contents(folder: Path) -> tuple[int, int]:
    """Files and bytes in a folder, hidden ones included: they go with it."""
    count = size = 0
    for root, _, names in os.walk(folder, followlinks=False):
        count += len(names)
        if count > MAX_TRASHED:
            raise _too_many()
        for name in names:
            try:
                size += os.lstat(Path(root, name)).st_size
            except OSError:
                continue
    return count, size


def _target(value, session) -> _Target:
    named = _named(value, session)
    real = _checked(named)
    # Trashing a link moves the link, and the card has to say so.
    path = named.parent.resolve() / named.name if named.is_symlink() else real
    if any(folder.is_relative_to(path) for folder in _xdg_folders().values()):
        raise ValueError(f"I won't trash your whole {path.name} folder. Say which files in it.")
    try:
        info = os.lstat(path)
    except OSError as e:
        raise ValueError(NOT_FOUND) from e
    if stat.S_ISDIR(info.st_mode):
        count, size = _contents(path)
        return _Target(path, _identity(path), size, count, True)
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise ValueError("That isn't an ordinary file, so I am leaving it alone.")
    return _Target(path, _identity(path), info.st_size)


def _swept(value, older_than) -> list[_Target]:
    folder = _folder(value)
    if folder == _home():
        raise ValueError("I won't go through your whole home folder. Name a folder inside it.")
    days = None
    if _given(older_than):
        days = _days(older_than)
        if days is None:    # dropping it would widen the list to every file in the folder
            raise ValueError("Say how many days old the files should be.")
    cutoff = time.time() - days * 86400 if days else None
    chosen = [(path, info) for path, info in _files_in(folder)
              if cutoff is None or info.st_mtime < cutoff]
    if len(chosen) > MAX_TRASHED:
        raise _too_many()
    return [_Target(path, (info.st_dev, info.st_ino), info.st_size)
            for path, info in sorted(chosen, key=lambda item: item[0])]


def _move(targets: list[_Target], seconds: float) -> dict:
    """Moves what the card showed and nothing else. Whatever is still there afterwards failed."""
    moved = 0
    deadline = time.monotonic() + seconds
    for start in range(0, len(targets), TRASH_BATCH):
        batch = targets[start:start + TRASH_BATCH]
        paths = [str(t.path) for t in batch if _identity(t.path) == t.identity]
        left = deadline - time.monotonic()
        if paths and left > 0:
            _run(["gio", "trash", "--", *paths], min(BATCH_TIMEOUT_S, left))
            moved += sum(not os.path.lexists(path) for path in paths)
    return {"moved": moved, "failed": len(targets) - moved}


def _card(targets: list[_Target]) -> Ask:
    folders = sum(t.is_folder for t in targets)
    size = _size(sum(t.size for t in targets))
    if folders:
        names = [_plural(n, noun) for n, noun in ((len(targets) - folders, "file"),
                                                  (folders, "folder")) if n]
        inside = _plural(sum(t.files for t in targets), "file")
        what = f"{' and '.join(names)} ({inside} in all, {size})"
    else:
        what = f"{_plural(len(targets), 'file')} ({size})"
    lines = [t.line() for t in targets[:MAX_LISTED]]
    if len(targets) > MAX_LISTED:
        lines.append(f"and {len(targets) - MAX_LISTED:,} more")
    seconds = 30 + len(targets) // TRASH_FILES_PER_S
    return Ask(f"Move {what} to the Trash?", "\n".join(lines),
               lambda: _move(targets, seconds), timeout_s=seconds)


def trash(args: dict, _config, session):
    if _given(args.get("file")):
        named = args["file"] if isinstance(args["file"], list) else [args["file"]]
        if len(named) > MAX_LISTED:     # the card must show every file that was picked by name
            raise ValueError(f"That's too many to name one by one. I can move {MAX_LISTED} at a "
                             "time, or a whole folder.")
        targets = list({t.path: t for t in (_target(v, session) for v in named)}.values())
    elif _given(args.get("folder")):
        targets = _swept(args["folder"], args.get("older_than_days"))
    else:
        raise ValueError("Say which file or folder to move to the Trash.")
    if not targets:
        return {"moved": 0, "failed": 0, "note": "Nothing matched, so nothing was moved."}
    return _card(targets)


HANDLERS = {"file.find": find, "file.open": open_file, "file.trash": trash}
