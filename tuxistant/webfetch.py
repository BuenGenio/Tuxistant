"""Reading a web page, from the user's own computer.

A page address comes out of a search result, so anyone on the web can choose it. Left alone it
could make this machine talk to the router, the LocalAI server or something on localhost. So every
hop is checked, and the connection goes to the address that was checked rather than to the name:
looked up a second time, a name can answer differently.

What comes back is other people's text. clean() is what stands between it and a model.

A recipe's request goes through place() and send() instead: to a host its file names, which may
be a device at home but never this computer or the servers this install talks to.
"""
import codecs
import concurrent.futures
import html
import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from typing import NamedTuple

from .realtime import _urls

TOTAL_S = 8             # the whole fetch, however many redirects it takes
CONNECT_S = 4
MAX_REDIRECTS = 3
MAX_BYTES = 1 << 20     # more than this is cut off: the start of a page is what gets read
MAX_URL_CHARS = 2000
MAX_TEXT_CHARS = 6000   # a quarter of what the model can hold, with room for its reply
MIN_LINE_CHARS = 40     # shorter lines are menus and buttons
MIN_PAGE_CHARS = 200    # less than this is a page that needs a browser to show anything
SCHEMES = {"http": 80, "https": 443}
TEXT_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}
REDIRECTS = {301, 302, 303, 307, 308}
USER_AGENT = "Mozilla/5.0 (compatible; Tuxistant)"
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# A device in the house: private ranges, and Tailscale's or a carrier's shared range.
LOCAL_NETS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                              "100.64.0.0/10", "fc00::/7")))
# They look like a device at home but answer on this computer, or are a cloud's metadata service:
# Tailscale's own DNS and API, which tailscaled answers here, and AWS's IPv6 metadata address.
NOT_DEVICES = tuple(map(ipaddress.ip_network, ("100.100.100.100/32", "fd7a:115c:a1e0::53/128",
                                               "fd00:ec2::254/128")))
SYS_NET = "/sys/class/net"
ROUTES = "/proc/net/route"
ROUTES6 = "/proc/net/ipv6_route"
IFF_TAP = 0x0002
# Virtio headers. QEMU, Firecracker and crosvm turn them on for a virtual machine's tap; ZeroTier
# and OpenVPN don't on theirs today. A VPN may, for offloads, as tailscaled does on its tun.
IFF_VNET_HDR = 0x4000
OWN_S = 60              # how long the addresses of the install's own servers are kept
OWN_TRIES = 3           # lookups when the settings keep changing under one request
MAX_REPLY_BYTES = 64 * 1024
RECIPE_AGENT = "Tuxistant"
RECIPE_ACCEPT = "application/json, text/plain;q=0.9"
# A page names its own codec, and punycode took 101 seconds over 1 MB. So only what the web really
# uses, and everything else is read as UTF-8.
CHARSETS = re.compile(r"utf[-_]8|ascii|iso8859-\d+|cp125\d|koi8[-_][ru]|shift_jis|euc_jp|euc_kr|"
                      r"gb2312|gbk|gb18030|big5")

NOT_ALLOWED = "That address isn't one I'll read."
UNREADABLE = "That page couldn't be read."
HIDDEN_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
TAG = re.compile(r"<[^<>]*>")    # not [^>]: that goes quadratic on a page of a million "<"
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")
IGNORED_TAGS = {"script", "style", "noscript", "template", "svg", "head", "nav", "header",
                "footer", "aside", "form", "button", "select", "iframe", "object", "dialog"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
             "track", "wbr"}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "br", "tr", "td", "th", "table", "section", "article",
              "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt", "hr"}


class PageError(Exception):
    """This page won't be read. The message is a sentence, and it never repeats the address."""


class Target(NamedTuple):
    scheme: str
    host: str
    port: int
    path: str
    addresses: list[str]


class Page(NamedTuple):
    url: str        # where it ended up, after any redirects
    text: str


class Place(NamedTuple):
    """Where a recipe's request may go: the checked addresses, whether they are at home, and the
    names of the install's own servers they were checked against."""
    addresses: list[str]
    local: bool
    own: frozenset = frozenset()


class _Lookup(NamedTuple):
    host: str
    future: concurrent.futures.Future
    started: float


