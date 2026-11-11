"""Your calendars, through Evolution Data Server, the service GNOME's own Calendar uses.

Accounts are added in GNOME Settings under Online Accounts: Google, Microsoft 365 and CalDAV all
arrive here as calendars. So Tuxistant holds no password or token for any of them, and has one
way of reading and writing instead of three.

Reading needs no card, but what it returns is private, so the conversation then counts as having
read something private. Adding always shows a card with the title, the time and the calendar,
written here from what will really be saved.

The model is bad at dates, so it passes the day and time as the user said them and they are
worked out here. An event's title is other people's text, as anyone can send an invitation: it is
cut short and its control characters shown as escapes before the model sees it.
"""
import re
import threading
from datetime import date, datetime, time, timedelta

from .gate import Ask, shown

MAX_TITLE_CHARS = 80
MAX_SPOKEN = 12             # a small model reads out all it is given
NEXT_COUNT = 3
NEXT_DAYS = 60              # how far ahead "what's next" looks
MAX_RANGE_DAYS = 31
MAX_AHEAD_DAYS = 366 * 5    # a date further off than this is the model inventing one
CONNECT_S = 1               # an offline account takes all of it, then answers from its cache
DEFAULT_MINUTES = 60
AFTERNOON_UNTIL = 6         # "at 3" with no am or pm means the afternoon, up to this hour

NO_EDS = ("The calendar needs Evolution Data Server and its Python bindings, the packages "
          "evolution-data-server and gir1.2-ecal-2.0, and they aren't installed.")
NO_CALENDARS = "No calendars are set up. Add an account in GNOME Settings, under Online Accounts."
NO_WRITABLE = "None of your calendars can be changed from here."

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
          "october", "november", "december"]
FILLER = re.compile(r"\b(?:the|on|of|this|coming|at|for|morning|afternoon|evening|night)\b")
ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b")
CALENDAR_NOISE = {"my", "our", "a", "one", "calendar", "calendars"}
NEXT = {"next", "upcoming", "soon", "next event", "next meeting", "next appointment"}
CLOCK = re.compile(r"(\d{1,2})(?:[:.h](\d{2}))?(?::\d{2})?\s*(am|pm)?")
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?")

_lock = threading.Lock()    # one request at a time: the bindings are not made for more


def _words(text) -> str:
    text = ORDINAL.sub("", str(text or "").casefold().replace(",", " ").replace(".", ""))
    return " ".join(FILLER.sub(" ", text).split())


def _weekday(name: str, today: date, next_week: bool) -> date:
    ahead = (WEEKDAYS.index(name) - today.weekday()) % 7
    if next_week:       # the one in next week, not merely the next one
        ahead = 7 - today.weekday() + WEEKDAYS.index(name)
    return today + timedelta(days=ahead)


def _month(word: str) -> int | None:
    matches = [i for i, month in enumerate(MONTHS, 1) if len(word) >= 3 and month.startswith(word)]
    return matches[0] if len(matches) == 1 else None


def _dated(words: str, today: date) -> date | None:
    """"2026-11-14", "14 november", "november 14 2026" or a bare "14"."""
    try:
        return date.fromisoformat(words[:10])
    except ValueError:
        pass
    parts = words.split()
    numbers = [int(p) for p in parts if p.isdigit()]
    months = [m for m in map(_month, parts) if m]
    try:
        if len(months) == 1 and numbers and len(parts) <= 3:
            year = numbers[1] if len(numbers) > 1 else today.year
            found = date(year, months[0], numbers[0])
            # With no year it is the next one, unless it has only just gone by.
            if len(numbers) == 1 and found < today - timedelta(days=30):
                found = found.replace(year=year + 1)
            return found
        if len(parts) == 1 and len(numbers) == 1:
            found = today.replace(day=numbers[0])
            return found if found >= today else (found.replace(day=1) + timedelta(days=32)).replace(
                day=numbers[0])
    except ValueError:
        return None
    return None


