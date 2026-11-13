"""Home Assistant on your own network, through its REST API and a long-lived access token.

Two actions, because every extra tool costs a small model accuracy: home.set changes something
and home.status reads something. What the person called it is matched against the names Home
Assistant gives its entities and its areas.

Only typed values reach the server: an entity id from its own list, a service from the table
here, a number in range, a colour from the list in colours.py. Only typed values come back too: a
number, or one of Home Assistant's own state words. A name is whatever anyone typed into Home
Assistant, so it is cut short and its control characters shown as escapes.

Unlocking a lock, opening a cover (a garage door is one), activating a scene and running a
script return an Ask: they wait for a click on a card written here from the real entity and
service. So does a switch or a helper whose name says it works an alarm, a lock or a door. An
alarm panel is only read, and a siren is neither read nor changed.

The token is as good as a key to the house. It goes to the server named in Settings and nowhere
else: no proxy, no redirect, and never over plain http to an address that isn't private.
"""
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace

from . import colours
from .gate import Ask, shown

PORT = 8123                     # Home Assistant's own, for a plain address that names none
TIMEOUT_S = 6
SERVICE_TIMEOUT_S = 15          # a lock answers once it has moved
READ_S = 20                     # for one whole reply, however slowly it arrives
MAX_BYTES = 1 << 20
MAX_STATES_BYTES = 16 << 20     # /api/states holds every attribute of every entity
NAMES_FOR_S = 60                # how long the list is trusted for finding a name
LOOK_AGAIN_S = 2                # before saying nothing has that name, unless it only just looked
STATES_FOR_S = 5                # how long it is trusted for the state of many things at once
EXTRAS_FOR_S = 600              # areas and the unit of temperature change rarely
MAX_SINGLE_READS = 12           # this many are asked about one by one, which is always fresh
SETTLE_S = (0.4, 0.8)           # waits before reading again: some devices lag their service call
MAX_NAME_CHARS = 40             # a name is whatever anyone typed into Home Assistant
MAX_JUDGED_CHARS = 255          # of a name, when finding a thing or judging whether it needs a click
MAX_TOKEN_CHARS = 1024
MAX_SPOKEN = 5                  # a small model reads out all it is given
MAX_PROBLEMS = 2                # a house can have a dozen lamps unplugged at the wall
MAX_ON_A_CARD = 8
FEW = 5
USER_AGENT = "Tuxistant"

NO_SERVER = "Add your Home Assistant address in Settings, under Home Assistant, so I can reach it."
BAD_SERVER = ("The Home Assistant address in Settings isn't one I can use. It should look like "
              "homeassistant.local:8123.")
NO_TOKEN = ("Add a long-lived access token in Settings, under Home Assistant. You make one on "
            "your profile page in Home Assistant, under Security.")
BAD_TOKEN = ("The Home Assistant token in Settings isn't a valid one. Make a new long-lived "
             "access token in Home Assistant and paste it in.")
WRONG_TOKEN = ("Home Assistant turned down the token in Settings. Make a new long-lived access "
               "token on your profile page in Home Assistant and paste it in.")
NOT_PRIVATE = ("I won't send your Home Assistant token over plain http to an address on the "
               "internet. Use an https address in Settings.")
REDIRECTED = ("The Home Assistant address in Settings sent me somewhere else, and I won't follow "
              "it with your token. Check the address.")
BANNED = ("Home Assistant is refusing this computer. If it banned this address after a wrong "
          "token, lift the ban in Home Assistant.")
BROKEN = "Home Assistant ran into a problem of its own. Try again in a moment."
UNREACHABLE = ("I can't reach Home Assistant at {}. Check that it's running and that the address "
               "in Settings is right.")
SLOW = "Home Assistant at {} is taking too long to answer. Try again in a moment."
UNSURE = ("Home Assistant took too long to answer, so I can't tell whether that worked. Ask me "
          "how it is now.")
NO_CERTIFICATE = ("This computer doesn't trust the certificate of Home Assistant at {}, so "
                  "nothing was sent.")
NOT_HOME_ASSISTANT = ("Something answered at {}, but not the way Home Assistant does. Check the "
                      "address in Settings.")
TOO_BIG = "Home Assistant sent more than I can take in at once."
NO_ALARMS = "I don't arm or disarm alarms. That has to be done in Home Assistant."
WHAT_TO_CHANGE = "Say what to change, such as the kitchen light or the heating."
WHAT_TO_CHECK = "Say what to check, such as the front door or the lights."
WHAT_TO_DO = "Say what to do: on or off, open or close, lock or unlock, or a temperature."

SERVER = re.compile(r"(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9._-]+)(?::(\d{1,5}))?")
TOKEN = re.compile(r"[\x21-\x7e]+")     # it goes into a header, where a line break is an injection
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")       # it goes into a URL
STATE_WORD = re.compile(r"[a-z0-9_]{1,32}")     # Home Assistant's own state words, and no prose
SHARED = ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT, where Tailscale puts a home

# Areas come from the template engine, because the registries are only on the WebSocket API. The
# template is fixed: nothing from the model or the user is ever rendered.
AREAS = ("{% set ns = namespace(found=[]) %}{% for area in areas() %}"
         "{% set ns.found = ns.found + [[area_name(area), area_entities(area)]] %}"
         "{% endfor %}{{ ns.found | tojson }}")

ON_OFF = {"on": ("turn_on", {"on"}), "off": ("turn_off", {"off"}), "toggle": ("toggle", None)}
# What can be asked of each kind of entity: the service, and the states that show it worked.
# Nothing outside this table is ever sent.
SERVICES = {
    "light": ON_OFF, "switch": ON_OFF, "fan": ON_OFF, "input_boolean": ON_OFF,
    "media_player": {"on": ("turn_on", None), "off": ("turn_off", {"off", "standby"})},
    "climate": {"on": ("turn_on", None), "off": ("turn_off", {"off"}),
                "temperature": ("set_temperature", None)},
    "scene": {"on": ("turn_on", None)},
    "script": {"on": ("turn_on", None)},
    "cover": {"open": ("open_cover", {"open", "opening"}),
              "close": ("close_cover", {"closed", "closing"})},
    "lock": {"lock": ("lock", {"locked", "locking"}),
             "unlock": ("unlock", {"unlocked", "unlocking", "open"})},
}
# These wait for a click. The words start the card's title.
ASKS = {("lock", "unlock"): "Unlock", ("cover", "open_cover"): "Open",
        ("scene", "turn_on"): "Activate the scene", ("script", "turn_on"): "Run the script"}
