"""Your mail from an account in GNOME Online Accounts, for people who don't use Thunderbird.

Mail search reads Thunderbird's index when there is one with mail in it (see mail.py). Without
it, the mail accounts added in GNOME Settings under Online Accounts are searched on their own
servers, over IMAP. The rules are mail.py's: the same words looked for, the same codes hidden, the
same senders warned about, and the answer read out by the app from a model call with no tools.

Online Accounts holds the sign-in. An account that signs in through its provider's own page, as
Google and Microsoft do, hands out an access token that lasts about an hour, and that is all Tux
asks for: once per search, kept in memory for that search only, sent only to the account's own
mail server over TLS with the certificate checked, and never written, logged or shown. An account
set up with a password would hand over the password itself, which works for as long as it isn't
changed and opens everything else that password opens, so Tux never asks for one and leaves those
accounts alone.

The server does the searching, so a mailbox is never downloaded. Mailboxes are opened read-only,
mail is read without marking it read, and of each mail only its sender, its subject and the start
of its text are fetched, for at most 40 mails a mailbox. Only the inbox, or Gmail's All Mail, and
the Sent folder are searched: never Junk, Trash or Drafts. A server's answers are cut off past
what was asked for, and the whole search past its time.

When a sender first wrote is the date the server took their first mail in, not the date on the
mail, which its sender wrote.
"""
import asyncio
import base64
import email
import email.errors
import email.header
import email.message
import email.policy
import html
import imaplib
import re
import socket
import ssl
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from dbus_fast import AuthError, DBusError, Message, MessageType
from dbus_fast.aio import MessageBus

from . import mail

GOA = "org.gnome.OnlineAccounts"
ROOT = "/org/gnome/OnlineAccounts"
ACCOUNTS = ROOT + "/Accounts/"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
ACCOUNT, MAIL, OAUTH2 = GOA + ".Account", GOA + ".Mail", GOA + ".OAuth2Based"

CALL_S = 4              # one call to Online Accounts, which may refresh a token first
SIGN_IN_S = 5           # all of Online Accounts: connecting, listing, and every token at once
CONNECT_S = 4           # reaching a mail server
SEARCH_S = 7            # signing in, searching and fetching
LOOKUP_S = 3            # then looking up who the best few are from
MAX_ACCOUNTS = 3
EVERY_HITS = 20         # the newest mails holding every word
FETCHED = 40            # and with the newest holding some of them, all that is fetched of a mailbox
HEADER_BYTES = 4096     # of a mail's sender and subject
TEXT_BYTES = 16_000     # of the rest of it
FROM_BYTES = 1024
MAX_LITERAL = 64_000    # more than was asked for is a server not keeping to the request
MAX_TAKEN = 4_000_000   # all one search may take from a server
MAX_TOKEN_CHARS = 4096
MAX_USER_CHARS = 320
FROM_CHECKED = 10       # a sender's older mails whose sender line is read, to find their own
FIELDS = "FROM SUBJECT MIME-VERSION CONTENT-TYPE CONTENT-TRANSFER-ENCODING"
CA_FILE = None          # the system's certificates. Only a test points this anywhere else
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

PASSWORD_ONLY = ("Your mail account in Online Accounts signs in with a password. Tux only uses "
                 "accounts that sign in through their provider's own page, such as Google or "
                 "Microsoft, so it never holds your password.")
SIGN_IN_AGAIN = "Your mail account needs you to sign in again, in Settings under Online Accounts."
NOT_SAFE = ("Your mail account is set up to connect without checking who the server is, so I "
            "didn't use it.")
UNREACHABLE = "I couldn't reach your mail server just now."
NO_ANSWER = "Online Accounts didn't answer in time, so I couldn't search your mail just now."
BAD_CERTIFICATE = "Your mail server's certificate couldn't be checked, so I didn't sign in."
REFUSED = "Your mail server didn't accept the sign-in from Online Accounts."

HOST = re.compile(r"(?:\[([0-9A-Fa-f:.]{2,45})\]|([A-Za-z0-9.-]{1,253}))(?::(\d{1,5}))?")
LISTED = re.compile(rb'\(([^)]*)\) ("(?:[^"\\]|\\.)*"|NIL) ("(?:[^"\\]|\\.)*"|[^\s"(){}]+)')
NEVER_OPENED = {b"\\noselect", b"\\nonexistent", b"\\junk", b"\\trash", b"\\drafts"}
TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|[^\s()"]+')
SECTION = re.compile(rb"BODY\[(TEXT|HEADER\.FIELDS \([^)]*\))\](?:<\d+>)? \{\d+\}$", re.I)
INTERNAL = re.compile(r'"\s?(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)"')
SKIPPED_HTML = re.compile(r"(?is)<(script|style|head)\b.*?</\1\s*>")
BREAK = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d|table|blockquote)\s*>")
TAG = re.compile(r"<[^>]*>")