def period(text, today: date) -> tuple[date, date] | None:
    """The first and last day a phrase means, or None if it isn't one this knows."""
    words = _words(text)
    if words in ("", "today", "tonight", "now"):
        return today, today
    if words == "tomorrow":
        return (today + timedelta(days=1),) * 2
    if words == "day after tomorrow":
        return (today + timedelta(days=2),) * 2
    if words == "yesterday":
        return (today - timedelta(days=1),) * 2
    sunday = today + timedelta(days=6 - today.weekday())
    if words in ("week", "rest week"):
        return today, sunday
    if words == "next week":
        return sunday + timedelta(days=1), sunday + timedelta(days=7)
    if words == "weekend":
        return max(today, sunday - timedelta(days=1)), sunday
    if words == "next weekend":
        return sunday + timedelta(days=6), sunday + timedelta(days=7)
    if words == "month":
        return today, min((today.replace(day=1) + timedelta(days=32)).replace(day=1)
                          - timedelta(days=1), today + timedelta(days=MAX_RANGE_DAYS - 1))
    name = words.removeprefix("next ")
    if name in WEEKDAYS:
        return (_weekday(name, today, words.startswith("next ")),) * 2
    found = _dated(words, today)
    return (found, found) if found else None


def clock(text) -> time | None:
    """"3pm", "3:30 pm", "15:30", "15.30", "noon". None if it isn't a time."""
    words = str(text or "").casefold().replace(".m.", "m").replace(" ", "").replace("o'clock", "")
    if words in ("noon", "midday", "12noon"):
        return time(12)
    if words == "midnight":
        return time(0)
    match = CLOCK.fullmatch(words.rpartition("t")[2] if "t" in words[:-1] else words)
    if not match:
        return None
    hour, minute, half = int(match[1]), int(match[2] or 0), match[3]
    if half and not 1 <= hour <= 12:
        return None
    if half:
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif 1 <= hour <= AFTERNOON_UNTIL and not match[1].startswith("0"):
        hour += 12      # "at 3" is rarely the small hours; "03:00" is, and the card shows it
    return time(hour, minute) if hour < 24 and minute < 60 else None


