"""Whatever is playing on this computer, and how loud it is.

Players are reached through MPRIS: the org.mpris.MediaPlayer2.* names on the session bus, which
Spotify, VLC, Firefox, Chromium, Rhythmbox and mpv with its plugin all own. The volume is the
system's, through PipeWire's wpctl, never a player's own.

None of this asks for a click, and what makes that safe is how little can leave here. A player is
only ever sent one of six methods that take no argument, at a name the bus itself listed, and the
volume stops at 100 percent. A confused model can pause, skip, or resume what was already there at an ordinary volume.
Player text is spoken by the app and never handed to the model with tools. Keep it that
way: OpenUri and AddTrack would let the model choose what plays and where it is fetched from, Quit
closes an app, and nothing the model wrote may ever travel as a D-Bus or wpctl argument.

The handlers are plain functions, so the daemon runs them in a worker thread. A dbus_fast
connection belongs to the event loop it was made on, so the daemon's own is never borrowed: each
request makes a loop and a connection of its own in that thread, asks every player at once under
one time limit, and closes both. Gio could make the same calls from a thread, but one after
another, so each frozen player would cost its own wait.
"""
import asyncio
import math
import os
import re
import subprocess
from urllib.parse import unquote, urlsplit

from dbus_fast import AuthError, DBusError, Message, MessageFlag, MessageType, unpack_variants
from dbus_fast.aio import MessageBus

from .gate import APP_SAYS, Speak, shown

DBUS, DBUS_PATH = "org.freedesktop.DBus", "/org/freedesktop/DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"
ROOT, OBJECT = "org.mpris.MediaPlayer2", "/org/mpris/MediaPlayer2"
PREFIX, PLAYER = ROOT + ".", ROOT + ".Player"
# playerctld stands in for whichever player changed last, so it is not one itself. When it is
# running it is also the only thing on the bus that knows which one that was.
PLAYERCTLD, PLAYERCTLD_INTERFACE = PREFIX + "playerctld", "com.github.altdesktop.playerctld"
METHODS = {"play": "Play", "pause": "Pause", "toggle": "PlayPause", "next": "Next",
           "previous": "Previous", "stop": "Stop"}     # all there is: see the top of this file
NEEDS = {"play": "CanPlay", "pause": "CanPause", "next": "CanGoNext",
         "previous": "CanGoPrevious", "stop": "CanControl"}
CALL_S = 2          # players are asked all at once, so a frozen one costs this once
SETTLE_S = 1.5      # how long a player gets to show that it did what it was told
POLL_S = 0.1
MAX_PLAYERS = 20
MAX_SAID = 4        # in one sentence or one answer
MAX_APP_CHARS, MAX_TITLE_CHARS, MAX_ARTIST_CHARS, MAX_ALBUM_CHARS = 40, 80, 40, 60
SINK = "@DEFAULT_AUDIO_SINK@"   # the speakers. The microphone is never touched
WPCTL_S = 3
STEP = 10           # "up" and "down"
BIGGEST_STEP = 30   # "up 20" can be a step. "up 50" is a level, whatever the model made of it
LOUDEST = 100       # wpctl would take 1.5 for 150 percent, and more

NO_BUS = "I can't reach this desktop's session bus, so I can't see what's playing."
NO_PLAYERS = "Nothing is playing: no media player is open."
NOTHING = "Nothing is playing."
NO_WPCTL = "I can't change the volume, because PipeWire's wpctl isn't installed."
NO_VOLUME = "PipeWire didn't answer, so I can't change the volume."
SAY_WHAT = "Say play, pause, next, previous or stop, or a volume."
SAY_VOLUME = "Say a volume from 0 to 100 percent, or up, down, mute or unmute."
CANNOT = {"play": "has nothing to play right now", "pause": "can't be paused right now",
          "next": "can't skip to a next track right now",
          "previous": "can't go back a track right now"}
UNMOVED = {"play": "to play, but it hasn't started", "pause": "to pause, but it's still playing",
           "stop": "to stop, but it's still playing",
           "toggle": "to play or pause, but nothing changed"}

