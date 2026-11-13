"""Recipe skills: one YAML file in your skills folder, and no code.

A recipe can do three things, each built here: ask a host it names for something over HTTP, open
one installed app or Settings page, and say a sentence. It has no shell, files, clipboard, D-Bus or
headers, so nothing it holds can carry a key or reach the desktop's own services.

Everything a recipe reads is a stranger's text, so none of it goes back to the model: picked
values are cleaned, put into the author's sentence and said by the app, and the model is told
only that the app is answering. Cards are written here from the request that will really be sent,
and only the recipe's checked id comes from the manifest.
"""
import asyncio
import ipaddress
import json
import math
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import apps, webfetch
from .gate import APP_SAYS, MAX_DETAIL_CHARS, Ask, Speak, shown
from .skillhost import (HOST, MAX_ACTION_TEXT, MAX_MANIFEST_BYTES, PARAM, Unaliased, _bracketed,
                        _schema_ok, _text, action_description, action_name, load_yaml, skill_id)

FIELDS = {"id", "name", "description", "version", "permissions", "actions"}
ACTION_FIELDS = {"description", "params", "required", "get", "post", "open", "json", "text",
                 "pick", "say"}
PARAM_FIELDS = {"type", "description", "max", "min", "of", "default"}
MAX_ACTIONS = 4
MAX_PARAMS = 4
MAX_HOSTS = 4
MAX_PICKS = 4
MAX_PICK_DEPTH = 6
MAX_URL_CHARS = 500
MAX_BODY_CHARS = 2000
MAX_BODY_DEPTH = 4
MAX_SAY_CHARS = 300
MAX_PICK_CHARS = 80
MAX_TEXT_PARAM = 200
DEFAULT_TEXT_PARAM = 60
MAX_NUMBER = 10 ** 9
MAX_CHOICES = 20
SEND_S = 8
CARD_S = 15
AGAIN_S = 5             # a 4B often sends the same call twice in one reply
PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_]{0,39})\}")
CHOICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
APP_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")
INTEGER = re.compile(r"-?\d{1,12}")
HIDDEN = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
SCHEMES = {"https": 443, "http": 80}
JSON_TYPES = ("application/json",)

DONE = "Done."
NO_ANSWER = "{host} didn't answer."
SAID_NO = "{host} said no to that."
UNREADABLE = "{host} sent an answer I couldn't read."
NOT_THERE = "That recipe's host, {host}, isn't somewhere I'll send to."
UNENCRYPTED = ("{host} is on the internet, and that recipe would send to it without encryption, "
               "so I didn't.")
TOO_LONG = "That request is too long to show on a card, so I left it alone."
TOO_LONG_TO_SEND = "That's too long to send."


class ArgumentError(ValueError):
    """What the model sent can't be used. The message is a sentence for the model to say."""


# Loading

def load(file: Path) -> dict:
    """A recipe, checked. Raises ValueError with a reason fit to be spoken."""
    data = _read(file)
    unknown = set(data) - FIELDS
    if "risk" in unknown:
        raise ValueError("A recipe can't set its own risk. Tux works it out from the hosts it "
                         "names.")
    if "run" in unknown:
        raise ValueError("A recipe has no run line. A skill with code of its own goes in a "
                         "folder.")
    if unknown:
        raise ValueError("It has fields a recipe doesn't have: "
                         f"{shown(', '.join(sorted(map(str, unknown))), 100)}.")
    if isinstance(data.get("version"), float):
        raise ValueError("Put its version in quotes, like \"1.0\", or YAML reads it as a number.")
    hosts = _hosts(data.get("permissions"))
    actions = _actions(data.get("actions"), set(hosts))
    used = {action["plan"].host for action in actions.values() if action["plan"].host}
    if set(hosts) - used:
        raise ValueError(f"It names {', '.join(sorted(set(hosts) - used))} in its permissions, "
                         "but no action reaches it.")
    manifest_id = skill_id(data)
    for action in actions.values():
        action["plan"] = _owned(action["plan"], manifest_id)
    return {"id": manifest_id, "name": _text(data, "name"),
            "description": _text(data, "description"),
            "version": _text(data, "version", required=False),
            "permissions": {"network": hosts} if hosts else {}, "config": [],
            "actions": actions, "source": "yours", "recipe": True}


