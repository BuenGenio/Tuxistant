"""Timers and reminders, which Tux says out loud when they come due.

Two layers. The store and the scheduler know nothing about the daemon: the store keeps what is
pending in one small file, so that it outlives a restart, and run() sleeps until the next one is
due and hands it to whatever announce function it was given. Above them are the three actions the
model sees, the words and the notification for an item that has come due, and an Announcer that
ties the scheduler to what a daemon can do.

The model is bad at dates and sums, so it passes how long, the day and the time as the user said
them and they are worked out here, with calendars.py reading the days and the clock times. A
label came through speech recognition and a model, so it is cut short, and its control characters
are shown as escapes wherever it is said, shown or handed back to the model.

Nothing here needs an approval card. Setting, listing and cancelling change one file of
Tuxistant's own and nothing outside it. What is said later is said by the app itself and not by
the model, so a label can never become an instruction.
"""
import array
import asyncio
import difflib
import functools
import json
import logging
import math
import os
import re
import threading
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import time as epoch

from .audio import RATE
# The two private names are how the calendar says a day and a time. Reminders have to say them
# the same way, and a copy here would drift.
from .calendars import _clock_name, _day_name, clock, minutes, period
from .gate import shown

log = logging.getLogger("tuxistant")

KINDS = ("timer", "reminder")
STATES = ("pending", "done", "missed", "cancelled")
MAX_PENDING = 50            # more than anyone sets by voice, and where a model in a loop stops
MAX_KEPT = 50               # said, missed and cancelled ones, kept as a record
MAX_LABEL_CHARS = 60        # a label is a few words; a longer one is the model rambling
MAX_AHEAD_S = 366 * 86400   # a year. Further off is the model inventing a date
# What a saved file may hold. Past these a number is damage: a time after the year 9999 can't be
# said or shown, and an id past what JSON keeps exact would be lost on the next start.
LAST_S = 253402300799
MAX_ID = 2 ** 53
MAX_SPOKEN = 6              # a small model reads out all it is given
LATE_AFTER_S = 60           # under a minute late isn't worth mentioning
# How long after it was due an item is still worth saying aloud. A timer is about something
# happening now: an hour on, the pasta is past saving. A reminder is a thing still to do, so it
# keeps for a day. After that, speech would come as a surprise in whatever room you are in by
# then, so it goes to the record and a quiet notification instead.
STALE_AFTER_S = {"timer": 3600, "reminder": 86400}
# asyncio sleeps on a clock that stops while the computer is suspended, and that never notices
# the wall clock being set. So no sleep is longer than this before the time is looked at again.
MAX_NAP_S = 30
RETRY_S = 2                 # how soon to offer an item again after announce said not now
# When speech fails before a word is heard, as with the speech server down, the item waits this
# long to be tried again, twice as long each time, and after FAILED_TRIES it is missed. Each try
# shows an error, so trying every RETRY_S would flash one every two seconds. The first wait is
# a minute, so a second try says it is late and has no chime.
FAILED_WAIT_S = 60
FAILED_TRIES = 4
NOTE_WAIT_S = 10            # a note that takes longer than this is given up on
NOTIFY_WAIT_S = 3           # the desktop answers a notification at once, or isn't going to
TWICE_S = 10                # the same thing set again this soon is the model repeating itself

DEFAULT_HOUR = 9            # "remind me tomorrow", with no time: when most people are at a desk
PART_HOURS = {"morning": 9, "afternoon": 15, "evening": 19, "night": 21}
PART_WORDS = {**{word: word for word in PART_HOURS}, "tonight": "night"}
NIGHT_UNTIL = 6             # a bare "at 3" is never the small hours, unless it is to wake you
CLOSE = 0.7                 # how alike a misheard name must be: "pastor" finds "pasta"
ICON = "alarm-symbolic"

NOT_HERE = "Timers aren't available right now."
NOT_SAVED = "I couldn't save that, so nothing has changed."
PASSED = "That time has already passed."
TOO_FAR = "That's too far ahead. I can keep a timer or a reminder for up to a year."
NO_WHEN = "Say how long the timer is for, or when to remind you."
NOTHING = {"timer": "No timers are running.", "reminder": "No reminders are set.",
           None: "No timers or reminders are set."}

LONG_KEYS = ("duration", "length", "long")
UNIT_KEYS = ("seconds", "minutes", "hours")
DAY_KEYS = ("day", "date", "when")
TIME_KEYS = ("time", "at", "clock")
NAME_KEYS = ("label", "name")
ABOUT_KEYS = ("about", "reminder", "message", "text", "title", "what", "task", "note")
WHICH_KEYS = ("label", "name", "about", "which", "timer", "reminder", "title")
ABSENT = {"", "none", "null", "n/a", "undefined"}    # how a small model fills what it doesn't need

NUMBERS = {word: n for n, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
NUMBERS.update({word: 20 + 10 * n for n, word in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())})
NUMBERS.update(a=1, an=1)
UNIT_S = {**dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1),
          **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60),
          **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600),
          **dict.fromkeys(("d", "day", "days"), 86400),
          **dict.fromkeys(("w", "wk", "wks", "week", "weeks"), 7 * 86400)}
