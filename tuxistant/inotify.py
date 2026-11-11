"""Watch the skill folders and say when something in them changed.

Each root is watched, and so is each folder directly inside it (one per skill). Watches follow
folders as they come and go. A root that doesn't exist yet is waited for from its nearest
existing parent. Editors write several files per save, so events are coalesced: one on_change
per quiet period. Without inotify (no libc, or out of watches) it polls modification times.
"""
import asyncio
import ctypes
import ctypes.util
import errno
import inspect
import logging
import os
import struct
from pathlib import Path
from typing import Callable

log = logging.getLogger("tuxistant")

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000

MASK = (IN_CREATE | IN_DELETE | IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
        | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR)
SELF = IN_DELETE_SELF | IN_MOVE_SELF
# Events that can change which folders exist, so the watch set must be checked again.
STRUCTURE = SELF | IN_IGNORED | IN_Q_OVERFLOW

EVENT = struct.Struct("iIII")
READ_SIZE = 64 * 1024
POLL_S = 2.0
# A steady stream of writes must not hold a reload back for ever.
MAX_WAIT_FACTOR = 10

IGNORED_NAMES = frozenset({"__pycache__", "4913"})        # 4913: vim's "can I write here" probe
IGNORED_SUFFIXES = (".pyc", ".swp", ".swo", ".swx", "~")


def ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.endswith(IGNORED_SUFFIXES) or name.startswith(".#")


class _Inotify:
    """The three libc calls, with errors turned into OSError."""

    def __init__(self):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        self._init1, self._add, self._rm = (
            libc.inotify_init1, libc.inotify_add_watch, libc.inotify_rm_watch)
        self._init1.argtypes = [ctypes.c_int]
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._rm.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self._check(self._init1(os.O_NONBLOCK | os.O_CLOEXEC))

    @staticmethod
    def _check(result: int) -> int:
        if result < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        return result

    def add(self, path: Path) -> int:
        return self._check(self._add(self.fd, os.fsencode(path), MASK))

    def remove(self, wd: int) -> None:
        self._rm(self.fd, wd)        # fails harmlessly when the kernel already dropped it

    def close(self) -> None:
        os.close(self.fd)


def _inotify_init() -> "_Inotify | None":
    try:
        return _Inotify()
    except (OSError, AttributeError) as e:
        log.warning("inotify is unavailable (%s); polling the skill folders instead", e)
        return None


def _parse(data: bytes):
    offset = 0
    while offset + EVENT.size <= len(data):
        wd, mask, _cookie, length = EVENT.unpack_from(data, offset)
        offset += EVENT.size
        name = data[offset:offset + length].split(b"\0", 1)[0]
        offset += length
        yield wd, mask, os.fsdecode(name)


def _existing_ancestor(path: Path) -> Path:
    while not path.is_dir() and path.parent != path:
        path = path.parent
    return path


def _entries(folder: Path) -> list[tuple]:
    out = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if ignored(entry.name):
                    continue
                try:
                    if entry.is_dir():
                        # A folder's own time moves with ignored files like __pycache__.
                        out.append((entry.name, True, 0, 0))
                        continue
                    st = entry.stat()
                    out.append((entry.name, False, st.st_mtime_ns, st.st_size))
                except OSError:
                    out.append((entry.name, None, 0, 0))
    except OSError:
        return [None]
    return sorted(out, key=lambda e: e[0])


