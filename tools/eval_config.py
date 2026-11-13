"""Keep the stored key away from a server nobody chose for this evaluation.

The key goes to a server named for this run, by TUXISTANT_SERVER or --server, or to the one in
Settings when that is not the default. The default is localhost:8080, and on a machine where
something else listens there, that something would be handed the key. So the default, and a
saved address on this computer, get it only when named for the run.

"On this computer" is judged with no name looked up: loopback and unspecified addresses in every
form the resolver takes (127.1, 2130706433, 0x7f000001, ::ffff:127.0.0.1), the localhost names
/etc/hosts gives by convention, this computer's own name with or without .local, which Debian and
Ubuntu map to 127.0.1.1 and mDNS answers, and the addresses its own network interfaces hold, as
the kernel lists them in /proc (LAN, docker0). Other names /etc/hosts maps to this computer are
not caught.
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


def _address(host: str) -> str | None:
    """One spelling of the address the resolver would read, or None for a name. inet_aton reads
    the short and number forms the way getaddrinfo will; ipaddress does not."""
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError):
        pass
    try:
        address = ipaddress.ip_address(host.partition("%")[0])
    except ValueError:
        return None
    return str(address.ipv4_mapped or address) if address.version == 6 else str(address)


def _own_names() -> set:
    name = socket.gethostname().rstrip(".").casefold()
    short = name.partition(".")[0]
    return {name, short, f"{short}.local"} - {"", ".local"}


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as file:
            return file.read()
    except OSError:
        return ""


def _interface_addresses() -> set:
    """What the kernel says this computer's interfaces hold. A port Docker publishes answers on
    every one of them, not only on loopback."""
    found, last = set(), None
    for line in _read("/proc/net/fib_trie").splitlines():
        if line.strip().startswith("|-- "):
            last = line.strip()[4:]
        elif "/32 host LOCAL" in line and last:
            found.add(last)
    for line in _read("/proc/net/if_inet6").splitlines():
        packed = (line.split() or [""])[0]
        if len(packed) == 32:
            found.add(":".join(packed[i:i + 4] for i in range(0, 32, 4)))
    return {_address(address) for address in found} - {None}


def on_this_computer(server: str) -> bool:
    """Whether the address is this computer, by how it is written and what the interfaces
    hold. No name is looked up."""
    host = _host(server).rstrip(".").casefold()
    if host in LOCAL_NAMES or host.endswith(".localhost") or host in _own_names():
        return True
    address = _address(host)
    if address is None:
        return False
    ip = ipaddress.ip_address(address)
    return ip.is_loopback or ip.is_unspecified or address in _interface_addresses()


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


class _RunOnly:
    """No saved settings: the key came with the run, so the server has to as well."""
    def __init__(self):
        self.values = {"server": DEFAULTS["server"], "api_key": ""}

    def __getitem__(self, key):
        return self.values[key]


def run_server(argv: list | None = None) -> str:
    """For a run handed its own key, like tests/shell_harness/e2e.sh: the server named for it,
    or Refused. Settings are never read, so the default is never taken."""
    return evaluation_server(_RunOnly(), argv)


if __name__ == "__main__":
    print(run_server())