# Read in this order: "skip back" is previous, "stop playing" is stop, "play next" is next.
# "on" and "off" are left to the lights: a request that strayed here should start nothing.
ACTIONS = (("toggle", {"toggle", "playpause"}),
           ("previous", {"previous", "prev", "back", "backward", "backwards", "last"}),
           ("next", {"next", "skip", "forward", "forwards", "following"}),
           ("pause", {"pause", "paused", "hold"}),
           ("stop", {"stop", "stopped", "halt"}),
           ("play", {"play", "resume", "continue", "unpause", "start"}))
WANTS = {"play": ("playing", "paused", "stopped"), "toggle": ("playing", "paused", "stopped"),
         "pause": ("playing",), "stop": ("playing",), "next": ("playing", "paused"),
         "previous": ("playing", "paused")}
LOUDER = {"up", "louder", "increase", "raise", "higher", "more", "+"}
QUIETER = {"down", "quieter", "softer", "decrease", "lower", "less", "reduce", "-"}
MUTE, UNMUTE = {"mute", "muted"}, {"unmute", "unmuted"}    # not "silence": Tux goes quiet too
LEVELS = {"max": 100, "maximum": 100, "full": 100, "loudest": 100, "half": 50, "halfway": 50,
          "medium": 50, "middle": 50, "min": 0, "minimum": 0, "zero": 0}
# Words that make a phrase about the volume, whatever else is in it: "turn off the sound".
SOUND = {"volume", "sound", "loud", "louder", "quieter", "softer", *MUTE, *UNMUTE}
# What may stand beside a bare "up" or "down" for it to be about the volume: "turn it up".
TURN = {"turn", "it", "the", "a", "bit", "little", "please"}
# What a small model puts where a name goes when nobody was named.
NOT_NAMES = {"the", "my", "a", "an", "this", "that", "it", "on", "in", "music", "song", "songs",
             "track", "media", "player", "players", "video", "audio", "sound", "playback", "app",
             "current", "currently", "playing", "active", "default", "none", "null", "any", "all",
             "everything", "computer", "system", "one"}
ALIASES = {"chrome": "chromium"}    # Chrome's bus name says chromium, and people say either
NUMBER = r"[+-]?\d+(?:[.,]\d+)?"


class Refused(Exception):
    """A call that came back as an error. silent: it didn't come back at all."""

    def __init__(self, error: str = ""):
        super().__init__(error or "no answer")
        self.silent = not error


class Bus:
    """One connection, for one request."""

    def __init__(self, connection):
        self.connection = connection

    async def call(self, name, path, interface, member, signature="", body=()) -> list:
        # No auto-start: asking after a player that has just gone must never launch one.
        message = Message(destination=name, path=path, interface=interface, member=member,
                          signature=signature, body=list(body), flags=MessageFlag.NO_AUTOSTART)
        try:
            reply = await asyncio.wait_for(self.connection.call(message), CALL_S)
        except Exception as e:      # the connection dropping arrives as whatever broke it
            raise Refused() from e
        if reply.message_type == MessageType.ERROR:
            raise Refused(reply.error_name or "error")
        return reply.body

    def close(self) -> None:
        self.connection.disconnect()


async def _connect() -> Bus:
    """The one place a connection is made, so a test or an evaluation has one thing to replace."""
    connection = None
    try:
        connection = MessageBus()
        await asyncio.wait_for(connection.connect(), CALL_S)
    except (OSError, EOFError, LookupError, ValueError, TypeError, AuthError, DBusError) as e:
        if connection:
            connection.disconnect()
        raise ValueError(NO_BUS) from e
    return Bus(connection)