class Reply(NamedTuple):
    status: int
    kind: str
    data: bytes | None      # None: compressed, or more than MAX_REPLY_BYTES


def clean(text: str, limit: int) -> str:
    """Web text as plain words, for a model. No angle brackets survive: <|im_start|> would be a real
    control token to the chat template and <tool_call> a real call to the server's parser, and
    both can arrive escaped. Hidden characters go too, as they can carry what a reader can't see.
    The text is cut first, so a hostile page can't make this expensive."""
    text = TAG.sub(" ", html.unescape(text[:limit * 4])).replace("<", " ").replace(">", " ")
    text = " ".join("".join(" " if unicodedata.category(c) in HIDDEN_CATEGORIES else c
                            for c in text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0]


def public(address: str) -> bool:
    """Whether an address is somewhere on the open internet. is_global alone would pass multicast,
    fec0:: and addresses that only wrap a private one, so each is looked at."""
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.sixtofour or ip.teredo:
            return False
        if ip.ipv4_mapped:
            return public(str(ip.ipv4_mapped))
        if ip in NAT64:     # a translator would deliver this to the IPv4 address in its last bits
            return public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip.is_site_local:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback
                                 or ip.is_link_local or ip.is_unspecified or ip.is_private)


def _resolve(host: str, port: int) -> list[str]:
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):     # UnicodeError: a label IDNA can't encode
        raise PageError(UNREADABLE) from None
    return list(dict.fromkeys(entry[4][0] for entry in found))


def _name(host: str) -> str:
    """A host as the resolver looks it up: IDNA, as getaddrinfo encodes a name, lowercased and
    without a trailing dot. So ｖｏｉｃｅ.example, or voice.example with a soft hyphen in it, is
    voice.example. UnicodeError if IDNA can't encode it, and then nothing can look it up."""
    return host.encode("idna").decode("ascii").rstrip(".").lower()


def _request_name(host: str) -> str:
    """_name, for a host a page or a recipe names: one IDNA can't encode is refused."""
    try:
        return _name(host)
    except UnicodeError:
        raise PageError(NOT_ALLOWED) from None


def _same(address: str) -> str:
    """One spelling for an address, as ::ffff:8.8.8.8 is 8.8.8.8 and must match it."""
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return address
    return str(getattr(ip, "ipv4_mapped", None) or ip)


def _server_host(server: str) -> str | None:
    """The host of a server in the settings, as _name gives it, so it compares with a page's or a
    recipe's however either is spelled. None if there is none, or IDNA can't encode it: then no
    one can look it up, this install included."""
    try:
        host = urllib.parse.urlsplit(_urls(server)[0]).hostname
        return _name(host) if host else None
    except (ValueError, UnicodeError):      # config.json can hold anything
        return None


OWN_KEYS = ("server", "translate_server", "search_server", "ha_server")
_own_lock = threading.Lock()
# host: (until, addresses), only ever from a lookup that answered. A failure is never kept.
_own_addresses: dict[str, tuple[float, frozenset]] = {}
_own_pending: dict[str, _Lookup] = {}


def own_servers(config, seconds: float, since: float | None = None) -> tuple[set[str], set[str]]:
    """Names and addresses of the servers this install talks to. A page has no business reaching
    them, even if they are on the open internet. The names come from the settings every time,
    and are refused whatever their addresses. Each name is looked up in a thread of its own, all
    at once. What a lookup answers is kept for a minute, and kept on while the next lookup runs
    and when it fails, so a slow refresh never drops it. A failure isn't kept, as the name may
    answer a moment later, when the network comes up or the computer gets home. So a name with
    no answer kept is waited for, seconds at most, joining a lookup of it already running. If
    that one fails and began before this call, it may have been sent before the network came up,
    so a fresh one is started and waited for in the time left. since, if given, is when the
    caller's request began, having started lookups itself. TimeoutError if one is still running
    when the time is up, so the caller refuses rather than go on without its addresses. A name
    whose lookup started during the request and failed has no address to reach now: it is
    refused by name."""
    now = time.monotonic()
    hosts = _own_hosts(config)
    return _own_wait(hosts, _start_own_lookups(hosts), now + seconds,
                     now if since is None else since)


def start_own_lookups(config):
    """Starts what own_servers will need, without waiting for it."""
    _start_own_lookups(_own_hosts(config))