def minutes(value) -> int | None:
    """How long, from a number of minutes or "90 minutes", "2 hours", "half an hour"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value) if 1 <= value <= 24 * 60 else None
    words = str(value or "").casefold().strip()
    for said, digits in (("half an hour", "30 minutes"), ("an hour", "1 hour"), ("one ", "1 "),
                         ("two ", "2 "), ("three ", "3 ")):
        words = words.replace(said, digits)
    match = DURATION.fullmatch(words)
    if not match:
        return None
    long = float(match[1]) * (60 if (match[2] or "m").startswith("h") else 1)
    return round(long) if 1 <= long <= 24 * 60 else None


def _day_name(day: date, today: date) -> str:
    named = {0: "today", 1: "tomorrow", -1: "yesterday"}.get((day - today).days)
    full = f"{day:%A} {day.day} {day:%B}" + ("" if day.year == today.year else f" {day.year}")
    return f"{named}, {full}" if named else full


def _clock_name(at) -> str:
    hour = at.hour % 12 or 12
    return f"{hour}{'' if not at.minute else f':{at.minute:02}'} {'AM' if at.hour < 12 else 'PM'}"


def _title(text) -> str:
    return shown(" ".join(str(text or "").split()), MAX_TITLE_CHARS) or "An event with no title"


def _spoken(event: dict) -> str:
    title = _title(event["title"])
    if event["all_day"]:
        return f"{title}, all day"
    until = "" if event["end"] <= event["start"] else f" to {_clock_name(event['end'])}"
    return f"{title} from {_clock_name(event['start'])}{until}" if until else (
        f"{title} at {_clock_name(event['start'])}")


def _first_day(event: dict) -> date:
    return event["start"] if event["all_day"] else event["start"].date()


def _sort_key(event: dict):
    start = event["start"]
    return _first_day(event), not event["all_day"], time(0) if event["all_day"] else start.time()


class Eds:
    """The only code that talks to Evolution Data Server. Made fresh for each request, so an
    account added a minute ago is there, and nothing stale is kept."""

    def __init__(self):
        try:
            import gi
            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            gi.require_version("ICalGLib", "3.0")
            from gi.repository import ECal, EDataServer, GLib, ICalGLib
        except (ImportError, ValueError) as e:
            raise ValueError(NO_EDS) from e
        self.ecal, self.eds, self.ical, self.glib = ECal, EDataServer, ICalGLib, GLib
        try:
            self.registry = EDataServer.SourceRegistry.new_sync(None)
        except GLib.Error as e:
            raise ValueError(NO_EDS) from e
        self.extension = EDataServer.SOURCE_EXTENSION_CALENDAR
        self.clients = {}

    def _account(self, source) -> str:
        parent = self.registry.ref_source(source.get_parent()) if source.get_parent() else None
        return parent.get_display_name() if parent else ""

    def _client(self, source):
        uid = source.get_uid()
        if uid not in self.clients:
            self.clients[uid] = self.ecal.Client.connect_sync(
                source, self.ecal.ClientSourceType.EVENTS, CONNECT_S, None)
        return self.clients[uid]

    def calendars(self) -> list[dict]:
        """Every calendar that is switched on. "shown" is the tick in GNOME Calendar's list."""
        default = self.registry.ref_default_calendar()
        return [{"id": source.get_uid(), "name": source.get_display_name(),
                 "account": self._account(source),
                 "shown": source.get_extension(self.extension).get_selected(),
                 "default": bool(default) and default.get_uid() == source.get_uid()}
                for source in self.registry.list_enabled(self.extension)]

    def writable(self, uid: str) -> bool:
        source = self.registry.ref_source(uid)
        try:
            return bool(source) and not self._client(source).is_readonly()
        except self.glib.Error:
            return False

    def _local(self, moment):
        zone = moment.get_timezone() or self.ecal.util_get_system_timezone()
        zone = zone or self.ical.Timezone.get_utc_timezone()
        return datetime.fromtimestamp(moment.as_timet_with_zone(zone)).astimezone()

    def _event(self, component, start, end) -> dict | None:
        if component.get_status() == self.ical.PropertyStatus.CANCELLED:
            return None
        if start.is_date():     # all day: the last day is the one before DTEND
            first = date(start.get_year(), start.get_month(), start.get_day())
            last = date(end.get_year(), end.get_month(), end.get_day()) - timedelta(days=1)
            return {"title": component.get_summary(), "all_day": True, "start": first,
                    "end": max(first, last)}
        return {"title": component.get_summary(), "all_day": False, "start": self._local(start),
                "end": self._local(end)}

    def events(self, uid: str, first: datetime, last: datetime) -> list[dict]:
        """What one calendar holds between two moments, with repeating events spelled out."""
        found = []

        def seen(component, start, end, *_rest):
            event = self._event(component, start, end)
            if event:
                found.append(event)
            return True

        try:
            client = self._client(self.registry.ref_source(uid))
            client.generate_instances_sync(
                int(first.timestamp()), int(last.timestamp()), None, seen)
        except self.glib.Error as e:
            raise ConnectionError(str(e.message)) from e
        return found

    def _moment(self, value, all_day: bool):
        if all_day:
            moment = self.ical.Time.new_null_time()
            moment.set_date(value.year, value.month, value.day)
            moment.set_is_date(True)
            return moment
        # UTC is the one zone every calendar server agrees on.
        return self.ical.Time.new_from_timet_with_zone(
            int(value.timestamp()), 0, self.ical.Timezone.get_utc_timezone())

    def add(self, uid: str, title: str, start, end, all_day: bool) -> None:
        component = self.ical.Component.new(self.ical.ComponentKind.VEVENT_COMPONENT)
        component.set_uid((getattr(self.ecal, "util_generate_uid", None)
                           or self.eds.util_generate_uid)())
        component.set_summary(title)
        component.set_dtstart(self._moment(start, all_day))
        component.set_dtend(self._moment(end + timedelta(days=1) if all_day else end, all_day))
        try:
            client = self._client(self.registry.ref_source(uid))
            client.create_object_sync(component, self.ecal.OperationFlags.NONE, None)
        except self.glib.Error as e:
            raise ConnectionError(f"The calendar didn't take it: {e.message}") from e


