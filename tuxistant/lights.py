"""Tapo smart lights on the local network, such as the L530.

Bulbs are found by a UDP broadcast, so an address that changes with DHCP doesn't matter. A bulb
saving power only wakes for some broadcasts, but the router holds packets sent straight to it, so
bulbs once found are remembered, asked directly, and kept until they stop answering. Tapo
bulbs speak KLAP over plain HTTP. In two handshakes each side proves it knows a hash of the
TP-Link account, and the bulb proves it first: a device that only pretends to be a bulb learns
nothing about the password. After that each request is AES-encrypted with keys from the handshake.

Only typed values reach a light: on or off, a number, a colour from a fixed list. So the skill
can't carry anything private off this computer, and needs no network permission.
"""
import base64
import colorsys
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import threading
import time

from .gate import shown

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:     # only the lights need it, so the rest of Tuxistant runs without it
    Cipher = None

DISCOVERY_PORT = 20002
DISCOVERY_QUERY = bytes.fromhex("020000010000000000000000463cb5d3")
DISCOVERY_S = 1.5
DISCOVERY_SENDS = 3         # spread over the wait: UDP gets lost, and a dozing bulb misses some
BROADCAST = "255.255.255.255"
REMEMBERED = "lights.json"  # beside the config, so a restart doesn't depend on a broadcast
FOUND_FOR_S = 600           # after this, broadcast again in case a light moved or was added
TIMEOUT_S = 4
SESSION_MARGIN_S = 1200     # the bulb's clock drifts, so a session is dropped well before it ends
MAX_NAME_CHARS = 40         # a name is whatever anyone on the network typed into the Tapo app
STEP = 25                   # "brighter" and "dimmer"
WARMEST, COOLEST = 2500, 6500

NO_CRYPTO = "The lights need the python3-cryptography package, and it isn't installed."
NO_LOGIN = "Add your TP-Link email and password in Settings, under Lights, so I can reach them."
WRONG_LOGIN = ("The TP-Link email or password in Settings doesn't match the one your light "
               "knows.")
NONE_FOUND = "I can't find any Tapo lights on this network."

# Saturation 100 unless it says otherwise. Tapo hues run 0 to 360.
COLOURS = {"red": (0, 100), "orange": (25, 100), "amber": (40, 100), "yellow": (55, 100),
           "lime": (90, 100), "green": (120, 100), "teal": (170, 100), "cyan": (185, 100),
           "turquoise": (175, 80), "sky blue": (200, 70), "blue": (240, 100),
           "indigo": (255, 100), "purple": (275, 100), "violet": (270, 70),
           "lavender": (270, 35), "magenta": (300, 100), "pink": (330, 55),
           "hot pink": (330, 80), "crimson": (348, 100)}
WHITES = {"candle": 2500, "warm white": 2700, "warm": 2700, "soft white": 2700, "yellow white": 3000,
          "white": 4000, "neutral white": 4000, "neutral": 4000, "natural": 4000, "normal": 4000,
          "cool white": 5500, "cool": 5500, "bright white": 5500, "cold": 6500,
          "cold white": 6500, "daylight": 6500, "blue white": 6500}
NOISE = re.compile(r"\b(?:colou?r|light|lights|bulb|the|a|to|in|please|ish|shade|of)\b")
BRIGHTER = {"brighter", "up", "more", "increase", "higher", "raise"}
DIMMER = {"dimmer", "dim", "down", "less", "decrease", "lower", "darker"}
LEVELS = {"full": 100, "max": 100, "maximum": 100, "brightest": 100, "half": 50,
          "min": 1, "minimum": 1, "lowest": 1, "dimmest": 1}
ON = {"on", "true", "yes", "1", "turn on", "switch on"}
OFF = {"off", "false", "no", "0", "turn off", "switch off"}
ALL = {"", "light", "lights", "the light", "the lights", "all", "all lights", "all the lights",
       "every light", "lamp", "bulb", "my light", "my lights", "none", "null", "default"}


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def auth_hash(email: str, password: str) -> bytes:
    """What KLAP version 2 proves knowledge of."""
    return _sha256(hashlib.sha1(email.encode()).digest() + hashlib.sha1(password.encode()).digest())