def _run(coroutine):
    """One request, on a loop of its own. The thread a handler is given has none, and the
    daemon's loop must not wait on a player."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    coroutine.close()
    raise RuntimeError("The media handlers wait on other programs, so they need a thread.")


def _text(value, limit: int) -> str:
    """Other people's text for a result: a web page writes a title, any program its own name.
    Flattened, cut and escaped. The cut is made here so it ends in a plain ellipsis, where
    shown() would add a count of characters for the voice to read out. It still does that to
    text that grew in the escaping, which no real title does."""
    if not isinstance(value, str):
        return ""
    flat = " ".join(value.split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return shown(flat, limit + 1)


def _strings(value) -> list[str]:
    """xesam:artist is a list, and some players send one string anyway."""
    values = value if isinstance(value, list) else [value]
    return [text for text in values if isinstance(text, str) and text.strip()]


def _join(names: list[str]) -> str:
    return " and ".join(filter(None, [", ".join(names[:-1]), *names[-1:]]))


def _file_name(url) -> str:
    """A file with no tags has no title, only a place."""
    if not isinstance(url, str) or not url.startswith("file:"):
        return ""
    try:
        name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    except ValueError:
        return ""
    return name.rsplit(".", 1)[0] or name


def _track(meta: dict) -> str:
    """What is on, as a phrase: "Heroes by David Bowie"."""
    titles = _strings(meta.get("xesam:title"))
    title = _text(titles[0] if titles else _file_name(meta.get("xesam:url")), MAX_TITLE_CHARS)
    artists = _strings(meta.get("xesam:artist")) or _strings(meta.get("xesam:albumArtist"))
    artist = _join([_text(name, MAX_ARTIST_CHARS) for name in artists[:3]])
    if title and artist:
        return f"{title} by {artist}"
    return title or (f"something by {artist}" if artist else "")


def _mark(meta: dict) -> tuple:
    """Tells one track from the next. Compared, never said."""
    return tuple(str(meta.get(key, ""))[:200]
                 for key in ("mpris:trackid", "xesam:title", "xesam:url"))


def _kind(name: str) -> str:
    """org.mpris.MediaPlayer2.chromium.instance4242 is chromium."""
    return name[len(PREFIX):].split(".")[0]


def _app(name: str, identity) -> str:
    """What to call a player out loud. One with no Identity goes by its bus name."""
    fallback = re.sub(r"[_-]+", " ", _kind(name)).capitalize()
    return _text(identity, MAX_APP_CHARS) or _text(fallback, MAX_APP_CHARS) or "A player"


def _player(name: str, root: dict, props: dict) -> dict:
    """One player as plain values. A flag it leaves out counts as yes, and the call decides."""
    meta = props.get("Metadata")
    meta = meta if isinstance(meta, dict) else {}
    status = str(props.get("PlaybackStatus", "")).casefold()
    controls = props.get("CanControl") is not False
    album = _text(next(iter(_strings(meta.get("xesam:album"))), ""), MAX_ALBUM_CHARS)
    return {"name": name, "app": _app(name, root.get("Identity")),
            "status": status if status in ("playing", "paused") else "stopped",
            "controls": controls,
            "can": {action: controls and props.get(flag) is not False
                    for action, flag in NEEDS.items()},
            "track": _track(meta), "album": album, "mark": _mark(meta)}


async def _properties(bus, name: str, interface: str) -> dict | None:
    try:
        found = unpack_variants((await bus.call(name, OBJECT, PROPERTIES, "GetAll", "s",
                                                [interface]))[0])
    except (Refused, LookupError, TypeError):
        return None
    return found if isinstance(found, dict) else None


async def _state(bus, name: str) -> dict | None:
    """A player as it stands, or None if it didn't say. One that is frozen can't be told to
    pause either, so it is left out."""
    root, props = await asyncio.gather(_properties(bus, name, ROOT),
                                       _properties(bus, name, PLAYER))
    return None if props is None else _player(name, root or {}, props)


async def _recent(bus, names: list) -> list[str]:
    """Players by how lately each changed, latest first, if playerctld is there to have kept
    track. Nothing else on the bus remembers which one started last."""
    if PLAYERCTLD not in names:
        return []
    try:
        order = unpack_variants((await bus.call(PLAYERCTLD, OBJECT, PROPERTIES, "Get", "ss",
                                                [PLAYERCTLD_INTERFACE, "PlayerNames"]))[0])
    except (Refused, LookupError, TypeError):
        return []
    return [name for name in order if isinstance(name, str)] if isinstance(order, list) else []


async def _look(bus) -> tuple[list[dict], list[str]]:
    """Every player that answers, and the order they last changed in if anything knows."""
    try:
        names = (await bus.call(DBUS, DBUS_PATH, DBUS, "ListNames"))[0]
    except (Refused, LookupError, TypeError) as e:
        raise ValueError(NO_BUS) from e
    mpris = sorted(name for name in names if isinstance(name, str) and name.startswith(PREFIX)
                   and name != PLAYERCTLD)[:MAX_PLAYERS]
    *players, recent = await asyncio.gather(*(_state(bus, name) for name in mpris),
                                            _recent(bus, names))
    return [player for player in players if player], recent


def _tokens(text) -> list[str]:
    """Letters and digits apart, so "V L C" and "vlc" can be made the same thing."""
    return re.findall(r"[^\W\d_]+|\d+", str(text).casefold())


def _own(player: dict) -> set[str]:
    return {*_tokens(player["app"]), *_tokens(_kind(player["name"]))}


def _named(players: list[dict], wanted) -> list[dict] | None:
    """The players a spoken name can mean, tightest reading first, or None if it names none:
    "the music" doesn't say which. A title is the last resort, for two tabs of one browser."""
    words = [word for word in _tokens(wanted or "") if word not in NOT_NAMES]
    if not words:
        return None
    said = "".join(words)
    readings = (
        lambda p: said == "".join(_tokens(p["app"])),
        lambda p: all(word in _own(p) or ALIASES.get(word) in _own(p) for word in words),
        lambda p: len(said) > 2 and any(said in "".join(_tokens(text))
                                        for text in (p["app"], _kind(p["name"]))),
        lambda p: all(word in _tokens(p["track"]) for word in words))
    for fits in readings:
        if found := [player for player in players if fits(player)]:
            return found
    return []


