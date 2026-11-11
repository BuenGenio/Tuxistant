"""The lights skill's process: serves lights.set and lights.status over MCP on stdin and stdout.

The daemon sends the TP-Link account in a tuxistant/config notification, and skillkit hands it to
each call. Errors are raised as plain sentences, which skillkit passes back to be spoken.
"""
import os
import sys
from pathlib import Path

# python3 -I leaves both the script's folder and PYTHONPATH off the path, so add them by hand.
sys.path[:0] = [str(Path(__file__).resolve().parent),
                *(p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p)]

import lights  # noqa: E402
from skillkit import Skill  # noqa: E402

skill = Skill("org.tuxistant.lights")
if Path("/state").is_dir():   # only inside the sandbox: remember bulbs across restarts
    lights.STATE_DIR = Path("/state")


@skill.action("lights.set")
def set_lights(args: dict, config: dict) -> dict:
    return lights.set_lights(args, config)


@skill.action("lights.status")
def status(args: dict, config: dict) -> dict:
    return lights.status(args, config)


if __name__ == "__main__":
    skill.run()