class Account(NamedTuple):
    path: str
    address: str        # the account's own: a mail from it outside Sent is not the user's
    host: str
    port: int
    starttls: bool
    user: str


class Unusable(Exception):
    """An account that can't be searched, with what to say about it."""


async def _connect() -> MessageBus:
    """The one place a bus connection is made, so a test or an evaluation has one thing to
    replace."""
    return await asyncio.wait_for(MessageBus().connect(), CALL_S)


def _left(until: float) -> float:
    return max(0.1, min(CALL_S, until - time.monotonic()))


async def _call(bus, path: str, interface: str, member: str, until: float) -> list:
    reply = await asyncio.wait_for(bus.call(Message(
        destination=GOA, path=path, interface=interface, member=member)), _left(until))
    if reply.message_type == MessageType.ERROR:
        raise Unusable(SIGN_IN_AGAIN)
    return reply.body


def _value(properties, name: str, kind: type):
    value = getattr(properties.get(name), "value", None) if isinstance(properties, dict) else None
    return value if isinstance(value, kind) else None


def _account(path: str, interfaces: dict) -> Account | None:
    """The account at path if it can be searched, None if it has no mail, and Unusable if it
    has mail Tux won't use. The settings are the user's own, from Online Accounts."""
    found, settings = interfaces.get(ACCOUNT), interfaces.get(MAIL)
    if not isinstance(settings, dict) or _value(found, "MailDisabled", bool) is not False \
            or _value(settings, "ImapSupported", bool) is False:
        return None
    if OAUTH2 not in interfaces:
        raise Unusable(PASSWORD_ONLY)
    if _value(found, "AttentionNeeded", bool):
        raise Unusable(SIGN_IN_AGAIN)
    ssl_on, tls_on = (_value(settings, name, bool) for name in ("ImapUseSsl", "ImapUseTls"))
    if _value(settings, "ImapAcceptSslErrors", bool) is not False or not (ssl_on or tls_on):
        raise Unusable(NOT_SAFE)
    host = HOST.fullmatch(_value(settings, "ImapHost", str) or "")
    user = _value(settings, "ImapUserName", str) or ""
    if not host or not user.isprintable() or len(user) > MAX_USER_CHARS:
        return None
    port = int(host[3] or (993 if ssl_on else 143))
    if not 0 < port < 65536:
        return None
    return Account(path, (_value(settings, "EmailAddress", str) or "").strip().lower(),
                   host[1] or host[2], port, not ssl_on, user)


GOA_FAILED = (DBusError, TimeoutError, EOFError, OSError, IndexError, TypeError, ValueError)


async def _sign_ins(until: float) -> tuple[list[tuple[Account, str]], str]:
    """([(account, access token)], what to say if there are none to use), by until. Online
    Accounts is asked for tokens only, never for a password, and for all of them at once: a
    token past its hour is fetched again from the provider, which can be slow. What went wrong
    with an account that could have been searched is said before what is wrong with the rest."""
    try:
        bus = await asyncio.wait_for(_connect(), _left(until))
    except (OSError, EOFError, LookupError, ValueError, TypeError, AuthError, DBusError,
            TimeoutError):
        return [], ""
    usable, failed, problems = [], [], []
    try:
        objects = (await _call(bus, ROOT, OBJECT_MANAGER, "GetManagedObjects", until))[0]
        accounts = []
        for path in sorted(objects if isinstance(objects, dict) else ()):
            try:
                account = path.startswith(ACCOUNTS) and _account(path, objects[path])
            except Unusable as e:
                problems.append(str(e))
                continue
            if account and len(accounts) < MAX_ACCOUNTS:
                accounts.append(account)
        tokens = await asyncio.gather(*(_token_of(bus, account, until) for account in accounts),
                                      return_exceptions=True)
        for account, token in zip(accounts, tokens):
            if isinstance(token, str):
                usable.append((account, token))
            elif isinstance(token, Unusable):
                failed.append(str(token))
            elif isinstance(token, GOA_FAILED):
                failed.append(NO_ANSWER)    # the account is there: it is no "no mail here"
            else:
                raise token
    except (Unusable, *GOA_FAILED):
        pass    # no Online Accounts on this desktop
    finally:
        bus.disconnect()
    return usable, next(iter([*failed, *problems]), "")