class Klap:
    """One encrypted session with one bulb. Requests are numbered, so they go one at a time."""

    def __init__(self, host: str, port: int, auth: bytes):
        self.host, self.port, self.auth = host, port, auth
        self.lock = threading.Lock()
        self.cookie = None
        self.expires = 0.0

    def _post(self, path: str, body: bytes, cookie: str | None = None):
        # http.client rather than urllib, which would honour a proxy from the environment.
        connection = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT_S)
        try:
            headers = {"Content-Type": "application/octet-stream"}
            if cookie:
                headers["Cookie"] = cookie
            connection.request("POST", path, body, headers)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Set-Cookie") or ""
        except (OSError, http.client.HTTPException) as e:
            raise ConnectionError(f"The light at {self.host} didn't answer.") from e
        finally:
            connection.close()

    def _handshake(self) -> None:
        local = secrets.token_bytes(16)
        status, reply, set_cookie = self._post("/app/handshake1", local)
        if status != 200 or len(reply) < 48:
            raise ConnectionError(f"The light at {self.host} refused to talk.")
        remote, proof = reply[:16], reply[16:48]
        if not secrets.compare_digest(proof, _sha256(local + remote + self.auth)):
            raise PermissionError(WRONG_LOGIN)
        fields = dict(part.strip().partition("=")[::2] for part in re.split(r"[;,]", set_cookie))
        if not fields.get("TP_SESSIONID"):
            raise ConnectionError(f"The light at {self.host} gave no session.")
        cookie = f"TP_SESSIONID={fields['TP_SESSIONID']}"
        status, _, _ = self._post("/app/handshake2", _sha256(remote + local + self.auth), cookie)
        if status != 200:
            raise PermissionError(WRONG_LOGIN)
        seeds = local + remote + self.auth
        self.key = _sha256(b"lsk" + seeds)[:16]
        iv = _sha256(b"iv" + seeds)
        self.iv, self.seq = iv[:12], int.from_bytes(iv[-4:], "big", signed=True)
        self.sig = _sha256(b"ldk" + seeds)[:28]
        timeout = fields.get("TIMEOUT", "")
        lasts = int(timeout) if timeout.isdigit() else 86400
        self.cookie, self.expires = cookie, time.monotonic() + lasts - SESSION_MARGIN_S

    def _cipher(self, seq: int):
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv + seq.to_bytes(4, "big", signed=True)))

    def _send(self, request: bytes):
        self.seq = (self.seq + 1 + 2 ** 31) % 2 ** 32 - 2 ** 31     # a signed 32-bit counter
        seq = self.seq
        padder = padding.PKCS7(128).padder()
        encryptor = self._cipher(seq).encryptor()
        sealed = encryptor.update(padder.update(request) + padder.finalize()) + encryptor.finalize()
        signature = _sha256(self.sig + seq.to_bytes(4, "big", signed=True) + sealed)
        status, reply, _ = self._post(f"/app/request?seq={seq}", signature + sealed, self.cookie)
        return status, seq, reply

    def _open(self, seq: int, reply: bytes) -> dict:
        # The first 32 bytes are a signature, which bulbs don't fill in the way requests are
        # signed, so it isn't checked. Without the key nothing can be made to decrypt as JSON.
        if len(reply) < 48 or (len(reply) - 32) % 16:
            raise ConnectionError(f"The light at {self.host} sent something I can't read.")
        decryptor = self._cipher(seq).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        try:
            plain = decryptor.update(reply[32:]) + decryptor.finalize()
            return json.loads(unpadder.update(plain) + unpadder.finalize())
        except ValueError as e:
            raise ConnectionError(f"The light at {self.host} sent something I can't read.") from e

    def call(self, method: str, params: dict | None = None) -> dict:
        body = {"method": method, "request_time_milis": int(time.time() * 1000)}
        if params is not None:
            body["params"] = params
        request = json.dumps(body).encode()
        with self.lock:
            for attempt in range(2):
                if not self.cookie or time.monotonic() > self.expires:
                    self._handshake()
                status, seq, reply = self._send(request)
                if status == 200:
                    break
                self.cookie = None      # the bulb forgot the session: start a new one, once
            else:
                raise ConnectionError(f"The light at {self.host} turned the request down.")
            answer = self._open(seq, reply)
        if not isinstance(answer, dict) or answer.get("error_code") != 0:
            raise ValueError("The light didn't accept that.")
        return answer.get("result") or {}


