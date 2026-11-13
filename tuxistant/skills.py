"""Skill runtime. A skill declares typed actions and the permissions it needs.

Risk is computed here from the permissions, never taken from the manifest, so an author cannot
label a dangerous skill as safe. execute() is the one way an action runs: it holds the gates, so
whatever asks for an action, the model or the planner, meets the same checks. Built-in handlers
are Python; sandboxed code skills reach it through SkillHost as async handlers of their own.
"""
import asyncio
import inspect
import json
import shutil
import time
from pathlib import Path

import yaml

from . import (apps, calendars, files, homeassistant, knowledge, livedata, mail, media, phone,
               planner, research, shellgate, system, timers, translate, websearch)
from .config import DEFAULTS
from .gate import MAX_DETAIL_CHARS, Ask, Session, declined, shown

BUILTIN = Path(__file__).resolve().parent / "skills_builtin"
RISK_ORDER = ["low", "medium", "high"]
HOME = "home"   # a network permission naming the server this install already talks to


def risk_of(permissions: dict) -> str:
    if permissions.get("shell") or permissions.get("files_write"):
        return "high"
    if any(permissions.get(p) for p in ("network", "files_read", "clipboard", "calendar_read",
                                        "calendar_write", "mail_read", "home_read",
                                        "home_control")):
        return "medium"
    return "low"


def reads_private(permissions: dict) -> bool:
    return any(permissions.get(p) for p in ("files_read", "shell", "clipboard", "calendar_read",
                                            "mail_read", "home_read"))


def leaves_home(permissions: dict) -> bool:
    """Whether the action can reach anything but the user's own server."""
    network = permissions.get("network")
    return bool(network) and (network is True or any(host != HOME for host in network))


def _time_now(_args, _config, _session=None):
    return {"time": time.strftime("%H:%M"), "date": time.strftime("%A %d %B %Y"),
            "timezone": time.strftime("%Z")}


def _disk_usage(args, _config, _session=None):
    path = str(Path(args.get("path") or "~").expanduser())
    usage = shutil.disk_usage(path)
    gb = 1000 ** 3
    return {"path": path, "free_gb": round(usage.free / gb, 1), "used_gb": round(usage.used / gb, 1),
            "total_gb": round(usage.total / gb, 1)}


def _plain(handler):
    """For handlers that do not care which conversation they are in."""
    return lambda args, config, _session: handler(args, config)


# Which of a skill's declared hosts one call will reach, where a setting decides it. The card
# shown after private data was read names these, not every host the skill could have reached.
REACHES = {"news.headlines": livedata.news_hosts}
# Offered to the model only while its setting is on: every tool in the list costs the small model
# some of its other answers, and this one is off until asked for. The handler checks again.
SWITCHED = {"mail.search": "mail_search"}
# Offered only once Settings holds what it needs, for the same reason: on a computer with no Home
# Assistant the model's list is what it was without these, and Tapo's tools would take "the
# light" from Home Assistant without a bulb to show for it. Each is asked with the config. The
# action stays registered, and its handler says where to set things up if the model names it.
def _tapo_account(config) -> bool:
    return all(str(config[key] or "").strip() for key in ("tapo_email", "tapo_password"))


SET_UP = {"home.set": homeassistant.configured, "home.status": homeassistant.configured,
          "lights.set": _tapo_account, "lights.status": _tapo_account}

# A handler takes (args, config, session) and returns a dict, or an Ask. One defined with async
# def runs on the event loop and may use the session's confirm, busy, selection and execute.
HANDLERS = {"time.now": _time_now, "disk.usage": _disk_usage,
            "weather.forecast": _plain(livedata.weather),
            "news.headlines": _plain(livedata.headlines),
            **apps.HANDLERS, **calendars.HANDLERS, **files.HANDLERS, **mail.HANDLERS,
            **media.HANDLERS, **timers.HANDLERS, **homeassistant.HANDLERS, **phone.HANDLERS,
            **shellgate.HANDLERS, **system.HANDLERS, **translate.HANDLERS, **websearch.HANDLERS,
            **research.HANDLERS, **knowledge.HANDLERS, **planner.HANDLERS}


def _as_sent(arguments: str) -> str:
    """Arguments as parsed, which is what the handler gets: written twice, a key shows one value
    on a card and sends the other. What can't be parsed is shown as it came, and fails later."""
    try:
        return json.dumps(json.loads(arguments or "{}"), ensure_ascii=False)
    except ValueError:
        return arguments