async def _token_of(bus, account: Account, until: float) -> str:
    token, *_ = await _call(bus, account.path, OAUTH2, "GetAccessToken", until)
    return _token(token)


def _token(token) -> str:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS \
            or not token.isprintable() or not token.isascii():
        raise Unusable(SIGN_IN_AGAIN)
    return token


def _context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=CA_FILE)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _Bounded:
    """A connection that stops at a time, whatever the server does, and takes no more than it
    asked for. A literal is read whole by imaplib, so its size is checked first."""

    def _limit(self, until: float) -> None:
        self.until, self.taken, self._cutter = until, 0, None

    def open(self, host, port, timeout):
        super().open(host, port, max(0.1, min(timeout, self.until - time.monotonic())))
        self._cutter = threading.Timer(max(0, self.until - time.monotonic()), self._cut)
        self._cutter.daemon = True
        self._cutter.start()

    def _cut(self) -> None:
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        except OSError:
            pass

    def _take(self, size: int) -> None:
        self.taken += size
        if self.taken > MAX_TAKEN or time.monotonic() > self.until:
            raise self.abort("out of time or too much")

    def read(self, size):
        if size > MAX_LITERAL:
            raise self.abort("more than was asked for")
        self._take(size)
        return super().read(size)

    def readline(self):
        line = super().readline()
        self._take(len(line))
        return line

    def shutdown(self):
        if self._cutter:
            self._cutter.cancel()
        super().shutdown()


class _Tls(_Bounded, imaplib.IMAP4_SSL):
    def __init__(self, host, port, until):
        self._limit(until)
        super().__init__(host, port, ssl_context=_context(), timeout=CONNECT_S)


class _Plain(_Bounded, imaplib.IMAP4):
    """Only ever used to start TLS, and refused if the server doesn't offer it."""

    def __init__(self, host, port, until):
        self._limit(until)
        super().__init__(host, port, timeout=CONNECT_S)

    heard = None    # the lines read while STARTTLS is asked for

    def _get_line(self):
        line = super()._get_line()
        if self.heard is not None:
            self.heard.append(line)
        return line

    def starttls(self, ssl_context=None):
        """imaplib's, except that anything but the server's yes is refused, whether it came
        before it or after it, and nothing said in the clear is kept: imaplib would read it as if
        it came over TLS, so whoever sits on the network could write the server's first answers,
        such as which folder holds the mail the user sent. The lines themselves are counted, as a
        code in brackets on the yes, such as [ALERT], is kept by imaplib as if it came apart."""
        if "STARTTLS" not in self.capabilities:
            raise self.abort("no STARTTLS")
        self.untagged_responses.clear()
        self.heard = []
        try:
            typ, _data = self._simple_command("STARTTLS")
        finally:
            heard, self.heard = self.heard, None
        if typ != "OK" or len(heard) != 1 or any(getattr(self, "_readbuf", ())):
            raise self.abort("STARTTLS refused or tampered with")
        self.untagged_responses.clear()
        self.sock = ssl_context.wrap_socket(self.sock, server_hostname=self.host)
        self._file = self.sock.makefile("rb")
        self._tls_established = True
        self.untagged_responses.clear()
        self._get_capabilities()


def _signed_in(account: Account, token: str, until: float) -> imaplib.IMAP4:
    try:
        if account.starttls:
            imap = _Plain(account.host, account.port, until)
            try:
                imap.starttls(_context())
            except BaseException:
                imap.shutdown()
                raise
        else:
            imap = _Tls(account.host, account.port, until)
    except ssl.SSLCertVerificationError:
        raise Unusable(BAD_CERTIFICATE) from None
    except (OSError, imaplib.IMAP4.error, ValueError):
        raise Unusable(UNREACHABLE) from None
    said = [f"user={account.user}\x01auth=Bearer {token}\x01\x01".encode()]
    try:
        if "AUTH=XOAUTH2" not in imap.capabilities:
            raise Unusable(REFUSED)
        imap.authenticate("XOAUTH2", lambda _challenge: said.pop() if said else None)
    except BaseException as e:
        imap.shutdown()
        if isinstance(e, Unusable):
            raise
        if isinstance(e, (OSError, imaplib.IMAP4.abort)):
            raise Unusable(UNREACHABLE) from None
        if isinstance(e, imaplib.IMAP4.error):
            raise Unusable(REFUSED) from None
        raise
    return imap