def _refusal(player: dict, action: str) -> str | None:
    """Why a player can't do this, by its own account, before anything is sent to it."""
    if action == "toggle":
        action = "pause" if player["status"] == "playing" else "play"
    if not player["controls"]:
        return f"{player['app']} can't be controlled from here."
    return None if player["can"][action] else f"{player['app']} {CANNOT[action]}."


def _labels(players: list[dict]) -> list[str]:
    """Names to tell players apart by. Two tabs of one browser share a name, so those get
    what they are playing as well."""
    apps = [player["app"] for player in players]
    return [f"{player['app']} with {player['track']}"
            if apps.count(player["app"]) > 1 and player["track"] else player["app"]
            for player in players[:MAX_SAID]]


def _which(players: list[dict]) -> str:
    state = players[0]["status"] if players[0]["status"] != "stopped" else "open"
    return (f"{_join(_labels(players))} are {'both' if len(players) == 2 else 'all'} {state}. "
            "Which one do you mean?")


def _distinct(players: list[dict]) -> list[dict]:
    """One app can announce the same thing twice, and then either one will do."""
    kept = {}
    for player in players:
        same = (player["app"], player["track"], player["status"])
        kept.setdefault(same if player["track"] else player["name"], player)
    return list(kept.values())


def _one(found: list[dict], action: str, recent: list[str]) -> dict:
    """One of several players in the same state: the only one that can do this, else the one
    that changed last if that is known. Otherwise it's a question, not a guess."""
    found = _distinct(found)
    able = [player for player in found if not _refusal(player, action)] or found
    if len(able) == 1:
        return able[0]
    if known := [player for player in able if player["name"] in recent]:
        return min(known, key=lambda player: recent.index(player["name"]))
    raise ValueError(_which(able))


