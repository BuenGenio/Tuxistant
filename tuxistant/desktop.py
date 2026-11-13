"""Named desktop actions, the only way a recipe reaches the desktop.

A recipe names an entry of DESKTOP and fills its typed fields, such as
`desktop: {action: night_light, turn: on}`. It never names a bus, a path, an interface, a method,
a schema or a key: those are fixed here, beside the entry's own sentence and its own card rule, so
what a call does can always be said in words before it runs. A D-Bus method name can't say that,
which is why recipes have no D-Bus of their own.

Every call to the desktop goes through _gsettings or _bus, so a test has two things to replace.
Values that reach them are fixed in this file, apart from a notification's text, which travels as
a typed D-Bus string and is never parsed.
"""
import asyncio
import subprocess
from dataclasses import dataclass
from typing import Callable

from dbus_fast import AuthError, DBusError, Message, MessageFlag, MessageType
from dbus_fast.aio import MessageBus

from .gate import shown

TOOL_S = 5
BUS_S = 5
MAX_NOTE_CHARS = 200
ON_WORDS = ("on", "yes", "true")
OFF_WORDS = ("off", "no", "false")

NOTIFICATIONS = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                 "org.freedesktop.Notifications")
SCREENSAVER = ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver")
INTERFACE = "org.gnome.desktop.interface"
NIGHT_LIGHT = "org.gnome.settings-daemon.plugins.color"
BANNERS = "org.gnome.desktop.notifications"


class DesktopError(Exception):
    """The desktop didn't do it. The message is a sentence for Tux to say."""


@dataclass(frozen=True)
class Entry:
    name: str
    fields: tuple                       # (name, kind): kind is "switch" or "words"
    run: Callable[[dict, str], None]    # (values, recipe id), raises DesktopError
    said: Callable[[dict], str]         # what Tux says once it is done
    card: Callable[[dict, str], tuple | None]   # (values, recipe id) -> (title, detail) or None


# The two ways out

def _gsettings(schema: str, key: str, value: str) -> None:
    """One key set to a GVariant written in this file. The tool checks the schema and the type,
    and a desktop without the schema says no rather than writing a stray key."""
    try:
        done = subprocess.run(["gsettings", "set", schema, key, value],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=TOOL_S)
    except (OSError, subprocess.SubprocessError):
        raise DesktopError("") from None
    if done.returncode != 0:
        raise DesktopError("")


def _bus(target: tuple, member: str, signature: str = "", body=()) -> list:
    """One method call on the session bus, typed, on a loop of its own: handlers run in a
    thread, and a connection belongs to the loop that made it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_call(target, member, signature, list(body)))
    raise RuntimeError("Desktop actions wait on other programs, so they need a thread.")


async def _call(target: tuple, member: str, signature: str, body: list) -> list:
    name, path, interface = target
    connection = None
    try:
        connection = MessageBus()     # with no session bus address, this is what fails
        await asyncio.wait_for(connection.connect(), BUS_S)
        message = Message(destination=name, path=path, interface=interface, member=member,
                          signature=signature, body=body, flags=MessageFlag.NO_AUTOSTART)
        reply = await asyncio.wait_for(connection.call(message), BUS_S)
    except (OSError, EOFError, TimeoutError, LookupError, ValueError, TypeError, AuthError,
            DBusError):
        raise DesktopError("") from None
    finally:
        if connection is not None:
            connection.disconnect()
    if reply.message_type == MessageType.ERROR:
        raise DesktopError("")
    return reply.body


def _failed(said: str):
    """A run whose own failure says what didn't happen, in place of the empty one above."""
    def wrap(run):
        def call(values: dict, recipe: str) -> None:
            try:
                run(values, recipe)
            except DesktopError:
                raise DesktopError(said) from None
        return call
    return wrap


# The entries

def _from(recipe: str) -> str:
    return f"From the recipe {shown(recipe, 200)}, which you added."


@_failed("The notification didn't show. This desktop may not have a notification service.")
def _notify(values: dict, recipe: str) -> None:
    # Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout).
    # The title names the recipe, so it can't pass for the system or another app.
    _bus(NOTIFICATIONS, "Notify", "susssasa{sv}i",
         ["Tuxistant", 0, "", shown(f"From the recipe {recipe}", 200), values["text"], [], {},
          -1])


def _switched(word: str) -> Callable[[dict], str]:
    return lambda values: f"{word} is {'on' if values['turn'] else 'off'}."


@_failed("I couldn't change Do Not Disturb on this desktop.")
def _do_not_disturb(values: dict, _recipe: str) -> None:
    _gsettings(BANNERS, "show-banners", "false" if values["turn"] else "true")


def _dnd_card(values: dict, recipe: str) -> tuple | None:
    """On is quiet: nothing on screen changes, and reminders and calls stop popping up until
    someone turns it off. Off brings them back, which nobody misses."""
    if not values["turn"]:
        return None
    return ("Turn on Do Not Disturb?",
            f"{_from(recipe)}\nNotifications, reminders and calls included, stop popping up "
            "until Do Not Disturb is turned off. They still go to the list.")


@_failed("I couldn't change Night Light on this desktop.")
def _night_light(values: dict, _recipe: str) -> None:
    _gsettings(NIGHT_LIGHT, "night-light-enabled", "true" if values["turn"] else "false")


@_failed("I couldn't change the style on this desktop.")
def _dark_style(values: dict, _recipe: str) -> None:
    _gsettings(INTERFACE, "color-scheme", "'prefer-dark'" if values["turn"] else "'default'")


@_failed("The screen didn't lock.")
def _lock_screen(_values: dict, _recipe: str) -> None:
    _bus(SCREENSAVER, "Lock")


def _lock_card(_values: dict, recipe: str) -> tuple:
    """Always: it takes the screen away from whoever is using it, mid-sentence or mid-talk."""
    return ("Lock the screen?",
            f"{_from(recipe)}\nThe screen locks now, and it takes your password to get back. "
            "Apps keep running.")


def _no_card(_values: dict, _recipe: str) -> None:
    return None


DESKTOP = {
    # Text on screen, written by the recipe's author with only choices and numbers in it, under a
    # title that names the recipe. Nothing changes, so no card.
    "notify": Entry("notify", (("text", "words"),), _notify, lambda _: "Done.", _no_card),
    "do_not_disturb": Entry("do_not_disturb", (("turn", "switch"),), _do_not_disturb,
                            _switched("Do Not Disturb"), _dnd_card),
    # Seen at once and undone in one click from the top bar, so no card.
    "night_light": Entry("night_light", (("turn", "switch"),), _night_light,
                         _switched("Night Light"), _no_card),
    "dark_style": Entry("dark_style", (("turn", "switch"),), _dark_style,
                        _switched("Dark style"), _no_card),
    "lock_screen": Entry("lock_screen", (), _lock_screen, lambda _: "Locking the screen.",
                         _lock_card),
}


def listed() -> str:
    names = list(DESKTOP)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def run(entry: Entry, values: dict, recipe: str) -> tuple[bool, str]:
    """In a thread, after any click: whether it was done, and the sentence to say."""
    try:
        entry.run(values, recipe)
    except DesktopError as e:
        return False, str(e)
    return True, entry.said(values)