def _named(calendar: dict) -> str:
    name, account = shown(calendar["name"], 60), shown(calendar["account"], 60)
    return f"{name} ({account})" if account else name


def _gather(backend, calendars: list[dict], first: date,
            last: date) -> tuple[list[dict], list[str]]:
    start = datetime.combine(first, time(0)).astimezone()
    end = datetime.combine(last + timedelta(days=1), time(0)).astimezone()
    events, problems = [], []
    for calendar in calendars:
        try:
            for event in backend.events(calendar["id"], start, end):
                events.append({**event, "calendar": calendar["name"]})
        except ConnectionError:
            problems.append(f"I couldn't read the calendar {_named(calendar)}.")
    # What comes back overlaps the window as the server counts it. An all-day event has no zone,
    # so yesterday's can spill in, and a meeting that ended at midnight is not today's.
    events = [e for e in events if ((e["start"] <= last and e["end"] >= first) if e["all_day"]
                                    else (e["end"] > start or e["start"] >= start))]
    return sorted(events, key=_sort_key), problems


def _by_day(events: list[dict], first: date, last: date, today: date,
            always_name_day: bool = False) -> str:
    """One string of sentences: given a list, the model reads it back as a numbered list."""
    spoken, more = events[:MAX_SPOKEN], len(events) - MAX_SPOKEN
    days = {}
    for event in spoken:
        days.setdefault(max(_first_day(event), first), []).append(_spoken(event))
    if first == last and not always_name_day:
        text = ". ".join(days.get(first, [])) + "."
    else:
        text = " ".join(f"{_day_name(day, today)}: {'. '.join(said)}."
                        for day, said in days.items())
    return text + (f" And {more} more." if more > 0 else "")


def _next(backend, calendars: list[dict], now: datetime) -> dict:
    today = now.date()
    events, problems = _gather(backend, calendars, today, today + timedelta(days=NEXT_DAYS))
    # "What's next" means something with a time. A birthday or a holiday only if nothing else is.
    ahead = ([e for e in events if not e["all_day"] and e["start"] >= now]
             or [e for e in events if e["all_day"] and e["start"] >= today])[:NEXT_COUNT]
    if not ahead:
        nothing = f"Nothing is on your calendar in the next {NEXT_DAYS} days."
        return _with({"next": nothing}, problems)
    last = _first_day(ahead[-1])
    return _with({"next": _by_day(ahead, today, last, today, always_name_day=True)}, problems)


def _with(result: dict, problems: list[str]) -> dict:
    return {**result, "problems": problems} if problems else result


def _backend():
    return Eds()