def _read(file: Path) -> dict:
    if file.is_symlink():
        raise ValueError(f"{file.name} is a link. A recipe has to be a real file.")
    try:
        if file.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError(f"{file.name} is too big.")
        data = load_yaml(file.read_text(), Unaliased)
    except FileNotFoundError:
        raise ValueError(f"{file.name} has gone.") from None
    except UnicodeDecodeError:
        raise ValueError(f"{file.name} is not text.") from None
    except yaml.YAMLError as e:     # its own text could quote the file, so only the line goes out
        mark = getattr(e, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark else ""
        raise ValueError(f"{file.name} is not valid YAML{where}.") from None
    except OSError as e:
        raise ValueError(f"{file.name} could not be read: {e.strerror or e}.") from None
    if not isinstance(data, dict):
        raise ValueError(f"{file.name} has to be a mapping of fields.")
    return data


def _hosts(permissions) -> list[str]:
    if permissions is None:
        return []
    if not isinstance(permissions, dict):
        raise ValueError("Its permissions have to be a mapping.")
    for key in permissions:
        if key != "network":
            raise ValueError(f"It asks for {shown(key, 40)}, and a recipe may only name the "
                             "hosts it reaches.")
    network = permissions.get("network")
    if network is True:
        raise ValueError("A recipe has to name the hosts it reaches.")
    if not network:
        return []
    if not isinstance(network, list) or len(network) > MAX_HOSTS:
        raise ValueError(f"Its network permission has to be a list of up to {MAX_HOSTS} hosts.")
    hosts = [_host(host) for host in network]
    if len(set(hosts)) != len(hosts):
        raise ValueError("It names the same host twice.")
    return hosts


def _host(host) -> str:
    if not isinstance(host, str) or len(host) > 253:
        raise ValueError("Its network permission has to be a list of host names.")
    host = host.rstrip(".").lower()
    if not HOST.fullmatch(host) or host == "home":
        raise ValueError(f"{shown(host, 60)} isn't a host name a recipe can use.")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("A recipe can't reach this computer.")
    _plain_address(host)
    return host


def _plain_address(host: str) -> None:
    """The resolver reads 127.1, 2130706433 and 0x7f000001 as addresses. Only the usual
    spelling is let through, and only for somewhere that isn't this computer, Tailscale's own
    address, which tailscaled answers on this computer, or the cloud's metadata service."""
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return
    if socket.inet_ntoa(packed) != host:
        raise ValueError(f"Write the address {shown(host, 60)} as four numbers, like "
                         "192.168.1.40.")
    ip = ipaddress.ip_address(host)
    if (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved or host in ("255.255.255.255", "100.100.100.100")):
        raise ValueError(f"A recipe can't reach {host}.")


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                   # "text", "number" or "choice"
    description: str = ""
    longest: int = DEFAULT_TEXT_PARAM
    low: int = 0
    high: int = 0
    options: tuple = ()
    default: object = None


@dataclass(frozen=True)
class Plan:
    """One action, as it was checked. Templates are split once, so a value is never read as a
    placeholder."""
    skill: str
    action: str
    params: tuple
    required: tuple
    effect: str                 # "get", "post", "open" or "say"
    scheme: str = ""
    host: str = ""
    port: int = 0
    path: tuple = ()
    query: tuple = ()           # (key, has "=", value parts)
    body_kind: str = ""         # "json", "text" or ""
    body: object = None
    picks: tuple = ()           # (name, path)
    say: tuple | None = None
    app: str = ""
    page: str = ""
    free_text: bool = False     # a text param goes into the request


def _owned(plan: Plan, owner: str) -> Plan:
    return Plan(**{**plan.__dict__, "skill": owner})


def _actions(actions, hosts: set) -> dict:
    if not isinstance(actions, dict) or not actions:
        raise ValueError("It declares no actions.")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"A recipe may have at most {MAX_ACTIONS} actions.")
    checked = {}
    for name, action in actions.items():
        action_name(name)
        if not isinstance(action, dict):
            raise ValueError(f"The action {name} has to be a mapping of fields.")
        unknown = set(action) - ACTION_FIELDS
        if unknown:
            raise ValueError(f"The action {name} has fields a recipe doesn't have: "
                             f"{shown(', '.join(sorted(map(str, unknown))), 100)}.")
        plan = _plan(name, action, hosts)
        params = {p.name: _schema(p) for p in plan.params}
        if not _schema_ok(params) or _bracketed(json.dumps(params)):
            raise ValueError(f"The params of {name} are too big or hold characters that can't "
                             "be shown.")
        checked[name] = {"description": action_description(name, action), "params": params,
                         "required": list(plan.required), "plan": plan}
    return checked