# What a card adds for the two that can do anything: the skill can't tell what is inside one.
UNSEEN = {"scene": "A scene can change anything in Home Assistant, a lock or a garage door "
                   "included, and I can't see what this one does.",
          "script": "A script can do anything Home Assistant can, and I can't see what this "
                    "one does."}
# A switch or a helper can be wired to anything, so one whose name or id says it works an alarm,
# a lock or a way in waits for a click, whichever way it is switched: a garage door's relay opens
# it on and off alike, and switching an alarm's helper off can disarm it. A way in also names a
# place, so "Garage light" and "Gate lamp", which say what they are, don't. The name and the id are
# judged apart, since people rename a switch.porch_light "Garage door" when they rewire it.
GUARDED_DOMAINS = {"switch", "input_boolean"}
# "Arm away" and "Arm home" are what home-made alarms call their helpers.
GUARDED_WORDS = {"alarm", "siren", "lock", "unlock", "security", "arm", "armed", "disarmed",
                 "disarm", "arming", "disarming", "deadbolt"}
WAY_IN = {"garage", "door", "gate"}
HARMLESS = {"light", "lamp", "bulb", "heater", "fan"}
# Joined up, as ids and names often are: "doorlock", "SmartLock", "frontdoor", "drivewaygate",
# "Garageopener". Not a clock, a block, a doorbell or the lights outdoors, nor a network gateway,
# and a "Garagelight" says what it is as plainly as a "Garage light".
JOINED_LOCK = re.compile(r"(?<![cb])lock")
JOINED_WAY_IN = re.compile(r"door(?!bell)|(?:front|back|side|garden|drive|driveway|yard|main"
                           r"|garage)gate|^gate(?!way)|^garage(?!(?:" + "|".join(HARMLESS)
                           + r")s?$)")
NOT_DOORS = {"indoor", "outdoor"}
CAMEL_BREAK = re.compile(r"(?<=[a-z])(?=[A-Z])")
# Where a name is split into the words it is found and judged by.
WORD_BREAK = re.compile(r"[\W_]|" + CAMEL_BREAK.pattern)
GUARDED_VERBS = {"turn_on": "Turn on", "turn_off": "Turn off", "toggle": "Toggle"}
GUARDED = ("Its name says it may work an alarm, a lock or a door, and a switch does whatever "
           "it is wired to.")
LOCK_VERBS = {"open": "unlock", "close": "lock"}
DONE_WORDS = {"scene": "is activated", "script": "has started"}
READABLE = {*SERVICES, "sensor", "binary_sensor", "alarm_control_panel"}
ROOM_NAMED = {"media_player", "climate"}    # a speaker or a thermostat is often named as its room
ORDER = ("lock", "cover", "light", "switch", "fan", "climate", "media_player", "input_boolean",
         "scene", "script", "alarm_control_panel", "binary_sensor", "sensor")
DOMAIN_WORDS = {"light": "light", "switch": "switch", "fan": "fan", "input_boolean": "switch",
                "media_player": "media player", "climate": "thermostat", "scene": "scene",
                "script": "script", "cover": "cover", "lock": "lock", "sensor": "sensor",
                "binary_sensor": "sensor", "alarm_control_panel": "alarm"}
CHOICES = {"cover": "open or close it", "lock": "lock or unlock it", "scene": "turn it on",
           "script": "run it", "climate": "turn it on or off, or what temperature to set"}

VERBS = {
    "on": colours.ON | {"enable", "enabled", "activate", "activated", "start", "run", "play"},
    "off": colours.OFF | {"disable", "disabled", "deactivate", "deactivated", "stop"},
    "toggle": {"toggle", "flip", "switch", "invert"},
    "open": {"open", "opened", "raise", "up"},
    "close": {"close", "closed", "shut", "lower", "down"},
    "lock": {"lock", "locked", "secure"},
    "unlock": {"unlock", "unlocked"},
}
VERB_NOISE = re.compile(r"\b(?:turn|turned|switch|switched|set|it|the|them|to|please|power)\b")
BLANK = {"", "none", "null", "n/a", "unchanged", "default", "undefined"}
NAME_KEYS = ("name", "device", "entity", "entity_id", "target", "light", "room", "area")
FILLER = {"the", "my", "our", "a", "an", "in", "of", "at", "for", "to", "please", "all", "every",
          "each", "both", "is", "are", "turn", "on", "off"}
EVERY = {"all", "every", "each", "both"}


def _kinds(*rows) -> dict:
    return {word: kinds for words, kinds in rows for word in words.split()}


# What people call a kind of thing: the domains it means, and the device classes where a domain
# alone is too wide. None is any class. Plurals are folded away before the lookup.
KINDS = _kinds(
    ("light lamp bulb", (("light", None),)),
    ("switch plug socket outlet", (("switch", None),)),
    ("fan", (("fan", None),)),
    ("lock", (("lock", None),)),
    ("cover", (("cover", None),)),
    # A garage door is a cover too, so "the blinds" has to be said by the cover or its name.
    ("blind shade", (("cover", {"blind", "shade"}),)),
    ("curtain", (("cover", {"curtain"}),)),
    ("shutter", (("cover", {"shutter"}),)),
    ("awning", (("cover", {"awning"}),)),
    ("door", (("lock", None), ("cover", {"door", "garage", "gate"}),
              ("binary_sensor", {"door", "garage_door", "opening", "lock"}))),
    ("window", (("cover", {"window"}), ("binary_sensor", {"window"}))),
    ("thermostat heating heater climate", (("climate", None),)),
    ("temperature", (("sensor", {"temperature"}), ("climate", None))),
    ("humidity", (("sensor", {"humidity"}),)),
    ("battery", (("sensor", {"battery"}),)),
    ("motion movement", (("binary_sensor", {"motion", "occupancy", "presence"}),)),
    ("scene", (("scene", None),)),
    ("script", (("script", None),)),
    ("tv television speaker", (("media_player", None),)),
    ("sensor", (("sensor", None), ("binary_sensor", None))),
    ("alarm", (("alarm_control_panel", None),)),
)
KIND_PLURALS = {"light": "lights", "switch": "switches", "fan": "fans", "cover": "covers",
                "lock": "locks", "climate": "thermostats", "media_player": "media players",
                "scene": "scenes", "script": "scripts", "input_boolean": "switches"}