UNITS = (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))
LEAD = re.compile(r"\b(?:in|for|about|around|after|from now|time|long|timer)\b")
# Tried in this order, on words whose numbers are already digits and whose "a" is a 1.
HALVES = (
    (re.compile(r"(\d+) and 1 half ([a-z]+)"), r"\1.5 \2"),         # two and a half hours
    (re.compile(r"(\d+) ([a-z]+) and 1 half\b"), r"\1.5 \2"),       # an hour and a half
    (re.compile(r"\bhalf (?:of )?(?:1 )?([a-z]+)"), r"0.5 \1"),     # half an hour
    (re.compile(r"\b1 couple (?:of )?"), "2 "),
    (re.compile(r"(?:(\d+) )?quarters? (?:of )?(?:1 )?hours?"),
     lambda found: f"{int(found[1] or 1) * 15} minutes"),
)
PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]*)")

MERIDIEM = re.compile(r"\b([ap])\.?\s?m\b\.?")
LITTLE = re.compile(r"\b(?:on|in|the|by|around|about|for|of|coming)\b")
ISO = re.compile(r"(\d{4}-\d{2}-\d{2})[t ](\d{1,2}:\d{2})\S*")
TIME = re.compile(r"(\d{1,2})(?:[:.h]\d{2})?(?::\d{2})?\s*(am|pm)?")
WORD_TIMES = ("noon", "midday", "midnight")
SPOKEN = (
    (re.compile(r"\bhalf past (\d{1,2})\b"), r"\1:30"),
    (re.compile(r"\b(?:1 )?quarter past (\d{1,2})\b"), r"\1:15"),
    (re.compile(r"\b(?:1 )?quarter to (\d{1,2})\b"),
     lambda found: f"{(int(found[1]) - 2) % 12 + 1}:45"),
    (re.compile(r"\b(\d{1,2}) (\d{2})\b"), r"\1:\2"),               # "seven thirty"
)
WAKING = re.compile(r"\b(?:wake|waking|get up|alarm)")
EDGES = re.compile(r"^(?:(?:to|that|about|for|the|a|an|my)\s+)+|\s+(?:timer|reminder|alarm)$",
                   re.IGNORECASE)
TRIM = " .,:;!?\"'"
NO_LABEL = {"", "timer", "reminder", "alarm", "none", "null", "unnamed", "default"}
KIND_WORDS = {"timer": "timer", "timers": "timer", "reminder": "reminder",
              "reminders": "reminder"}
PLURALS = {"timers", "reminders", "alarms"}
EVERY = {"all", "every", "everything", "both", "each"}
NOISE = {*KIND_WORDS, *EVERY, "alarm", "alarms", "the", "my", "a", "an", "of", "them", "one",
         "ones", "for", "to", "please", "that", "this", "set", "running", "current", "is", "are",
         "any", "on", "left", "it", "called", "named", "o", "clock", "oclock", "and"}


@dataclass(frozen=True)
class Item:
    id: int
    kind: str                   # "timer": made from how long. "reminder": from a day or a time
    label: str                  # as it was said, cut short. Escape it with shown() on the way out
    due: float                  # seconds since the epoch, as are made, rung and ended
    made: float
    state: str = "pending"      # then "done", "missed" or "cancelled"
    rung: float | None = None   # when it was found due and noted, while it waits to be said
    ended: float | None = None


class Full(ValueError):
    """The cap on pending items was reached. Its text is a sentence to say."""


def _number(value, top: float = math.inf) -> bool:
    """A real number from 0 to top. A huge integer is compared, never turned into a float."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= top)


def _count(value) -> bool:
    return isinstance(value, int) and _number(value, MAX_ID)


def _instant(value) -> bool:
    return _number(value, LAST_S)


def _item(row) -> Item | None:
    """One saved row, or None if it isn't one. A bad row must not cost the others."""
    try:
        item = Item(**row)
    except TypeError:
        return None
    sound = (_count(item.id) and _instant(item.due) and _instant(item.made)
             and isinstance(item.label, str) and item.kind in KINDS and item.state in STATES
             and all(when is None or _instant(when) for when in (item.rung, item.ended)))
    return item if sound else None