def _plan(name: str, action: dict, hosts: set) -> Plan:
    params = _params(name, action.get("params"))
    required = _required(name, action.get("required"), params)
    effect = _effect(name, action)
    by_name = {p.name: p for p in params}
    fields = {"skill": "", "action": name, "params": tuple(params), "required": required,
              "effect": effect}
    if effect in ("get", "post"):
        fields.update(_request(name, action, effect, hosts, by_name))
    elif effect == "open":
        if params:
            raise ValueError(f"The action {name} opens one thing, so it takes no params.")
        fields.update(_opened(name, action["open"]))
    picks = dict(fields.get("picks", ()))
    if "say" in action:
        fields["say"] = _template(name, "say line", action["say"], MAX_SAY_CHARS,
                                  set(by_name) | set(picks))
    _defaults_cover(name, fields, by_name, required)
    return Plan(**fields)


def _effect(name: str, action: dict) -> str:
    effects = [key for key in ("get", "post", "open") if key in action]
    if len(effects) > 1:
        raise ValueError(f"The action {name} has more than one of get, post and open.")
    if not effects and "say" not in action:
        raise ValueError(f"The action {name} does nothing. Give it one of get, post, open or "
                         "say.")
    effect = effects[0] if effects else "say"
    if effect != "post" and ("json" in action or "text" in action):
        raise ValueError(f"The action {name} has a body, which only post can send.")
    if effect not in ("get", "post") and "pick" in action:
        raise ValueError(f"The action {name} picks from an answer, which only get and post "
                         "have.")
    if effect == "open" and "say" in action:
        raise ValueError(f"The action {name} opens something and says something. Opening "
                         "already says what it opens.")
    return effect


def _params(name: str, params) -> list[Param]:
    if params is None:
        return []
    if not isinstance(params, dict):
        raise ValueError(f"The params of {name} have to be a mapping by name.")
    if len(params) > MAX_PARAMS:
        raise ValueError(f"The action {name} has more than {MAX_PARAMS} params.")
    return [_param(name, key, value) for key, value in params.items()]


def _param(action: str, name, spec) -> Param:
    if not isinstance(name, str) or not PARAM.fullmatch(name):
        raise ValueError(f"The params of {action} have to be a mapping by name.")
    where = f"The param {name} of {action}"
    if not isinstance(spec, dict) or set(spec) - PARAM_FIELDS:
        raise ValueError(f"{where} needs a type, and may have only description, max, min, of "
                         "and default.")
    kind = spec.get("type")
    description = _param_text(where, spec.get("description"), kind == "text")
    if kind == "text":
        param = _text_param(where, name, spec, description)
    elif kind == "number":
        param = _number_param(where, name, spec, description)
    elif kind == "choice":
        param = _choice_param(where, name, spec, description)
    else:
        raise ValueError(f"{where} has to be of type text, number or choice.")
    if "default" in spec:
        try:
            default = coerce(param, spec["default"])
        except ArgumentError:
            raise ValueError(f"{where} has a default that doesn't fit its type.") from None
        param = Param(**{**param.__dict__, "default": default})
    return param


def _param_text(where: str, text, required: bool) -> str:
    if text is None and not required:
        return ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where} needs a description.")
    text = " ".join(text.split())
    if len(text) > MAX_ACTION_TEXT or not text.isprintable() or _bracketed(text):
        raise ValueError(f"{where} has a description that is too long or holds characters "
                         "that can't be shown.")
    return text


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text_param(where: str, name: str, spec: dict, description: str) -> Param:
    if "min" in spec or "of" in spec:
        raise ValueError(f"{where} is text, so it takes only a max.")
    longest = spec.get("max", DEFAULT_TEXT_PARAM)
    if not _whole(longest) or not 1 <= longest <= MAX_TEXT_PARAM:
        raise ValueError(f"{where} needs a max from 1 to {MAX_TEXT_PARAM} characters.")
    return Param(name, "text", description, longest=longest)


def _number_param(where: str, name: str, spec: dict, description: str) -> Param:
    low, high = spec.get("min"), spec.get("max")
    if "of" in spec or not _whole(low) or not _whole(high) or not (
            -MAX_NUMBER <= low <= high <= MAX_NUMBER):
        raise ValueError(f"{where} needs a whole-number min and max, the min no bigger than the "
                         "max.")
    return Param(name, "number", description, low=low, high=high)


def _choice_param(where: str, name: str, spec: dict, description: str) -> Param:
    options = spec.get("of")
    if "min" in spec or "max" in spec or not isinstance(options, list) or not (
            1 <= len(options) <= MAX_CHOICES):
        raise ValueError(f"{where} needs a list of 1 to {MAX_CHOICES} choices in of.")
    for option in options:
        if isinstance(option, bool):    # YAML 1.1 reads on, off, yes and no as true and false
            raise ValueError(f"{where} has a choice YAML read as {str(option).lower()}. Put "
                             "each choice like on or yes in quotes.")
        if isinstance(option, (int, float)):
            raise ValueError(f"{where} has a choice YAML read as a number. Put each choice like "
                             "1 or 2.5 in quotes.")
        if not isinstance(option, str) or not CHOICE.fullmatch(option):
            raise ValueError(f"{where} has a choice that isn't a short word or two.")
    if len({option.casefold() for option in options}) != len(options):
        raise ValueError(f"{where} has the same choice twice.")
    return Param(name, "choice", description, options=tuple(options))