SCALES = {"°C": " degrees Celsius", "°F": " degrees Fahrenheit"}
RANGES = {" degrees Fahrenheit": (40, 95)}      # when a thermostat doesn't give its own
UNITS = {"°C": "degrees Celsius", "°F": "degrees Fahrenheit", "%": "percent", "W": "watts",
         "kW": "kilowatts", "Wh": "watt hours", "kWh": "kilowatt hours", "MWh": "megawatt hours",
         "V": "volts", "A": "amps", "lx": "lux", "hPa": "hectopascals", "mbar": "millibars",
         "ppm": "parts per million", "m³": "cubic metres", "L": "litres", "mm": "millimetres",
         "km/h": "kilometres per hour", "mph": "miles per hour", "m/s": "metres per second",
         "dB": "decibels", "min": "minutes", "h": "hours", "s": "seconds", "d": "days"}
COLOUR_MODES = {"hs", "rgb", "rgbw", "rgbww", "xy"}
SENSED = (({"door", "garage_door", "opening", "window"}, ("is open", "is closed")),
          ({"lock"}, ("is unlocked", "is locked")),
          ({"motion", "occupancy", "presence", "smoke", "gas", "moisture", "carbon_monoxide",
            "problem", "safety", "sound", "tamper", "vibration"}, ("is detecting", "is clear")))
NOT_KNOWN = "is in a state Home Assistant doesn't know"
# Past MAX_SPOKEN, things in one of these states are counted, not listed.
RESTING = {"is off", "is closed", "is locked", "is clear", "isn't running", NOT_KNOWN}


def clean_server(text: str) -> str | None:
    """What Settings keeps for ha_server, or None. As config.clean_server does for LocalAI:
    host:port means plain http, and https://host stays as it is so the token is never sent in
    the clear. A plain address with no port gets Home Assistant's own."""
    scheme, _, rest = text.strip().rstrip("/").rpartition("://")
    scheme = scheme.lower()
    match = SERVER.fullmatch(rest)
    if scheme not in ("", "http", "https") or not match:
        return None
    if match[2] and not 0 < int(match[2]) <= 65535:
        return None
    labels = match[1].rstrip(".").split(".")    # the limits IDNA enforces, by raising
    if match[1][0] != "[" and (len(match[1]) > 253 or not all(0 < len(l) < 64 for l in labels)):
        return None
    if scheme == "https":
        return f"https://{rest}"
    return rest if match[2] else f"{rest}:{PORT}"


def valid_token(token) -> bool:
    return (isinstance(token, str) and len(token) <= MAX_TOKEN_CHARS
            and TOKEN.fullmatch(token) is not None)


@dataclass(frozen=True)
class _Server:
    url: str        # where requests go. For plain http it is the very address that was checked
    host: str       # the Host header: what Settings names
    label: str      # what a card or a sentence calls it
    token: str = field(repr=False)


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo):
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in SHARED


def _private_address(host: str, port: int) -> str:
    """Where plain http may go: an address on a private network or on this computer. The request
    then goes to that very address, so the name can't mean somewhere else a moment later."""
    try:
        found = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except OSError:
        raise ConnectionError(UNREACHABLE.format(f"{host}:{port}")) from None
    addresses = sorted({info[4][0] for info in found}, key=lambda a: (":" in a, a))
    if not addresses or not all(_private(address) for address in addresses):
        raise ValueError(NOT_PRIVATE)
    first = addresses[0]    # IPv4 before IPv6, whose link-local addresses need a zone
    return f"[{first.replace('%', '%25')}]" if ":" in first else first


def _connect(config) -> _Server:
    """The server and token in Settings, checked. config.json can hold anything."""
    typed, token = str(config["ha_server"] or "").strip(), str(config["ha_token"] or "").strip()
    if not typed:
        raise ValueError(NO_SERVER)
    cleaned = clean_server(typed)
    if cleaned is None:
        raise ValueError(BAD_SERVER)
    if not token:
        raise ValueError(NO_TOKEN)
    if not valid_token(token):
        raise ValueError(BAD_TOKEN)
    if cleaned.startswith("https://"):
        return _Server(cleaned, cleaned.removeprefix("https://"), cleaned, token)
    host, _, port = cleaned.rpartition(":")
    return _Server(f"http://{_private_address(host, int(port))}:{port}", cleaned, cleaned, token)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None     # the 3xx then surfaces as an HTTPError


# As realtime._opener does for the API key: a proxy or a redirect would be handed the token.
# livedata.OPENER follows a redirect on the same host and uses a proxy from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


class _Refused(Exception):
    """The server answered with an error. The message is what to say if nothing better is known."""

    def __init__(self, code: int, server: _Server):
        self.code = code
        if code == 401:
            said = WRONG_TOKEN
        elif code == 403:
            said = BANNED
        elif 300 <= code < 400:
            said = REDIRECTED
        else:
            said = BROKEN if code >= 500 else NOT_HOME_ASSISTANT.format(server.label)
        super().__init__(said)


class _Slow(ConnectionError):
    """No answer in time. After a request to change something, that leaves it unknown."""


def _read(response, limit: int) -> bytes:
    """No more than limit bytes and no longer than READ_S, however the server sends them."""
    deadline, parts, size = time.monotonic() + READ_S, [], 0
    while chunk := response.read1(1 << 16):
        size += len(chunk)
        if size > limit:
            raise ValueError(TOO_BIG)
        if time.monotonic() > deadline:
            raise TimeoutError
        parts.append(chunk)
    return b"".join(parts)


def _unreachable(server: _Server, error) -> ConnectionError:
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ConnectionError(NO_CERTIFICATE.format(server.label))
    if isinstance(reason, TimeoutError):
        return _Slow(SLOW.format(server.label))
    return ConnectionError(UNREACHABLE.format(server.label))