class Store:
    """Every timer and reminder, in one JSON file that is replaced whole on each change.

    A file and not a table: there are a hundred small rows at most, a whole file swapped into
    place can't be half written, and one that is damaged anyway can be set aside and begun again.
    It may be used from the event loop and from handler threads at once. It never reads a clock:
    the caller says what time it is.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.listeners = []     # called after every change, from whichever thread made it
        self.items, self.next_id = [], 1
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            self.path.chmod(0o600)      # what people ask to be reminded of is as private as history
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except OSError as e:    # start empty rather than not at all. A save may fail too
            log.warning("timers: cannot read %s: %s", self.path, e)
            return
        except (ValueError, RecursionError):
            saved = None
        rows = saved.get("items") if isinstance(saved, dict) else None
        if not isinstance(rows, list):
            return self._set_aside()
        self.items = list({item.id: item for item in map(_item, rows) if item}.values())
        counter = saved.get("next")
        self.next_id = max([counter if _count(counter) else 1,
                            *(item.id + 1 for item in self.items)])

    def _set_aside(self) -> None:
        """A file that isn't ours to read is kept beside the new one for a person to look at.
        Refusing to start over it would cost every timer from then on."""
        spare = self.path.with_name(self.path.name + ".broken")
        log.warning("timers: %s is damaged, so it is now %s", self.path, spare.name)
        try:
            self.path.replace(spare)
        except OSError as e:
            log.warning("timers: could not move it: %s", e)

    def _write(self, items: list[Item], next_id: int) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w") as file:
            os.fchmod(fd, 0o600)    # a leftover from a crash keeps whatever mode it had
            json.dump({"version": 1, "next": next_id, "items": [asdict(i) for i in items]}, file)
            file.flush()
            os.fsync(fd)            # a timer that was confirmed aloud has to survive a power cut
        tmp.replace(self.path)

    def _save(self, items: list[Item], next_id: int, anyway: bool = False) -> None:
        """The disk first and memory second, so that nothing is claimed that wasn't kept. With
        anyway, memory changes even if the disk refuses: for what has already been said, where
        the other order would say it again for ever."""
        over = sorted((i for i in items if i.state != "pending"), key=lambda i: i.ended or 0)
        items = [i for i in items if i.state == "pending"] + over[-MAX_KEPT:]
        try:
            self._write(items, next_id)
        except OSError as e:
            if not anyway:
                raise
            log.warning("timers: could not save: %s", e)
        self.items, self.next_id = items, next_id
        for tell in list(self.listeners):
            try:
                tell()
            except Exception:   # a listener whose loop has gone must not undo a save
                log.exception("timers: a listener failed")

    def _change(self, ids, anyway: bool = False, **fields) -> list[Item]:
        """Only what is still pending changes, so a late finish can't undo a cancel."""
        with self.lock:
            changed = {i.id: replace(i, **fields) for i in self.items
                       if i.id in ids and i.state == "pending"}
            if changed:
                self._save([changed.get(i.id, i) for i in self.items], self.next_id, anyway)
            return sorted(changed.values(), key=lambda i: (i.due, i.id))

    def add(self, kind: str, label: str, due: float, now: float) -> Item:
        with self.lock:
            if len(self.pending()) >= MAX_PENDING:
                raise Full(f"You already have {MAX_PENDING} timers and reminders. "
                           "Cancel one first.")
            item = Item(self.next_id, kind, label, due, now)
            self._save([*self.items, item], self.next_id + 1)
            return item

    def pending(self) -> list[Item]:
        """What is still to be said, soonest first."""
        with self.lock:
            return sorted((i for i in self.items if i.state == "pending"),
                          key=lambda i: (i.due, i.id))

    def due(self, now: float) -> list[Item]:
        return [item for item in self.pending() if item.due <= now]

    def live(self, id: int) -> Item | None:
        """The item, if it is still pending."""
        return next((item for item in self.pending() if item.id == id), None)

    def kept(self) -> list[Item]:
        """The record: what was said, missed or cancelled, newest first."""
        with self.lock:
            return sorted((i for i in self.items if i.state != "pending"),
                          key=lambda i: i.ended or 0, reverse=True)

    def cancel(self, ids, now: float) -> list[Item]:
        return self._change(set(ids), state="cancelled", ended=now)

    def finish(self, id: int, state: str, now: float) -> Item | None:
        """Marks one as said ("done"), or as too old to say ("missed")."""
        return next(iter(self._change({id}, anyway=True, state=state, ended=now)), None)

    def rang(self, id: int, now: float) -> Item | None:
        """Notes that it was found due, so that a restart doesn't note it a second time."""
        return next(iter(self._change({id}, anyway=True, rung=now)), None)

    def listen(self, tell) -> None:
        self.listeners.append(tell)

    def unlisten(self, tell) -> None:
        if tell in self.listeners:
            self.listeners.remove(tell)


def _stale(item: Item, now: float) -> bool:
    return now - item.due > STALE_AFTER_S[item.kind]


async def _noted(note, item: Item) -> None:
    try:
        await asyncio.wait_for(note(item), NOTE_WAIT_S)
    except Exception:       # someone else's function must not stop the timers, nor stall them
        log.exception("timers: could not note item %s", item.id)


async def _ring(store: Store, item: Item, note, now: float) -> None:
    """What happens once, when an item is found due: it is marked, and noted for a trace. One too
    old to say ends here. One that rang at the time and then waited too long has its trace."""
    first = item.rung is None
    marked = store.finish(item.id, "missed", now) if _stale(item, now) else (
        store.rang(item.id, now) if first else None)
    if marked and first and note:
        await _noted(note, marked)


async def _say(store: Store, announce, item: Item, now) -> bool:
    """Offers one item to announce. False if it has to wait."""
    try:
        if await announce(item) is False:
            return False
        state = "done"
    except Exception:       # as above. It was not said, and the record says so
        log.exception("timers: could not announce item %s", item.id)
        state = "missed"
    store.finish(item.id, state, now())
    return True


async def _fire(store: Store, announce, note, now) -> bool:
    """One pass over what has come due. Returns whether any is still waiting to be said."""
    for item in store.due(now()):       # each gets its trace at once, even when speech must wait
        await _ring(store, item, note, now())
    for found in store.due(now()):
        item = store.live(found.id)     # the one before took a while: this may be cancelled now
        if item is None or item.rung is None:
            continue
        if not await _say(store, announce, item, now):
            return True                 # in order: nothing overtakes one that is waiting
    return False


