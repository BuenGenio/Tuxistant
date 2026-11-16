"""A made-up IMAP server on loopback, and a made-up GNOME Online Accounts on a private bus, for
tests of tuxistant/goamail.py.

The server keeps its mail in memory and writes down every command it is sent, so a test can see
what was asked for as well as what came back. It speaks enough IMAP for goamail and no more:
CAPABILITY, STARTTLS, AUTHENTICATE XOAUTH2, LIST, SELECT and EXAMINE, UID SEARCH, UID FETCH and
LOGOUT. Its certificate is made for each test run and trusted only where a test says so.

Online Accounts is three or four interfaces on a dbus-daemon of the test's own, with no service
files, so nothing on the real bus is ever started or asked.
"""
import asyncio
import base64
import email.message
import email.utils
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
TOKEN = "ya29.made-up-token-never-real"
USER = "someone@mailbox.example"


def certificate(folder: Path) -> tuple[Path, Path]:
    """(certificate, key) for 127.0.0.1, made now and good for a day."""
    cert, key = folder / "server.pem", folder / "server.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-subj", "/CN=127.0.0.1", "-days", "1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return cert, key


def message(subject="Hello", author="Someone <someone@people.example>", body="Hello.",
            html=None, dated=None, extra=()) -> bytes:
    """A mail as it would arrive, with its lines ended by CRLF as a real server sends them.
    html adds an HTML part, or is the only part when body is None. dated is the Date line its
    sender wrote, in days ago."""
    mail = email.message.EmailMessage()
    mail["From"] = author
    mail["To"] = USER
    mail["Subject"] = subject
    mail["Date"] = email.utils.format_datetime(
        datetime.now(timezone.utc) - timedelta(days=dated or 0))
    for name, value in extra:
        mail[name] = value
    if body is not None:
        mail.set_content(body)
    if html is not None:
        if body is None:
            mail.set_content(html, subtype="html")
        else:
            mail.add_alternative(html, subtype="html")
    return mail.as_bytes(policy=mail.policy.clone(linesep="\r\n"))


class Box:
    def __init__(self, name: str, flags: str = "", mails=()):
        self.name, self.flags, self.mails = name, flags, []
        for raw, days_ago in mails:
            self.add(raw, days_ago)

    def add(self, raw: bytes, days_ago: float = 1) -> None:
        """days_ago is when the server took it in: its INTERNALDATE."""
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        self.mails.append((len(self.mails) + 1, raw, when))


def _internal(when: datetime) -> str:
    return f"{when.day:02d}-{MONTHS[when.month - 1]}-{when.year} {when:%H:%M:%S} +0000"


def _text_of(raw: bytes, head: bool = True) -> str:
    """What a real server searches: the header lines, unless it is asked for the BODY only, and
    the decoded text of each part."""
    parsed = email.message_from_bytes(raw)
    words = [raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0].decode("utf-8", "replace")
             if head else ""]
    for part in parsed.walk():
        if not part.is_multipart():
            data = part.get_payload(decode=True) or b""
            try:
                words.append(data.decode(part.get_content_charset() or "utf-8", "replace"))
            except LookupError:
                words.append(data.decode("utf-8", "replace"))
    return "\n".join(words)


class Server:
    """mode is "ssl" (TLS from the start), "starttls", or "plain" (offers no STARTTLS)."""

    def __init__(self, cert: Path, key: Path, boxes: list[Box], mode="ssl", whole_words=False):
        self.boxes, self.mode, self.whole_words = {box.name: box for box in boxes}, mode, whole_words
        self.commands, self.signed_in_with, self.in_clear = [], [], []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.huge = False           # answer a FETCH with a literal far bigger than was asked for
        self.fat = 0                # pad every literal to this many bytes, whatever was asked for
        self.stall = 0              # seconds to wait before answering a SEARCH
        self.stall_from = 0         # and before answering one for a sender's mail
        self.drip = False           # answer a SEARCH a byte at a time, never ending the line
        self.inject = b""           # sent in the clear straight after STARTTLS's OK
        self.inject_before = b""    # and straight before it
        self.yes = "OK begin TLS"   # STARTTLS's yes, after its tag
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.threads = []
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self) -> None:
        self.listener.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _serve(self, conn) -> None:
        session = None
        try:
            if self.mode == "ssl":
                conn = self.context.wrap_socket(conn, server_side=True)
            session = Session(self, conn)
            session.run()
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            for opened in (conn, session and session.conn):    # STARTTLS wraps a new one
                try:
                    if opened:
                        opened.close()
                except OSError:
                    pass