def _own_hosts(config) -> list[str]:
    return list(dict.fromkeys(host for key in OWN_KEYS
                              if config[key] and (host := _server_host(config[key]))))


def _own_names(config) -> set[str]:
    return set(_own_hosts(config))     # already as _name gives them


def _own_now(config, own, deadline: float, since: float | None = None) -> tuple[set[str], set[str]]:
    """own, if it was judged against every server the settings name now, or else the servers
    looked up again by deadline, since as for own_servers. Either way with every answer the
    servers have given since, which costs no wait. Settings change the config in place, at any
    moment, so this is asked just before each request is dialled. If they change again while it
    waits, it looks again, and gives up after a few tries with TimeoutError."""
    tries = 0
    while own is None or not _own_names(config) <= own[0]:
        if tries == OWN_TRIES:
            raise TimeoutError
        tries += 1
        own = own_servers(config, max(deadline - time.monotonic(), 0), since)
    return own[0], own[1] | _own_kept(config)


def _own_kept(config) -> set[str]:
    """What the servers the settings name have answered with, kept: no wait."""
    with _own_lock:
        kept = [_own_addresses.get(host) for host in _own_hosts(config)]
    return set().union(*(entry[1] for entry in kept if entry))


def _start_own_lookups(hosts) -> list[_Lookup]:
    """Starts a lookup of each name without a current answer, one per name at a time, and
    returns those of names with no answer kept at all: they must be waited for."""
    now, waits = time.monotonic(), []
    for host in hosts:
        with _own_lock:
            kept = _own_addresses.get(host)
            if kept and kept[1] and now < kept[0]:
                continue
            lookup = _own_pending.get(host)
            started = lookup is None
            if started:
                lookup = _own_pending[host] = _Lookup(host, concurrent.futures.Future(),
                                                      time.monotonic())
        if started:     # a daemon thread: a lookup can't be stopped, and mustn't hold up exit
            threading.Thread(target=_own_resolve, args=(lookup,), daemon=True).start()
        if not (kept and kept[1]):
            waits.append(lookup)
    return waits


def _own_wait(hosts, waits: list[_Lookup], deadline: float,
              since: float) -> tuple[set[str], set[str]]:
    """The names, and the addresses kept for them along with those of the lookups waited for.
    A joined lookup that began before since and failed is followed by a fresh one."""
    done, running = concurrent.futures.wait([w.future for w in waits],
                                            timeout=max(deadline - time.monotonic(), 0))
    if running:
        raise TimeoutError
    stale = [w.host for w in waits if w.started < since and not w.future.result()]
    found = set().union(*(w.future.result() for w in waits))
    if stale:
        found |= _own_wait(stale, _start_own_lookups(stale), deadline, since)[1]
    with _own_lock:
        kept = [_own_addresses.get(host) for host in hosts]
    return set(hosts), found.union(*(entry[1] for entry in kept if entry))


def _own_resolve(lookup: _Lookup):
    try:
        found = frozenset(map(_same, _resolve(lookup.host, 0)))
    except Exception:   # anything: a lookup that never ended would make every call wait for it
        found = frozenset()
    with _own_lock:     # stored before the waiters wake, so they find it
        if _own_pending.get(lookup.host) is lookup:
            del _own_pending[lookup.host]
            kept = _own_addresses.get(lookup.host)
            if found or (kept and kept[1]):
                _own_addresses[lookup.host] = (time.monotonic() + OWN_S, found or kept[1])
    lookup.future.set_result(found)


def forget_own_servers():
    with _own_lock:
        _own_addresses.clear()
        _own_pending.clear()


