"""What stands in for the real desktop, phone, home and mail while the real handlers run:
tools/eval_flows.py and tests/test_realtime_live.py both use install(), so the two can't drift.

Every card is declined in an evaluation. That alone would not make it safe, as actions with no
card really run: one once opened the owner's CV on screen. So every way out that an action with
no card has is replaced below, and ACTIONS names, for every action there is, what it meets here.
tests/test_eval_config.py fails when an action is added and not named in ACTIONS.
"""
import contextlib
import importlib
import tempfile
from pathlib import Path
from unittest import mock

from tests.mail_index import PROVIDER, SAMPLE, build
from tuxistant import homeassistant, mail, media, timers
from tuxistant.skillhost import Offline

# Every action, and the stand-ins its real handler meets here. An empty entry is let run, and
# the comment says why that is safe.
ACTIONS = {
    # Read only, on this computer: the clock, disk and folder sizes, system facts, file search.
    "time.now": (), "disk.usage": (), "disk.folders": (), "system.info": (),
    "system.running": (), "file.find": (),
    # The web and your own servers, by design: nothing private goes, and nothing at home moves.
    "weather.forecast": (), "news.headlines": (), "web.search": (), "research.run": (),
    "knowledge.answer": (), "translate.text": (),
    # Selected text comes from the session, which has none here.
    "translate.selection": (),
    # Each has a card for what it changes, and every card is declined. The shell is sandboxed.
    "file.trash": (), "shell.run": (), "phone.send": ("phone._activate",),
    # Runs others through Skills.execute, so each of them meets its own stand-ins.
    "task.run": (),
    # Reads which phones KDE Connect knows and their battery. Nothing is sent to one.
    "phone.battery": (), "phone.ring": ("phone._activate",),
    "file.open": ("files._launch",),
    "calendar.events": ("calendars.Eds", "calendars._backend"),
    "calendar.add": ("calendars.Eds", "calendars._backend"),
    "mail.search": ("mail.ROOTS",),
    "media.control": ("media._connect", "media._wpctl"),
    "media.playing": ("media._connect", "media._wpctl"),
    "timer.set": ("timers._attached", "timers.notify"),
    "timer.list": ("timers._attached", "timers.notify"),
    "timer.cancel": ("timers._attached", "timers.notify"),
    "home.set": ("homeassistant._opener.open", "homeassistant._connect", "homeassistant._call"),
    "home.status": ("homeassistant._opener.open", "homeassistant._connect",
                    "homeassistant._call"),
    # Code skills: offered, but no process is ever started, so no bulb is reached.
    "lights.set": ("mcp.SkillProcess",), "lights.status": ("mcp.SkillProcess",),
}


def blocked(*_args, **_kwargs):
    raise RuntimeError("That is blocked in an evaluation.")


def _open_file(path):
    print(f"   (would open {path.name})")
    return True


def _no_process(name, *_args, **_kwargs):
    return Offline({"name": name})


class NoCalendars:
    """In place of the real calendars: reading them would put the owner's week into a log, and
    adding has a card, which is declined here, but must not be able to write even so."""

    def calendars(self):
        return [{"id": "eval", "name": "Eval", "account": "", "shown": True, "default": True}]

    def writable(self, _uid):
        return True

    def events(self, _uid, first, _last):
        noon = first.replace(hour=12, minute=0)
        return [{"title": "Lunch with Sam", "all_day": False, "start": noon,
                 "end": noon.replace(hour=13)}]

    add = blocked


class MadeUpPlayer:
    """In place of the session bus: one player with a made-up song that does as it is told, so
    an evaluation never pauses the owner's music or reads what they are listening to."""

    def __init__(self):
        self.status, self.song = "Playing", 1

    async def call(self, _name, _path, _interface, member, _signature="", body=()):
        if member == "ListNames":
            return [[media.PREFIX + "eval"]]
        if member == "NameHasOwner":
            return [True]
        if member == "GetAll" and body[0] == media.ROOT:
            return [{"Identity": "Eval Player"}]
        if member == "GetAll":
            return [{"PlaybackStatus": self.status, "Metadata": {
                "xesam:title": f"Song {self.song}", "xesam:artist": ["The Evals"]}}]
        print(f"   (would send {member} to the player)")
        if member == "PlayPause":
            member = "Pause" if self.status == "Playing" else "Play"
        self.song += (member == "Next") - (member == "Previous")
        self.status = {"Play": "Playing", "Pause": "Paused", "Stop": "Stopped"}.get(
            member, self.status)
        return []

    def close(self):
        pass