def _nap(store: Store, now: float, waiting: bool) -> float | None:
    """How long to sleep. None: until the list changes."""
    lefts = [item.due - now for item in store.pending()]
    ahead = [left for left in lefts if left > 0]
    if lefts and lefts[0] <= 0:
        return min([RETRY_S, *ahead[:1]]) if waiting else 0.0
    return min(ahead[0], MAX_NAP_S) if ahead else None


async def _doze(changed: asyncio.Event, seconds: float | None, sleep) -> None:
    """Until the time is up or the list changes, whichever comes first."""
    waits = [asyncio.ensure_future(changed.wait())]
    if seconds is not None:
        waits.append(asyncio.ensure_future(sleep(seconds)))
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


async def run(store: Store, announce, now=epoch, sleep=asyncio.sleep, note=None) -> None:
    """Hands each item to announce when it comes due, soonest first, until cancelled.

    announce(item) is awaited, and is what says it. False means not now, as when the screen is
    locked or Tux is in the middle of a sentence: the item stays pending and is offered again
    every RETRY_S seconds, so a restart meanwhile loses nothing. Anything else means it was said.
    note(item), if given, is awaited once for each item at the moment it is found due, whether or
    not it can be said: it is for a trace, such as a notification. An item too old to be said
    reaches note alone, with its state "missed".
    """
    changed = asyncio.Event()
    loop, home = asyncio.get_running_loop(), threading.get_ident()

    def wake():     # a handler changes the list from its own thread
        if threading.get_ident() == home:
            changed.set()
        else:
            loop.call_soon_threadsafe(changed.set)

    store.listen(wake)
    try:
        while True:
            waiting = await _fire(store, announce, note, now)
            # Cleared here and not before: the pass changes the list itself. Nothing is lost,
            # as the sleep is worked out from the list as it is now.
            changed.clear()
            await _doze(changed, _nap(store, now(), waiting), sleep)
    finally:
        store.unlisten(wake)


def _digits(text: str) -> str:
    """"twenty five minutes" as "25 minutes", "an hour" as "1 hour"."""
    out = []
    for word in text.split():
        value = NUMBERS.get(word)
        tens = out and out[-1].isdigit() and int(out[-1]) in range(20, 100, 10)
        if tens and value in range(1, 10) and word not in ("a", "an"):
            out[-1] = str(int(out[-1]) + value)
        else:
            out.append(word if value is None else str(value))
    return " ".join(out)


def _spelled(text: str) -> int | None:
    """What the calendar's minutes() doesn't know: seconds, days, numbers as words, "an hour and
    a half", "2 minutes 30 seconds", "1h30m"."""
    words = LEAD.sub(" ", text.casefold().replace("-", " ").replace(",", " ")).strip(" .!?")
    words = _digits(" ".join(words.split()).removeprefix("pt"))
    for pattern, becomes in HALVES:
        words = pattern.sub(becomes, words)
    parts = PART.findall(words)
    if not parts or PART.sub("", words).replace("and", "").strip():
        return None
    if len(parts) == 1 and not parts[0][1]:
        parts = [(parts[0][0], "minutes")]      # a bare number is minutes
    if any(unit not in UNIT_S for _, unit in parts):
        return None
    total = sum(float(count) * UNIT_S[unit] for count, unit in parts)
    return round(total) if 1 <= total < math.inf else None


def seconds(value) -> int | None:
    """How long, in seconds: a number of minutes, or words such as "90 seconds", "an hour and a
    half" or "2 minutes 30 seconds". None if it isn't a length of time."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    text = value if isinstance(value, str) else f"{value} minutes"
    if text.lstrip().startswith("-"):   # further down, a hyphen is only "forty-five"
        return None
    # The calendar's reading comes first, so the two skills agree on all that it knows. It
    # counts in whole minutes, so anything with a fraction is worked out here.
    known = None if "." in text else minutes(text)
    return known * 60 if known is not None else _spelled(text)


def _first(args: dict, keys):
    """The first of these arguments that holds anything, or ""."""
    for key in keys:
        value = args.get(key)
        if value is not None and str(value).strip().casefold() not in ABSENT:
            return value
    return ""


def _length(args: dict):
    """How long, as it came: the duration, or a number under a key that names its unit, since
    {"seconds": 90} must not be read as minutes."""
    given = _first(args, LONG_KEYS)
    for unit in () if given != "" else UNIT_KEYS:
        count = _first(args, (unit,))
        if count != "":
            return f"{count} {unit}" if seconds(f"{count} {unit}") is not None else count
    return given


def _how_long(args: dict, given) -> int | None:
    """Seconds from now. "in 20 minutes" in the time or the day field is still how long, but a
    bare number there is an hour or a date."""
    elsewhere = [value for value in (_first(args, TIME_KEYS), _first(args, DAY_KEYS))
                 if re.search(r"[a-z]", str(value).casefold())]
    return next((long for long in map(seconds, [given, *elsewhere]) if long is not None), None)


def _tidy(text) -> str:
    words = MERIDIEM.sub(r"\1m", str(text).casefold().replace(",", " "))
    return _digits(" ".join(LITTLE.sub(" ", words).split()))


def _clock(text: str) -> tuple[time, bool] | None:
    """A clock time, and whether its half of the day is certain: for "5" and "5:30" it is not.
    Only what is shaped like a time reaches the calendar's clock(), which takes more on trust."""
    words = re.sub(r"\bat\b|o'clock|oclock", " ", text)
    for pattern, becomes in SPOKEN:
        words = pattern.sub(becomes, words)
    words = " ".join(words.split())
    if words in WORD_TIMES:
        return clock(words), True
    match = TIME.fullmatch(words)
    at = clock(words) if match else None
    if at is None:
        return None
    vague = not match[2] and 1 <= int(match[1]) <= 12 and not match[1].startswith("0")
    return at, not vague


