"""Command line: tuxistant daemon | toggle | cancel | say <text> | history | skills reload | doctor
| settings."""
import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import BUS_NAME, BUS_PATH, __version__, calendars, mail
from .config import Config
from .fnclient import SOCKET

EXTENSION = "tuxistant@buengenio"
FN_HELPER = "/usr/local/lib/tuxistant/tuxistant-fn"
# What the daemon imports, and the Debian and Ubuntu package each comes in.
MODULES = {"dbus_fast": "python3-dbus-fast", "websockets": "python3-websockets",
           "yaml": "python3-yaml", "gi": "python3-gi"}
SKILLS_LOAD = "skills load"


async def call(member: str, signature: str = "", body: tuple = ()) -> list:
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    bus = await MessageBus().connect()
    try:
        reply = await bus.call(Message(destination=BUS_NAME, path=BUS_PATH, interface=BUS_NAME,
                                       member=member, signature=signature, body=list(body)))
    finally:
        bus.disconnect()
    if reply.message_type == MessageType.ERROR:
        raise SystemExit(f"tuxistant: {reply.body[0] if reply.body else reply.error_name}")
    return reply.body


def show_history(limit: int) -> int:
    for row in reversed(json.loads(asyncio.run(call("GetHistory", "i", (limit,)))[0])):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
        ran = ", ".join(action["name"] for action in row["actions"])
        print(f"{when}  {row['status']:9}  {row['heard'] or '(nothing heard)'}")
        if ran:
            print(f"{'':28}ran: {ran}")
        if row["reply"]:
            print(f"{'':28}{row['reply']}")
    return 0


def reload_skills() -> int:
    problems = json.loads(asyncio.run(call("ReloadSkills"))[0])
    for where, problem in sorted(problems.items()):
        print(f"{where}: {problem}")
    if not problems:
        print("The skills are loaded, with no problems.")
    return 1 if problems else 0


def extension_installed() -> bool:
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return any((Path(root) / "gnome-shell/extensions" / EXTENSION / "metadata.json").is_file()
               for root in [data_home, *data_dirs.split(":")])


