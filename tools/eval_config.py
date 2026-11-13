"""Keep the stored key away from a server nobody chose for this evaluation.

The key goes to a server named for this run, by TUXISTANT_SERVER or --server, or to the one in
Settings when that is not the default. The default is localhost:8080, and on a machine where
something else listens there, that something would be handed the key. So the default, and any
address on this computer that wasn't named for the run, never gets it.

"On this computer" is judged by how the address is written, with nothing looked up: loopback and
unspecified addresses in every form the resolver takes (127.1, 2130706433, 0x7f000001,
::ffff:127.0.0.1), the localhost names /etc/hosts gives by convention, and this computer's own
name, which Debian and Ubuntu map to 127.0.1.1.
"""
import ipaddress
import os
import socket
import sys

from tuxistant.config import DEFAULTS, clean_server

ENV = "TUXISTANT_SERVER"
FLAG = "--server"
NOTHING_SENT = "Nothing was sent."
LOCAL_NAMES = {"localhost", "localhost.localdomain", "localhost4", "localhost6",
               "localhost4.localdomain4", "localhost6.localdomain6", "ip6-localhost",
               "ip6-loopback"}


class Refused(SystemExit):
    """Still ends a tool that doesn't catch it, and lets one that can go on without the server."""


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


def _local_ipv4(host: str) -> bool | None:
    """inet_aton reads the short and number forms the way getaddrinfo will; ipaddress does not."""
    try:
        packed = socket.inet_aton(host)
    except (OSError, ValueError):
        return None
    return packed[0] == 127 or packed == bytes(4)


def _local_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.partition("%")[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _own_names() -> set:
    name = socket.gethostname().rstrip(".").casefold()
    return {name, name.partition(".")[0]} - {""}


def on_this_computer(server: str) -> bool:
    """Whether the address is this computer, by how it is written. Nothing is looked up."""
    host = _host(server).rstrip(".").casefold()
    if host in LOCAL_NAMES or host.endswith(".localhost") or host in _own_names():
        return True
    ipv4 = _local_ipv4(host)
    return ipv4 if ipv4 is not None else _local_ip(host)


def _refuse(config, why: str):
    config.values["api_key"] = ""   # whoever catches this has no key left to send
    raise Refused(f"{why} {NOTHING_SENT}")


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