def events(args: dict, _config, _session=None, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    asked = args.get("day", args.get("when", ""))
    with _lock:
        backend = _backend()
        calendars = [c for c in backend.calendars() if c["shown"]]
        if not calendars:
            raise ValueError(NO_CALENDARS)
        if _words(asked) in NEXT:
            return _next(backend, calendars, now)
        days = period(asked, now.date())
        if days is None:
            raise ValueError(f"I didn't understand the day {shown(asked, 40)}. Say a day such "
                             "as tomorrow, Friday or 14 November.")
        first, last = days
        found, problems = _gather(backend, calendars, first, last)
    when = _day_name(first, now.date()) if first == last else (
        f"{_day_name(first, now.date())} to {_day_name(last, now.date())}")
    if not found:
        return _with({"when": when, "events": "Nothing is on your calendar."}, problems)
    return _with({"when": when, "events": _by_day(found, first, last, now.date())}, problems)


def _target(backend, wanted, config) -> dict:
    """The calendar to add to: one the user named, then the one chosen in Settings, then the
    system's own default, then any that can be changed."""
    calendars = backend.calendars()
    if not calendars:
        raise ValueError(NO_CALENDARS)
    words = set(_words(wanted).split()) - CALENDAR_NOISE
    if words:
        named = [c for c in calendars if words <= set(_words(c["name"]).split())]
        if len(named) != 1:
            names = ", ".join(shown(c["name"], 40) for c in calendars)
            raise ValueError(f"I don't know which calendar {shown(wanted, 40)} is. "
                             f"Your calendars are {names}.")
        if not backend.writable(named[0]["id"]):
            raise ValueError(f"The calendar {shown(named[0]['name'], 40)} can't be changed.")
        return named[0]
    chosen = [c for c in calendars if c["id"] == config["calendar_add_to"]]
    for calendar in chosen + [c for c in calendars if c["default"]] + calendars:
        if backend.writable(calendar["id"]):
            return calendar
    raise ValueError(NO_WRITABLE)


def _start(args: dict, now: datetime) -> tuple[date, time | None]:
    day_text, time_text = args.get("day", args.get("date", "")), args.get("time", "")
    if not time_text and re.search(r"\dt\d", str(day_text).casefold()):     # 2026-11-12T15:00
        day_text, _, time_text = str(day_text).casefold().partition("t")
    days = period(day_text, now.date())
    if days is None or days[0] != days[1]:
        raise ValueError(f"I didn't understand the day {shown(day_text, 40)}. Say one day, such "
                         "as tomorrow, Friday or 14 November.")
    at = clock(time_text) if str(time_text or "").strip() else None
    if at is None and str(time_text or "").strip():
        raise ValueError(f"I didn't understand the time {shown(time_text, 40)}. Say a time such "
                         "as 3 PM or 15:30.")
    return days[0], at


def add(args: dict, config, _session=None, now: datetime | None = None) -> Ask:
    now = now or datetime.now().astimezone()
    title = " ".join(str(args.get("title") or "").split())
    if not title:
        raise ValueError("Say what to call the event.")
    day, at = _start(args, now)
    raw = args.get("duration", args.get("minutes"))
    long = DEFAULT_MINUTES if raw in (None, "") else minutes(raw)
    if long is None:
        raise ValueError(f"I didn't understand how long {shown(raw, 40)} is. Say it in minutes.")
    if day > now.date() + timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError("That date is too far ahead.")
    all_day = at is None
    start = day if all_day else datetime.combine(day, at).astimezone()
    end = day if all_day else start + timedelta(minutes=long)
    if (day < now.date()) if all_day else (start < now - timedelta(minutes=1)):
        raise ValueError("That time has already passed.")
    with _lock:
        backend = _backend()
        calendar = _target(backend, args.get("calendar"), config)
    full = f"{day:%A} {day.day} {day:%B} {day.year}"
    when = f"{full}, all day" if all_day else (
        f"{full}, {_clock_name(start)} to {_clock_name(end)}"
        + ("" if end.date() == day else " the next day"))
    said = _day_name(day, now.date()) + (", all day" if all_day else f" at {_clock_name(start)}")

    def run() -> dict:
        with _lock:
            _backend().add(calendar["id"], title, start, end, all_day)
        return {"added": _title(title), "when": said, "calendar": shown(calendar["name"], 60)}

    return Ask(f'Add "{_title(title)}" to your calendar?',
               f"{shown(title, 300)}\n{when}\nCalendar: {_named(calendar)}", run)


def writable(_config=None) -> list[dict]:
    """For Settings: the calendars an event can be added to."""
    try:
        with _lock:
            backend = _backend()
            return [{"id": c["id"], "name": c["name"], "account": c["account"]}
                    for c in backend.calendars() if backend.writable(c["id"])]
    except ValueError:
        return []


def available() -> bool:
    """For doctor: whether the bindings are here at all."""
    try:
        import gi
        gi.require_version("ECal", "2.0")
        gi.require_version("EDataServer", "1.2")
        return True
    except (ImportError, ValueError):
        return False


HANDLERS = {"calendar.events": events, "calendar.add": add}
