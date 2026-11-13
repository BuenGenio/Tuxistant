"""Run by test_apps.py with temporary XDG folders, a PATH of stand-ins and no session bus. It reads
the real Gio app list there and prints it as JSON on its last line. Nothing is launched.

It refuses to run anywhere that looks like somebody's real desktop.
"""
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import apps  # noqa: E402


def refuse_a_real_home() -> None:
    temp = Path(tempfile.gettempdir()).resolve()
    folders = [os.environ.get(name, "") for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME")]
    folders += os.environ.get("XDG_DATA_DIRS", "").split(":")
    if os.environ.get("TUX_APPS_ISOLATED") != "1" or not all(
            folder and temp in Path(folder).resolve().parents for folder in folders):
        sys.exit("not isolated: this must run from test_apps.py")


def no_launch(*_args, **_kwargs):
    sys.exit("something tried to launch an app")


def main() -> None:
    refuse_a_real_home()
    apps._launch = no_launch
    report = {"apps": [asdict(app) for app in apps._installed()],
              "https": apps._default_for("https")}
    print(json.dumps(report, default=sorted))


if __name__ == "__main__":
    main()