def _required(name: str, required, params: list[Param]) -> tuple:
    if required is None:
        return ()
    declared = {p.name: p for p in params}
    if not isinstance(required, list) or not all(
            isinstance(r, str) and r in declared for r in required):
        raise ValueError(f"The action {name} requires a parameter it doesn't declare.")
    if any(declared[r].default is not None for r in required):
        raise ValueError(f"The action {name} requires a parameter that has a default.")
    return tuple(dict.fromkeys(required))


def _schema(param: Param) -> dict:
    """What the model is told, made here: an author can't add a keyword to it."""
    if param.kind == "text":
        schema = {"type": "string", "maxLength": param.longest}
    elif param.kind == "number":
        schema = {"type": "integer", "minimum": param.low, "maximum": param.high}
    else:
        schema = {"type": "string", "enum": list(param.options)}
    return {**schema, "description": param.description} if param.description else schema


def _unquoted(value) -> str:
    """Unquoted, "{level}" is YAML for a mapping, {"level": None}: its name, or ""."""
    if isinstance(value, dict) and len(value) == 1:
        (key, item), = value.items()
        if isinstance(key, str) and item is None and PLACEHOLDER.fullmatch(f"{{{key}}}"):
            return key
    return ""


def _quote_it(name: str) -> str:
    return f'Put "{{{name}}}" in quotes, or YAML reads it as a mapping.'


def _template(action: str, what: str, text, limit: int, names: set) -> tuple:
    """Literal text and placeholder names, alternating. The only syntax is {name}."""
    if _unquoted(text):
        raise ValueError(f"Its {what} for {action} isn't text. {_quote_it(_unquoted(text))}")
    if not isinstance(text, str) or len(text) > limit:
        raise ValueError(f"Its {what} for {action} has to be text of at most {limit} "
                         "characters.")
    if not text.isprintable() or _bracketed(text):
        raise ValueError(f"Its {what} for {action} holds characters that can't be shown, or "
                         "< or >.")
    parts = tuple(PLACEHOLDER.split(text))
    for literal in parts[::2]:
        if "{" in literal or "}" in literal:
            raise ValueError(f"Its {what} for {action} has a {{ or }} that isn't a placeholder.")
    for placeholder in parts[1::2]:
        if placeholder not in names:
            raise ValueError(f"Its {what} for {action} uses {{{placeholder}}}, which it doesn't "
                             "declare.")
    return parts


def _names(parts: tuple) -> set:
    return set(parts[1::2])


def _request(name: str, action: dict, effect: str, hosts: set, params: dict) -> dict:
    fields = _url(name, action[effect], hosts, set(params))
    used = set().union(_names(fields["path"]), *(_names(v) for _, _, v in fields["query"]))
    if "json" in action and "text" in action:
        raise ValueError(f"The action {name} has both a json and a text body. Give it one.")
    if "json" in action:
        fields["body_kind"], fields["body"] = "json", _json(name, action["json"], set(params))
        used |= _json_names(fields["body"])
    elif "text" in action:
        fields["body_kind"] = "text"
        fields["body"] = _template(name, "text body", action["text"], MAX_BODY_CHARS,
                                   set(params))
        used |= _names(fields["body"])
    fields["picks"] = _picks(name, action.get("pick"), set(params))
    fields["free_text"] = any(params[p].kind == "text" for p in used)
    return fields


def _url(name: str, url, hosts: set, params: set) -> dict:
    where = f"The address of {name}"
    if not isinstance(url, str) or len(url) > MAX_URL_CHARS:
        raise ValueError(f"{where} has to be text of at most {MAX_URL_CHARS} characters.")
    if not url.isascii() or any(c <= " " or c == "\x7f" for c in url):
        raise ValueError(f"{where} has to be plain letters with no spaces. Put a placeholder "
                         "where a word goes.")
    scheme, found, rest = url.partition("://")
    if not found or scheme not in SCHEMES:
        raise ValueError(f"{where} has to start with https:// or http://.")
    if "#" in rest:
        raise ValueError(f"{where} has a #, which a recipe can't send.")
    authority = re.match(r"[^/?]*", rest).group()
    remainder = rest[len(authority):]
    if "{" in authority or "}" in authority or "@" in authority:
        raise ValueError(f"{where} has to name its host as it is, with no placeholder or @.")
    if remainder and not remainder.startswith("/"):
        raise ValueError(f"{where} needs a / after the host.")
    host, port = _authority(where, authority, SCHEMES[scheme])
    if host not in hosts:
        raise ValueError(f"{where} reaches {host}, which its permissions don't name.")
    path, _, query = (remainder or "/").partition("?")
    return {"scheme": scheme, "host": host, "port": port,
            "path": _template(name, "address", path, MAX_URL_CHARS, params),
            "query": _query(name, query, params)}


