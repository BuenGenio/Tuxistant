#!/usr/bin/python3
"""Whole requests against the real model with the real tools: what it calls, what the cards say
and what Tux would say.

    python3 tools/eval_flows.py ["a request" ...]

Every card is declined. That alone would not make this safe, as actions with no card really run:
it once opened the user's CV on screen. So opening files and phone actions are cut off below, and
any new action with no card that touches the desktop or a device must be cut off here too. Code
skills, the lights among them, are offered but never started: each call gets an error back. What
still runs: file search, reading system facts, sandboxed read-only commands, weather, news,
translation on your own server, and web search and research, which reach real search engines and
real pages. Mail search runs too, on a made-up mailbox: the real one is never opened.
Media runs too, on a made-up player and a made-up volume: no real player is asked or told
anything, and the real volume is neither read nor changed. Timers and reminders run too, on a
store in a temporary folder: the real file is never opened, and nothing here runs the scheduler,
so nothing set in an evaluation is ever said or shown. Home Assistant runs too, on a made-up
home: no server is reached and the address and token in Settings are never used.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.mail_index import PROVIDER, SAMPLE, build  # noqa: E402
from tuxistant import (calendars, files, homeassistant, mail, media, phone,  # noqa: E402
                       timers)
from tools.eval_config import evaluation_server
from tuxistant.config import Config  # noqa: E402
from tuxistant.gate import APP_SAYS, Session  # noqa: E402
from tuxistant.skillhost import offline  # noqa: E402


def blocked(*_args, **_kwargs):
    raise RuntimeError("Blocked by eval_flows")


files._launch = lambda path: print(f"   (would open {path.name})") or True
phone._activate = blocked


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


calendars.Eds = blocked
calendars._backend = NoCalendars

# In place of the real mail, which must never end up in a log or on the way to a model from here.
MAILBOX = tempfile.TemporaryDirectory()
build(Path(MAILBOX.name), SAMPLE)
mail.ROOTS = (MAILBOX.name,)
mail.FREEMAIL |= {PROVIDER}    # the made-up mailbox provider its senders use


class MadeUpPlayer:
    """In place of the session bus: one player with a made-up song that does as it is told, so
    an evaluation never pauses the owner's music or reads what they are listening to."""
    status, song = "Playing", 1

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
    level, muted = 0.40, False

    def __call__(self, command, _sink, value=None):
        if command == "set-volume":
            self.level = float(value)
        if command == "set-mute":
            self.muted = value == "1"
        return f"Volume: {self.level:.2f}{' [MUTED]' if self.muted else ''}\n"


PLAYER = MadeUpPlayer()


async def made_up_bus():
    return PLAYER


# Both run with no card, so these two are all that keeps an evaluation off the real players and
# the real volume. media.py has no other way to either, and tests/test_media.py checks that.
media._connect = made_up_bus
media._wpctl = MadeUpVolume()

# Timers have no card either. With no store attached they refuse and write nothing, so this
# line is not the safety: it is what lets them be evaluated, on a file that goes away.
TIMERS = tempfile.TemporaryDirectory(prefix="tuxistant-eval-")
timers.attach(timers.Store(Path(TIMERS.name) / "timers.json"))


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


# A light, the heating and a lock being locked have no card for an evaluation to decline. The
# first line alone keeps every request off the network, whatever else changes: the module has no
# other way out, and tests/test_homeassistant.py checks that all three are still here.
homeassistant._opener.open = blocked
homeassistant._connect = lambda _config: homeassistant._Server(
    "http://eval.invalid", "eval.invalid", "a made-up home", "eval")    # the token stays unread
homeassistant._call = NoHome().call