def _is_day(text: str, today: date) -> bool:
    return not text or period(text, today) is not None


def _is_time(text: str) -> bool:
    return not text or _clock(text) is not None


def _part_of_day(day: str, at: str) -> tuple[str, str, str]:
    """Takes "morning" and the like out of both phrases. "tonight" and "this evening" name the
    day as well."""
    words = f"{day} {at}".split()
    part = next((PART_WORDS[word] for word in words if word in PART_WORDS), "")
    day, at = (" ".join(word for word in text.split() if word not in (*PART_WORDS, "this"))
               for text in (day, at))
    if not day and ("tonight" in words or (part and "this" in words)):
        day = "today"
    return part, day, at


def _apart(words: list[str], today: date) -> tuple[str, str] | None:
    """A day and a time out of one run of words, such as "5pm tomorrow"."""
    for cut in range(1, len(words)):
        ends = " ".join(words[:cut]), " ".join(words[cut:])
        for day, at in (ends, ends[::-1]):
            if _is_day(day, today) and _is_time(at):
                return day, at
    return None


def _said(args: dict, today: date) -> tuple[str, str, str]:
    """The day, the clock time and the part of the day, as phrases. A small model puts "tomorrow
    at 5pm" in either field, or an ISO date and time in one of them."""
    day, at = (_tidy(_first(args, keys)) for keys in (DAY_KEYS, TIME_KEYS))
    for text in (day, at):
        if match := ISO.fullmatch(text):
            return match[1], match[2], ""
    part, day, at = _part_of_day(day, at)
    # "at" is the surest place to cut: in "14 november at 5", the 5 is no year. With no "at"
    # there is one piece, which may be the right words in the wrong field.
    pieces = [piece.strip() for piece in re.split(r"\bat\b", f"{day} at {at}") if piece.strip()]
    tries = [tuple(pieces), tuple(pieces[::-1])] if len(pieces) == 2 else [(day, at), (at, day)]
    for one, other in tries:
        if _is_day(one, today) and _is_time(other):
            return one, other, part
    words = list(dict.fromkeys(" ".join(pieces).split()))     # "tomorrow" in both fields is one
    return (*(_apart(words, today) or (day, at)), part)


def _day(text: str, today: date) -> date:
    days = period(text, today)
    if days is None:
        raise ValueError(f"I didn't understand the day {shown(text, 40)}. Say a day such as "
                         "tomorrow, Friday or 14 November.")
    if days[0] != days[1] and days[0] <= today:     # "this week": which day of it?
        raise ValueError("Say which day, such as tomorrow or Friday.")
    if days[0] < today:
        raise ValueError("That day has already passed.")
    if (days[0] - today).days > MAX_AHEAD_S // 86400:   # and the year 9999 has no local time
        raise ValueError(TOO_FAR)
    return days[0]      # "next week" is its Monday


def _readings(at: time, sure: bool, part: str, waking: bool) -> list[time]:
    """What a clock time can mean, the likeliest first. A bare "7" is two times of day."""
    if sure:
        return [at]
    am, pm = at.replace(hour=at.hour % 12), at.replace(hour=at.hour % 12 + 12)
    if part == "night":
        return [pm if pm.hour >= 12 + NIGHT_UNTIL else am]
    if part or waking:
        return [pm if part in ("afternoon", "evening") else am]
    return sorted((when for when in (am, pm) if when.hour >= NIGHT_UNTIL),
                  key=lambda when: when != at)


def _moment(args: dict, now: datetime, waking: bool) -> datetime:
    """When a reminder is for. With no day it is the next time the clock shows that time."""
    today = now.date()
    day_text, at_text, part = _said(args, today)
    if not (day_text or at_text or part):
        raise ValueError(NO_WHEN)
    day = _day(day_text, today) if day_text else None
    found = _clock(at_text) if at_text else None
    if at_text and found is None:
        raise ValueError(f"I didn't understand the time {shown(at_text, 40)}. Say a time such "
                         "as 5 PM or 17:30.")
    times = _readings(*found, part, waking) if found else [
        time(PART_HOURS.get(part, DEFAULT_HOUR))]
    whens = [datetime.combine(one, at).astimezone()
             for one in ([day] if day else [today, today + timedelta(days=1)]) for at in times]
    ahead = [when for when in (whens if day else sorted(whens)) if when > now]
    if not ahead:
        raise ValueError(PASSED if found else "Say what time, such as 5 PM.")
    return ahead[0]


def _instead(given, now: datetime, waking: bool) -> datetime:
    """A clock time or a day in the duration field is still a time. A bare number never is."""
    try:
        if re.search(r"[a-z:]", str(given).casefold()):
            return _moment({"time": given}, now, waking)
    except ValueError:
        pass
    raise ValueError(f"I didn't understand how long {shown(given, 40)} is. Say it like "
                     "10 minutes or an hour and a half.")


