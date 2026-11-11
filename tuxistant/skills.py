"""Skill runtime. A skill declares typed actions and the permissions it needs.

Risk is computed here from the permissions, never taken from the manifest, so an author cannot
label a dangerous skill as safe. execute() is the one way an action runs: it holds the gates, so
whatever asks for an action, the model or the planner, meets the same checks. Built-in handlers
are Python; recipe steps and sandboxed code skills plug into the same registry later.
"""
import asyncio
import inspect
import json
import shutil
import time
from pathlib import Path

import yaml

from . import calendars, files, lights, livedata, phone, planner, shellgate, system, translate
from .config import DEFAULTS
from .gate import Ask, Session, declined

BUILTIN = Path(__file__).resolve().parent / "skills_builtin"
RISK_ORDER = ["low", "medium", "high"]
HOME = "home"   # a network permission naming the server this install already talks to


def risk_of(permissions: dict) -> str:
    if permissions.get("shell") or permissions.get("files_write"):
        return "high"
    if any(permissions.get(p) for p in ("network", "files_read", "clipboard", "calendar_read",
                                        "calendar_write")):
        return "medium"
    return "low"


def reads_private(permissions: dict) -> bool:
    return any(permissions.get(p) for p in ("files_read", "shell", "clipboard", "calendar_read"))


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

# A handler takes (args, config, session) and returns a dict, or an Ask. One defined with async
# def runs on the event loop and may use the session's confirm, busy, selection and execute.
HANDLERS = {"time.now": _time_now, "disk.usage": _disk_usage,
            "weather.forecast": _plain(livedata.weather),
            "news.headlines": _plain(livedata.headlines),
            **calendars.HANDLERS, **files.HANDLERS, **lights.HANDLERS, **phone.HANDLERS,
            **shellgate.HANDLERS,
            **system.HANDLERS, **translate.HANDLERS, **planner.HANDLERS}


class Skills:
    def __init__(self, folder: Path = BUILTIN, config=None):
        self.config = DEFAULTS if config is None else config
        self.skills = {}
        self.actions = {}
        for file in sorted(folder.glob("*.yaml")):
            skill = yaml.safe_load(file.read_text())
            perms = skill.get("permissions") or {}
            skill["risk"] = risk_of(perms)
            self.skills[skill["id"]] = skill
            for name, action in skill["actions"].items():
                self.actions[name] = {**action, "skill": skill["id"], "risk": skill["risk"],
                                      "permissions": perms}
        self.tool_names = {name.replace(".", "_"): name for name in self.actions}

    def tools(self) -> list[dict]:
        """OpenAI Realtime tool definitions. Dots are not valid in function names."""
        return [{
            "type": "function", "name": name.replace(".", "_"),
            "description": action["description"],
            "parameters": {"type": "object", "properties": action.get("params") or {},
                           "required": action.get("required") or []},
        } for name, action in self.actions.items()]

    def action_name(self, tool_name: str) -> str:
        return self.tool_names.get(tool_name, tool_name)

    def run(self, tool_name: str, arguments: str, session: Session | None = None):
        """Calls a thread handler, which may answer with an Ask rather than a result."""
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        return HANDLERS[self.action_name(tool_name)](args, self.config, session or Session())

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
                where = "the internet" if hosts is True else ", ".join(map(str, hosts))
                if not await session.confirm(Ask(
                        f"Let {action['skill']} reach {where}?",
                        "This conversation has already read something private from this "
                        f"computer.\nAction: {name}\nArguments: {arguments}", dict)):
                    return name, declined()
            if inspect.iscoroutinefunction(HANDLERS.get(name)):
                args = json.loads(arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                outcome = await HANDLERS[name](args, self.config, session)
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
                 "actions": sorted(s["actions"])} for s in self.skills.values()]