def _authority(where: str, authority: str, default: int) -> tuple[str, int]:
    host, colon, port = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if colon and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError(f"{where} has a port that isn't a number.")
    host = host.rstrip(".").lower()
    if not host or not HOST.fullmatch(host):
        raise ValueError(f"{where} has a host that isn't a plain name or address.")
    return host, int(port) if colon else default


def _query(name: str, query: str, params: set) -> tuple:
    pairs = []
    for pair in query.split("&") if query else ():
        key, equals, value = pair.partition("=")
        if "{" in key or "}" in key:
            raise ValueError(f"The address of {name} has a placeholder in a query name. Only "
                             "values may have one.")
        pairs.append((key, bool(equals), _template(name, "address", value, MAX_URL_CHARS,
                                                   params)))
    return tuple(pairs)


def _json(name: str, body, params: set):
    def walk(value, depth):
        if depth > MAX_BODY_DEPTH:
            raise ValueError(f"The json body of {name} is nested too deep.")
        if isinstance(value, dict):
            if _unquoted(value) in params:
                raise ValueError(f"The json body of {name} has {{{_unquoted(value)}}} as a "
                                 f"mapping. {_quote_it(_unquoted(value))}")
            for key in value:
                _json_key(name, key)
            return {key: walk(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item, depth + 1) for item in value]
        if isinstance(value, str):
            return _Slot(_template(name, "json body", value, MAX_BODY_CHARS, params))
        if value is None or isinstance(value, (bool, int)) or (
                isinstance(value, float) and math.isfinite(value)):
            return value
        raise ValueError(f"The json body of {name} holds something JSON can't carry.")
    if not isinstance(body, dict):
        raise ValueError(f"The json body of {name} has to be a mapping.")
    checked = walk(body, 1)
    if len(json.dumps(body, ensure_ascii=False)) > MAX_BODY_CHARS:
        raise ValueError(f"The json body of {name} is longer than {MAX_BODY_CHARS} characters.")
    return checked


def _json_key(name: str, key) -> None:
    if isinstance(key, bool):   # YAML 1.1 reads an unquoted on, off, yes or no as true or false
        raise ValueError(f"The json body of {name} has a key YAML read as "
                         f"{str(key).lower()}. Put keys like \"on\" in quotes.")
    if not isinstance(key, str) or "{" in key or "}" in key or _bracketed(key) or (
            not key.isprintable()):
        raise ValueError(f"The json body of {name} has a key that isn't plain text.")


@dataclass(frozen=True)
class _Slot:
    """A string in a json body. Exactly "{p}" becomes p's own value, so a number stays one."""
    parts: tuple


def _json_names(value) -> set:
    if isinstance(value, dict):
        return set().union(*map(_json_names, value.values()))
    if isinstance(value, list):
        return set().union(*map(_json_names, value))
    return _names(value.parts) if isinstance(value, _Slot) else set()


def _picks(name: str, picks, params: set) -> tuple:
    if picks is None:
        return ()
    if not isinstance(picks, dict) or len(picks) > MAX_PICKS:
        raise ValueError(f"The picks of {name} have to be a mapping of up to {MAX_PICKS}.")
    checked = []
    for key, path in picks.items():
        if not isinstance(key, str) or not PARAM.fullmatch(key):
            raise ValueError(f"The picks of {name} have to be named like params.")
        if key in params:
            raise ValueError(f"The pick {key} of {name} has the name of a param.")
        if not isinstance(path, list) or len(path) > MAX_PICK_DEPTH or not all(
                _whole(step) and step >= 0 or isinstance(step, str) and len(step) <= 100
                for step in path):
            raise ValueError(f"The pick {key} of {name} has to be a list of up to "
                             f"{MAX_PICK_DEPTH} names and numbers.")
        checked.append((key, tuple(path)))
    return tuple(checked)