def _shown_name(name: bytes) -> str:
    """A mailbox's name as people see it: unquoted and out of IMAP's own UTF-7."""
    text = name.decode("ascii", "replace")
    if text.startswith('"'):
        text = re.sub(r"\\(.)", r"\1", text[1:-1])
    return mail.IMAP_NAME.sub(mail._imap, text)


def _boxes(imap) -> tuple[list[str], str | None]:
    """([where mail from others is], where mail the user sent is). Gmail's All Mail if there is
    one, as most of its mail is archived, or else the inbox. Folders marked Junk, Trash or
    Drafts, or named as one, are never opened. The inbox is never taken for either: whatever
    else it is called, it is where anyone's mail comes in, so its mail is never the user's own.
    Its name is the one name IMAP doesn't tell apart by case."""
    typ, data = imap.list()
    everything = sent = named_sent = None
    for line in data if typ == "OK" else ():
        found = LISTED.match(line) if isinstance(line, bytes) and line.isascii() else None
        if not found:
            continue
        flags = set(found[1].lower().split())
        name, shown = found[3].decode(), _shown_name(found[3])
        delimiter = _shown_name(found[2]) if found[2] != b"NIL" else ""
        parts = shown.split(delimiter) if delimiter else [shown]
        if flags & NEVER_OPENED or any(mail._junk(part) for part in [shown, *parts]) \
                or _inbox(shown):
            continue
        if b"\\all" in flags:
            everything = everything or name
        elif b"\\sent" in flags:
            sent = sent or name
        elif mail.SENT.fullmatch(parts[-1].strip()):
            named_sent = named_sent or name
    return [everything or "INBOX"], sent or named_sent


def _inbox(name: str) -> bool:
    return name.upper() == "INBOX"


def _ask(imap, command: str, *args) -> tuple:
    """A UID command, with a refusal as no answer. A connection that broke still breaks."""
    try:
        return imap.uid(command, *args)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        imap.literal = None
        return "NO", []


def _uids(reply) -> set[int]:
    typ, data = reply
    if typ != "OK":
        return set()
    return {int(word) for line in data if isinstance(line, bytes)
            for word in line.split() if word.isdigit()}


def _hits(imap, stems: list[mail.Term], until: float) -> list[set[int]]:
    """For each word, the mails holding it. The server is asked for its stem and for the word
    itself, as some servers, Gmail's among them, only match whole words. The words are letters
    and digits only, so they need no escaping."""
    hits = []
    for term in stems:
        if time.monotonic() > until:
            return []
        strings = list(dict.fromkeys(text for text in (term.stem, term.word) if text))
        plain = [text for text in strings if text.isascii()]
        found = set()
        if plain:
            asked = " ".join(f'TEXT "{text}"' for text in plain)
            found |= _uids(_ask(imap, "SEARCH", f"OR {asked}" if len(plain) == 2 else asked))
        for text in strings:
            if not text.isascii():
                imap.literal = text.encode()
                found |= _uids(_ask(imap, "SEARCH", "CHARSET", "UTF-8", "TEXT"))
        hits.append(found)
    return hits


def _picked(hits: list[set[int]]) -> list[tuple[int, bool]]:
    """[(uid, holds every word)]: the newest holding every word, then the newest holding most."""
    every = set.intersection(*hits) if hits else set()
    first = sorted(every, reverse=True)[:EVERY_HITS]
    counts = Counter(uid for found in hits for uid in found)
    rest = sorted((uid for uid in counts if uid not in first),
                  key=lambda uid: (counts[uid], uid), reverse=True)[:FETCHED - len(first)]
    return [(uid, True) for uid in first] + [(uid, False) for uid in rest]


def _fetched(data: list) -> dict[int, dict]:
    """{uid: {b"INTERNALDATE": ..., b"TEXT": ..., b"HEADER": ...}} from a FETCH reply. A mail's
    own bytes only ever arrive as a literal, which is never searched for the reply's structure,
    and a quoted string is one token, so neither can pass for a UID or a date."""
    mails = []
    for item in data:
        head, literal = item if isinstance(item, tuple) else (item, None)
        if not isinstance(head, bytes):
            continue
        if re.match(rb"\d+ \(", head):
            mails.append({})
        if not mails:
            continue
        found, tokens = mails[-1], TOKEN.findall(head)
        for at, token in enumerate(tokens[:-1]):
            if token.upper() in (b"UID", b"INTERNALDATE"):
                found.setdefault(token.upper(), tokens[at + 1])
        section = SECTION.search(head)
        if section and isinstance(literal, bytes):
            found.setdefault(b"TEXT" if section[1].upper() == b"TEXT" else b"HEADER", literal)
    return {int(found[b"UID"]): found for found in mails if found.get(b"UID", b"").isdigit()}