class Session:
    def __init__(self, server: Server, conn):
        self.server, self.conn, self.box = server, conn, None
        self.tls = server.mode == "ssl"
        self.file = conn.makefile("rb")

    def send(self, data: bytes) -> None:
        self.conn.sendall(data)

    def line(self, text: str) -> None:
        self.send(text.encode() + b"\r\n")

    def read_command(self) -> bytes | None:
        """One command, with any literals in it inlined as quoted strings."""
        line = self.file.readline(100_000)
        if not line:
            return None
        whole = b""
        while True:
            found = re.search(rb"\{(\d+)\}\r\n$", line)
            if not found:
                return whole + line.rstrip(b"\r\n")
            self.line("+ go ahead")
            literal = self.file.read(int(found[1]))
            whole += line[:found.start()] + b'"' + literal.replace(b'"', b'\\"') + b'"'
            line = self.file.readline(100_000)

    def run(self) -> None:
        self.line("* OK made-up IMAP ready")
        while True:
            command = self.read_command()
            if command is None:
                return
            text = command.decode("utf-8", "replace")
            self.server.commands.append(text)
            tag, _, rest = text.partition(" ")
            verb, _, args = rest.partition(" ")
            handler = getattr(self, "do_" + verb.upper().replace(" ", "_"), None)
            if handler is None:
                self.line(f"{tag} BAD not here")
            elif handler(tag, args) == "bye":
                return

    def do_CAPABILITY(self, tag, _args):
        offered = " STARTTLS" if self.server.mode == "starttls" and not self.tls else ""
        self.line(f"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2{offered}")
        self.line(f"{tag} OK done")

    def do_STARTTLS(self, tag, _args):
        if self.server.mode != "starttls":
            return self.line(f"{tag} BAD no")
        self.send(self.server.inject_before + f"{tag} {self.server.yes}\r\n".encode()
                  + self.server.inject)
        self.conn = self.server.context.wrap_socket(self.conn, server_side=True)
        self.file, self.tls = self.conn.makefile("rb"), True

    def do_AUTHENTICATE(self, tag, args):
        self.line("+ ")
        answer = self.file.readline(100_000).strip()
        said = base64.b64decode(answer).decode("utf-8", "replace") if answer not in (b"", b"*") \
            else ""
        if not self.tls:
            self.server.in_clear.append(said)
        self.server.signed_in_with.append(said)
        if args.upper() == "XOAUTH2" and said == f"user={USER}\x01auth=Bearer {TOKEN}\x01\x01":
            return self.line(f"{tag} OK signed in")
        self.line("+ " + base64.b64encode(b'{"status":"401"}').decode())
        self.file.readline(100_000)
        self.line(f"{tag} NO not you")

    def do_LIST(self, tag, _args):
        for box in self.server.boxes.values():
            quoted = box.name.replace("\\", "\\\\").replace('"', '\\"')
            self.line(f'* LIST ({box.flags}) "/" "{quoted}"')
        self.line(f"{tag} OK done")

    def _open(self, tag, args, verb):
        name = args.strip()
        if name.startswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        self.box = self.server.boxes.get(name) or (
            self.server.boxes.get("INBOX") if name.upper() == "INBOX" else None)
        if self.box is None:
            return self.line(f"{tag} NO no such mailbox")
        self.line(f"* {len(self.box.mails)} EXISTS")
        self.line(f"{tag} OK [{'READ-ONLY' if verb == 'EXAMINE' else 'READ-WRITE'}] {verb} done")

    def do_EXAMINE(self, tag, args):
        self._open(tag, args, "EXAMINE")

    def do_SELECT(self, tag, args):
        self._open(tag, args, "SELECT")

    def do_LOGOUT(self, tag, _args):
        self.line("* BYE")
        self.line(f"{tag} OK bye")
        return "bye"

    def do_UID(self, tag, args):
        verb, _, rest = args.partition(" ")
        if self.box is None:
            return self.line(f"{tag} BAD no mailbox")
        if verb.upper() == "SEARCH":
            time.sleep(self.server.stall)
            if "FROM" in rest.upper().split():
                time.sleep(self.server.stall_from)
            while self.server.drip:
                self.send(b"*")
                time.sleep(0.1)
            found = [uid for uid, raw, when in self.box.mails
                     if self._matches(_tokens(rest), raw, when, uid)]
            self.line("* SEARCH" + "".join(f" {uid}" for uid in found))
            return self.line(f"{tag} OK done")
        if verb.upper() == "FETCH":
            return self._fetch(tag, rest)
        self.line(f"{tag} BAD not here")

    def _matches(self, tokens: list[str], raw: bytes, when: datetime, uid: int) -> bool:
        tokens = list(tokens)
        if tokens[:1] == ["CHARSET"]:
            tokens = tokens[2:]
        results = []
        while tokens:
            results.append(self._one(tokens, raw, when, uid))
        return all(results)

    def _one(self, tokens, raw, when, uid) -> bool:
        key = tokens.pop(0).upper()
        if key == "OR":
            return self._one(tokens, raw, when, uid) | self._one(tokens, raw, when, uid)
        if key == "ALL":
            return True
        value = tokens.pop(0)
        if key == "BEFORE":
            day, month, year = value.split("-")
            return when.date() < datetime(int(year), MONTHS.index(month) + 1, int(day)).date()
        if key == "UID":
            return str(uid) in value.split(",")
        haystack = _text_of(raw, key == "TEXT") if key in ("TEXT", "BODY") else _header(
            raw, key.title())
        if key in ("TEXT", "BODY") and self.server.whole_words:
            return bool(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", haystack, re.I))
        return value.lower() in haystack.lower()

    def _fetch(self, tag, rest):
        uids, _, items = rest.partition(" ")
        wanted = {int(uid) for uid in uids.split(",")}
        for seq, (uid, raw, when) in enumerate(self.box.mails, 1):
            if uid not in wanted:
                continue
            out = f"* {seq} FETCH (UID {uid} INTERNALDATE \"{_internal(when)}\"".encode()
            for section, fields, limit in re.findall(
                    r"BODY\.PEEK\[(HEADER\.FIELDS \(([^)]*)\)|TEXT)\]<0\.(\d+)>", items):
                head, _, body = raw.partition(b"\n\n") if b"\r\n\r\n" not in raw else \
                    raw.partition(b"\r\n\r\n")
                data = (_fields(head, fields.split()) if fields else body)[:int(limit)]
                if self.server.huge:
                    out += f" BODY[{section}]<0> {{1000000}}\r\n".encode()
                    self.send(out)
                    self.send(b"x" * 1_000_000)
                    return
                data = data.ljust(self.server.fat, b" ")
                out += f" BODY[{section}]<0> {{{len(data)}}}\r\n".encode() + data
            self.send(out + b")\r\n")
        self.line(f"{tag} OK done")


def _tokens(text: str) -> list[str]:
    return [quoted.replace('\\"', '"') if quoted else atom
            for quoted, atom in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', text)]


def _header(raw: bytes, name: str) -> str:
    return " ".join(str(value) for value in email.message_from_bytes(raw).get_all(name) or [])


def _fields(head: bytes, names: list[str]) -> bytes:
    lines, keep = [], False
    for line in re.split(rb"\r?\n", head):
        if line[:1] in (b" ", b"\t"):
            if keep:
                lines.append(line)
            continue
        keep = line.split(b":", 1)[0].strip().upper() in {n.upper().encode() for n in names}
        if keep:
            lines.append(line)
    return b"".join(line + b"\r\n" for line in lines) + b"\r\n"


# Online Accounts.

GOA = "org.gnome.OnlineAccounts"
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class PrivateBus:
    """A dbus-daemon of the test's own, with no service files: nothing can be started on it."""

    def __init__(self):
        self.folder = tempfile.TemporaryDirectory(prefix="tux-goa-bus-")
        socket_path = Path(self.folder.name) / "bus"
        config = Path(self.folder.name) / "bus.conf"
        config.write_text(BUS_CONFIG.format(socket=socket_path))
        self.process = subprocess.Popen(["dbus-daemon", f"--config-file={config}", "--nofork"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            if socket_path.exists():
                break
            time.sleep(0.01)
        self.address = f"unix:path={socket_path}"

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(5)
        self.folder.cleanup()


class Calls:
    def __init__(self):
        self.made = []
        self.done = asyncio.Event()     # set when the test ends, so a slow token answers then


class AccountIface(ServiceInterface):
    def __init__(self, spec):
        super().__init__(GOA + ".Account")
        self.spec = spec

    @dbus_property(access=PropertyAccess.READ)
    def ProviderType(self) -> "s":
        return self.spec.get("provider", "google")

    @dbus_property(access=PropertyAccess.READ)
    def PresentationIdentity(self) -> "s":
        return USER

    @dbus_property(access=PropertyAccess.READ)
    def MailDisabled(self) -> "b":
        return self.spec.get("mail_disabled", False)

    @dbus_property(access=PropertyAccess.READ)
    def AttentionNeeded(self) -> "b":
        return self.spec.get("attention", False)


class MailIface(ServiceInterface):
    def __init__(self, spec):
        super().__init__(GOA + ".Mail")
        self.spec = spec

    @dbus_property(access=PropertyAccess.READ)
    def EmailAddress(self) -> "s":
        return self.spec.get("address", USER)

    @dbus_property(access=PropertyAccess.READ)
    def ImapSupported(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def ImapHost(self) -> "s":
        return self.spec["host"]

    @dbus_property(access=PropertyAccess.READ)
    def ImapUserName(self) -> "s":
        return USER

    @dbus_property(access=PropertyAccess.READ)
    def ImapUseSsl(self) -> "b":
        return self.spec.get("ssl", True)

    @dbus_property(access=PropertyAccess.READ)
    def ImapUseTls(self) -> "b":
        return self.spec.get("tls", False)

    @dbus_property(access=PropertyAccess.READ)
    def ImapAcceptSslErrors(self) -> "b":
        return self.spec.get("accept_errors", False)


class OAuth2Iface(ServiceInterface):
    def __init__(self, spec, calls):
        super().__init__(GOA + ".OAuth2Based")
        self.spec, self.calls = spec, calls

    @method()
    async def GetAccessToken(self) -> "si":
        self.calls.made.append("GetAccessToken")
        try:    # refreshing it from the provider
            await asyncio.wait_for(self.calls.done.wait(), self.spec.get("slow", 0))
        except TimeoutError:
            pass
        return [self.spec.get("token", TOKEN), 3600]


class PasswordIface(ServiceInterface):
    def __init__(self, calls):
        super().__init__(GOA + ".PasswordBased")
        self.calls = calls

    @method()
    def GetPassword(self, _id: "s") -> "s":
        self.calls.made.append("GetPassword")
        return "the-users-real-password"


async def online_accounts(address: str, accounts: list[dict],
                          stalled: bool = False) -> tuple[MessageBus, Calls]:
    """Online Accounts on the private bus at address, with these accounts. Each is a dict of
    host, and optionally oauth2 (default True), password, ssl, tls, accept_errors, attention,
    mail_disabled, token, slow (seconds a token takes) and address. stalled: it never answers
    when asked to list them, as a goa-daemon that was just started or is busy may not in time."""
    assert "tux-goa-bus-" in address, "only on the test's own bus"
    bus = await MessageBus(bus_address=address).connect()
    if stalled:
        bus.add_message_handler(lambda asked: asked.member == "GetManagedObjects" or None)
    calls = Calls()
    for number, spec in enumerate(accounts, 1):
        path = f"/org/gnome/OnlineAccounts/Accounts/account_{number}"
        bus.export(path, AccountIface(spec))
        bus.export(path, MailIface(spec))
        if spec.get("oauth2", True):
            bus.export(path, OAuth2Iface(spec, calls))
        if spec.get("password"):
            bus.export(path, PasswordIface(calls))
    await bus.request_name(GOA)
    return bus, calls