def _choose(players: list[dict], wanted, action: str, recent=()) -> dict:
    """The player a request is about. Named or not, the one that is playing comes first, and
    only play, next and previous look at paused ones."""
    if not players:
        raise ValueError(NO_PLAYERS)
    named = _named(players, wanted)
    # The model invents names, and with one player open there is only one it can mean. But
    # not to start it: "open Spotify" must not set a video someone left paused playing.
    if named == [] and (len(players) > 1 or action in ("play", "toggle")):
        raise ValueError(f"I can't find a player by that name. {_join(_labels(players))} "
                         f"{'is' if len(players) == 1 else 'are'} open.")
    pool = named or players
    for status in WANTS[action]:
        if found := [player for player in pool if player["status"] == status]:
            return _one(found, action, list(recent))
    if len(pool) == 1:      # the handler says how it stands: "Spotify is already paused."
        return pool[0]
    raise ValueError(NOTHING)


def _now(player: dict) -> str:
    """How a player stands, as a sentence to say."""
    app, track = player["app"], player["track"]
    if player["status"] == "playing":
        return f"{app} is playing {track}." if track else f"{app} is playing."
    return f"{'Paused' if player['status'] == 'paused' else 'Stopped'} {app}."


def _on(player: dict) -> str:
    """After a skip, where nothing was paused by us even if it is paused."""
    if player["status"] == "playing":
        return _now(player)
    where = f" on {player['track']}" if player["track"] else ""
    return f"{player['app']} is {player['status']}{where}."


def _already(player: dict) -> str:
    app, track = player["app"], player["track"]
    if player["status"] == "paused":
        return f"{app} is already paused."
    return f"{app} is already playing {track}." if track else f"{app} is already playing."


def _settled(action: str, before: dict, now: dict) -> bool:
    if action in ("next", "previous"):
        return now["mark"] != before["mark"]
    if action == "toggle":
        return now["status"] != before["status"]
    return (now["status"] == "playing") == (action == "play")


async def _gone(bus, name: str) -> bool:
    try:
        return not (await bus.call(DBUS, DBUS_PATH, DBUS, "NameHasOwner", "s", [name]))[0]
    except (Refused, LookupError, TypeError):
        return False


async def _after(bus, before: dict, action: str) -> dict | None:
    """The player once it shows it has acted, or as it stands when the wait is over. None if
    it has left the bus, which is how a browser stops."""
    deadline = asyncio.get_running_loop().time() + SETTLE_S
    while True:
        now = await _state(bus, before["name"])
        if now is None:
            if await _gone(bus, before["name"]):
                return None
            raise ValueError(f"{before['app']} has stopped answering.")
        if _settled(action, before, now) or asyncio.get_running_loop().time() >= deadline:
            return now
        await asyncio.sleep(POLL_S)


def _outcome(action: str, before: dict, now: dict | None) -> str:
    """What is true now, read back from the player, not assumed from what was sent."""
    app = before["app"]
    if now is None:
        if action in ("pause", "stop", "toggle"):
            return f"Stopped {app}."
        raise ValueError(f"{app} has closed.")
    if action in ("next", "previous"):
        stuck = action == "next" and now["mark"] == before["mark"]
        return f"I asked {app} to skip, and it hasn't moved on. {_on(now)}" if stuck else _on(now)
    if not _settled(action, before, now):
        raise ValueError(f"I asked {app} {UNMOVED[action]}.")
    return _now(now)


async def _do(bus, player: dict, action: str) -> str:
    app, status = player["app"], player["status"]
    if {"play": "playing", "pause": "paused"}.get(action) == status:
        return _already(player)
    if action in ("pause", "stop") and status != "playing":
        return f"{app} isn't playing."
    if refusal := _refusal(player, action):
        raise ValueError(refusal)
    try:
        await bus.call(player["name"], OBJECT, PLAYER, METHODS[action])
    except Refused as e:
        raise ValueError(f"{app} didn't answer." if e.silent
                         else f"{app} wouldn't do that.") from e
    return _outcome(action, player, await _after(bus, player, action))


async def _act(action: str, wanted) -> str:
    bus = await _connect()
    try:
        players, recent = await _look(bus)
        return await _do(bus, _choose(players, wanted, action, recent), action)
    finally:
        bus.close()