_found: dict[str, dict] = {}
_heard: set[str] = set()    # the bulbs that answered the last discovery
_found_at = 0.0
_sessions: dict[tuple, Klap] = {}
_state_lock = threading.Lock()


def _send_query(sock, targets) -> None:
    for target in targets:
        try:
            sock.sendto(DISCOVERY_QUERY, (target, DISCOVERY_PORT))
        except OSError:
            if target == BROADCAST:     # no network at all
                raise


def discover(wait: float = DISCOVERY_S, targets=(BROADCAST,)) -> dict[str, dict]:
    """Tapo bulbs that answer, by address. Nothing about them is trusted yet."""
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        start = time.monotonic()
        sends = [start + wait * i / DISCOVERY_SENDS for i in range(DISCOVERY_SENDS)]
        while (left := start + wait - time.monotonic()) > 0:
            if sends and time.monotonic() >= sends[0]:
                sends.pop(0)
                _send_query(sock, targets)
            sock.settimeout(max(0.01, min(left, sends[0] - time.monotonic()) if sends else left))
            try:
                data, (host, _port) = sock.recvfrom(4096)
                result = json.loads(data[16:])["result"]
                scheme = result.get("mgt_encrypt_schm") or {}
                port = int(scheme.get("http_port") or 80)
            except (OSError, ValueError, LookupError, TypeError, AttributeError):
                continue    # a timeout too: the loop sends again or ends
            if str(result.get("device_type", "")).startswith("SMART.TAPOBULB") and 0 < port < 65536:
                found[host] = {"host": host, "port": port, "klap": scheme.get("encrypt_type") == "KLAP",
                               "version": scheme.get("lv", 1),
                               "model": shown(result.get("device_model", "Tapo bulb"), 40)}
    return found


def _remembered_path(config):
    path = getattr(config, "path", None)    # tests pass a plain dict, so they never write here
    return path.parent / REMEMBERED if path else None


def _bulb_ok(host, bulb) -> bool:
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return (isinstance(bulb, dict) and bulb.get("host") == host
            and isinstance(bulb.get("port"), int) and 0 < bulb["port"] < 65536
            and isinstance(bulb.get("klap"), bool) and isinstance(bulb.get("version"), int)
            and isinstance(bulb.get("model"), str))


def _recall(config) -> dict[str, dict]:
    path = _remembered_path(config)
    try:
        saved = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {host: {**bulb, "model": shown(bulb["model"], 40)}
            for host, bulb in saved.items() if _bulb_ok(host, bulb)}


def _remember(config) -> None:
    path = _remembered_path(config)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_found, indent=2))
        tmp.replace(path)
    except OSError:
        pass        # only a shortcut: the broadcast still works without it


def _bulbs(config, fresh: bool = False) -> dict[str, dict]:
    global _found, _found_at, _heard
    with _state_lock:
        if not _found and not _found_at:
            _found = _recall(config)
        if fresh or not _found_at or time.monotonic() - _found_at > FOUND_FOR_S:
            try:
                answered = discover(targets=(BROADCAST, *_found))
            except OSError as e:
                if not _found:
                    raise ConnectionError(NONE_FOUND) from e
                answered = {}
            # A bulb that missed this one stays until talking to it fails.
            _found, _heard, _found_at = {**_found, **answered}, set(answered), time.monotonic()
            _remember(config)
        return dict(_found)


def _forget(config, host: str) -> None:
    with _state_lock:
        if _found.pop(host, None):
            _remember(config)