cfg = Config()
evaluation_server(cfg)
cfg.values["mail_search"] = True    # here only: nothing is saved
# Its tools are offered only when it is set up, and they are to be evaluated whether the owner
# has one or not. Made-up values, so the real address and token are not in what Skills is given.
cfg.values["ha_server"], cfg.values["ha_token"] = "eval.invalid:8123", "eval"
cfg.values["tapo_email"], cfg.values["tapo_password"] = "eval@example.test", "eval"   # the same
KEY = cfg["api_key"]
skills = offline(cfg).current()     # no code skill runs, so no light is reached
ASKS = sys.argv[1:] or [
    "Find my tax return for 2024 and send it to my phone",
    "Find my CV and send it to my phone",
    "Clean up my Downloads folder",
    "Move everything in Downloads older than a year to the trash",
    "Which folder in my home uses the most space?",
    "How many files are in my Documents folder?",
    "What's my IP address?",
    "How long has this computer been on?",
    "Is docker running?",
    "Translate where is the train station into Ukrainian",
    "How do you say thank you very much in Spanish?",
    "Search the web for the latest Ubuntu release",
    "What's the price of bitcoin?",
    "Research electric bikes for me",
    "How many files are in my Documents folder? Then search the web for that number",
    "Delete my old log files",
    "What's on my calendar tomorrow?",
    "Add dentist on Friday at 3pm to my calendar",
    "When did I apply to renew my passport?",
    "What did my landlord say about the rent?",
    "Did the bank email me about my new card?",
    "Was my application to renew my passport rejected?",
    "Check my email for the dentist appointment, then search the web for the dentist's reviews",
    "Install VLC",
    "Pause the music", "What's playing?", "Skip this song", "Turn it up",
    "Set the volume to 40 percent", "Mute",
    "Set a pasta timer for 8 minutes", "Remind me at 5pm to call mum",
    "Remind me tomorrow morning to take the bins out", "How long is left on the pasta?",
    "Cancel my timers",
    "Turn off the kitchen light", "Is the front door locked?", "Set the heating to 21",
    "Unlock the front door", "Open the garage", "Activate movie time",
]

async def turn(text):
    url = "ws://" + cfg["server"] + "/v1/realtime?model=" + cfg["pipeline"]
    cards, calls, started = [], [], time.monotonic()
    async def decline(ask):
        cards.append((ask.level, ask.title, ask.detail[:120].replace("\n", " | ")))
        return False
    session = Session(skills, decline)
    async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + KEY}, max_size=None) as ws:
        while json.loads(await ws.recv())["type"] != "session.created":
            pass
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "realtime", "instructions": cfg["instructions"], "tools": skills.tools()}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
        reply, called, report, rounds = "", False, False, 0
        while True:
            e = json.loads(await asyncio.wait_for(ws.recv(), 120))
            kind = e["type"]
            if kind == "response.function_call_arguments.done":
                _, result = await skills.execute(e["name"], e["arguments"], session)
                calls.append(f'{e["name"]}({e["arguments"][:80]}) -> {json.dumps(result, ensure_ascii=False)[:150]}')
                called = True
                report = report or not result.get(APP_SAYS)     # as Realtime._run_tool has it
                await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": e["call_id"], "output": json.dumps(result)}}))
            elif kind.endswith("transcript.done") or kind == "response.output_text.done":
                reply = e.get("transcript") or e.get("text") or reply
            elif kind == "error":
                return calls, cards, session, "ERROR " + json.dumps(e.get("error"))[:120], time.monotonic() - started
            elif kind == "response.done":
                if called and report and rounds < 5:
                    called, report, rounds = False, False, rounds + 1
                    await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["text"]}}))
                else:
                    return calls, cards, session, reply, time.monotonic() - started

async def main():
    for text in ASKS:
        calls, cards, session, reply, took = await turn(text)
        print(f"\n>>> {text}   [{took:.1f} s]")
        for c in calls: print("   call:", c)
        for c in cards: print("   CARD:", c)
        for s in session.take_after(): print("   app says:", s.text, "| voice:", s.model or s.voice or "(shown only)")
        print("   Tux:", reply[:260])

asyncio.run(main())