def _call(server: _Server, path: str, body: dict | None = None, limit: int | None = None,
          timeout: float | None = None):
    """One request, and the JSON that came back. Every failure is a sentence to say, and none of
    them is made from anything the request held."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(server.url + path, data, {
        "Authorization": f"Bearer {server.token}", "Host": server.host,
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with _opener.open(request, timeout=timeout or TIMEOUT_S) as response:
            raw = _read(response, limit or MAX_BYTES)
    except urllib.error.HTTPError as e:
        e.close()
        raise _Refused(e.code, server) from None
    except (OSError, http.client.HTTPException) as e:
        raise _unreachable(server, e) from None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ConnectionError(NOT_HOME_ASSISTANT.format(server.label)) from None


def _stem(word: str) -> str:
    """Plural or not must not decide a match: "the kitchen lights" and "Kitchen light"."""
    for ending, instead in (("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("xes", "x")):
        if word.endswith(ending) and len(word) > len(ending) + 1:
            return word[:-len(ending)] + instead
    plural = len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us"))
    return word[:-1] if plural else word


def _spoken_words(text) -> list[str]:
    return re.findall(r"[^\W_]+", str(text or "").casefold())


def _words(text) -> list[str]:
    return [_stem(word) for word in _spoken_words(text) if word not in FILLER]


@dataclass(frozen=True)
class Entity:
    id: str             # as the server listed it, and checked against ENTITY_ID
    name: str           # cleaned and cut: fit to say and to show
    state: str
    attrs: dict         # only the attributes in KEPT
    words: frozenset    # of the name up to its last word break in MAX_JUDGED_CHARS, else the id's
    where: frozenset    # the words of its area's name
    squashed: str       # those words run together: "HeatPump" is "heat pump"
    guarded: bool       # its first MAX_JUDGED_CHARS characters say alarm, lock or a way in

    @property
    def domain(self) -> str:
        return self.id.partition(".")[0]


KEPT = ("brightness", "color_mode", "color_temp_kelvin", "hs_color", "supported_color_modes",
        "temperature", "current_temperature", "min_temp", "max_temp", "unit_of_measurement",
        "device_class", "entity_id", "current_position", "percentage")
WORDS_KEPT = ("color_mode", "unit_of_measurement", "device_class")    # kept only if they are text


def _cleaned(text, token: str) -> str:
    """A name from the server, uncut. It is other people's text, and a server that echoed the
    token into it would otherwise have it read out."""
    return " ".join(str(text or "").replace(token, "").replace("<", "").replace(">", "").split())


def _name(text, token: str) -> str:
    """A name from the server, fit to say and to show."""
    return shown(_cleaned(text, token), MAX_NAME_CHARS)


def _judged(text, token: str) -> str:
    """A name from the server for judging whether it needs a click, never to say. The cut for
    saying would lose "garage door" from a long name and add the words of its
    "(9 more characters)"."""
    return _cleaned(text, token)[:MAX_JUDGED_CHARS]


def _findable(text, token: str) -> str:
    """The judged name backed up to the last place it splits into words, for finding it, never to
    say. The start of a word split by the cut, "garage do", would be a word that finds it, and the
    "Fan" of a joined "GarageFancy" would make a garage relay look like a fan."""
    whole = _cleaned(text, token)
    cut = whole[:MAX_JUDGED_CHARS]
    if len(whole) <= MAX_JUDGED_CHARS or WORD_BREAK.search(whole[len(cut) - 1:len(cut) + 1]):
        return cut
    return cut[:max((found.start() for found in WORD_BREAK.finditer(cut)), default=0)]


def _kind(entity: Entity) -> str:
    """What kind of thing it is, to say. A domain no table here knows is the server's text."""
    return DOMAIN_WORDS.get(entity.domain) or shown(entity.domain.replace("_", " "), 20)


def _entity(raw, token: str, areas: dict) -> Entity | None:
    """One entry of /api/states cut down to what is used here, or None if it isn't one."""
    entity_id = raw.get("entity_id") if isinstance(raw, dict) else None
    if not isinstance(entity_id, str) or len(entity_id) > 255 or not ENTITY_ID.fullmatch(entity_id):
        return None
    attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    friendly = attrs.get("friendly_name")
    by_id = entity_id.partition(".")[2].replace("_", " ")
    findable = _findable(friendly, token)
    name = _name(friendly, token) or shown(by_id, MAX_NAME_CHARS)
    state = raw.get("state") if isinstance(raw.get("state"), str) else "unknown"
    words = _words(findable or by_id)
    where = frozenset(word for area in areas.get(entity_id, ()) for word in _words(area))
    kept = {key: attrs[key] for key in KEPT
            if key in attrs and (key not in WORDS_KEPT or isinstance(attrs[key], str))}
    guarded = (entity_id.partition(".")[0] in GUARDED_DOMAINS
               and _named_guarded(_judged(friendly, token), findable))
    return Entity(entity_id, name, state[:64], kept, frozenset(words), where, "".join(words), guarded)


@dataclass(frozen=True)
class _Home:
    entities: tuple     # sorted the way they are listed aloud
    areas: dict         # entity id: the names of the areas it is in, for finding, never said
    unit: str           # " degrees Celsius", or " degrees" when the server didn't say
    names_at: float     # when the list was fetched
    states_at: float    # the same, until something here changes a state
    extras_at: float    # when the areas and the unit were


_cache: dict[tuple, _Home] = {}
_lock = threading.Lock()


def _areas(server: _Server) -> dict:
    """The areas each entity is in, or nothing: only an administrator's token may render a
    template, and matching works on names alone without it."""
    try:
        listed = _call(server, "/api/template", {"template": AREAS})
    except (_Refused, ConnectionError, ValueError):
        return {}
    found = {}
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list):
            for entity_id in entry[1]:
                if isinstance(entity_id, str) and (name := _findable(entry[0], server.token)):
                    found.setdefault(entity_id, []).append(name)
    return found


def _unit(server: _Server) -> str:
    """How Home Assistant counts temperature, which a thermostat's own state doesn't say."""
    try:
        system = _call(server, "/api/config").get("unit_system")
        return SCALES.get(system.get("temperature"), " degrees")
    except (_Refused, ConnectionError, ValueError, AttributeError, TypeError):
        return " degrees"


def _order(entity: Entity) -> tuple:
    place = ORDER.index(entity.domain) if entity.domain in ORDER else len(ORDER)
    return place, entity.name.casefold(), entity.id


