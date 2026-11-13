"""Keep the stored key away from a server nobody chose for this evaluation.

The key goes to a server named for this run, by TUXISTANT_SERVER or --server, or to the one in
Settings when that is not the default. The default is localhost:8080, and on a machine where
something else listens there, that something would be handed the key. So the default, and any
address on this computer that wasn't named for the run, never gets it.
"""
import ipaddress
import os
import sys

from tuxistant.config import DEFAULTS, clean_server

ENV = "TUXISTANT_SERVER"
FLAG = "--server"
NOTHING_SENT = "Nothing was sent."


def _named(argv: list) -> str | None:
    """The server named for this run, taken out of argv so the tool's own arguments are left."""
    for i, word in enumerate(argv[1:], 1):
        if word == FLAG:
            del argv[i]
            return argv.pop(i) if i < len(argv) else ""     # no address is a bad one
        if word.startswith(FLAG + "="):
            return argv.pop(i).partition("=")[2]
    return os.environ.get(ENV) or None


def _host(server: str) -> str:
    rest = server.rpartition("://")[2]
    if rest.startswith("["):
        return rest[1:].partition("]")[0]
    return rest.rpartition(":")[0] if rest.count(":") == 1 else rest


def on_this_computer(server: str) -> bool:
    """Whether the address is this computer, by how it is written. Nothing is looked up."""
    host = _host(server).rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _refuse(config, why: str):
    config.values["api_key"] = ""   # whoever catches this has no key left to send
    raise SystemExit(f"{why} {NOTHING_SENT}")


def evaluation_server(config, argv: list | None = None) -> str:
    """The server this run may send the key to, set in config in memory only, or SystemExit."""
    named = _named(sys.argv if argv is None else argv)
    if named is not None:
        server = clean_server(named)
        if server is None:
            _refuse(config, "That is not a valid server address.")
        config.values["server"] = server
        return server
    saved = clean_server(str(config["server"] or ""))
    if saved is None or saved == DEFAULTS["server"]:
        _refuse(config, f"No model server is set up. Name one for this run with {ENV} "
                        f"or {FLAG}.")
    if on_this_computer(saved):
        _refuse(config, f"The saved server is on this computer, which gets the key only when "
                        f"named for this run. Use {ENV} or {FLAG}.")
    config.values["server"] = saved
    return saved