def _due(args: dict, now: datetime, waking: bool) -> tuple[bool, datetime]:
    """Whether it was said as how long, and when it is due."""
    given = _length(args)
    long = _how_long(args, given)
    if long is not None:
        if long > MAX_AHEAD_S:
            raise ValueError(TOO_FAR)
        return True, now + timedelta(seconds=long)
    if any(_first(args, keys) != "" for keys in (DAY_KEYS, TIME_KEYS)):
        when = _moment(args, now, waking)
    elif given == "":
        raise ValueError(NO_WHEN)
    else:
        when = _instead(given, now, waking)
    if when - now > timedelta(seconds=MAX_AHEAD_S):
        raise ValueError(TOO_FAR)
    return False, when


def _label(text) -> str:
    """A label as it is kept: one line, a few words, and "the pasta timer" as "pasta"."""
    whole = " ".join(str(text).split()).strip(TRIM)
    words = EDGES.sub("", whole).strip(TRIM)
    if words.casefold() in NO_LABEL or any(seconds(phrase) for phrase in (whole, words)):
        return ""       # nor is "the 10 minute timer" a name: that is how long it is
    return words if len(words) <= MAX_LABEL_CHARS else (
        words[:MAX_LABEL_CHARS].rsplit(" ", 1)[0])


def _local(stamp: float) -> datetime:
    return datetime.fromtimestamp(stamp).astimezone()


def _when(due: float, now: float) -> str:
    """"today at 5 PM", "tomorrow, Thursday 12 November at 5 PM"."""
    at, today = _local(due), _local(now).date()
    day = "today" if at.date() == today else _day_name(at.date(), today)
    return f"{day} at {_clock_name(at)}"


def _span(long: float, plural: bool = True) -> str:
    """"8 minutes 20 seconds", or "10 minute" to name a timer by. The biggest unit and the one
    below it: nobody needs the seconds of an hour."""
    whole = max(1, round(long))
    top = next(i for i, (_, size) in enumerate(UNITS) if whole >= size)
    grain = UNITS[min(top + 1, len(UNITS) - 1)][1]
    left, parts = round(whole / grain) * grain, []
    for name, size in UNITS:
        count, left = divmod(left, size)
        if count:
            parts.append(f"{count} {name}{'s' if plural and count != 1 else ''}")
    return " ".join(parts)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _called(item: Item) -> str:
    """"pasta timer", "10 minute timer", "call mum reminder", "7 AM reminder"."""
    if item.label:
        return f"{shown(item.label, MAX_LABEL_CHARS)} {item.kind}"
    if item.kind == "timer":
        return f"{_span(item.due - item.made, plural=False)} timer"
    return f"{_clock_name(_local(item.due))} reminder"


def _about(item: Item) -> str:
    return f": {shown(item.label, MAX_LABEL_CHARS)}" if item.label else ""


def _listed(items: list[Item]) -> str:
    names = [f"the {_called(item)}" for item in items[:MAX_SPOKEN]]
    if len(items) > MAX_SPOKEN:
        names.append(f"{len(items) - MAX_SPOKEN} more")
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _status(item: Item, now: float) -> str:
    """One sentence about one item, for timer.list."""
    if item.kind == "reminder":
        return f"A reminder {_when(item.due, now)}{_about(item)}."
    left = item.due - now
    return f"{_span(left)} left on the {_called(item)}." if left >= 1 else (
        f"The {_called(item)} has just finished.")


def _confirmation(item: Item, relative: bool, now: float) -> str:
    if item.kind == "timer":
        return _capital(f"{_called(item) if item.label else 'timer'} set for "
                        f"{_span(item.due - item.made)}.")
    soon = relative and item.due - now < 86400     # further off, the day says more than a count
    when = f"in {_span(item.due - now)}" if soon else _when(item.due, now)
    return f"I'll remind you {when}{_about(item)}."


def words(item: Item, now: float) -> str:
    """What is said aloud for an item that has come due. Past LATE_AFTER_S it says it is late."""
    late = now - item.due
    if item.kind == "timer":
        return f"Your {_called(item)} is done." if late < LATE_AFTER_S else (
            f"Your {_called(item)} finished {_span(max(60, round(late / 60) * 60))} ago.")
    if late >= LATE_AFTER_S:
        return f"You missed a reminder {_when(item.due, now)}{_about(item)}."
    return f"Reminder{_about(item)}." if item.label else f"This is your {_called(item)}."


def notice(item: Item, now: float) -> dict:
    """The desktop notification that goes with it, so that speech nobody heard leaves a trace.

    The label is in the summary alone, which every desktop shows as plain text. The body is
    markup on GNOME, so nothing a person said goes there. One that was missed is low urgency:
    it goes to the list with no banner.
    """
    missed = item.state == "missed"
    if item.kind == "timer":
        summary = f"Missed: {_called(item)}" if missed else f"{_capital(_called(item))} is done"
    else:
        summary = f"{'Missed reminder' if missed else 'Reminder'}{_about(item)}"
    did = "Finished" if item.kind == "timer" else "Due"
    return {"summary": summary, "body": f"{did} {_when(item.due, now)}.", "icon": ICON,
            "urgency": 0 if missed else 1}