def _session(bulb: dict, auth: bytes) -> Klap:
    key = (bulb["host"], bulb["port"], auth)
    with _state_lock:
        if key not in _sessions:
            _sessions[key] = Klap(bulb["host"], bulb["port"], auth)
        return _sessions[key]


def _login(config) -> bytes:
    if Cipher is None:
        raise ValueError(NO_CRYPTO)
    email, password = config["tapo_email"].strip(), config["tapo_password"]
    if not email or not password:
        raise ValueError(NO_LOGIN)
    return auth_hash(email, password)


def _name(info: dict, bulb: dict) -> str:
    try:
        name = base64.b64decode(info.get("nickname", ""), validate=True).decode()
    except ValueError:
        name = ""
    return shown(" ".join(name.split()), MAX_NAME_CHARS) or bulb["model"]


def _connect(config) -> tuple[list[tuple[str, Klap, dict]], list[str]]:
    """(name, session, device info) for every light this account opens, and what went wrong
    with the others."""
    auth = _login(config)
    for fresh in (False, True):     # a light that stopped answering may just have moved
        bulbs = _bulbs(config, fresh)
        lights, problems = [], []
        for host, bulb in bulbs.items():
            if not bulb["klap"] or bulb["version"] != 2:
                problems.append(ConnectionError(
                    f"The {bulb['model']} at {bulb['host']} uses an older protocol I don't speak."))
                continue
            session = _session(bulb, auth)
            try:
                info = session.call("get_device_info")
            except ConnectionError as e:
                if host in _heard:
                    problems.append(e)
                else:       # silent to discovery and to HTTP: gone or moved, so not news
                    _forget(config, host)
                continue
            except (PermissionError, ValueError) as e:
                problems.append(e)
                continue
            lights.append((_name(info, bulb), session, info))
        if lights:
            return sorted(lights, key=lambda light: light[0].casefold()), [str(p) for p in problems]
    if not problems:
        raise ConnectionError(NONE_FOUND)
    raise next((p for p in problems if isinstance(p, PermissionError)), problems[0])


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def _choose(lights: list, wanted) -> list:
    """The lights a spoken name means. No name, or a word like "lights", means all of them."""
    wanted = " ".join(str(wanted or "").casefold().split())
    if wanted in ALL:
        return lights
    exact = [light for light in lights if light[0].casefold() == wanted]
    words = [w for w in _words(wanted) if w not in {"the", "my", "light", "lights", "lamp", "bulb"}]
    chosen = exact or [light for light in lights if words and set(words) <= set(_words(light[0]))]
    if chosen:
        return chosen
    if len(lights) == 1:    # the model invents names: with one light there's only one it can mean
        return lights
    names = [light[0] for light in lights]
    raise ValueError(f"No light is called {shown(wanted, MAX_NAME_CHARS)}. "
                     f"The lights are {', '.join(names[:-1])} and {names[-1]}.")


def _switch(value) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = " ".join(str(value).casefold().split())
    if text in ON:
        return True
    if text in OFF:
        return False
    raise ValueError("Say whether the light should be on or off.")