def shell_setting(key: str) -> str:
    try:
        return subprocess.run(["gsettings", "get", "org.gnome.shell", key], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def extension_enabled() -> bool:
    """On disk is not enough: this is what makes the shortcut, the blob and the icon exist."""
    return EXTENSION in shell_setting("enabled-extensions") and (
        "true" not in shell_setting("disable-user-extensions"))


def daemon_activatable() -> bool:
    """Whether a call can start the daemon. Not running is normal: it starts on demand."""
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return any((Path(root) / "dbus-1/services" / f"{BUS_NAME}.service").is_file()
               for root in [data_home, *data_dirs.split(":")])


def extension_can(method: str) -> tuple[bool, str]:
    """Asks the daemon what the extension told it. Asking must not start a daemon, and a daemon
    that isn't running is normal, so that says nothing either way."""
    try:
        done = subprocess.run(["busctl", "--user", "--auto-start=no", "call", BUS_NAME, BUS_PATH,
                               BUS_NAME, method], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True, "not checked"
    if done.returncode:
        return True, "not checked: the daemon isn't running"
    ready = done.stdout.strip() == "b true"
    return ready, "" if ready else "log out and back in to load the new extension"


def is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def missing_packages() -> list[str]:
    return [package for module, package in MODULES.items()
            if importlib.util.find_spec(module) is None]


def fn_helper(socket: str) -> tuple[bool, str]:
    """Holding Fn is optional, so no helper passes. One that is installed but not running fails."""
    if is_socket(socket):
        return True, socket
    if Path(os.environ.get("TUXISTANT_FN_HELPER", FN_HELPER)).exists():
        return False, f"installed, but nothing is listening on {socket}"
    return True, "not installed, so holding Fn does nothing"


def sandbox_ready() -> tuple[bool, str]:
    from . import skillbox
    if skillbox.available():
        return True, "bubblewrap"
    return False, "skills with code of their own need bubblewrap: install the bubblewrap package"


def skill_problems(config) -> dict[str, str]:
    """Read from the files here, so it starts no skill and needs no daemon."""
    from .skillhost import offline
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return offline(config, user=data_home / "tuxistant" / "skills").problems


def checks(config) -> list[tuple[bool, str, str]]:
    """(passed, what, detail) for each thing Tuxistant depends on."""
    missing = missing_packages()
    if missing:     # nothing else can be checked, or run, without them
        return [(False, "Python modules present",
                 "on Ubuntu or Debian: sudo apt install " + " ".join(missing))]
    from .realtime import AuthError, RealtimeError, list_models
    server, pipeline, tts = config["server"], config["pipeline"], config["tts_model"]
    reachable, accepted, detail, models = True, False, "", []
    try:
        models = list_models(config)
        accepted = True
    except AuthError as e:
        detail = str(e)
    except RealtimeError as e:
        reachable = accepted = False
        detail = "not checked: " + str(e)
    fn_running, fn_detail = fn_helper(os.environ.get("TUXISTANT_FN_SOCKET", SOCKET))
    tools = [t for t in ("pw-record", "pw-cat") if not shutil.which(t)]
    cards, cards_detail = extension_can("CanAsk")
    typing, typing_detail = extension_can("CanType")
    speech_to_text = config["transcribe_model"]
    sandbox, sandbox_detail = sandbox_ready()
    problems = skill_problems(config)
    return [
        (True, "Python modules present", ""),
        (reachable, "server reachable", server),
        (accepted, "API key accepted", detail),
        (pipeline in models, "pipeline present", pipeline),
        (tts in models, "voice model present", tts),
        (speech_to_text in models, "speech to text model present", speech_to_text),
        (not tools, "PipeWire tools present", "missing: " + ", ".join(tools) if tools else ""),
        (fn_running, "Fn helper running", fn_detail),
        # Optional, like the helper: without the bindings only the calendar is missing.
        (True, "calendar available", "" if calendars.available() else
         "not installed: the calendar needs evolution-data-server and gir1.2-ecal-2.0"),
        (True, "mail search available", "" if mail.available() else
         "no mail found: mail search reads the index Thunderbird keeps"),
        (extension_installed(), "GNOME extension installed", EXTENSION),
        (extension_enabled(), "GNOME extension enabled", EXTENSION),
        (daemon_activatable(), "daemon can be started", BUS_NAME),
        (cards, "approval cards ready", cards_detail),
        (typing, "dictation ready", typing_detail),
        (sandbox, "skill sandbox ready", sandbox_detail),
        (not problems, SKILLS_LOAD, f"{len(problems)} with a problem" if problems else ""),
    ]


def doctor() -> int:
    config = Config()
    results = checks(config)
    for passed, what, detail in results:
        print(f"{'ok' if passed else 'FAIL':5} {what}{'  ' + detail if detail else ''}")
        if not passed and what == SKILLS_LOAD:
            for where, problem in sorted(skill_problems(config).items()):
                print(f"{'':6}{where}: {problem}")
    return 0 if all(passed for passed, _, _ in results) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tuxistant", description="Voice assistant for Linux.")
    parser.add_argument("--version", action="version", version=f"tuxistant {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon", help="run the daemon")
    commands.add_parser("toggle", help="start listening, or cancel what is going on")
    commands.add_parser("cancel", help="stop everything")
    commands.add_parser("say", help="speak some text").add_argument("text", nargs="+")
    commands.add_parser("history", help="show recent requests").add_argument(
        "--limit", type=int, default=20)
    skills = commands.add_parser("skills", help="manage skills").add_subparsers(
        dest="skills_command", required=True)
    skills.add_parser("reload", help="load the skills again now and show any problems")
    commands.add_parser("doctor", help="check that everything is in place")
    commands.add_parser("settings", help="open the Settings window")
    args = parser.parse_args(argv)
    try:
        match args.command:
            case "daemon":
                from .service import run
                return asyncio.run(run())
            case "toggle":
                asyncio.run(call("Toggle"))
            case "cancel":
                asyncio.run(call("Cancel"))
            case "say":
                asyncio.run(call("Say", "s", (" ".join(args.text),)))
            case "history":
                return show_history(args.limit)
            case "skills":
                return reload_skills()
            case "doctor":
                return doctor()
            case "settings":
                from .settings_app import main as settings
                return settings()
    except ImportError as e:
        print(f"tuxistant: the Python module {e.name} is missing. Run tuxistant doctor.",
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:      # no session bus, or nothing answering on it
        print(f"tuxistant: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