async def notify(bus, item: Item, now: float | None = None) -> None:
    """Sends notice() to the desktop, over a session bus connection from dbus_fast. A desktop
    with nothing to show it is not an error.

    Tux makes its own sound, so the desktop is asked for none. Nothing is started to receive
    it: GNOME Shell is already there, and a test's private bus must never launch a real
    notification service.
    """
    from dbus_fast import Message, MessageFlag, Variant     # the daemon's, and only this needs it
    text = notice(item, epoch() if now is None else now)
    hints = {"urgency": Variant("y", text["urgency"]), "suppress-sound": Variant("b", True)}
    await asyncio.wait_for(bus.call(Message(
        destination="org.freedesktop.Notifications", path="/org/freedesktop/Notifications",
        interface="org.freedesktop.Notifications", member="Notify", signature="susssasa{sv}i",
        flags=MessageFlag.NO_AUTOSTART,
        body=["Tuxistant", 0, text["icon"], text["summary"], text["body"], [], hints, -1])),
        NOTIFY_WAIT_S)


@functools.cache
def chime() -> bytes:
    """Two soft notes, as PCM for the Speaker. It is the only sound a timer makes on a locked
    screen, as it has no words for anyone to overhear."""
    samples, long = array.array("h"), int(RATE * 0.25)
    for pitch in (880, 1320):
        for n in range(long):
            swell = min(1.0, n / (RATE * 0.005)) * math.exp(-5 * n / long)   # no click at the start
            samples.append(int(9000 * swell * math.sin(2 * math.pi * pitch * n / RATE)))
    return samples.tobytes()


class Announcer:
    """The scheduler's two callbacks, in terms of four things a daemon can do.

    say(text, lead, heard) starts speaking, after the sound lead, and returns its job to wait
    for, whose result is what went wrong, if anything. It calls heard() once words have gone to
    the speaker. It returns None when it must not speak now: Tux is in a turn, or the screen is
    locked.
    sound(pcm) plays a sound with no words, locked or not. notify(item) leaves a notification.
    locked() says whether the screen is locked, and chimes() whether the chime is wanted.
    """

    def __init__(self, say, sound, notify, locked, chimes=lambda: True, now=epoch):
        self.say, self.sound, self.notify = say, sound, notify
        self.locked, self.chimes, self.now = locked, chimes, now
        self.failed = {}        # item id: (tries that failed, when the next may start)

    async def note(self, item: Item) -> None:
        """At the moment it is due: the trace. On a locked screen the chime too, as the words
        have to wait for the unlock and a kitchen timer nobody hears is no timer."""
        try:
            await self.notify(item)
        except Exception:       # no notification is no reason to skip the chime
            log.exception("timers: could not notify for item %s", item.id)
        if item.state == "pending" and self.locked() and self.chimes():
            await self.sound(chime())

    async def announce(self, item: Item) -> bool:
        """Says it if Tux is free, and reports whether it was. Said late, it needs no chime: the
        person has just come back. One whose words failed before any was heard is not said: it
        waits and is tried again, and raises when it has failed FAILED_TRIES times."""
        tries, after = self.failed.get(item.id, (0, 0))
        if self.now() < after:
            return False
        on_time = self.now() - item.due < LATE_AFTER_S
        heard = []
        job = await self.say(words(item, self.now()),
                             chime() if on_time and self.chimes() else b"",
                             lambda: heard.append(True))
        if job is None:
            return False
        await asyncio.wait({job})   # the next one must not start over this. Not ours to cancel
        problem = None if job.cancelled() else job.result()
        if problem is None or heard:    # some was heard: saying it again would say it twice
            self.failed.pop(item.id, None)
            return True
        return self._failed(item, tries + 1, problem)

    def _failed(self, item: Item, tries: int, problem: str) -> bool:
        if tries >= FAILED_TRIES:
            self.failed.pop(item.id, None)
            raise RuntimeError(problem)
        wait = FAILED_WAIT_S * 2 ** (tries - 1)
        self.failed[item.id] = (tries, self.now() + wait)
        log.warning("timers: could not say item %s, trying again in %d seconds: %s",
                    item.id, wait, problem)
        return False


_attached: Store | None = None


def attach(store: Store | None) -> None:
    """Gives the three actions their store. There is no default, on purpose: a test or an
    evaluation that forgets this gets a sentence back, and not a file under the real home."""
    global _attached
    _attached = store


def _store() -> Store:
    if _attached is None:
        raise ValueError(NOT_HERE)
    return _attached


def _saved(change):
    try:
        return change()
    except OSError as e:
        log.warning("timers: could not save: %s", e)
        raise ValueError(NOT_SAVED) from None


def _stamp(now: datetime | None) -> float:
    return (now or datetime.now().astimezone()).timestamp()


def _twin(store: Store, kind: str, label: str, due: float, now: float) -> Item | None:
    """One just like it, set a moment ago: a small model sometimes makes the same call twice,
    and two timers would both go off."""
    return next((item for item in store.pending()
                 if (item.kind, item.label) == (kind, label) and abs(item.due - due) < TWICE_S
                 and 0 <= now - item.made < TWICE_S), None)