def _fetch(server: _Server, before: _Home | None) -> _Home:
    listed = _call(server, "/api/states", limit=MAX_STATES_BYTES)
    if not isinstance(listed, list):
        raise ConnectionError(NOT_HOME_ASSISTANT.format(server.label))
    now = time.monotonic()
    if before and now - before.extras_at < EXTRAS_FOR_S:
        areas, unit, extras_at = before.areas, before.unit, before.extras_at
    else:
        areas, unit, extras_at = _areas(server), _unit(server), now
    entities = [e for raw in listed if (e := _entity(raw, server.token, areas))]
    return _Home(tuple(sorted(entities, key=_order)), areas, unit, now, now, extras_at)


def _key(server: _Server) -> tuple:
    return server.url, server.host, hashlib.sha256(server.token.encode()).digest()


def _home(server: _Server, names_for: float = NAMES_FOR_S,
          states_for: float | None = None) -> _Home:
    """Everything the server has, from memory while that is fresh enough for the question."""
    key = _key(server)
    with _lock:
        held, now = _cache.get(key), time.monotonic()
        stale = held is None or now - held.names_at > names_for
        if stale or (states_for is not None and now - held.states_at > states_for):
            held = _fetch(server, held)
            _cache.clear()
            _cache[key] = held
        return held


def _states_changed(server: _Server) -> None:
    """The names are still good, but what is remembered of the states no longer is."""
    with _lock:
        if held := _cache.get(_key(server)):
            _cache[_key(server)] = replace(held, states_at=float("-inf"))


@dataclass(frozen=True)
class _Said:
    raw: str            # as it came, folded: what an entity id has to equal
    words: frozenset    # of the name, without filler and plurals
    kinds: tuple        # what kind of thing: the kind asked for, or the name's last kind word
    rest: frozenset     # the words of the name other than that one
    squashed: str
    many: bool          # "all", or a plural: more than one is meant


def _said(name, kind=None) -> _Said:
    """In "front door lock" the kind is a lock, and the door is part of its name."""
    words = _words(name)
    head = next((word for word in reversed(words) if word in KINDS), None)
    asked = next((word for word in _words(kind) if word in KINDS), head)
    spoken = _spoken_words(f"{name or ''} {kind or ''}")
    many = any(w in EVERY or (_stem(w) in KINDS and _stem(w) != w) for w in spoken)
    return _Said(" ".join(str(name or "").casefold().split()), frozenset(words),
                 KINDS[asked] if asked else (), frozenset(words) - {head}, "".join(words), many)


def _is(entity: Entity, kinds) -> bool:
    return any(entity.domain == domain
               and (classes is None or entity.attrs.get("device_class") in classes)
               for domain, classes in kinds)


def _exactly(entity: Entity, said: _Said) -> bool:
    return said.raw == entity.id or (bool(said.squashed) and said.squashed == entity.squashed)


def _fits(entity: Entity, said: _Said) -> bool:
    """Whether a spoken name could mean this: every word is in its name or its area's, or it is
    the kind of thing that was said and the other words are."""
    known = entity.words | entity.where
    if _exactly(entity, said) or (said.words and said.words <= known):
        return True
    return bool(said.kinds) and _is(entity, said.kinds) and said.rest <= known


def _found(server: _Server, said: _Said) -> tuple[_Home, list[Entity]]:
    """What the name could mean. Before saying nothing has it, the list is fetched again, as
    something may have been added a moment ago."""
    for names_for in (NAMES_FOR_S, LOOK_AGAIN_S):
        home = _home(server, names_for)
        found = [entity for entity in home.entities if _fits(entity, said)]
        if found:
            break
    return home, found


def _listed(names: list[str]) -> str:
    more = len(names) - FEW
    names = names[:FEW] + ([f"{more} more"] if more > 0 else [])
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _nothing_called(home: _Home, said: _Said, domains) -> str:
    """Says so, and names a few that exist: the ones sharing a word first."""
    known = [entity for entity in home.entities if entity.domain in domains]
    if not known:
        return "Home Assistant has nothing here that I can work with."
    near = sorted(known, key=lambda e: (-len(said.words & (e.words | e.where)), _order(e)))
    return (f"I can't find anything called {shown(said.raw, MAX_NAME_CHARS)} in Home Assistant. "
            f"I can see {_listed([entity.name for entity in near])}.")


def _given(args: dict, *keys):
    """The first of these the model filled in. It also sends "", "none" and "null" for nothing."""
    for key in keys:
        value = args.get(key)
        if value is not None and not (isinstance(value, str) and value.strip().casefold() in BLANK):
            return value
    return None