class MadeUpVolume:
    """In place of wpctl: a volume that is only a number here."""

    def __init__(self):
        self.level, self.muted = 0.40, False

    def __call__(self, command, _sink, value=None):
        if command == "set-volume":
            self.level = float(value)
        if command == "set-mute":
            self.muted = value == "1"
        return f"Volume: {self.level:.2f}{' [MUTED]' if self.muted else ''}\n"


class NoHome:
    """In place of the real Home Assistant: an evaluation must never switch a real light or say
    whether the owner's door is locked. Service calls change this made-up house and nothing else."""
    MOVES = {"turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed",
             "lock": "locked", "unlock": "unlocked"}

    def __init__(self):
        self.states = {
            "light.kitchen": ("on", {"friendly_name": "Kitchen light", "brightness": 153}),
            "light.bedroom": ("off", {"friendly_name": "Bedroom lamp"}),
            "lock.front_door": ("locked", {"friendly_name": "Front door"}),
            "cover.garage_door": ("closed", {"friendly_name": "Garage door",
                                             "device_class": "garage"}),
            "scene.movie_time": ("unknown", {"friendly_name": "Movie time"}),
            "climate.heating": ("heat", {"friendly_name": "Heating", "temperature": 20,
                                         "current_temperature": 19, "min_temp": 7, "max_temp": 30}),
            "sensor.bedroom_temperature": ("19.5", {"friendly_name": "Bedroom temperature",
                                                    "device_class": "temperature",
                                                    "unit_of_measurement": "°C"}),
        }

    def one(self, entity_id):
        state, attributes = self.states[entity_id]
        return {"entity_id": entity_id, "state": state, "attributes": attributes}

    def call(self, _server, path, body=None, **_limits):
        if path == "/api/states":
            return [self.one(entity_id) for entity_id in self.states]
        if path.startswith("/api/states/"):
            return self.one(path.removeprefix("/api/states/"))
        if path == "/api/config":
            return {"unit_system": {"temperature": "°C"}}
        if path == "/api/template":
            return []
        service = path.rpartition("/")[2]
        print(f"   (would call {path.removeprefix('/api/services/')} in the home)")
        for entity_id in body["entity_id"]:
            state, attributes = self.states[entity_id]
            if service == "set_temperature":
                attributes["temperature"] = body["temperature"]
            does = ("turn_off" if state == "on" else "turn_on") if service == "toggle" else service
            self.states[entity_id] = (self.MOVES.get(does, state), attributes)
        return []


def _made_up_server(_config):
    """The address and token in Settings are never read."""
    return homeassistant._Server("http://eval.invalid", "eval.invalid", "a made-up home", "eval")


def stand_ins(folder: Path) -> dict:
    """Each way out, by the name ACTIONS uses, and what replaces it. New ones each time, so two
    runs never share a player, a home or a timer."""
    build(folder / "mail", SAMPLE)
    player = MadeUpPlayer()

    async def made_up_bus():
        return player

    return {
        "files._launch": _open_file,
        "phone._activate": blocked,
        "calendars.Eds": blocked,
        "calendars._backend": NoCalendars,
        "mail.ROOTS": (str(folder / "mail"),),
        "mail.FREEMAIL": mail.FREEMAIL | {PROVIDER},    # the made-up mailbox's senders use it
        "media._connect": made_up_bus,
        "media._wpctl": MadeUpVolume(),
        # With no store the timer actions refuse and write nothing, so this is not the safety:
        # it is what lets them be evaluated, on a file that goes away. Nothing here runs the
        # scheduler, and notify is blocked, so nothing set is ever said or shown.
        "timers._attached": timers.Store(folder / "timers.json"),
        "timers.notify": blocked,
        # This one alone keeps every request off the network: the module has no other way out.
        "homeassistant._opener.open": blocked,
        "homeassistant._connect": _made_up_server,
        "homeassistant._call": NoHome().call,
        "mcp.SkillProcess": _no_process,
    }


def owner(name: str):
    """The object and attribute a stand-in's name points at, as in "homeassistant._opener.open"."""
    first, *middle, attribute = name.split(".")
    found = importlib.import_module(f"tuxistant.{first}")
    for part in middle:
        found = getattr(found, part)
    return found, attribute


def install() -> contextlib.ExitStack:
    """Puts every stand-in in place until the stack it returns is closed."""
    with contextlib.ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="tuxistant-eval-")))
        for name, stand_in in stand_ins(folder).items():
            stack.enter_context(mock.patch.object(*owner(name), stand_in))
        return stack.pop_all()