def _opened(name: str, target) -> dict:
    if not isinstance(target, dict) or len(target) != 1:
        raise ValueError(f"The action {name} opens one app or one settings page.")
    if "app" in target:
        app = target["app"]
        if not isinstance(app, str) or not APP_ID.fullmatch(app):
            raise ValueError(f"The action {name} names an app id that isn't one.")
        return {"app": app}
    page = _page(target.get("settings"))
    if not page:
        raise ValueError(f"The action {name} names a settings page Tux doesn't know. Use its "
                         "name as Settings shows it, such as Sound, Bluetooth, Displays or "
                         "Accessibility.")
    return {"page": page}


def _page(name) -> str:
    """A Settings page by its title, a word app.open knows it by, or its file's name, in any
    case: appearance, Wi-Fi, universal-access. The file's name is what open_entry takes."""
    if not isinstance(name, str):
        return ""
    said = apps._squash(apps._words(name))
    for panel in apps.PANELS:
        stem = panel.desktop.removeprefix("gnome-").removesuffix("-panel.desktop")
        if said in {apps._squash(apps._words(n)) for n in (stem, panel.title, *panel.words)}:
            return stem
    return ""


def _defaults_cover(name: str, fields: dict, params: dict, required: tuple) -> None:
    used = set().union(_names(fields.get("path", ())),
                       *(_names(v) for _, _, v in fields.get("query", ())),
                       _json_names(fields.get("body")) if fields.get("body_kind") == "json"
                       else _names(fields.get("body") or ()),
                       _names(fields.get("say") or ()))
    for param in used & set(params):
        if param not in required and params[param].default is None:
            raise ValueError(f"The action {name} uses {param}, which is optional and has no "
                             "default.")


# Arguments

def _clean(text: str) -> str:
    text = "".join(" " if unicodedata.category(c) in HIDDEN or c in "<>" else c for c in text)
    return " ".join(text.split())


def _cut(text: str, longest: int) -> str:
    if len(text) <= longest:
        return text
    cut = text[:longest]
    if text[longest] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def coerce(param: Param, raw):
    """The model's value as the param's type, or ArgumentError with a sentence for it."""
    if param.kind == "text":
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ArgumentError(f"I need the {param.name} for that.")
        value = _cut(_clean(str(raw)), param.longest)
        if not value:
            raise ArgumentError(f"I need the {param.name} for that.")
        return value
    if param.kind == "number":
        return _number(param, raw)
    return _choice(param, raw)


def _number(param: Param, raw) -> int:
    value = None
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        digits = raw.replace(",", "").replace(" ", "").strip()
        value = int(digits) if INTEGER.fullmatch(digits) else None
    if value is None:
        raise ArgumentError(f"Say a number from {param.low} to {param.high}.")
    return min(max(value, param.low), param.high)


def _choice(param: Param, raw) -> str:
    said = " ".join(str(raw).split()).casefold().rstrip(".!?") if isinstance(
        raw, (str, int)) and not isinstance(raw, bool) else ""
    for option in param.options:
        if option.casefold() == said:
            return option
    options = list(param.options)
    listed = options[0] if len(options) == 1 else f"{', '.join(options[:-1])} or {options[-1]}"
    raise ArgumentError(f"Say one of: {listed}.")


def values(plan: Plan, args: dict) -> dict:
    """Every declared param the call has, typed. Keys the model made up are left out."""
    found = {}
    for param in plan.params:
        raw = args.get(param.name)
        if raw is None or raw == "":
            if param.default is not None:
                found[param.name] = param.default
            elif param.name in plan.required:
                raise ArgumentError(f"I need the {param.name} for that.")
            continue
        found[param.name] = coerce(param, raw)
    return found


# Requests

@dataclass(frozen=True)
class Request:
    method: str
    url: str
    scheme: str
    host: str
    port: int
    path: str               # with the query
    body: bytes | None
    content_type: str | None
    shown_body: str
    free_text: bool


def _filled(parts: tuple, found: dict, encode) -> str:
    return "".join(part if i % 2 == 0 else encode(_as_text(found[part]))
                   for i, part in enumerate(parts))