class Watcher:
    poll_s = POLL_S

    def __init__(self, roots: list[Path], on_change: Callable[[], None],
                 debounce_s: float = 0.3):
        self.roots = [Path(os.path.abspath(r)) for r in roots]
        self.on_change = on_change
        self.debounce_s = debounce_s
        self.polling = False
        self._loop = None
        self._inotify = None
        self._watches: dict[int, list[tuple[Path, str]]] = {}    # wd -> [(path, kind)]
        self._present: set[Path] = set()
        self._timer = None
        self._first_pending = None
        self._poller = None
        self._snapshot = None

    async def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._inotify = _inotify_init()
        if self._inotify is None:
            self._start_polling()
            return
        self._loop.add_reader(self._inotify.fd, self._readable)
        self._present = self._existing_roots()
        self._sync()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._poller is not None:
            self._poller.cancel()
            self._poller = None
        self._close_inotify()
        self._loop = None

    # Watches

    def _existing_roots(self) -> set[Path]:
        return {r for r in self.roots if r.is_dir()}

    def _wanted(self) -> list[tuple[Path, str]]:
        wanted = []
        for root in self.roots:
            if not root.is_dir():
                wanted.append((_existing_ancestor(root.parent), "anchor"))
                continue
            wanted.append((root, "root"))
            for entry in _entries(root):
                if entry is not None and entry[1]:
                    wanted.append((root / entry[0], "skill"))
        return wanted

    def _sync(self) -> None:
        """Make the watch set match the folders that exist now."""
        if self._inotify is None:
            return
        watches: dict[int, list[tuple[Path, str]]] = {}
        for path, kind in self._wanted():
            try:
                wd = self._inotify.add(path)
            except OSError as e:
                if e.errno in (errno.ENOSPC, errno.ENOMEM):
                    log.warning("Out of inotify watches; polling the skill folders instead")
                    self._close_inotify()
                    self._start_polling()
                    return
                continue      # it vanished or can't be read; the next event or sync catches up
            watches.setdefault(wd, []).append((path, kind))
        for wd in self._watches.keys() - watches.keys():
            self._inotify.remove(wd)
        self._watches = watches
        present = self._existing_roots()
        if present != self._present:
            self._present = present
            self._changed()

    def _close_inotify(self) -> None:
        if self._inotify is None:
            return
        if self._loop is not None:
            self._loop.remove_reader(self._inotify.fd)
        self._inotify.close()
        self._inotify = None
        self._watches = {}

    # Events

    def _readable(self) -> None:
        data = b""
        while self._inotify is not None:
            try:
                chunk = os.read(self._inotify.fd, READ_SIZE)
            except BlockingIOError:
                break
            except OSError as e:
                log.warning("Reading skill folder changes failed: %s", e)
                break
            if not chunk:
                break
            data += chunk
        relevant, resync = False, False
        for wd, mask, name in _parse(data):
            seen, structural = self._judge(wd, mask, name)
            relevant |= seen
            resync |= structural
        if resync:
            self._sync()
        if relevant:
            self._changed()

    def _judge(self, wd: int, mask: int, name: str) -> tuple[bool, bool]:
        """Whether an event means the skills changed, and whether the watch set needs a look."""
        if mask & IN_Q_OVERFLOW:
            return True, True
        if name and ignored(name):
            return False, False
        targets = self._watches.get(wd, [])
        if not targets:
            return False, bool(mask & IN_IGNORED)
        structural = bool(mask & (STRUCTURE | IN_ISDIR))
        if all(kind == "anchor" for _path, kind in targets):
            return False, True      # _sync notices a root that has appeared
        return not mask & IN_IGNORED, structural

    def _changed(self) -> None:
        if self._loop is None:
            return
        now = self._loop.time()
        if self._first_pending is None:
            self._first_pending = now
        if self._timer is not None:
            self._timer.cancel()
        latest = self._first_pending + self.debounce_s * MAX_WAIT_FACTOR
        self._timer = self._loop.call_at(min(now + self.debounce_s, latest), self._fire)

    def _fire(self) -> None:
        self._timer = None
        self._first_pending = None
        try:
            result = self.on_change()
            if inspect.isawaitable(result):
                asyncio.ensure_future(result)
        except Exception:
            log.exception("Reloading skills after a change failed")

    # Polling fallback

    def _start_polling(self) -> None:
        self.polling = True
        self._snapshot = self._scan()
        self._poller = self._loop.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            snapshot = self._scan()
            if snapshot != self._snapshot:
                self._snapshot = snapshot
                self._changed()

    def _scan(self) -> tuple:
        state = []
        for root in self.roots:
            if not root.is_dir():
                state.append((str(root), None))
                continue
            top = _entries(root)
            subs = tuple((e[0], tuple(_entries(root / e[0])))
                         for e in top if e is not None and e[1])
            state.append((str(root), tuple(top), subs))
        return tuple(state)
