#!/usr/bin/python3
"""A stand-in MPRIS player for tests/test_media.py: real D-Bus, made-up music.

    python3 tests/media_fake_player.py '{"name": "spotify", "log": "/tmp/x/spotify.log"}'

It refuses to start anywhere but the private bus the test made. Every method call that reaches
it goes into its log before anything else happens, wanted or not, so a test can say what a
player was really sent. It prints "ready" once it owns its name.
"""
import asyncio
import json
import os
import sys

from dbus_fast import DBusError, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

PRIVATE, REAL_BUS = "TUXISTANT_TEST_PRIVATE_BUS", "TUXISTANT_TEST_REAL_BUS"
ROOT, OBJECT = "org.mpris.MediaPlayer2", "/org/mpris/MediaPlayer2"
LIFETIME_S = 120    # a test that forgot one doesn't leave it behind
HEROES = [["mpris:trackid", "o", "/track/1"], ["xesam:title", "s", "Heroes"],
          ["xesam:artist", "as", ["David Bowie"]], ["xesam:album", "s", "Heroes"]]
DEFAULTS = {"identity": "Fake Player", "status": "Playing", "tracks": [HEROES], "can": {},
            "lag": 0, "deaf": False, "frozen": False, "refuses": False, "leaves": False,
            "playerctld": None}


class State:
    def __init__(self, spec: dict):
        self.spec = spec
        self.status = spec["status"]
        self.at = 0
        self.bus = None

    def metadata(self) -> dict:
        return {key: Variant(signature, value)
                for key, signature, value in self.spec["tracks"][self.at]}

    def told(self, member: str) -> None:
        if self.spec["refuses"]:
            raise DBusError("org.freedesktop.DBus.Error.NotSupported", "Not today")
        if not self.spec["deaf"]:
            asyncio.get_running_loop().call_later(self.spec["lag"], self.act, member)

    def act(self, member: str) -> None:
        if member == "PlayPause":
            self.status = "Paused" if self.status == "Playing" else "Playing"
        elif member in ("Next", "Previous"):
            step = 1 if member == "Next" else -1
            self.at = max(0, min(len(self.spec["tracks"]) - 1, self.at + step))
        else:
            self.status = {"Play": "Playing", "Pause": "Paused", "Stop": "Stopped"}[member]
        if member == "Stop" and self.spec["leaves"]:    # how a browser stops
            self.bus.disconnect()


class Root(ServiceInterface):
    def __init__(self, state: State):
        super().__init__(ROOT)
        self.state = state

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return self.state.spec["identity"]

    @method()
    def Quit(self):
        pass

    @method()
    def Raise(self):
        pass


class Nameless(ServiceInterface):
    """A player that never says what it is called."""

    def __init__(self):
        super().__init__(ROOT)

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False


class Player(ServiceInterface):
    def __init__(self, state: State):
        super().__init__(ROOT + ".Player")
        self.state = state

    def can(self, flag: str) -> bool:
        return self.state.spec["can"].get(flag, True)

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self.state.status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self.state.metadata()

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return 1_000_000

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return self.can("CanControl")

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return self.can("CanPlay")

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return self.can("CanPause")

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return self.can("CanGoNext")

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return self.can("CanGoPrevious")

    @method()
    def Play(self):
        self.state.told("Play")

    @method()
    def Pause(self):
        self.state.told("Pause")

    @method()
    def PlayPause(self):
        self.state.told("PlayPause")

    @method()
    def Next(self):
        self.state.told("Next")

    @method()
    def Previous(self):
        self.state.told("Previous")

    @method()
    def Stop(self):
        self.state.told("Stop")

    # Never to be called. They are here so that a call would arrive, and be seen in the log.
    @method()
    def OpenUri(self, uri: "s"):
        pass

    @method()
    def Seek(self, offset: "x"):
        pass

    @method()
    def SetPosition(self, track: "o", position: "x"):
        pass


class Playerctld(ServiceInterface):
    def __init__(self, names: list):
        super().__init__("com.github.altdesktop.playerctld")
        self.names = names

    @dbus_property(access=PropertyAccess.READ)
    def PlayerNames(self) -> "as":
        return self.names


def recorder(spec: dict):
    def record(message):
        if message.message_type != MessageType.METHOD_CALL:
            return None
        with open(spec["log"], "a") as log:
            log.write(json.dumps([f"{message.interface}.{message.member}", message.body]) + "\n")
        return True if spec["frozen"] else None     # True: handled, and no reply ever goes out

    return record


async def main(spec: dict) -> None:
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not (os.environ.get(PRIVATE) and address and REAL_BUS in os.environ
            and address != os.environ[REAL_BUS]):
        sys.exit("refusing to run outside the test's private bus")
    state = State(spec)
    bus = state.bus = await MessageBus().connect()
    bus.add_message_handler(recorder(spec))
    bus.export(OBJECT, Root(state) if spec["identity"] is not None else Nameless())
    bus.export(OBJECT, Player(state))
    if spec["playerctld"] is not None:
        bus.export(OBJECT, Playerctld(spec["playerctld"]))
    await bus.request_name(f"{ROOT}.{spec['name']}")
    print("ready", flush=True)
    try:
        await asyncio.wait_for(bus.wait_for_disconnect(), LIFETIME_S)
    except (asyncio.TimeoutError, EOFError, OSError):
        pass


if __name__ == "__main__":
    asyncio.run(main({**DEFAULTS, **json.loads(sys.argv[1])}))