def listing(now: float | None = None) -> list[dict]:
    """For Settings or the panel, should they come to show them: what is pending, soonest
    first, with the label ready to display."""
    now = epoch() if now is None else now
    return [{"id": item.id, "kind": item.kind, "label": shown(item.label, MAX_LABEL_CHARS),
             "due": item.due, "text": _status(item, now)}
            for item in (_attached.pending() if _attached else [])]


def set_timer(args: dict, _config, _session=None, now: datetime | None = None) -> dict:
    """A duration makes a timer, and a day or a time makes a reminder. "Remind me in half an
    hour to stretch" is how long with something to say, so it is a reminder too."""
    now = now or datetime.now().astimezone()
    store = _store()
    name, about = (_label(_first(args, keys)) for keys in (NAME_KEYS, ABOUT_KEYS))
    relative, due = _due(args, now, bool(WAKING.search(f"{name} {about}".casefold())))
    kind, label, stamp = ("timer" if relative and (name or not about) else "reminder",
                          name or about, now.timestamp())
    item = _twin(store, kind, label, due.timestamp(), stamp) or _saved(
        lambda: store.add(kind, label, due.timestamp(), stamp))
    return {"set": _confirmation(item, relative, stamp)}


def _wanted(args: dict) -> tuple[str | None, list[str], bool]:
    """The kind, the words of a name and whether all were meant, from what was said to pick
    them. "My timers" is all of the timers."""
    said = re.findall(r"[^\W_]+", str(_first(args, WHICH_KEYS)).casefold())
    kinds = {KIND_WORDS[word] for word in said if word in KIND_WORDS}
    kind = kinds.pop() if len(kinds) == 1 else None     # "timers and reminders" is both
    name = [word for word in said if word not in NOISE]
    every = args.get("all") is True or str(args.get("all")).casefold() in ("true", "yes", "all")
    return kind, name, every or bool(EVERY & set(said)) or (not name and bool(PLURALS & set(said)))


def _stems(text) -> set[str]:
    found = re.findall(r"[^\W_]+", str(text).casefold())
    return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in found}


def _same_time(item: Item, at: tuple[time, bool] | None) -> bool:
    due = _local(item.due)
    return bool(at) and item.kind == "reminder" and due.minute == at[0].minute and (
        due.hour == at[0].hour if at[1] else due.hour % 12 == at[0].hour % 12)


def _matching(items: list[Item], name: list[str]) -> list[Item]:
    """The items a spoken name means, by the first of these that finds any: the same words, all
    of its words, a timer's length or a reminder's time, a near miss in the spelling, or one
    word in common."""
    said, want = " ".join(name), _stems(" ".join(name))
    long, at = seconds(said), _clock(said)
    tests = (
        lambda i: want == _stems(i.label),
        lambda i: want <= _stems(i.label),
        lambda i: (i.kind == "timer" and long == round(i.due - i.made)) or _same_time(i, at),
        lambda i: difflib.SequenceMatcher(None, said, i.label.casefold()).ratio() >= CLOSE,
        lambda i: want & _stems(i.label),
    )
    return next((found for found in ([i for i in items if test(i)] for test in tests) if found),
                [])


def _seen(session, items: list[Item]) -> None:
    """A stored time or label came from an earlier conversation. Handing it back to the model is reading
    something private, as a calendar entry is, so from here nothing leaves the user's own server
    without a click. See Skills.execute."""
    if session is not None and items:
        session.tainted = True


def _choice(args: dict) -> tuple[str | None, list[str], bool, list[Item]]:
    kind, name, every = _wanted(args)
    return kind, name, every, [i for i in _store().pending() if kind in (None, i.kind)]


def list_timers(args: dict, _config, session=None, now: datetime | None = None) -> dict:
    """One string of sentences: given a list, the model reads it back as a numbered list."""
    kind, name, _every, pending = _choice(args)
    if not pending:
        return {"timers": NOTHING[kind]}
    # Reading harms nothing, so a name that matches nothing gets all of them: the model invents
    # names, and the answer still holds what was asked.
    found = (_matching(pending, name) if name else []) or pending
    _seen(session, found)
    more = len(found) - MAX_SPOKEN
    spoken = " ".join(_status(item, _stamp(now)) for item in found[:MAX_SPOKEN])
    return {"timers": spoken + (f" And {more} more." if more > 0 else "")}


def cancel_timers(args: dict, _config, session=None, now: datetime | None = None) -> dict:
    """Cancels the one that was named, or the only one there is. With several and nothing to
    choose by, it says which there are and cancels nothing, unless all were meant."""
    kind, name, every, pending = _choice(args)
    if not pending:
        raise ValueError(NOTHING[kind])
    found = _matching(pending, name) if name else pending
    _seen(session, found or pending)
    if not found:
        raise ValueError(f"No {kind or 'timer or reminder'} is called "
                         f"{shown(' '.join(name), 40)}. You have {_listed(pending)}.")
    if len(found) > 1 and not every:
        raise ValueError(f"You have {_listed(found)}. Say which one to cancel, or say all of "
                         "them.")
    gone = _saved(lambda: _store().cancel({item.id for item in found}, _stamp(now)))
    return {"cancelled": f"I've cancelled {_listed(gone)}."}


HANDLERS = {"timer.set": set_timer, "timer.list": list_timers, "timer.cancel": cancel_timers}
