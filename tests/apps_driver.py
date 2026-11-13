"""Run by test_apps.py with temporary XDG folders, a PATH of stand-ins and no session bus. It reads
the real Gio app list there and prints it as JSON on its last line. Nothing is launched.

It refuses to run unless HOME, XDG_DATA_HOME, XDG_DATA_DIRS, XDG_CONFIG_HOME and XDG_CONFIG_DIRS
are set to folders in the temp folder, as are XDG_CACHE_HOME, XDG_STATE_HOME and XDG_RUNTIME_DIR
when set, its session bus socket is in the temp folder and no display is set, so it can't reach
somebody's real desktop.
"""
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import apps  # noqa: E402


def in_temp(folder: str) -> bool:
    return bool(folder) and Path(tempfile.gettempdir()).resolve() in Path(folder).resolve().parents


def refuse_a_real_desktop() -> None:
    folders = [os.environ.get(name, "") for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME")]
    folders += [folder for name in ("XDG_DATA_DIRS", "XDG_CONFIG_DIRS")
                for folder in os.environ.get(name, "").split(":")]
    folders += [os.environ[name] for name in ("XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
                if name in os.environ]
    bus = re.fullmatch(r"unix:path=([^,;]+)", os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
    if (os.environ.get("TUX_APPS_ISOLATED") != "1" or not all(map(in_temp, folders))
            or not (bus and in_temp(bus[1]))
            or {"DISPLAY", "WAYLAND_DISPLAY"} & os.environ.keys()):
        sys.exit("not isolated: this must run from test_apps.py")


def no_launch(*_args, **_kwargs):
    sys.exit("something tried to launch an app")


def main() -> None:
    refuse_a_real_desktop()
    apps._launch = no_launch
    report = {"apps": [asdict(app) for app in apps._installed()],
              "https": apps._default_for("https")}
    print(json.dumps(report, default=sorted))


if __name__ == "__main__":
    main()