def _reach_card(action: dict, name: str, arguments: str, hosts) -> Ask:
    """Written here from the real action. A line break in the arguments could fake a line of the
    card, so they show as escapes."""
    # A code skill's network is the whole of it, the bulbs at home included, not just the internet.
    where = "the internet and your local network" if hosts is True else ", ".join(map(str, hosts))
    whose = "\nIt is a skill you added." if action.get("source") == "yours" else ""
    return Ask(f"Let {shown(action['skill'])} reach {where}?",
               "This conversation has already read something private from this computer."
               f"{whose}\nAction: {shown(name)}\nArguments: {shown(arguments, MAX_DETAIL_CHARS)}",
               dict)


class Skills:
    """The skills one turn can use. Made once and never changed, so a reload can swap in a new one
    without a turn ever seeing half of each. A plain Skills(folder) reads YAML manifests whose
    handlers are all in HANDLERS; SkillHost builds one from manifests it has already checked."""

    def __init__(self, folder: Path = BUILTIN, config=None, manifests=None, handlers=None):
        self.config = DEFAULTS if config is None else config
        self.handlers = HANDLERS if handlers is None else handlers
        if manifests is None:
            manifests = [{**yaml.safe_load(file.read_text()), "source": "built-in"}
                         for file in sorted(folder.glob("*.yaml"))]
        self.skills = {}
        self.actions = {}
        for manifest in manifests:
            skill = dict(manifest)
            perms = skill.get("permissions") or {}
            skill["risk"] = risk_of(perms)
            skill.setdefault("source", "built-in")
            self.skills[skill["id"]] = skill
            for name, action in skill["actions"].items():
                self.actions[name] = {**action, "skill": skill["id"], "risk": skill["risk"],
                                      "permissions": perms, "source": skill["source"]}
        self.tool_names = {name.replace(".", "_"): name for name in self.actions}

    def tools(self) -> list[dict]:
        """OpenAI Realtime tool definitions. Dots are not valid in function names."""
        return [{
            "type": "function", "name": name.replace(".", "_"),
            "description": action["description"],
            "parameters": {"type": "object", "properties": action.get("params") or {},
                           "required": action.get("required") or []},
        } for name, action in self.actions.items() if self.offered(name)]

    def offered(self, name: str) -> bool:
        """Whether the model is told of this action. Settings lists its skill either way."""
        if name in SWITCHED and self.config[SWITCHED[name]] is not True:
            return False
        return name not in SET_UP or SET_UP[name](self.config) is True

    def action_name(self, tool_name: str) -> str:
        return self.tool_names.get(tool_name, tool_name)

    def run(self, tool_name: str, arguments: str, session: Session | None = None):
        """Calls a thread handler, which may answer with an Ask rather than a result."""
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        return self.handlers[self.action_name(tool_name)](args, self.config, session or Session())

    async def execute(self, tool_name: str, arguments: str, session: Session) -> tuple[str, dict]:
        """Returns (action name, result). Every failure is a result the model can speak.

        Two skills can do together what neither could alone: one reads your files and another
        sends them out. So once a conversation has read something private, nothing leaves the
        house without a click, whichever skill asks.
        """
        name = self.action_name(tool_name)
        action = self.actions.get(name)
        if action is None:
            return name, {"error": f"unknown action {tool_name}"}
        permissions = action["permissions"]
        try:
            if session.tainted and leaves_home(permissions):
                hosts = permissions["network"]
                if name in REACHES and hosts is not True:   # never more than was declared
                    hosts = [h for h in REACHES[name](self.config) if h in hosts] or hosts
                if not await session.confirm(_reach_card(action, name, _as_sent(arguments), hosts)):
                    return name, declined()
            if inspect.iscoroutinefunction(self.handlers.get(name)):
                args = json.loads(arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                outcome = await self.handlers[name](args, self.config, session)
            else:
                outcome = await asyncio.to_thread(self.run, tool_name, arguments, session)
            if isinstance(outcome, Ask):
                if not await session.confirm(outcome):
                    return name, declined()
                session.busy(outcome.timeout_s + 5)
                outcome = await asyncio.to_thread(outcome.run)
            if reads_private(permissions):
                session.tainted = True
            return name, outcome
        except Exception as e:
            return name, {"error": str(e) or type(e).__name__}

    def listing(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"], "description": s["description"],
                 "risk": s["risk"], "permissions": s.get("permissions") or {},
                 "actions": sorted(s["actions"]), "source": s["source"]}
                for s in self.skills.values()]
