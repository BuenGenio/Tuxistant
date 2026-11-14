"""A pretend internet for tests: names come from a table and every connection goes to one
server on loopback, and this computer's addresses and routes are made up. Nothing here reaches a
real network or a real resolver, or reads this computer's /proc or /sys."""
import http.server
import socket
import threading
import time
from unittest import mock

from tuxistant import webfetch
from tuxistant.webfetch import PageError

PUBLIC = "93.184.216.34"
WORDS = "This sentence is long enough to count as a line of real page text. " * 6


class Quiet(http.server.ThreadingHTTPServer):
    def handle_error(self, *_):     # the guard hangs up on purpose
        pass


class Web:
    """Names from a table, every connection to one loopback server. requests holds (path,
    headers) for each request, and sent holds (method, path, headers, body)."""

    def __init__(self, test, names=None):
        self.names = {"example.com": [PUBLIC], "tuxserver": ["192.168.1.20"],
                      "translator.lan": ["192.168.1.157"], "searx.lan": ["192.168.1.158"],
                      **(names or {})}
        self.dialled, self.lookups, self.requests, self.sent = [], [], [], []
        self.routes = {}
        self.delay = 0.0
        web = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.answer(b"")

            def do_POST(self):
                self.answer(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

            def answer(self, body):
                headers = dict(self.headers.items())
                web.requests.append((self.path, headers))
                web.sent.append((self.command, self.path, headers, body))
                if web.delay:
                    time.sleep(web.delay)
                route = web.routes.get(self.path, (200, {"Content-Type": "text/html"}, WORDS))
                status, extra, reply = route
                reply = reply.encode() if isinstance(reply, str) else reply
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.http = Quiet(("127.0.0.1", 0), Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()
        test.addCleanup(self.close)
        self.guests = []    # the networks of this computer's containers, made up
        self.house = []     # and the house's, made up: a more specific one wins
        self.here = set()   # the addresses this computer holds, made up
        webfetch.forget_own_servers()
        test.addCleanup(webfetch.forget_own_servers)
        for name, new in (("_dial", self.dial), ("_resolve", self.resolve),
                          ("kernel_routes", self.kernel_routes),
                          ("this_computer", lambda: set(self.here))):
            patcher = mock.patch.object(webfetch, name, new)
            patcher.start()
            test.addCleanup(patcher.stop)

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def kernel_routes(self):
        return ([webfetch.Route(net, True) for net in self.guests]
                + [webfetch.Route(net, False) for net in self.house])

    def resolve(self, host, port):
        self.lookups.append(host)
        found = self.names.get(host.rstrip(".").lower())
        if found is None:
            raise PageError("no such name")
        return list(found.pop(0) if found and isinstance(found[0], list) else found)

    def dial(self, address, _port, seconds):
        self.dialled.append(address)
        return socket.create_connection(("127.0.0.1", self.port), timeout=seconds)

    def paths(self):
        return [path for path, _ in self.requests]


class OffThisComputer:
    """For a whole test module: what would read this computer's own /proc or /sys reads nothing
    and notes the path, and stop() fails if anything tried. A test that wants addresses or routes
    makes them up, as Web does. Set by hand rather than with mock, so a test's
    mock.patch.stopall() can't take it away."""

    def __init__(self):
        self.read, self.saved = [], {}

    def start(self):
        real_read, real_guests = webfetch._read_proc, webfetch._guest_interfaces

        def read_proc(path):
            return "" if self.real(path) else real_read(path)

        def guest_interfaces(root=webfetch.SYS_NET):
            return set() if self.real(root) else real_guests(root)
        for name, new in (("_read_proc", read_proc), ("_guest_interfaces", guest_interfaces)):
            self.saved[name] = getattr(webfetch, name)
            setattr(webfetch, name, new)

    def real(self, path) -> bool:
        if str(path).startswith(("/proc", "/sys")):
            self.read.append(str(path))
            return True
        return False

    def stop(self):
        for name, real in self.saved.items():
            setattr(webfetch, name, real)
        if self.read:
            raise AssertionError(f"read this computer's own {sorted(set(self.read))}")


def slow_server(test, steps) -> int:
    """A loopback server that reads one request and then sends each (wait, bytes) in turn, and
    hangs up at the end. Returns its port."""
    listener = socket.create_server(("127.0.0.1", 0))
    test.addCleanup(listener.close)
    stop = threading.Event()
    test.addCleanup(stop.set)

    def answer():
        try:
            sock, _ = listener.accept()
        except OSError:
            return
        with sock:
            sock.recv(4096)
            try:
                for wait, chunk in steps:
                    if stop.wait(wait):
                        return
                    sock.sendall(chunk)
            except OSError:
                pass
    threading.Thread(target=answer, daemon=True).start()
    return listener.getsockname()[1]