def _read_proc(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as file:
            return file.read()
    except OSError:
        return ""


def this_computer() -> set[str]:
    """What the kernel says this computer's interfaces hold. A port Docker or adb publishes
    answers on every one of them, not only on loopback."""
    found, last = set(), None
    for line in _read_proc("/proc/net/fib_trie").splitlines():
        if line.strip().startswith("|-- "):
            last = line.strip()[4:]
        elif "/32 host LOCAL" in line and last:
            found.add(last)
    for line in _read_proc("/proc/net/if_inet6").splitlines():
        packed = (line.split() or [""])[0]
        if len(packed) == 32:
            found.add(":".join(packed[i:i + 4] for i in range(0, 32, 4)))
    return {_same(address) for address in found if _is_address(address)}


def _sys(path: str) -> str:
    return _read_proc(path).strip()


def _devtype(path: str) -> str:
    """What the kernel calls the device, if anything: bridge, vlan, vxlan, wlan, wireguard and
    the like. A veth and a tap have none."""
    for line in _read_proc(os.path.join(path, "uevent")).splitlines():
        if line.startswith("DEVTYPE="):
            return line[8:].strip()
    return ""


def _guest_port(root: str, name: str, bridged: bool) -> bool:
    """A veth, whose other end is a container on this computer, or a virtual machine's tap. A
    physical card, a VLAN or bond stacked on one, a VXLAN or other tunnel, and a VPN aren't.
    A tap in a bridge is a virtual machine's. One on its own is taken for a VPN's, such as
    ZeroTier's or an OpenVPN tap, unless it carries virtio headers, as QEMU's, Firecracker's and
    crosvm's do. Those VPNs don't turn them on today, but one could, for offloads, as tailscaled
    does on its tun: a tap-mode VPN that did would be taken for a virtual machine, and what is
    reached through it refused, which fails closed."""
    path = os.path.join(root, name)
    if os.path.exists(os.path.join(path, "device")) or os.path.isdir(os.path.join(path, "bridge")):
        return False
    try:
        if any(entry.startswith("lower_") for entry in os.listdir(path)):
            return False
    except OSError:
        return False
    tun = _sys(os.path.join(path, "tun_flags"))
    if tun:
        try:
            flags = int(tun, 16)
        except ValueError:
            return False
        return bool(flags & IFF_TAP) and (bridged or bool(flags & IFF_VNET_HDR))
    if _devtype(path) or _sys(os.path.join(path, "type")) != "1":   # 1: Ethernet, not IP-only
        return False
    link = _sys(os.path.join(path, "iflink"))      # a veth's peer, in the container's namespace
    return link not in ("", "0") and link != _sys(os.path.join(path, "ifindex"))


def _guest_interfaces(root: str = SYS_NET) -> set[str]:
    """Docker's and Podman's bridges, libvirt's virbr0, LXD's and the like: a bridge none of whose
    ports is a physical card, and a veth or QEMU's tap on its own. A bridge with the house's card
    in it is the house."""
    try:
        names = os.listdir(root)
    except OSError:
        return set()
    found = set()
    for name in names:
        if os.path.isdir(os.path.join(root, name, "bridge")):
            try:
                ports = os.listdir(os.path.join(root, name, "brif"))
            except OSError:
                ports = []
            if all(_guest_port(root, port, True) for port in ports):
                found.add(name)
        elif _guest_port(root, name, False):
            found.add(name)
    return found


def _little_endian(hexed: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(bytes.fromhex(hexed)[::-1])


class Route(NamedTuple):
    net: ipaddress.IPv4Network | ipaddress.IPv6Network
    guest: bool     # straight onto a network of this computer's containers or virtual machines


def kernel_routes(root: str = SYS_NET, routes: str = ROUTES, routes6: str = ROUTES6) -> list:
    """The kernel's routes, each marked by whether it leads straight onto a container's or
    virtual machine's network. Their addresses look like the house's, but what answers there runs
    on this computer, and a container's port is reachable there even when Docker publishes it
    only on 127.0.0.1. IPv4 is the main table; IPv6 lists every table."""
    guests, found = _guest_interfaces(root), []
    for line in _read_proc(routes).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            net = ipaddress.IPv4Network(
                f"{_little_endian(fields[1])}/{_little_endian(fields[7])}", strict=False)
        except ValueError:
            continue
        found.append(Route(net, fields[0] in guests and fields[2] == "00000000"
                           and net.prefixlen > 0))
    for line in _read_proc(routes6).splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        packed = fields[0]
        try:
            net = ipaddress.IPv6Network(
                ":".join(packed[i:i + 4] for i in range(0, 32, 4)) + f"/{int(fields[1], 16)}",
                strict=False)
        except ValueError:
            continue
        found.append(Route(net, fields[9] in guests and set(fields[4]) == {"0"}
                           and net.prefixlen > 0))
    return found


def _guest(ip, routes: list) -> bool:
    """Whether the kernel would send to ip onto a guest network: its most specific route says.
    So a home network inside a wider range a Docker network was given elsewhere is the house.
    When equally specific routes disagree, it's a guest."""
    matching = [route for route in routes if route.net.version == ip.version and ip in route.net]
    if not matching:
        return False
    longest = max(route.net.prefixlen for route in matching)
    return any(route.guest for route in matching if route.net.prefixlen == longest)


def _not_a_device(address: str, routes: list) -> bool:
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return True
    return any(ip in net for net in NOT_DEVICES if net.version == ip.version) or _guest(ip, routes)


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _local(address: str) -> bool:
    """A device on the local network. An IPv6 form that wraps an IPv4 address isn't one, so a
    mapped or translated private address is refused rather than judged by what it wraps."""
    try:
        ip = ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return False
    return any(ip in net for net in LOCAL_NETS if net.version == ip.version)


def place(host: str, port: int, own: tuple[set[str], set[str]], addresses=None) -> Place:
    """Where a recipe may send: all on the open internet, or all in the house. Never this computer,
    its containers and virtual machines, the servers this install talks to, or a name that answers
    with a mix of the two. The host is looked up here unless its addresses are given."""
    names, own_addresses = own
    if _request_name(host) in names:
        raise PageError(NOT_ALLOWED)
    if addresses is None:
        addresses = _resolve(host, port)
    same = set(map(_same, addresses))
    if not addresses or same & own_addresses or same & this_computer():
        raise PageError(NOT_ALLOWED)
    routes = kernel_routes()
    if any(_not_a_device(address, routes) for address in same):
        raise PageError(NOT_ALLOWED)
    if all(map(public, addresses)):
        return Place(addresses, False, frozenset(names))
    if all(map(_local, addresses)):
        return Place(addresses, True, frozenset(names))
    raise PageError(NOT_ALLOWED)


def recipe_place(host: str, port: int, config, seconds: float) -> Place:
    """place(), with the install's own servers looked up alongside the host rather than before
    it, so a slow one can't use up the host's time. The host is judged once both are known, by
    the servers the settings name then. If an own server with no answer kept hasn't answered
    within seconds, TimeoutError: the call is refused rather than judged without its
    addresses."""
    since = time.monotonic()
    deadline = since + seconds
    hosts = _own_hosts(config)
    if _request_name(host) in hosts:
        raise PageError(NOT_ALLOWED)
    waits = _start_own_lookups(hosts)
    addresses = _resolve(host, port)
    own = _own_now(config, _own_wait(hosts, waits, deadline, since), deadline)
    return place(host, port, own, addresses)


def place_now(where: Place, host: str, port: int, config, seconds: float,
              since: float | None = None) -> Place:
    """where, judged again just before it is dialled, by the servers the settings name now and
    every address they have answered with since, as both can change while a card waits: another
    request may look up a server whose lookup had failed. After a card, since is when it was
    shown, and a server with no answer kept is looked up afresh and waited for, as for
    own_servers. TimeoutError if one hasn't answered within seconds."""
    deadline = time.monotonic() + seconds
    own = None if since is not None else (set(where.own), set())
    if own is None:
        own = own_servers(config, seconds, since)
    return place(host, port, _own_now(config, own, deadline), where.addresses)


def _path(parts) -> str:
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return f"{path}?{query}" if query else path


def _parse(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path), or PageError for anything but a plain http or https address."""
    # urlsplit quietly drops tabs and line breaks, which would change what was checked.
    if len(url) > MAX_URL_CHARS or any(c <= " " or c == "\x7f" for c in url):
        raise PageError(NOT_ALLOWED)
    try:
        parts = urllib.parse.urlsplit(url)
        host, port, path = parts.hostname, parts.port, _path(parts)
    except ValueError:      # a bad port, or brackets that don't hold an address
        raise PageError(NOT_ALLOWED) from None
    if parts.scheme not in SCHEMES or not host or parts.username is not None:
        raise PageError(NOT_ALLOWED)     # any "@" at all gives a username, even an empty one
    # As it will be looked up, so it is judged by the name the resolver sees, in any spelling.
    return parts.scheme, _request_name(host), port or SCHEMES[parts.scheme], path


def _judge(scheme: str, host: str, port: int, path: str, addresses: list[str],
           own: tuple[set[str], set[str]]) -> Target:
    names, own_addresses = own
    # All of them must pass: a name with one public address and one private one is an attack.
    if (host in names or not addresses or not all(map(public, addresses))
            or own_addresses.intersection(map(_same, addresses))):
        raise PageError(NOT_ALLOWED)
    return Target(scheme, host, port, path, addresses)


def check(url: str, own: tuple[set[str], set[str]]) -> Target:
    """Refuses anything but a plain address on the open internet, or returns where to connect."""
    scheme, host, port, path = _parse(url)
    return _judge(scheme, host, port, path, _resolve(host, port), own)


def _dial(address: str, port: int, seconds: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=seconds)


def _connect(target: Target, deadline: float) -> socket.socket:
    for address in target.addresses:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            return _dial(address, target.port, min(left, CONNECT_S))
        except OSError:
            continue
    raise PageError(UNREADABLE)


class _Plain(http.client.HTTPConnection):
    """Speaks to a socket that is already connected, so http.client looks up nothing itself."""

    def __init__(self, target: Target, raw: socket.socket):
        super().__init__(target.host, target.port)
        self.raw = raw

    def connect(self):
        self.sock = self.raw


class _Tls(http.client.HTTPSConnection):
    """The same over TLS. The certificate is checked against the name, not the address."""

    def __init__(self, target: Target, raw: socket.socket):
        self.tls = ssl.create_default_context()
        super().__init__(target.host, target.port, context=self.tls)
        self.raw = raw

    def connect(self):
        self.sock = self.tls.wrap_socket(self.raw, server_hostname=self.host)


def _hang_up(sock: socket.socket, late: threading.Event):
    late.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _cut(reply: http.client.HTTPResponse, data: bytes, limit: int) -> bool:
    """Whether the server stopped before the length it gave. http.client reads that as the end,
    and a price cut to its first digits is a wrong price said as if it were right."""
    return len(data) < limit and bool(reply.length)


def _decode(body: bytes, charset: str | None) -> str:
    try:
        name = codecs.lookup(charset or "utf-8").name
        return body.decode(name if CHARSETS.fullmatch(name) else "utf-8", "replace")
    except (LookupError, ValueError):   # a page can name any codec, including ones that raise
        return body.decode("utf-8", "replace")


def _read(reply: http.client.HTTPResponse) -> tuple[str, str]:
    """(type, text). Compression is refused rather than undone: nothing here asked for it, and a
    few kilobytes can unpack to gigabytes."""
    kind = (reply.getheader("Content-Type") or "").partition(";")[0].strip().lower()
    encoding = (reply.getheader("Content-Encoding") or "identity").strip().lower()
    if reply.status != 200 or kind not in TEXT_TYPES or encoding != "identity":
        raise PageError(UNREADABLE)
    body = reply.read(MAX_BYTES)
    if _cut(reply, body, MAX_BYTES):
        raise PageError(UNREADABLE)
    return kind, _decode(body, reply.headers.get_content_charset())


def _exchange(target: Target, method: str, body: bytes | None, headers: dict, deadline: float,
              read):
    """One request, with read(reply) run while the connection is open. A timeout on the socket
    only bounds each read, so a server dripping a byte at a time would go on for ever. Hanging up
    at the deadline ends it wherever it has got to, including the TLS handshake, which is why it
    holds its own handle. To http.client that hang-up looks like the end of the answer, so
    whatever was read by then is thrown away rather than taken as all of it."""
    raw = _connect(target, deadline)
    watch, late = raw.dup(), threading.Event()
    timer = threading.Timer(max(deadline - time.monotonic(), 0), _hang_up, [watch, late])
    timer.daemon = True
    connection = (_Tls if target.scheme == "https" else _Plain)(target, raw)
    raw.settimeout(CONNECT_S)
    timer.start()
    try:
        connection.request(method, target.path, body=body, headers=headers)
        answer = read(connection.getresponse())
        timer.cancel()
        if late.is_set():
            raise PageError(UNREADABLE)
        return answer
    except (OSError, ValueError, http.client.HTTPException):
        raise PageError(UNREADABLE) from None
    finally:
        timer.cancel()
        connection.close()
        raw.close()
        watch.close()


def _get(target: Target, deadline: float) -> tuple[str | None, str, str]:
    """One request: (redirect, type, text)."""
    def read(reply):
        if reply.status in REDIRECTS:
            return reply.getheader("Location") or "", "", ""
        return None, *_read(reply)
    return _exchange(target, "GET", None, {
        "User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}, deadline, read)


def _reply(reply: http.client.HTTPResponse) -> Reply:
    kind = (reply.getheader("Content-Type") or "").partition(";")[0].strip().lower()
    encoding = (reply.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding != "identity":      # nothing here asked for it, and it can unpack to gigabytes
        return Reply(reply.status, kind, None)
    data = reply.read(MAX_REPLY_BYTES + 1)
    if _cut(reply, data, MAX_REPLY_BYTES + 1):
        raise PageError(UNREADABLE)
    return Reply(reply.status, kind, data if len(data) <= MAX_REPLY_BYTES else None)


def send(where: Place, scheme: str, host: str, port: int, path: str, method: str,
         body: bytes | None = None, content_type: str | None = None,
         seconds: float = TOTAL_S) -> Reply:
    """A recipe's one request, to the addresses place() checked, or PageError if nothing answers.
    No redirect is followed and nothing but these headers goes, with the Host, Accept-Encoding:
    identity and, with a body, Content-Length that http.client adds: no cookie, key or
    referrer."""
    headers = {"User-Agent": RECIPE_AGENT, "Accept": RECIPE_ACCEPT}
    if body is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"
    target = Target(scheme, host, port, path, list(where.addresses))
    return _exchange(target, method, body, headers, time.monotonic() + seconds, _reply)


class _Text(HTMLParser):
    """The words a reader would see. Hidden text goes, which is the easy place to hide orders for
    a model, but this is a courtesy: what stops them is that the model reading has no tools."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = ""       # the tag that is being skipped, and how deep inside it this is
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            self.depth += tag == self.ignored
        elif tag in IGNORED_TAGS or self._hidden(dict(attrs)):
            if tag not in VOID_TAGS:
                self.ignored, self.depth = tag, 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == self.ignored:
            self.depth -= 1
            if not self.depth:
                self.ignored = ""
        elif not self.ignored and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)

    @staticmethod
    def _hidden(attrs: dict) -> bool:
        return ("hidden" in attrs or attrs.get("aria-hidden") == "true"
                or HIDDEN_STYLE.search((attrs.get("style") or "").lower()) is not None)


def _words(text: str, kind: str) -> str:
    if kind != "text/plain":
        parser = _Text()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError, RecursionError):    # its way of giving up on markup
            raise PageError(UNREADABLE) from None
        text = "".join(parser.parts)
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return clean(" ".join(line for line in lines if len(line) >= MIN_LINE_CHARS), MAX_TEXT_CHARS)