def _verb(value) -> str | None:
    """on, off, toggle, open, close, lock or unlock, from whatever a small model sends."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    text = " ".join(str(value).casefold().split())
    if text.startswith(("arm", "disarm")):
        raise ValueError(NO_ALARMS)
    for said in (text, " ".join(VERB_NOISE.sub(" ", text).split())):
        for verb, ways in VERBS.items():
            if said in ways:
                return verb
    raise ValueError(f"I don't know how to do {shown(value, 20)}. {WHAT_TO_DO}")


def _degrees(value) -> float | None:
    """A temperature from 21, 21.5, "21", "21 degrees" or "21°C"."""
    if value is None:
        return None
    match = None if isinstance(value, bool) else re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match or not math.isfinite(number := float(match[0].replace(",", "."))):
        raise ValueError("Say the temperature as a number of degrees.")
    return number


@dataclass(frozen=True)
class _Want:
    verb: str | None
    level: object           # the brightness as it was sent: "brighter" needs the light's own
    colour: dict | None     # as colours.colour gives it
    degrees: float | None


def _want(args: dict) -> _Want:
    level = _given(args, "brightness")
    colours.brightness(level, 50)   # one that makes no sense is said before anything is asked
    return _Want(_verb(_given(args, "state", "action", "command", "on")), level,
                 colours.colour(_given(args, "color", "colour")),
                 _degrees(_given(args, "temperature")))


def _action(domain: str, want: _Want) -> str | None:
    """Which row of SERVICES the request means for this kind of thing, or None. A small model
    invents arguments, so a brightness sent with "on" for a switch is passed over."""
    offered = SERVICES.get(domain, {})
    if want.verb != "off" and want.degrees is not None and "temperature" in offered:
        return "temperature"
    looks = want.level is not None or want.colour is not None
    verb = want.verb or ("on" if domain in DONE_WORDS or (looks and domain == "light") else None)
    if domain == "lock":
        verb = LOCK_VERBS.get(verb, verb)
    return verb if verb in offered else None


def _a(word: str) -> str:
    return f"an {word}" if word[0] in "aeiou" else f"a {word}"


def _cannot(entity: Entity, want: _Want) -> str:
    kind = _kind(entity)
    if entity.domain == "alarm_control_panel":
        return NO_ALARMS
    if entity.domain not in SERVICES:
        able = "can only read" if entity.domain in READABLE else "can't control"
        return f"{entity.name} is {_a(kind)}, which I {able}."
    if want.verb is None and want.degrees is None and want.level is None and want.colour is None:
        return f"Say what to do with {entity.name}: {CHOICES.get(entity.domain, 'on or off')}."
    choice = CHOICES.get(entity.domain, "turn it on or off")
    return f"{entity.name} is {_a(kind)}. Say whether to {choice}."


def _preferences(said: _Said, want: _Want) -> list:
    """Narrowing several matches, most telling first. Each is used only if it leaves something."""
    prefer = []
    if want.degrees is not None:
        prefer.append(lambda e: e.domain == "climate")
    if want.level is not None or want.colour is not None:
        prefer.append(lambda e: e.domain == "light")
    if said.kinds:
        return [*prefer, lambda e: _exactly(e, said), lambda e: _is(e, said.kinds)]
    if want.verb in ON_OFF:
        # "Turn off the kitchen" means its lights, though the speaker there is named Kitchen.
        return [*prefer, lambda e: e.domain not in ROOM_NAMED, lambda e: _exactly(e, said),
                lambda e: e.domain == "light"]
    return [*prefer, lambda e: _exactly(e, said)]


def _group_alone(entities: list[Entity]) -> list[Entity]:
    """A group that covers every other match stands for them."""
    ids = {entity.id for entity in entities}
    for entity in entities:
        members = entity.attrs.get("entity_id")
        members = [m for m in members if isinstance(m, str)] if isinstance(members, list) else []
        if len(ids) > 1 and ids - {entity.id} <= set(members):
            return [entity]
    return entities


def _which(entities: list[Entity]) -> str:
    return (f"There are {len(entities)} that could be: "
            f"{_listed([entity.name for entity in entities])}. Say which one.")


def _targets(home: _Home, found: list[Entity], said: _Said, want: _Want) -> list[Entity]:
    """The entities to change, or a sentence saying why none. Several are only ever changed
    together when they are one kind of thing: it never guesses between a lock and a light."""
    if not found:
        fitting = {domain for domain in SERVICES if _action(domain, want)}
        raise ValueError(_nothing_called(home, said, fitting or set(SERVICES)))
    able = [entity for entity in found if _action(entity.domain, want)]
    if not able:
        raise ValueError(_cannot(found[0], want))
    for prefer in _preferences(said, want):
        able = [entity for entity in able if prefer(entity)] or able
    able = _group_alone(able)
    one_meant = not said.rest and not said.many     # "the lamp", with six lights
    if one_meant:
        able = [entity for entity in able if said.words <= entity.words] or able
    if len(able) > 1 and (one_meant or len({entity.domain for entity in able}) > 1):
        raise ValueError(_which(able))
    return able


def _fresh(server: _Server, entities: list[Entity],
           states_for: float) -> tuple[list[Entity], list[str]]:
    """The same entities as they are now. A few are asked about one by one. Past that, one list
    of everything costs less."""
    if len(entities) > MAX_SINGLE_READS:
        latest = {e.id: e for e in _home(server, NAMES_FOR_S, states_for).entities}
        return [latest[entity.id] for entity in entities if entity.id in latest], []
    areas = _home(server).areas
    now, problems = [], []
    for entity in entities:
        try:
            read = _entity(_call(server, f"/api/states/{entity.id}"), server.token, areas)
        except _Refused as e:
            if e.code != 404:
                raise
            read = None
        if read is None or read.id != entity.id:
            problems.append(f"{entity.name} is no longer in Home Assistant.")
        else:
            now.append(read)
    return now, problems


def _float(value) -> float | None:
    """A number from the server, which can send anything where one belongs."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value) -> str | None:
    """A number to say, with at most one decimal, or None if it isn't one."""
    number = _float(value)
    return None if number is None else f"{number:.1f}".removesuffix(".0")


def _percent(entity: Entity) -> int | None:
    level = _float(entity.attrs.get("brightness"))
    return None if level is None else max(1, min(100, round(level / 255 * 100)))


def _light_colour(entity: Entity) -> str:
    kelvin, pair = _float(entity.attrs.get("color_temp_kelvin")), entity.attrs.get("hs_color")
    if entity.attrs.get("color_mode") == "color_temp" and kelvin:
        return colours.describe({"color_temp": kelvin})
    pair = [_float(part) for part in pair] if isinstance(pair, list) and len(pair) == 2 else []
    if pair and None not in pair:
        return colours.describe({"hue": pair[0], "saturation": pair[1]})
    return ""


def _light(entity: Entity, unit: str) -> str:
    if entity.state != "on":
        return _plain(entity, unit)
    level, colour = _percent(entity), _light_colour(entity)
    return "is on" + (f" at {level} percent" if level else "") + (f", {colour}" if colour else "")


def _fan(entity: Entity, unit: str) -> str:
    speed = _float(entity.attrs.get("percentage"))
    if entity.state != "on":
        return _plain(entity, unit)
    return "is on" + (f" at {_number(speed)} percent" if speed else "")


def _cover(entity: Entity, unit: str) -> str:
    position = _float(entity.attrs.get("current_position"))
    if entity.state == "open" and position and 0 < position < 100:
        return f"is {_number(position)} percent open"
    return _plain(entity, unit)


def _climate(entity: Entity, unit: str) -> str:
    target, now = (_number(entity.attrs.get(key)) for key in ("temperature", "current_temperature"))
    if entity.state != "off" and target is not None:
        return f"is set to {target}{unit}" + (f", and it is {now} there now" if now else "")
    mode = _plain(entity, unit)
    if entity.state != "off" and mode.startswith("is "):
        mode = f"is in {mode.removeprefix('is ')} mode"
    return mode + (f", and it is {now}{unit} there" if now else "")


def _sensed(entity: Entity, _unit: str) -> str:
    for classes, (on, off) in SENSED:
        if entity.attrs.get("device_class") in classes:
            return on if entity.state == "on" else off
    return _plain(entity, _unit)