def _brightness(value, current: int) -> int | None:
    """1 to 100, or 0 for off. Numbers can come as 40, 0.4, "40%" or words."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Say a brightness from 1 to 100 percent.")
    if isinstance(value, (int, float)):
        level = value * 100 if isinstance(value, float) and 0 < value < 1 else value
    else:
        text = " ".join(str(value).casefold().replace("percent", "").replace("%", "").split())
        if text in BRIGHTER:
            return min(100, current + STEP)
        if text in DIMMER:
            return max(1, current - STEP)
        if text in LEVELS:
            return LEVELS[text]
        try:
            level = float(text)
        except ValueError:
            raise ValueError("Say a brightness from 1 to 100 percent.") from None
    return max(0, min(100, round(level)))


def _colour(value) -> dict | None:
    """Device settings for a colour name, a white, a temperature in kelvin, or #rrggbb."""
    if value is None or value == "":
        return None
    text = " ".join(str(value).casefold().replace("-", " ").split())
    if match := re.fullmatch(r"#?([0-9a-f]{6})", text):
        red, green, blue = (int(match[1][i:i + 2], 16) / 255 for i in (0, 2, 4))
        hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
        if saturation < 0.1:
            return {"color_temp": WHITES["white"]}
        return {"hue": round(hue * 360) % 360, "saturation": max(1, round(saturation * 100)),
                "color_temp": 0}
    if match := re.fullmatch(r"(\d{4,5}) ?(?:k|kelvin|kelvins)?", text):
        return {"color_temp": max(WARMEST, min(COOLEST, int(match[1])))}
    for name in (text, " ".join(NOISE.sub(" ", text).split())):
        if name in WHITES:
            return {"color_temp": WHITES[name]}
        if name in COLOURS:
            hue, saturation = COLOURS[name]
            return {"hue": hue, "saturation": saturation, "color_temp": 0}
    raise ValueError(f"I don't know the colour {shown(value, 40)}. Try a name like blue, "
                     "warm white or daylight.")


def _describe_colour(info: dict) -> str:
    if info.get("dynamic_light_effect_enable"):
        return "a light effect"
    temp = info.get("color_temp") or 0
    if temp:
        return "warm white" if temp < 3300 else "white" if temp < 5000 else "cool white"
    hue, saturation = info.get("hue", 0) % 360, info.get("saturation", 0)
    if saturation < 15:
        return "white"

    def distance(name):
        h, s = COLOURS[name]
        return min(abs(hue - h), 360 - abs(hue - h)) + abs(saturation - s) / 2

    return min(COLOURS, key=distance)


def _state(name: str, info: dict) -> dict:
    if not info.get("device_on"):
        return {"light": name, "on": False}
    return {"light": name, "on": True, "brightness": f"{info.get('brightness', 100)} percent",
            "color": _describe_colour(info)}


def _result(states: list[dict], problems: list[str]) -> dict:
    out = states[0] if len(states) == 1 else {"lights": states}
    if problems:
        out = {**out, "problems": problems}
    return out


def set_lights(args: dict, config, _session=None) -> dict:
    power = _switch(args.get("state", args.get("on")))
    colour = _colour(args.get("color", args.get("colour")))
    raw_level = args.get("brightness")
    if power is None and colour is None and raw_level in (None, ""):
        raise ValueError("Say whether to turn the light on or off, or a brightness or colour.")
    lights, problems = _connect(config)
    lights, problems = _choose(lights, args.get("light")), _unreached(problems, args)
    states = []
    for name, session, info in lights:
        level = _brightness(raw_level, info.get("brightness", 100))
        change = dict(colour or {})
        if level:
            change["brightness"] = level
        # A brightness or colour is something to see, so it turns the light on; 0 turns it off.
        on = False if level == 0 else power if power is not None else True
        change["device_on"] = on
        if not on:
            change = {"device_on": False}
        try:
            session.call("set_device_info", change)
            states.append(_state(name, session.call("get_device_info")))
        except (ConnectionError, PermissionError, ValueError) as e:
            problems.append(f"{name}: {e}")
    if not states:
        raise ValueError(problems[0])
    return _result(states, problems)


def status(args: dict, config, _session=None) -> dict:
    lights, problems = _connect(config)
    return _result([_state(name, info) for name, _session, info in _choose(lights, args.get("light"))],
                   _unreached(problems, args))


def _unreached(problems: list[str], args: dict) -> list[str]:
    """Lights that didn't answer have no name, so they're only news when all were meant."""
    return problems if " ".join(str(args.get("light") or "").casefold().split()) in ALL else []


def lights(config) -> list[dict]:
    """For Settings: the lights found and whether the account in Settings opens them."""
    try:
        return [{"name": name, "on": bool(info.get("device_on"))}
                for name, _session, info in _connect(config)[0]]
    except (ConnectionError, PermissionError, ValueError):
        return []


HANDLERS = {"lights.set": set_lights, "lights.status": status}
