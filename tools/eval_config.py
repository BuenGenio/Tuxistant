"""Keep evaluation credentials away from an accidental default server."""
import json
import os

from tuxistant.config import DEFAULTS, clean_server


def evaluation_server(config):
    override = os.environ.get("TUXISTANT_SERVER")
    if override:
        server = clean_server(override)
        if server is None:
            raise SystemExit("TUXISTANT_SERVER is not a valid server address. Nothing was sent.")
        config.values["server"] = server
    elif config["server"] == DEFAULTS["server"]:
        saved = json.loads(config.path.read_text()) if config.path.exists() else {}
        if not saved.get("server"):
            raise SystemExit("No model server is configured. Set TUXISTANT_SERVER for this "
                             "evaluation. Nothing was sent.")