def _stamp(internal: bytes) -> float:
    """An INTERNALDATE in microseconds, as mail.py has dates. The server set it when the mail
    came, so its sender can't."""
    found = INTERNAL.fullmatch(internal.decode("ascii", "replace")) if internal else None
    if not found or found[2].title() not in MONTHS:
        return 0
    day, name, year, hour, minute, second, sign, hours, minutes = found.groups()
    offset = (1 if sign == "+" else -1) * timedelta(hours=int(hours), minutes=int(minutes))
    try:
        when = datetime(int(year), MONTHS.index(name.title()) + 1, int(day), int(hour), int(minute),
                        int(second), tzinfo=timezone(offset))
    except ValueError:
        return 0
    return when.timestamp() * 1e6


def _imap_date(when: datetime) -> str:
    return f"{when.day}-{MONTHS[when.month - 1]}-{when.year}"


def _flat(value) -> str:
    text = str(value or "").encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return " ".join(text.split())


def _parsed(header: bytes, text: bytes) -> email.message.Message | None:
    try:
        return email.message_from_bytes(header.rstrip(b"\r\n") + b"\r\n\r\n" + text,
                                        policy=email.policy.compat32)
    except (RecursionError, ValueError, TypeError, LookupError):
        return None


def _author(message) -> str:
    """The sender line as it came, not decoded: only the address in it counts (see
    mail._sender), and that is plain ASCII. Two sender lines are nobody's."""
    found = message.get_all("From") or []
    return _flat(found[0]) if len(found) == 1 else ""


def _subject(message) -> str:
    raw = _flat(message.get("Subject"))
    try:
        return _flat(email.header.make_header(email.header.decode_header(raw)))
    except (ValueError, LookupError, UnicodeError, email.errors.HeaderParseError):
        return raw