async def _players() -> list[dict]:
    bus = await _connect()
    try:
        return (await _look(bus))[0]
    finally:
        bus.close()


def _action(value) -> str | None:
    """The action a word or phrase means, or None if it isn't one."""
    text = re.sub(r"\bun[\s_-]+(?=pause)", "un", str(value or "").casefold())
    words = set(re.findall(r"[a-z]+", text.replace("play/pause", "playpause")))
    if {"play", "pause"} <= words:
        return "toggle"
    return next((action for action, names in ACTIONS if words & names), None)


def _percent(number: float) -> int:
    """40 for 40, and for 0.4 too, which is how some of them say 40 percent."""
    if not math.isfinite(number):
        raise ValueError(SAY_VOLUME)
    return round(max(-1000.0, min(1000.0, number * 100 if 0 < abs(number) < 1 else number)))


def _spoken_volume(text: str) -> tuple:
    text = re.sub(r"\bun[\s_-]+(?=mute)", "un", text.casefold())
    tokens = re.findall(rf"{NUMBER}|[^\W\d_]+|[+-]", text)
    words = set(tokens)
    if "shut" in words:     # "shut up" is no way to ask for louder
        raise ValueError(SAY_VOLUME)
    if words & UNMUTE or words & MUTE:
        return "mute", not words & UNMUTE
    number = next((token for token in tokens if re.fullmatch(NUMBER, token)), "")
    down = bool(words & QUIETER) or number.startswith("-")
    stepping = down or bool(words & LOUDER) or number.startswith("+")
    if number:
        amount = _percent(float(number.lstrip("+-").replace(",", ".")))
        if not stepping or "to" in words:       # "down to 20" is a level
            return "set", amount
        sure = "by" in words or number[0] in "+-"       # "down by 20" is a step
        return ("step" if sure else "toward"), -amount if down else amount
    if stepping:
        return "step", -STEP if down else STEP
    for word in tokens:
        if word in LEVELS:
            return "set", LEVELS[word]
    if words & {"on", "off"}:
        return "mute", "off" in words
    raise ValueError(SAY_VOLUME)


def _volume(value) -> tuple | None:
    """("set", percent), ("step", percent), ("toward", percent) or ("mute", yes or no), from 40,
    0.4, "40%", "up", "down by 20", "down 20", "half" or "mute". Past here there is no text of
    the model's, only these."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(SAY_VOLUME)
    if isinstance(value, (int, float)):
        return ("step" if value < 0 else "set"), _percent(float(value))
    return _spoken_volume(str(value))


def _asked(args: dict) -> tuple[str | None, tuple | None]:
    """What to do, as typed values. The model is steered to put "volume down" or "mute" under
    action, with a number under volume if there is one, so the two are read as one phrase.
    That takes a word about sound, or nothing but "up" or "down": "seek 10" must not set the
    volume to 10."""
    said, loud = args.get("action"), args.get("volume")
    action = _action(said)
    words = set(_tokens(said)) if isinstance(said, str) else set()
    if words & SOUND or not action and words - TURN in ({"up"}, {"down"}):
        try:
            return None, _volume(said if loud in (None, "") else f"{said} {loud}")
        except ValueError:
            if not action:      # "pause the sound" is still a pause
                raise
    try:
        volume = _volume(loud)
    except ValueError:
        if action or not (action := _action(loud)):
            raise
        volume = None
    if not action and volume is None:
        raise ValueError(SAY_WHAT)
    if action and volume and volume[0] == "set":
        volume = None   # a number nobody said: "play some jazz" came with a volume of 50
    return action, volume


def _wpctl(*arguments: str) -> str:
    """The one place wpctl runs, so a test or an evaluation has one thing to replace."""
    try:
        done = subprocess.run(["wpctl", *arguments], capture_output=True, text=True,
                              errors="replace", check=True, timeout=WPCTL_S,
                              env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError as e:
        raise ValueError(NO_WPCTL) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(NO_VOLUME) from e
    return done.stdout


def _loudness() -> tuple[int, bool]:
    """The volume in percent, and whether it is muted."""
    out = _wpctl("get-volume", SINK)
    found = re.search(r"Volume:\s*(\d+(?:[.,]\d+)?)", out)
    if not found:
        raise ValueError(NO_VOLUME)
    return round(float(found[1].replace(",", ".")) * 100), "MUTED" in out


def _fraction(percent: float) -> str:
    """wpctl's own number. The last thing before the command line, so nothing gets past it."""
    return f"{max(0, min(LOUDEST, round(percent))) / 100:.2f}"