def fetch(url: str, config, seconds: float = TOTAL_S, own=None) -> Page:
    """Reads a page, or raises PageError. The request carries no cookies and no key: nothing in
    this module ever sees one. own is what own_servers gave the caller for this request. Without
    it, or when the settings have named a server since, the install's own servers are waited for
    within the page's seconds, and the page is refused while one that no lookup has answered yet
    is still being looked up."""
    deadline = time.monotonic() + seconds
    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path = _parse(url)
        if host in _own_names(config):
            raise PageError(NOT_ALLOWED)
        since = time.monotonic()
        if own is None:     # looked up alongside the page's host
            start_own_lookups(config)
        addresses = _resolve(host, port)
        try:    # after the host's lookup, so nothing waits between this and the dial
            own = _own_now(config, own, deadline, since)
        except TimeoutError:
            raise PageError(UNREADABLE) from None
        location, kind, text = _get(_judge(scheme, host, port, path, addresses, own), deadline)
        if location is None:
            words = _words(text, kind)
            if len(words) < MIN_PAGE_CHARS:
                raise PageError(UNREADABLE)
            return Page(url, words)
        try:
            url = urllib.parse.urljoin(url, location)
        except ValueError:      # its message quotes the address, and that's a stranger's text
            raise PageError(UNREADABLE) from None
    raise PageError(UNREADABLE)
