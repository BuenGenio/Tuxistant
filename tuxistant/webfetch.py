"""Reading a web page, from the user's own computer.

A page address comes out of a search result, so anyone on the web can choose it. Left alone it
could make this machine talk to the router, the LocalAI server or something on localhost. So every
hop is checked, and the connection goes to the address that was checked rather than to the name:
looked up a second time, a name can answer differently.

What comes back is other people's text. clean() is what stands between it and a model.
"""
import html
import http.client
import ipaddress
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
    return host.rstrip(".").lower()


def _server_host(server: str) -> str | None:
    try:
        return urllib.parse.urlsplit(_urls(server)[0]).hostname
    except ValueError:      # config.json can hold anything
        return None


def own_servers(config) -> tuple[set[str], set[str]]:
    """Names and addresses of the servers this install talks to. A page has no business reaching
    them, even if they are on the open internet. A name that won't resolve is still refused."""
    names, addresses = set(), set()
    for key in ("server", "translate_server", "search_server"):
        host = _server_host(config[key]) if config[key] else None
        if host:
            names.add(_name(host))
            try:
                addresses.update(_resolve(host, 0))
            except PageError:
                pass
    return names, addresses


def _path(parts) -> str:
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return f"{path}?{query}" if query else path


def check(url: str, own: tuple[set[str], set[str]]) -> Target:
    """Refuses anything but a plain address on the open internet, or returns where to connect."""
    # urlsplit quietly drops tabs and line breaks, which would change what was checked.
    if len(url) > MAX_URL_CHARS or any(c <= " " or c == "\x7f" for c in url):
        raise PageError(NOT_ALLOWED)
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:      # a bad port, or brackets that don't hold an address
        raise PageError(NOT_ALLOWED) from None
    if parts.scheme not in SCHEMES or not host or parts.username is not None:
        raise PageError(NOT_ALLOWED)     # any "@" at all gives a username, even an empty one
    port = port or SCHEMES[parts.scheme]
    addresses = _resolve(host, port)
    names, own_addresses = own
    # All of them must pass: a name with one public address and one private one is an attack.
    if (_name(host) in names or not addresses or not all(map(public, addresses))
            or own_addresses.intersection(addresses)):
        raise PageError(NOT_ALLOWED)
    return Target(parts.scheme, host, port, _path(parts), addresses)


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


def _hang_up(sock: socket.socket):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", "replace")
    except (LookupError, ValueError):   # a page can name any codec, including ones that raise
        return body.decode("utf-8", "replace")


def _read(reply: http.client.HTTPResponse) -> tuple[str, str]:
    """(type, text). Compression is refused rather than undone: nothing here asked for it, and a
    few kilobytes can unpack to gigabytes."""
    kind = (reply.getheader("Content-Type") or "").partition(";")[0].strip().lower()
    encoding = (reply.getheader("Content-Encoding") or "identity").strip().lower()
    if reply.status != 200 or kind not in TEXT_TYPES or encoding != "identity":
        raise PageError(UNREADABLE)
    return kind, _decode(reply.read(MAX_BYTES), reply.headers.get_content_charset())


def _get(target: Target, deadline: float) -> tuple[str | None, str, str]:
    """One request: (redirect, type, text). A timeout on the socket only bounds each read, so a
    server dripping a byte at a time would go on for ever. Hanging up at the deadline ends it
    wherever it has got to, including the TLS handshake, which is why it holds its own handle."""
    raw = _connect(target, deadline)
    watch = raw.dup()
    timer = threading.Timer(max(deadline - time.monotonic(), 0), _hang_up, [watch])
    timer.daemon = True
    connection = (_Tls if target.scheme == "https" else _Plain)(target, raw)
    raw.settimeout(CONNECT_S)
    timer.start()
    try:
        connection.request("GET", target.path, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
        reply = connection.getresponse()
        if reply.status in REDIRECTS:
            return reply.getheader("Location") or "", "", ""
        return None, *_read(reply)
    except (OSError, ValueError, http.client.HTTPException):
        raise PageError(UNREADABLE) from None
    finally:
        timer.cancel()
        connection.close()
        raw.close()
        watch.close()


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


def fetch(url: str, config, seconds: float = TOTAL_S) -> Page:
    """Reads a page, or raises PageError. The request carries no cookies and no key: nothing in
    this module ever sees one."""
    deadline = time.monotonic() + seconds
    own = own_servers(config)
    for _ in range(MAX_REDIRECTS + 1):
        location, kind, text = _get(check(url, own), deadline)
        if location is None:
            words = _words(text, kind)
            if len(words) < MIN_PAGE_CHARS:
                raise PageError(UNREADABLE)
            return Page(url, words)
        url = urllib.parse.urljoin(url, location)
    raise PageError(UNREADABLE)