def _toward(level: int, amount: int) -> int:
    """Where "down 20" should end. The model sends the same for "down to 20" and "down by 20",
    so the number is read both ways and the one that moves the volume less wins. When the
    number as a level lies the wrong way, as "up 50" does at 60, only a small one is taken for
    a step: that ends at 50, where a step would end at full volume."""
    wanted, stepped = abs(amount), level + amount
    if (wanted > level) == (amount > 0):
        return min(wanted, stepped, key=lambda target: abs(target - level))
    return stepped if wanted <= BIGGEST_STEP else wanted


def _turn(change: tuple) -> str:
    kind, amount = change
    level, muted = _loudness()
    if kind == "mute":
        _wpctl("set-mute", SINK, "1" if amount else "0")
        return _volume_now()
    louder = kind != "set" and amount > 0
    if kind == "toward":
        kind, amount = "set", _toward(level, amount)
    target = amount if kind == "set" else level + amount
    # Someone who set 120 percent themselves and asks for "up" doesn't mean down to 100.
    if not (louder and level >= LOUDEST):
        _wpctl("set-volume", SINK, _fraction(target))
    if muted and (louder or kind == "set" and target > 0):
        _wpctl("set-mute", SINK, "0")       # a volume is something to hear
    return _volume_now(refused_more=target > LOUDEST and amount > 0)


def _volume_now(refused_more: bool = False) -> str:
    level, muted = _loudness()
    if muted:
        return f"Muted, at {level} percent."
    return f"Volume {level} percent." + (" I don't go above 100." if refused_more else "")


def control(args: dict, _config, _session=None) -> dict:
    action, volume = _asked(args)
    jobs = {}
    if action:
        jobs["playback"] = lambda: _run(_act(action, args.get("player")))
    if volume:
        jobs["volume"] = lambda: _turn(volume)
    done, problems = {}, []
    for key, job in jobs.items():
        try:
            done[key] = job()
        except ValueError as e:
            problems.append(str(e))
    if not done:
        raise ValueError(problems[0])
    return {**done, "problem": problems[0]} if problems else done


def _heard(player: dict) -> dict:
    said = {"player": player["app"], "state": player["status"]}
    if player["track"]:
        said["track"] = player["track"]
        if player["album"] and player["album"] not in player["track"]:
            said["track"] += f", from the album {player['album']}"
    return said


def playing(args: dict, _config, _session=None) -> dict:
    players = _run(_players())
    if not players:
        raise ValueError(NO_PLAYERS)
    named = _named(players, args.get("player"))
    pool = named or players
    for status in ("playing", "paused"):
        if found := _distinct([player for player in pool if player["status"] == status]):
            heard = [_heard(player) for player in found[:MAX_SAID]]
            return heard[0] if len(heard) == 1 else {"players": heard}
    raise ValueError(NOTHING)


def _spoken(handler):
    """Player metadata can come from a web page. Keep even error text out of the model."""
    def run(args, config, session):
        try:
            result = handler(args, config)
            if handler is playing:
                players = result.get("players", [result])
                text = " ".join(f"{p['player']} is {p['state']}"
                                + (f" {p['track']}" if p.get("track") else "") + "."
                                for p in players)
            else:
                text = " ".join(result.values())
        except ValueError as error:
            text = str(error)
        session.after.append(Speak(text, config["voice"]))
        return {APP_SAYS: True}
    return run


HANDLERS = {"media.control": _spoken(control), "media.playing": _spoken(playing)}