def _sensor(entity: Entity, _unit: str) -> str:
    number, unit = _number(entity.state), entity.attrs.get("unit_of_measurement")
    if number is None:
        return _plain(entity, _unit)
    words = UNITS.get(unit) or (shown(" ".join(unit.split()), 12) if isinstance(unit, str) else "")
    return f"is {number} {words}".rstrip()


def _plain(entity: Entity, _unit: str) -> str:
    """One of Home Assistant's own state words. Anything else is prose from somewhere, and a
    sensor can hold a message or a web page's title, so it is not read out."""
    if not STATE_WORD.fullmatch(entity.state):
        return "holds text, which I don't read out"
    return f"is {entity.state.replace('_', ' ')}"


DESCRIBE = {"light": _light, "fan": _fan, "cover": _cover, "climate": _climate,
            "binary_sensor": _sensed, "sensor": _sensor}


def _describe(entity: Entity, unit: str) -> str:
    """What to say after its name: "is on at 60 percent, warm white"."""
    if entity.state == "unavailable":
        return "isn't responding right now"
    if entity.domain == "scene":
        return "is a scene"
    if entity.domain == "script":
        return "is running" if entity.state == "on" else "isn't running"
    if entity.state == "unknown":
        return NOT_KNOWN
    return DESCRIBE.get(entity.domain, _plain)(entity, unit)


def _spoken(lines: list[tuple[str, str]]) -> str:
    """One string of sentences: given a list, the model reads it back as a numbered list. With
    too many to say, the ones at rest (off, closed, locked) are counted and the others said.
    Three or more all at rest the same way are one sentence."""
    alike = len(lines) > 2 and len({state for _name, state in lines}) == 1
    awake = [i for i, line in enumerate(lines) if not (alike and line[1] in RESTING)
             and (len(lines) <= MAX_SPOKEN or line[1] not in RESTING)]
    said = [lines[i] for i in awake[:MAX_SPOKEN]]
    left = [line[1] for i, line in enumerate(lines) if i not in awake[:MAX_SPOKEN]]
    text = " ".join(f"{name} {state}." for name, state in said)
    if not left:
        return text
    if len(set(left)) == 1 and left[0] in RESTING:
        are = left[0].replace("is ", "are ", 1).replace("isn't", "aren't", 1)
        if not said:
            return f"All {len(left)} {are}."
        other = f"The other one {left[0]}." if len(left) == 1 else f"The other {len(left)} {are}."
        return f"{text} {other}"
    return f"{text} And {len(left)} more.".strip()


def _result(key: str, lines: list[tuple[str, str]], problems: list[str]) -> dict:
    if not lines:
        raise ValueError(problems[0] if problems else "Home Assistant has nothing to say on that.")
    more = len(problems) - MAX_PROBLEMS
    if more > 0:
        problems = problems[:MAX_PROBLEMS] + [f"And {more} more had a problem."]
    return {key: _spoken(lines), "problems": problems} if problems else {key: _spoken(lines)}


@dataclass(frozen=True)
class _Step:
    entity: Entity
    service: str            # a row of SERVICES for the entity's domain
    data: dict              # typed values only
    done: object = None     # a check that it worked, given the entity as it is afterwards


def _near(reported, degrees: float) -> bool:
    """A thermostat that works in half degrees has still done what was asked."""
    now = _float(reported)
    return now is not None and abs(now - degrees) <= 0.5


def _in_range(entity: Entity, degrees: float, unit: str) -> float:
    low, high = RANGES.get(unit, (5, 35))
    its_low, its_high = (_float(entity.attrs.get(key)) for key in ("min_temp", "max_temp"))
    low, high = (low if its_low is None else its_low), (high if its_high is None else its_high)
    if not low <= degrees <= high:
        raise ValueError(f"{entity.name} can be set between {_number(low)} and "
                         f"{_number(high)}{unit}.")
    return round(degrees, 1)


def _light_data(entity: Entity, want: _Want) -> dict | None:
    """What to send a light with turn_on, or None when the brightness asked for is nothing."""
    modes = entity.attrs.get("supported_color_modes")
    known = isinstance(modes, list) and all(isinstance(mode, str) for mode in modes)
    modes = set(modes) if known else None
    now = _percent(entity) if entity.state == "on" else None
    level = colours.brightness(want.level, now or 50)
    if level == 0:
        return None
    data = {"brightness_pct": level} if level else {}
    if level and modes is not None and modes <= {"onoff"}:
        raise ValueError(f"{entity.name} can't be dimmed.")
    if want.colour:
        kelvin = want.colour.get("color_temp")
        if modes is not None and not modes & (COLOUR_MODES | ({"color_temp"} if kelvin else set())):
            raise ValueError(f"{entity.name} can't change colour.")
        data |= ({"color_temp_kelvin": kelvin} if kelvin
                 else {"hs_color": [want.colour["hue"], want.colour["saturation"]]})
    return data


def _plan(entity: Entity, want: _Want, unit: str) -> _Step:
    """The one service call that does what was asked to this entity, from typed values."""
    if entity.state == "unavailable":
        raise ValueError(f"{entity.name} isn't responding, so I can't change it. Check that it "
                         "has power and shows up in Home Assistant.")
    action, data = _action(entity.domain, want), {}
    if action == "temperature":
        degrees = _in_range(entity, want.degrees, unit)
        return _Step(entity, "set_temperature", {"temperature": degrees},
                     lambda now: _near(now.attrs.get("temperature"), degrees))
    if entity.domain == "light" and action == "on":
        data = _light_data(entity, want)
        action, data = ("off", {}) if data is None else ("on", data)
    service, states = SERVICES[entity.domain][action]
    return _Step(entity, service, data, states and (lambda now: now.state in states))


def _send(server: _Server, steps: list[_Step]) -> tuple[list[_Step], list[str]]:
    """Calls each service once, with every entity it is for. Returns the steps that were
    accepted and what was said about the others."""
    batches, sent, problems = {}, [], []
    for step in steps:
        key = (step.entity.domain, step.service, json.dumps(step.data, sort_keys=True))
        batches.setdefault(key, []).append(step)
    for (domain, service, _data), batch in batches.items():
        body = {**batch[0].data, "entity_id": [step.entity.id for step in batch]}
        try:
            _call(server, f"/api/services/{domain}/{service}", body, timeout=SERVICE_TIMEOUT_S)
            sent += batch
        except _Slow:
            raise ConnectionError(UNSURE) from None
        except _Refused as e:
            if e.code != 400 and e.code < 500:
                raise
            problems += [f"Home Assistant didn't accept that for {step.entity.name}."
                         for step in batch]
        finally:
            _states_changed(server)
    return sent, problems