def _decoded(part) -> str:
    """A part's text, from as much of it as was fetched: base64 cut short still decodes."""
    try:
        if str(part.get("Content-Transfer-Encoding", "")).strip().lower() == "base64":
            letters = re.sub(r"[^A-Za-z0-9+/]", "", str(part.get_payload()))
            data = base64.b64decode(letters[:len(letters) // 4 * 4])
        else:
            data = part.get_payload(decode=True) or b""
        try:
            return data.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, ValueError, TypeError):
            return data.decode("utf-8", "replace")
    except (ValueError, TypeError, LookupError, AttributeError):
        return ""


def _unhtml(text: str) -> str:
    text = TAG.sub(" ", BREAK.sub("\n", SKIPPED_HTML.sub(" ", text)))
    return "\n".join(" ".join(line.split()) for line in html.unescape(text).splitlines())


def _body(message) -> str:
    """Its plain text, or its HTML as text. Attachments are never read."""
    plain = marked = None
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_filename() or str(part.get("Content-Disposition", "")).lower().startswith(
                "attachment"):
            continue
        kind = part.get_content_type()
        if kind == "text/plain" and plain is None:
            plain = _decoded(part)
        elif kind == "text/html" and marked is None:
            marked = _decoded(part)
    text = plain if plain and plain.strip() else _unhtml(marked or "")
    return text[:mail.SCAN_CHARS]


def _select(imap, box: str) -> bool:
    """Opened read-only, so nothing in it can change: EXAMINE, not SELECT."""
    try:
        return imap.select(box, readonly=True)[0] == "OK"
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return False


def _rows(imap, box: str, folder: int, stems: list[mail.Term], until: float) -> list[tuple]:
    """Rows as mail._rows makes them, from one mailbox."""
    if time.monotonic() > until or not _select(imap, box):
        return []
    picked = _picked(_hits(imap, stems, until))
    if not picked:
        return []
    asked = (f"(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FIELDS})]<0.{HEADER_BYTES}> "
             f"BODY.PEEK[TEXT]<0.{TEXT_BYTES}>)")
    typ, data = _ask(imap, "FETCH", ",".join(str(uid) for uid, _ in picked), asked)
    fetched = _fetched(data) if typ == "OK" else {}
    rows = []
    for uid, every in picked:
        found = fetched.get(uid)
        message = found and _parsed(found.get(b"HEADER", b""), found.get(b"TEXT", b""))
        if message:
            cut = len(found.get(b"TEXT", b"")) >= TEXT_BYTES     # it may end inside a code
            kept = mail._kept(stems, _author(message), _subject(message), _body(message), every,
                              cut)
            if kept:
                rows.append(((folder, uid), _stamp(found.get(b"INTERNALDATE")), folder, *kept))
    return rows


def _wrote_before(imap, owner: str, before: str) -> bool:
    """Whether a mail from exactly this address came before that day. The server's FROM matches
    the name beside an address too, so the sender lines it finds are read here."""
    uids = sorted(_uids(_ask(imap, "SEARCH", "BEFORE", before, "FROM", f'"{owner}"')))
    if not uids:
        return False
    typ, data = _ask(imap, "FETCH", ",".join(map(str, uids[:FROM_CHECKED])),
                         f"(UID BODY.PEEK[HEADER.FIELDS (FROM)]<0.{FROM_BYTES}>)")
    for found in (_fetched(data) if typ == "OK" else {}).values():
        message = _parsed(found.get(b"HEADER", b""), b"")
        if message and mail._sender(_author(message))[1] == owner:
            return True
    return False


def _known(imap, boxes: list[str], owners: set[str], own: str, until: float) -> set[str]:
    """The senders whose first mail came at least SETTLED_DAYS ago. A mail from the account's
    own address that isn't in Sent is anyone's: that address is the easiest one to forge. Out of
    time counts as never: the warning costs less than its absence."""
    before = _imap_date(datetime.now(timezone.utc) - timedelta(days=mail.SETTLED_DAYS))
    found, owners = set(), {owner for owner in owners if owner and owner != own}
    for box in boxes:
        if not owners - found or time.monotonic() > until or not _select(imap, box):
            continue
        for owner in sorted(owners - found):
            if time.monotonic() > until:
                break
            if _wrote_before(imap, owner, before):
                found.add(owner)
    return found


def _search(account: Account, token: str, stems: list[mail.Term], until: tuple) -> tuple:
    """(the best of one account as mail.settled gives them, what to say if it failed). Runs in
    a thread. Out of time or cut off, what was found so far will do."""
    try:
        imap = _signed_in(account, token, until[1])
    except Unusable as e:
        return [], str(e)
    rows, scored = [], []
    try:
        boxes, sent = _boxes(imap)
        for folder, box in enumerate([*boxes, *([sent] if sent and sent not in boxes else [])]):
            rows += _rows(imap, box, folder, stems, until[0])
        scored = mail._ranked(rows, stems, {len(boxes)} if sent and sent not in boxes else set())
        owners = {item[3].owner for item in mail.checked(scored) if not item[3].by_user}
        known = _known(imap, boxes, owners, account.address,
                       min(time.monotonic() + LOOKUP_S, until[1]))
        return mail.settled(scored, lambda owner: owner in known), ""
    except (OSError, imaplib.IMAP4.error, ValueError):
        if not scored:
            scored = mail._ranked(rows, stems, set())
        return mail.settled(scored, lambda _owner: False), ""
    finally:
        try:
            imap.shutdown()
        except OSError:
            pass


async def find(stems: list[mail.Term], deadline: float) -> list[mail.Mail]:
    """The mails most likely to hold the answer, best first, from every account that can be
    searched, each in a thread of its own, all by deadline. Whatever signing in took comes off
    the search, never off the time left to read the answer."""
    accounts, problem = await _sign_ins(min(time.monotonic() + SIGN_IN_S, deadline))
    if not accounts:
        raise RuntimeError(problem or mail.NO_INDEX)
    started = time.monotonic()
    left = deadline - 1 - started   # a second for a thread to come back once its server is cut
    if left <= 0:
        raise RuntimeError(UNREACHABLE)
    until = (started + min(SEARCH_S, left * SEARCH_S / (SEARCH_S + LOOKUP_S)),
             started + min(SEARCH_S + LOOKUP_S, left))
    try:
        results = await asyncio.wait_for(asyncio.gather(*(
            asyncio.to_thread(_search, account, token, stems, until)
            for account, token in accounts)), until[1] - started + 1)
    except TimeoutError:
        raise RuntimeError(UNREACHABLE) from None
    if all(failed for _best, failed in results):
        raise RuntimeError(results[0][1])
    best = sorted((item for found, _failed in results for item in found),
                  key=lambda item: item[:3], reverse=True)
    return [item[3] for item in best]