def _as_text(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return format(value, ".10g")
    return str(value)


def _segment(value: str) -> str:
    if value in (".", ".."):
        raise ArgumentError("That can't go in the address.")
    return urllib.parse.quote(value, safe="")


def build(plan: Plan, found: dict) -> Request:
    path = _filled(plan.path, found, _segment)
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ArgumentError("That can't go in the address.")
    query = "&".join(key + ("=" + _filled(value, found, lambda v: urllib.parse.quote_plus(
        v, safe="")) if equals else "") for key, equals, value in plan.query)
    target = f"{path}?{query}" if plan.query else path
    default = SCHEMES[plan.scheme]
    authority = plan.host if plan.port == default else f"{plan.host}:{plan.port}"
    url = f"{plan.scheme}://{authority}{target}"
    _same_place(plan, url)
    body, kind, shown_body = _body(plan, found)
    return Request(plan.effect.upper(), url, plan.scheme, plan.host, plan.port, target, body,
                   kind, shown_body, plan.free_text)


def _same_place(plan: Plan, url: str) -> None:
    """A backstop: the encoding already keeps a value from moving the request anywhere else."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or SCHEMES[parts.scheme]
    except (ValueError, KeyError):
        port = None
    if (parts.scheme, parts.hostname, port) != (plan.scheme, plan.host, plan.port) or any(
            c <= " " or c == "\x7f" for c in url) or parts.fragment:
        raise ArgumentError("That can't go in the address.")


def _body(plan: Plan, found: dict) -> tuple[bytes | None, str | None, str]:
    if plan.body_kind == "json":
        text = json.dumps(_json_filled(plan.body, found), ensure_ascii=False,
                          separators=(", ", ": "))
        kind = "application/json"
    elif plan.body_kind == "text":
        text, kind = _filled(plan.body, found, str), "text/plain; charset=utf-8"
    else:
        return None, None, ""
    if len(text) > MAX_BODY_CHARS:
        raise ArgumentError(TOO_LONG_TO_SEND)
    return text.encode(), kind, text


def _json_filled(value, found: dict):
    if isinstance(value, dict):
        return {key: _json_filled(item, found) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_filled(item, found) for item in value]
    if isinstance(value, _Slot):
        parts = value.parts
        if len(parts) == 3 and parts[0] == parts[2] == "":
            return found[parts[1]]
        return _filled(parts, found, str)
    return value


# Handlers

def handlers(manifest: dict) -> dict:
    return {name: _handler(action["plan"]) for name, action in manifest["actions"].items()}


def offline_handlers(manifest: dict) -> dict:
    """For tuxistant doctor and the evaluations: a recipe there sends and opens nothing."""
    refusal = {"error": f"{manifest['name']} is not running here."}

    def refuse():
        async def call(_args, _config, _session) -> dict:
            return dict(refusal)
        return call
    return {name: refuse() for name in manifest["actions"]}


def _handler(plan: Plan):
    async def call(args: dict, config, session):
        return await _call(plan, args, config, session)
    call.sends = lambda args, _config: _sends(plan, args)
    return call


def _sends(plan: Plan, args: dict) -> bool:
    """For Skills' reach card: whether the call would send anything. A copy of a request just
    answered doesn't, so it gets no second card, and nor do arguments the handler refuses. A
    second less than the handler's, so what this calls a copy is still one when it runs."""
    if plan.effect not in ("get", "post"):
        return False
    try:
        request = build(plan, values(plan, args))
    except ArgumentError:
        return False
    return _again(_key(plan, request), AGAIN_S - 1) is None


async def _call(plan: Plan, args: dict, config, session):
    try:
        found = values(plan, args)
        if plan.effect == "say":
            return _say(session, config, _sentence(plan.say, found, {}))
        if plan.effect == "open":
            return await asyncio.to_thread(apps.open_entry, plan.app, plan.page, config, session)
        request = build(plan, found)
    except ArgumentError as e:
        return {"error": str(e)}
    said = _again(_key(plan, request))
    if said is not None:
        return _echo(session, config, said)
    session.busy(CARD_S)    # a name can take seconds to look up, and nothing has been said yet
    # The install's own servers are looked up alongside the host, inside the same SEND_S: a slow
    # one, such as homeassistant.local away from home, can't use up the host's time, and one
    # that hasn't answered by then makes the host "not answer" rather than go unchecked.
    try:    # waited for from out here: nothing stops a name lookup from inside the thread
        where = await asyncio.wait_for(asyncio.to_thread(
            webfetch.recipe_place, request.host, request.port, config, SEND_S), SEND_S)
    except TimeoutError:
        return _say(session, config, NO_ANSWER.format(host=request.host))
    except webfetch.PageError as e:
        refused = NOT_THERE if str(e) == webfetch.NOT_ALLOWED else NO_ANSWER
        return _say(session, config, refused.format(host=request.host))
    if not where.local and request.scheme != "https":
        return _say(session, config, UNENCRYPTED.format(host=request.host))
    if not (request.method == "POST" or where.local or request.free_text):
        session.busy(CARD_S)
        return await asyncio.to_thread(_send_and_say, plan, request, where, found, config,
                                       session)
    detail = _card_detail(plan, request, where)
    if len(detail) > MAX_DETAIL_CHARS:      # a cut card would hide what it cut
        return _say(session, config, TOO_LONG)
    return Ask(_card_title(request), detail,
               lambda: _send_and_say(plan, request, where, found, config, session),
               timeout_s=CARD_S)


def _card_title(request: Request) -> str:
    host = shown(request.host, 253)
    return f"Send this to {host}?" if request.method == "POST" else f"Ask {host} for this?"


def _card_detail(plan: Plan, request: Request, where: webfetch.Place) -> str:
    addresses = ", ".join(where.addresses)
    named = addresses if [request.host] == where.addresses else f"{request.host} ({addresses})"
    place = (f"a device on your local network, {named}" if where.local
             else f"the internet, {named}")
    lines = [f"From the recipe {plan.skill}, which you added.", f"Where: {place}",
             f"Method: {request.method}", f"Address: {request.url}"]
    if request.body is not None:
        lines.append(f"Body: {request.shown_body}")
    return "\n".join(shown(line, 1 << 20) for line in lines)


def _send_and_say(plan: Plan, request: Request, where: webfetch.Place, found: dict, config,
                  session) -> dict:
    """In a thread, after any click: the request the card showed, to the addresses it showed."""
    try:
        reply = webfetch.send(where, request.scheme, request.host, request.port, request.path,
                              request.method, request.body, request.content_type, SEND_S)
    except webfetch.PageError:
        return _say(session, config, NO_ANSWER.format(host=request.host))
    if not 200 <= reply.status < 300:
        said = SAID_NO if reply.status >= 400 else NO_ANSWER
        return _say(session, config, said.format(host=request.host))
    said = _answer(plan, reply, found, request.host)
    _remember(_key(plan, request), said)
    return _say(session, config, said)


def _answer(plan: Plan, reply: webfetch.Reply, found: dict, host: str) -> str:
    picked = _picked(plan, reply)
    if picked is None:
        return UNREADABLE.format(host=host)
    if plan.say is None:
        return DONE
    return _sentence(plan.say, found, picked)


def _picked(plan: Plan, reply: webfetch.Reply) -> dict | None:
    """Each pick as words fit to say, or None if any can't be had. The answer is never parsed
    when nothing is picked from it."""
    if not plan.picks:
        return {}
    if reply.data is None:
        return None
    document = _document(reply, any(path for _, path in plan.picks))
    if document is None:
        return None
    picked = {}
    for name, path in plan.picks:
        value = _walk(document, path)
        if value is None:
            return None
        picked[name] = value
    return picked


def _document(reply: webfetch.Reply, deep: bool):
    try:
        text = reply.data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if reply.kind in JSON_TYPES or reply.kind.endswith("+json"):
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return None
    return text if reply.kind == "text/plain" and not deep else None


def _walk(document, path: tuple) -> str | None:
    node = document
    for step in path:
        if isinstance(step, str) and isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(step, int) and isinstance(node, list) and step < len(node):
            node = node[step]
        else:
            return None
    if isinstance(node, bool):
        return "yes" if node else "no"
    if isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
        return None
    if not isinstance(node, (str, int, float)):
        return None
    words = webfetch.clean(_as_text(node), MAX_PICK_CHARS)
    return words or None


def _sentence(parts: tuple, found: dict, picked: dict) -> str:
    said = "".join(part if i % 2 == 0 else
                   (picked[part] if part in picked else _clean(_as_text(found[part])))
                   for i, part in enumerate(parts))
    return _cut(" ".join(said.split()), MAX_SAY_CHARS)


def _say(session, config, text: str) -> dict:
    session.after.append(Speak(text, config["voice"]))
    return {APP_SAYS: True}


def _echo(session, config, text: str) -> dict:
    """A copy sends nothing, but a question asked again gets its answer again: once a reply."""
    if any(speak.text == text for speak in session.after):
        return {APP_SAYS: True}
    return _say(session, config, text)


# Only requests that were answered, with what was said, so a copy after a failure tries again.
_recent: dict[tuple, tuple[float, str]] = {}
_recent_lock = threading.Lock()


def _key(plan: Plan, request: Request) -> tuple:
    return plan.skill, plan.action, request.method, request.url, request.body


def _again(key: tuple, within: float = AGAIN_S) -> str | None:
    """What was said for this request if it was answered in the last few seconds."""
    with _recent_lock:
        at, said = _recent.get(key, (-math.inf, ""))
        return said if time.monotonic() - at < within else None


def _remember(key: tuple, said: str) -> None:
    with _recent_lock:
        now = time.monotonic()
        for old in [k for k, (at, _) in _recent.items() if now - at >= AGAIN_S]:
            del _recent[old]
        _recent[key] = (now, said)