def _after(server: _Server, steps: list[_Step],
           unit: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Each entity as it is now, read back from the server: what to say, and what didn't take."""
    waits = list(SETTLE_S) if len(steps) <= MAX_SINGLE_READS else []
    while True:
        latest, problems = _fresh(server, [step.entity for step in steps], 0)
        now = {entity.id: entity for entity in latest}
        behind = [step for step in steps if step.entity.id in now and step.done
                  and not step.done(now[step.entity.id])]
        if not behind or not waits:
            break
        time.sleep(waits.pop(0))
    lines = []
    for step in steps:
        entity = now.get(step.entity.id)
        if entity is None:
            continue
        said = _describe(entity, unit)
        if step in behind:
            problems.append(f"{entity.name} {said}, though Home Assistant took the request.")
        else:
            lines.append((entity.name, DONE_WORDS.get(entity.domain, said)))
    return lines, problems


def _carry_out(server: _Server, steps: list[_Step], unit: str, problems: list[str]) -> dict:
    sent, refused = _send(server, steps)
    lines, behind = _after(server, sent, unit) if sent else ([], [])
    return _result("now", lines, problems + refused + behind)


def _works_a_lock(word: str) -> bool:
    return word in GUARDED_WORDS or bool(JOINED_LOCK.search(word))


def _way_in(word: str) -> bool:
    return word in WAY_IN or word not in NOT_DOORS and bool(JOINED_WAY_IN.search(word))


def _guard_words(text: str) -> set:
    return set(_words(CAMEL_BREAK.sub(" ", text)))


def _named_guarded(text: str, whole_words: str | None = None) -> bool:
    """Whether a name says alarm, lock or a way in. A word the cut split can add that but never
    take it away, so a light or a fan only counts among the whole words, when they are given."""
    words = _guard_words(text)
    if any(map(_works_a_lock, words)):
        return True
    harmless = words if whole_words is None else _guard_words(whole_words)
    return any(map(_way_in, words)) and not harmless & HARMLESS


def _guarded(entity: Entity) -> bool:
    """A switch named for an alarm, a lock or a way in. The id counts as much as the name, and
    "GarageDoor" as much as "garage door". The name is judged as the card shows it, cut to 40
    characters, and up to MAX_JUDGED_CHARS, so "Shelly relay channel one outside wall garage door"
    counts; a card is shown if either says so."""
    if entity.domain not in GUARDED_DOMAINS:
        return False
    return (entity.guarded or _named_guarded(entity.name)
            or _named_guarded(entity.id.partition(".")[2]))


def _needs_click(step: _Step) -> bool:
    return (step.entity.domain, step.service) in ASKS or _guarded(step.entity)


def _card(server: _Server, steps: list[_Step], run) -> Ask:
    """Written from the entities and the service that will really be called."""
    step = next(s for s in steps if _needs_click(s))
    domain, service = step.entity.domain, step.service
    verb = ASKS.get((domain, service)) or GUARDED_VERBS[service]
    few = verb.split()[0] if (domain, service) in ASKS else verb
    title = f"{verb} {steps[0].entity.name}?" if len(steps) == 1 else (
        f"{few} {len(steps)} {KIND_PLURALS[domain]}?")
    detail = [f"{s.entity.name} ({s.entity.id})" for s in steps]
    detail += [f"Service: {domain}.{service}", f"Home Assistant at {server.label}"]
    if domain in UNSEEN:
        detail.append(UNSEEN[domain])
    elif (domain, service) not in ASKS:
        detail.append(GUARDED)
    return Ask(title, "\n".join(detail), run)


def set_state(args: dict, config, _session=None) -> dict | Ask:
    want = _want(args)
    said = _said(_given(args, *NAME_KEYS))
    if not said.words:
        raise ValueError(WHAT_TO_CHANGE)
    if said.kinds == KINDS["alarm"] and not said.rest:
        raise ValueError(NO_ALARMS)
    server = _connect(config)
    home, found = _found(server, said)
    if _session is not None:
        _session.tainted = True
    targets, problems = _fresh(server, _targets(home, found, said, want), STATES_FOR_S)
    steps = []
    for entity in targets:
        try:
            steps.append(_plan(entity, want, home.unit))
        except ValueError as e:
            problems.append(str(e))
    if not steps:
        raise ValueError(problems[0])

    def run() -> dict:
        return _carry_out(server, steps, home.unit, list(problems))

    if not any(map(_needs_click, steps)):
        return run()
    if len(steps) > MAX_ON_A_CARD:
        raise ValueError(f"That is {len(steps)} at once, which is too many to check on one card. "
                         "Say which one, or make a group of them in Home Assistant.")
    return _card(server, steps, run)


def status(args: dict, config, _session=None) -> dict:
    said = _said(_given(args, *NAME_KEYS), _given(args, "kind", "type", "domain"))
    if not said.words and not said.kinds:
        raise ValueError(WHAT_TO_CHECK)
    server = _connect(config)
    home, found = _found(server, said)
    if _session is not None:
        _session.tainted = True
    if not found:
        raise ValueError(_nothing_called(home, said, READABLE))
    readable = [entity for entity in found if entity.domain in READABLE]
    if not readable:
        raise ValueError(f"{found[0].name} is {_a(_kind(found[0]))}, which I don't read.")
    chosen = [entity for entity in readable if _is(entity, said.kinds)] or readable
    latest, problems = _fresh(server, chosen, STATES_FOR_S)
    return _result("status", [(e.name, _describe(e, home.unit)) for e in latest], problems)


def configured(config) -> bool:
    """Whether Settings holds an address and a token. Without them the two tools would only
    cost the model accuracy, so they needn't be offered."""
    return all(str(config[key] or "").strip() for key in ("ha_server", "ha_token"))


def check(config) -> str:
    """For Settings and doctor: one sentence saying the address and token work, or raising the
    sentence that says why not."""
    home = _home(_connect(config), 0)
    return f"Home Assistant answers, with {len(home.entities)} things in it."


HANDLERS = {"home.set": set_state, "home.status": status}
