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
of its text are fetched, for at most 40 mails a mailbox, with the sender line of a few older mails
to see who wrote before, and of a few in Sent to see which of those is the user. Only the inbox,
or Gmail's All Mail, and the Sent folder are searched: never Junk, Trash or Drafts. A server's
answers are cut off past what was asked for, and the whole search past its time. Whatever goes
wrong, what is said is Tux's own sentence, never an error's text, which can be a stranger's (see
_search).

When a sender first wrote is the date the server took their first mail in, not the date on the
mail, which its sender wrote.
"""
import asyncio
import base64
import codecs
import email
import email.errors
import email.message
import email.policy
import email.quoprimime
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
MORE_BYTES = 24_000     # then at a time, of a part cut short there, as 0.5.0 read 20,000 characters
READ_ON_BYTES = 160_000     # and in all, as 20,000 characters of a shop's HTML can take that much
FROM_BYTES = 1024
MAX_LITERAL = 64_000    # more than was asked for is a server not keeping to the request
MAX_TAKEN = 4_000_000   # all one search may take from a server
MAX_TOKEN_CHARS = 4096
MAX_USER_CHARS = 320
FROM_CHECKED = 10       # a sender's older mails whose sender line is read, to find their own
FIELDS = "FROM SUBJECT MIME-VERSION CONTENT-TYPE CONTENT-TRANSFER-ENCODING"
# mail.SIGN_IN_WORDS as a server is asked for them: each way of writing a space, and a * left
# off, so "verif" finds "verification" on a server that finds any part of a word. Gmail's finds
# whole words only, so the words a * stands for most are asked for too, in Spanish and Portuguese
# as well, as 0.5.0's talk took "Verifica tu correo" and "Verifique o seu e-mail". One that isn't
# ASCII is asked for on its own (see _talking). So are those a shop's or a review's footer writes,
# "100% authentic" or "opiniones verificadas", which 0.5.0's talk took too.
TALK_TERMS = tuple(dict.fromkeys(
    [word.rstrip("*").replace(" ", gap) for word in mail.SIGN_IN_WORDS for gap in ("", " ", "-")]
    + ["verify", "verified", "verifies", "verifying", "verifiable", "verification",
       "authentic", "authenticity", "authentically", "authenticate", "authenticated",
       "authentication", "authenticator", "verifica", "verificar", "verifique", "verifiquem",
       "verificado", "verificada", "verificados", "verificadas", "verificamos", "verificaremos",
       "verificou", "verificara", "verificará", "verificacion", "verificacao", "verificaciones",
       "verificacoes", "verificación", "verificação", "verificações"]))
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
# An element whose text is never read, and its closer. A lazy match from each opener to its
# closer read on to the end of the text for every opener that never closed: a 16 KB part of
# "<head " took two seconds, holding the event loop, so each is looked for once (see _skipped).
SKIPPED_HTML = re.compile(r"(?i)<(script|style|head)\b")
CLOSER = {name: re.compile(rf"(?i)</{name}\s*>") for name in ("script", "style", "head")}
# Elements a browser and Thunderbird's index put on lines of their own: a line ends at each one's
# opener and closer, and at <br>, so "<div>Enter this:<div>KQ7X9B</div></div>" or a code in a
# <center> after a paragraph has a line to itself, as it had for 0.5.0.
BLOCKS = ("address|article|aside|blockquote|body|caption|center|dd|details|dialog|dir|div|dl|dt"
          "|fieldset|figcaption|figure|footer|form|h[1-6]|header|hgroup|hr|html|legend|li|listing"
          "|main|menu|nav|ol|p|plaintext|pre|section|summary|table|tbody|tfoot|thead|title|tr|ul"
          "|xmp")
BREAK = re.compile(rf"(?i)<br\b[^<>]*>|</?(?:{BLOCKS})\b[^<>]*>")
# A tag starts where HTML starts one, at a "<" before a letter, "/", "!" or "?". Any other "<" is
# text, as in Thunderbird's index: read as a tag to the next ">", "For orders < 100 € sign in to
# pay.</td><td>Order 5928374" lost its talk of signing in, and the order number 0.5.0 hid was shown.
TAG = re.compile(r"<(?=[A-Za-z/!?])[^>]*>")
# Every other element is inline, which a browser and Thunderbird's index join what stands either
# side of, but for a table's cell and a quote, which they set apart. Between two digits an inline
# tag is taken out: made spaces, "<span>48</span><span>29</span><span>13</span>" read "48 29 13",
# which no sender wrote. So it is beside a letter of a language that writes no spaces, which glues
# nothing to a code (see mail.SPACELESS) but keeps a number glued to it out of sign-in talk (see
# mail._glued), as in Thunderbird's index: "请致电<b>95588</b>" is a phone number after "call".
# Anywhere else it is mail.JOIN, which 0.5.0's rules read as nothing, as they read Thunderbird's
# index, and this version's own as a space, as a label joined to its code would hide it no more:
# "<span>Your code is</span><span>482913</span>". So "<kbd>KQ</kbd><kbd>7X</kbd><kbd>9B</kbd>" is
# KQ7X9B to 0.5.0's rules, as it was in Thunderbird. A tag holds no "<", so each is looked for no
# further than the next one, and a run of them only from its first. A comment is nothing (see
# _uncommented).
INLINE_TAG = rf"<(?!/?(?:t[dh]|q|{BLOCKS}|br)\b)/?[A-Za-z][^<>]*>"
INLINE = re.compile(rf"(?i)(?<=\d)(?:{INLINE_TAG})+(?=\d)|(?<={mail.GLUE_LETTER.pattern})"
                    rf"(?:{INLINE_TAG})+|(?<!>)(?:{INLINE_TAG})+(?={mail.GLUE_LETTER.pattern})")
JOINED = re.compile(rf"(?i)(?:{INLINE_TAG})+")
SPACING = re.compile(rf"[\s{mail.JOIN}]+")    # what str.split splits on, and the joins
# The first 0.5.0's 20,000 characters of a text as Thunderbird's index counts them, which holds
# nothing where a JOIN stands: counted with them, an HTML mail heavy in inline tags lost talk of
# signing in that 0.5.0 read near the end of its 20,000. _spaced leaves no two JOINs together.
INDEXED = re.compile(rf"(?:{mail.JOIN}?[^{mail.JOIN}]){{{mail.SCAN_CHARS}}}")
# A server ends each line of a mail with a carriage return, which Thunderbird's index doesn't
# keep: before one, a code was never alone on its line to 0.5.0's rules, and 0.5.0's reach of
# 60 or 160 characters was shorter by one for each line.
LINE_ENDS = re.compile(r"\r\n?")
# Charset labels as Thunderbird reads them, which is as browsers do (the WHATWG Encoding
# Standard), as senders label them: ISO-8859-1 and ASCII are Windows-1252, GB2312 and GBK are
# GB18030, and so on. Read as labelled, the "…" of an Outlook mail labelled ISO-8859-1 was a line
# end, which cut a password in two, and its € a control, which made a price a code.
CODECS = {"iso8859-1": "cp1252", "ascii": "cp1252", "iso8859-9": "cp1254", "tis-620": "cp874",
          "iso8859-11": "cp874", "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030",
          "euc_kr": "cp949"}
# The charset of an HTML part whose Content-Type names none, from a <meta> in its first 1,024
# bytes, as Thunderbird and browsers take it (the WHATWG prescan): `<meta charset="gb2312">` or
# `<meta http-equiv="Content-Type" content="text/html; charset=gb2312">`. Read as UTF-8, an older
# Chinese sender's 验证码 was gone, and the code after it with it.
META_CHARSET = re.compile(rb"(?i)<meta\b[^<>]*?charset\s*=\s*[\"']?\s*([\w.:-]{1,40})")
META_BYTES = 1024
# A part that names no charset at all, as an older sender's script sends one, Thunderbird reads in
# the charset it guesses. Read as UTF-8, "Tu código" was "Tu c\ufffddigo", 验证码 and رمز were
# gone, and the code after each was shown. See _guessed for the charsets it is told apart in.
HIGH_RUN = re.compile(rb"[\x80-\xff]+")
GUESSES = (("cp1256", re.compile("[\u0620-\u064a\u066e-\u06d3\u06fa-\u06ff]")),
           ("gb18030", re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")))
LATIN = re.compile("[A-Za-z\u00c0-\u024f]")
# A Latin accent or a Latin-1 sign glued to an Arabic letter, as GBK read in Windows-1256 is full
# of. Arabic quotes a word in « », which Windows-1256 writes as Latin-1 does, so only a « after a
# letter or a » before one, the wrong way round, counts: counted either way, "أدخل «رمز التحقق»"
# was read as Windows-1252, and the code after it was shown.
GLUED_TO_ARABIC = re.compile("[\u0620-\u064a\u066e-\u06d3\u06fa-\u06ff]"
                             "[\u00a1-\u00ba\u00bc-\u024f\u00d7\u00f7]|"
                             "[\u00a1-\u00aa\u00ac-\u024f\u00d7\u00f7]"
                             "[\u0620-\u064a\u066e-\u06d3\u06fa-\u06ff]")
# What a cut through an escape leaves at the end of quoted-printable once decoded: its "=" and
# one hex digit, which decoding leaves as they are (an "=" alone it drops, as a soft line break).
QP_CUT = re.compile(rb"=[0-9A-Fa-f]\Z")
# An encoded word (RFC 2047), and the space between two of them, which is not part of the text.
ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([QqBb])\?([^?\s]*)\?=")
BETWEEN_WORDS = re.compile(r"(?<=\?=)\s+(?==\?[^?\s]+\?[QqBb]\?)")


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
    except (TimeoutError, AuthError, DBusError):
        return [], NO_ANSWER    # a bus that is there but slow, or won't let Tux in: no "no mail"
    except (OSError, EOFError, LookupError, ValueError, TypeError):
        return [], ""           # no bus, or nothing listening where it should be
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
            elif isinstance(token, Exception):
                failed.append(NO_ANSWER)    # the account is there: it is no "no mail here"
            else:
                raise token
    except TimeoutError:
        failed.append(NO_ANSWER)    # slow to list its accounts, as when it was just started
    except (Unusable, *GOA_FAILED):
        pass    # no Online Accounts on this desktop
    except Exception:
        failed.append(NO_ANSWER)    # never an error's own text: see _search
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
        self.until, self.taken, self._cutter, self.addresses = until, 0, None, []

    def open(self, host, port, timeout):
        self.addresses = _addresses(host, port, self.until)
        super().open(host, port, max(0.1, min(timeout, self.until - time.monotonic())))
        self._cutter = threading.Timer(max(0, self.until - time.monotonic()), self._cut)
        self._cutter.daemon = True
        self._cutter.start()

    def _tcp(self, timeout) -> socket.socket:
        """socket.create_connection's, to each address looked up in time in turn."""
        failed = OSError("no address")
        for address in self.addresses:
            try:
                return socket.create_connection(
                    (address, self.port), max(0.1, min(timeout, self.until - time.monotonic())))
            except OSError as e:
                failed = e
        raise failed

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


def _addresses(host: str, port: int, until: float) -> list[str]:
    """The host's addresses, looked up in a thread of their own that is given up on at until.
    socket.create_connection looks a name up before its own timeout starts, so a resolver that
    never answers would hold the search's thread, and the token with it, past the search's
    time. The thread given up on holds only the host's name."""
    found = []

    def look_up():
        try:
            found.append([info[4][0] for info in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM) if isinstance(info[4][0], str)])
        except (OSError, UnicodeError, ValueError):
            found.append([])

    thread = threading.Thread(target=look_up, daemon=True)
    thread.start()
    thread.join(max(0, min(CONNECT_S, until - time.monotonic())))
    if not found or not found[0]:
        raise OSError("no address in time")
    return list(dict.fromkeys(found[0]))


class _Tls(_Bounded, imaplib.IMAP4_SSL):
    def __init__(self, host, port, until):
        self._limit(until)
        super().__init__(host, port, ssl_context=_context(), timeout=CONNECT_S)

    def _create_socket(self, timeout):
        """The address looked up, and the certificate checked against the host's name."""
        return self.ssl_context.wrap_socket(self._tcp(timeout), server_hostname=self.host)


class _Plain(_Bounded, imaplib.IMAP4):
    """Only ever used to start TLS, and refused if the server doesn't offer it."""

    def __init__(self, host, port, until):
        self._limit(until)
        super().__init__(host, port, timeout=CONNECT_S)

    def _create_socket(self, timeout):
        return self._tcp(timeout)

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
                _closed_after(imap)
                raise
        else:
            imap = _Tls(account.host, account.port, until)
    except ssl.SSLCertVerificationError:
        raise Unusable(BAD_CERTIFICATE) from None
    except Exception:   # a server's own words never leave here: see _search
        raise Unusable(UNREACHABLE) from None
    said = [f"user={account.user}\x01auth=Bearer {token}\x01\x01".encode()]
    try:
        if "AUTH=XOAUTH2" not in imap.capabilities:
            raise Unusable(REFUSED)
        imap.authenticate("XOAUTH2", lambda _challenge: said.pop() if said else None)
    except BaseException as e:
        imap.shutdown()
        if isinstance(e, Unusable) or not isinstance(e, Exception):
            raise
        if isinstance(e, (OSError, imaplib.IMAP4.abort)):
            raise Unusable(UNREACHABLE) from None
        raise Unusable(REFUSED) from None
    return imap


def _closed_after(imap) -> None:
    """A connection shut after a failed start of TLS, which can fail itself: its error took the
    place of the certificate's, and a server whose name the certificate didn't match was one
    that couldn't be reached."""
    try:
        imap.shutdown()
    except OSError:
        pass


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
    return " ".join(str(value or "").split())


def _codec(label: str | None, fallback: str = "utf-8") -> str:
    """The codec a charset label is read with, as Thunderbird reads it (see CODECS)."""
    label = (label or "").strip().lower()
    try:
        name = codecs.lookup(label).name
    except (LookupError, ValueError, TypeError):
        name = label if label in CODECS else fallback
    return CODECS.get(name, name)


def _header(message, name: str) -> str | None:
    """The first header of that name as it came, its bytes read as UTF-8 if they are that, and
    otherwise in the mail's own charset, or Windows-1252. Many senders write a subject in 8-bit
    bytes with no encoded word, which the email package gives back with each byte a U+FFFD:
    "Tu código" was "Tu c��digo", and the code after it was no longer after a word for one."""
    value = next((value for key, value in message.raw_items() if key.lower() == name.lower()),
                 None)
    if value is None:
        return None
    data = str(value).encode("utf-8", "surrogateescape")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        charset = next((found for part in message.walk()
                        if (found := part.get_content_charset())), None)
        codec = _codec(charset, "cp1252")
        try:
            return data.decode("cp1252" if codec == "utf-8" else codec, "replace")
        except LookupError:
            return data.decode("cp1252", "replace")


def _words(text: str) -> str:
    """A header with its encoded words read by their charset as Thunderbird reads it (see
    CODECS), and anything else as it is. A run of words side by side in one charset is read as
    one, as Thunderbird reads it: many senders split a long subject every so many bytes, through
    a character if one falls there, and each word read alone, "Seu código" was "Seu c��digo",
    and the code after it was no longer after a word for one. What can't be read stays as it
    came."""
    text = BETWEEN_WORDS.sub("", text)
    out, at, run = [], 0, None      # run: [codec, its bytes so far, where its first word starts]
    for found in ENCODED_WORD.finditer(text):
        word = _word(found)
        if run and (found.start() > at or word is None or word[0] != run[0]):
            out.append(_run(text, *run, at))
            run = None
        out.append(text[at:found.start()])
        if word is None:
            out.append(found[0])
        elif run:
            run[1] += word[1]
        else:
            run = [*word, found.start()]
        at = found.end()
    if run:
        out.append(_run(text, *run, at))
    return "".join(out) + text[at:]


def _word(found: re.Match) -> tuple[str, bytes] | None:
    """(the codec an encoded word is read with, its bytes), or None if it can't be read."""
    charset, kind, data = found.groups()
    try:
        if kind in "Bb":
            return _codec(charset.split("*")[0]), _base64(data)
        return (_codec(charset.split("*")[0]),
                email.quoprimime.header_decode(data).encode("latin-1", "replace"))
    except (ValueError, UnicodeError):
        return None


def _run(text: str, codec: str, data: bytes, start: int, end: int) -> str:
    """A run of encoded words in one charset read as one, or as it came if it can't be."""
    try:
        return data.decode(codec, "replace")
    except (LookupError, UnicodeError, ValueError, TypeError):
        return text[start:end]


def _parsed(header: bytes, text: bytes) -> email.message.Message | None:
    try:
        return email.message_from_bytes(header.rstrip(b"\r\n") + b"\r\n\r\n" + text,
                                        policy=email.policy.compat32)
    except (RecursionError, ValueError, TypeError, LookupError):
        return None


def _author(message) -> str:
    """The sender line as it came, its encoded words not read: only the address in it counts
    (see mail._sender), and that is plain ASCII. Two sender lines are nobody's."""
    found = message.get_all("From") or []
    return _flat(_header(message, "From")) if len(found) == 1 else ""


def _subject(message) -> str:
    """The subject with its encoded words read (see _words), and its 8-bit bytes (see _header).
    Nothing here raises over a charset's name, which is the sender's own text: see _search."""
    return _flat(_words(_header(message, "Subject") or ""))


def _decoded(part, cut: bool = False) -> str:
    """A part's text, from as much of it as was fetched: base64 cut short still decodes. Its last
    group is padded, not dropped, as one that ends a part with no padding would lose the last word
    of it, and "482 913 is your Acme code." was read as "... Acme cod". A single letter left over
    holds no whole byte. Quoted-printable cut short (cut) loses an escape the cut went through,
    as its "=" and the digit after it are no escape: "=E7=B", read as "\\xe7=B", was no UTF-8 but
    before its end, and was read as Windows-1252 (see _guessed)."""
    try:
        encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
        if encoding == "base64":
            data = _base64(str(part.get_payload()))
        else:
            data = part.get_payload(decode=True) or b""
            if cut and encoding == "quoted-printable":
                data = QP_CUT.sub(b"", data)
        label = part.get_content_charset()
        if label is None and part.get_content_type() == "text/html":
            meta = META_CHARSET.search(data, 0, META_BYTES)
            label = meta and meta[1].decode("ascii")
        try:
            return data.decode(_codec(label) if label else _guessed(data), "replace")
        except (LookupError, ValueError, TypeError):
            return data.decode("utf-8", "replace")
    except (ValueError, TypeError, LookupError, AttributeError):
        return ""


def _guessed(data: bytes) -> str:
    """The codec of a part that names no charset: UTF-8 if it is that, as nearly all are, but for
    a character the fetch cut at its end. Else, of the older charsets mail in the five languages
    the support scope names comes in, Windows-1252 where most of its bytes past ASCII stand one at
    a time, as the accents of Spanish or Portuguese do; Windows-1256 or GB18030 where nearly all
    its letters past ASCII are Arabic or Chinese ones in it; and Windows-1252 otherwise. Read in
    GB18030, an accent and the letter after it are one Chinese character between two Latin
    letters, "c骴igo", which no Chinese text has: so where more than one in ten are that, it is
    not Chinese. Windows-1256 reads any bytes, and GBK read in it is mostly Arabic letters too,
    with a Latin accent or a sign glued to them, "ؤرéض¤", which Arabic never writes but for its
    own quotes (see GLUED_TO_ARABIC)."""
    if _decodes(data, "utf-8") is not None:
        return "utf-8"
    runs = [len(run) for run in HIGH_RUN.findall(data)]
    if 2 * runs.count(1) >= sum(runs):
        return "cp1252"
    for codec, script in GUESSES:
        text = _decodes(data, codec)
        if text is None:
            continue
        letters = [at for at, char in enumerate(text) if char.isalpha() and not char.isascii()]
        ours = [at for at in letters if script.match(text, at)]
        between = sum(1 for at in ours if at and LATIN.match(text, at - 1)
                      and LATIN.match(text, at + 1))
        odd = len(GLUED_TO_ARABIC.findall(text)) if codec == "cp1256" else 0
        if ours and 10 * len(ours) >= 9 * len(letters) and 10 * between <= len(ours) \
                and 50 * odd <= len(ours):
            return codec
    return "cp1252"


def _decodes(data: bytes, codec: str) -> str | None:
    """data read in codec, but for a character cut short at its end, or None if it isn't that."""
    try:
        return codecs.getincrementaldecoder(codec)().decode(data, final=False)
    except UnicodeDecodeError:
        return None


def _base64(text: str) -> bytes:
    letters = re.sub(r"[^A-Za-z0-9+/]", "", text)
    letters = letters[:len(letters) - (len(letters) % 4 == 1)]
    return base64.b64decode(letters + "=" * (-len(letters) % 4))


def _unhtml(text: str) -> str:
    """HTML as text, with each line's spaces made one and mail.JOIN where an inline tag stood
    (see INLINE). One beside a space is that space."""
    text = _untagged(BREAK.sub("\n", _skipped(_uncommented(text.replace(mail.JOIN, " ")))))
    return "\n".join(_spaced(line) for line in html.unescape(text).splitlines())


def _spaced(line: str) -> str:
    return SPACING.sub(lambda run: " " if run[0].strip(mail.JOIN) else mail.JOIN,
                       line).strip(" " + mail.JOIN)


def _uncommented(text: str) -> str:
    """HTML without its comments, each to its first end, as nothing: made a space,
    "48<!-- -->29<!-- -->13" read "48 29 13", where Thunderbird's index has 482913. A comment
    with no end after it stays, and no later one is looked for, as none can end either."""
    out, at = [], 0
    while (start := text.find("<!--", at)) >= 0 and (end := text.find("-->", start + 4)) >= 0:
        out.append(text[at:start])
        at = end + 3
    return "".join(out) + text[at:]


def _skipped(text: str) -> str:
    """HTML without its scripts, styles and head, each to its first closer, in one pass. An
    opener with no closer after it stays, and its name is looked for no more: no closer comes
    after a later one either. So each closer is looked for over any part of the text at most
    once, where a lazy match read on to the end from every opener."""
    out, at, start, unclosed = [], 0, 0, set()
    while opener := SKIPPED_HTML.search(text, start):
        name = opener[1].lower()
        closer = None if name in unclosed else CLOSER[name].search(text, opener.end())
        if closer is None:
            unclosed.add(name)
            start = opener.end()
            continue
        out += [text[at:opener.start()], " "]
        at = start = closer.end()
    return "".join(out) + text[at:]


def _untagged(text: str) -> str:
    """Tags made spaces. A "<" with no ">" after it is no tag, and looking for its end from each
    one in turn read on to the end of the text every time, so nothing past the last ">" is
    looked at."""
    end = text.rfind(">") + 1
    return TAG.sub(" ", JOINED.sub(mail.JOIN, INLINE.sub("", text[:end + 1]))) + text[end + 1:]


def _body(message, fetched_cut: bool = False) -> tuple[str, bool]:
    """(its plain text, or its HTML as text; whether that was cut short). Attachments are never
    read. Where the fetch stopped short, only the part it stopped in, the last, was cut: a whole
    plain part before an attachment or a long HTML part is read as it is. HTML cut inside a tag
    loses that tag, or its name would be read as a word."""
    leaves = [part for part in message.walk() if not part.is_multipart()]
    plain = marked = None
    for part in leaves:
        if _attached(part):
            continue
        kind = part.get_content_type()
        cut = fetched_cut and part is leaves[-1]
        if kind == "text/plain" and plain is None:
            plain = (LINE_ENDS.sub("\n", _decoded(part, cut).replace(mail.JOIN, " ")), cut)
        elif kind == "text/html" and marked is None:
            marked = (_decoded(part, cut), cut)
    text, cut = plain if plain and plain[0].strip() else (
        (_unhtml(_closed(*marked)), marked[1]) if marked else ("", False))
    first = INDEXED.match(text)
    return (first[0], cut or first.end() < len(text)) if first else (text, cut)


def _short(body: str, cut: bool) -> bool:
    """Whether a mail's text was cut short where it was fetched before 0.5.0's 20,000 characters,
    as Thunderbird's index counts them."""
    return cut and mail.indexed(body) < mail.SCAN_CHARS


def _attached(part) -> bool:
    """Whether a part is an attachment. One whose filename can't be read is: the email package
    raised for a filename in IDNA, whose codec refuses to replace what it can't decode."""
    try:
        return bool(part.get_filename()) or str(part.get("Content-Disposition", "")).lower(
            ).startswith("attachment")
    except (ValueError, LookupError, TypeError, email.errors.MessageError):
        return True


def _closed(html: str, cut: bool) -> str:
    """HTML without a tag the cut left open."""
    start = html.rfind("<")
    return html[:start] if cut and start > html.rfind(">") else html


def _read_on(imap, read: list[tuple], until: float) -> list[tuple]:
    """read, as _rows has it, with each mail cut short in the part that is read before 0.5.0's
    20,000 characters fetched on, MORE_BYTES at a time in one fetch for them all, and read again,
    until it holds them, its part ends or READ_ON_BYTES of it have come: so that its text is as
    much as Thunderbird's index held of it for 0.5.0, and its talk of signing in is that text's
    own. Asked of the server instead, a server that matches whole words only didn't find "100%
    authentic" or "opiniones verificadas", where 0.5.0's verif\\w+ and authenti\\w+ did, and one
    that searches the markup found "signin" in a link; and the 40 KB of a shop's HTML fetched in
    one go held a third of those 20,000 characters. A mailbox's mails take no more than half of
    what a search may take, which leaves the rest for Sent. A mail that can't be fetched on in
    that, in time or at all, or read again, stays as it was, and is never fetched on again: each
    round fetches from where the last one stopped, so a mail left out of one, fetched at the next,
    skipped the 24 KB between and was read as whole."""
    at, given_up = TEXT_BYTES, set()
    while at < READ_ON_BYTES and time.monotonic() <= until:
        room = (MAX_TAKEN // 2 - imap.taken) // (MORE_BYTES + 256)
        listed = [uid for uid, *_, body, cut in read if uid not in given_up and _short(body, cut)]
        short = listed[:max(0, room)]
        given_up.update(listed[len(short):])
        if not short:
            break
        typ, data = _ask(imap, "FETCH", ",".join(map(str, short)),
                         f"(UID BODY.PEEK[TEXT]<{at}.{MORE_BYTES}>)")
        more, at, again = _fetched(data) if typ == "OK" else {}, at + MORE_BYTES, []
        for uid, every, found, *message in read:
            rest = more.get(uid, {}).get(b"TEXT") if uid in short else None
            grown = rest is not None and {**found, b"TEXT": found.get(b"TEXT", b"") + rest}
            longer = grown and _read(grown, at)
            if longer:
                found, message = grown, longer
            elif uid in short:
                given_up.add(uid)
            again.append((uid, every, found, *message))
        read = again
    return read


def _talking(imap, uids: list[int], until: float) -> set[int]:
    """Of these mails, cut short before 0.5.0's 20,000 characters even where they were fetched on
    to, the ones that talk of signing in anywhere in their text, past the cut too: one search for
    all of them, as the server reads each whole, and one more for each word that isn't ASCII,
    which goes as a UTF-8 literal. Any it can't be asked about, in time or at all, are taken to."""
    if not uids:
        return set()
    if time.monotonic() > until:
        return set(uids)
    listed, plain = ",".join(map(str, uids)), [term for term in TALK_TERMS if term.isascii()]
    replies = [_ask(imap, "SEARCH", "UID", listed,
                    "OR " * (len(plain) - 1) + " ".join(f'BODY "{term}"' for term in plain))]
    for term in TALK_TERMS:
        if not term.isascii() and replies[-1][0] == "OK":
            if time.monotonic() > until:
                return set(uids)
            imap.literal = term.encode()
            replies.append(_ask(imap, "SEARCH", "CHARSET", "UTF-8", "UID", listed, "BODY"))
    if any(typ != "OK" for typ, _data in replies):
        return set(uids)
    return set().union(*map(_uids, replies)) & set(uids)


def _subject_cut(message, header: bytes) -> bool:
    """Whether the fetch of the header stopped inside the subject: it is the last line read."""
    names = [name.lower() for name in message.keys()]
    return len(header) >= HEADER_BYTES and names[-1:] == ["subject"] and names.count("subject") == 1


def _select(imap, box: str) -> bool:
    """Opened read-only, so nothing in it can change: EXAMINE, not SELECT."""
    try:
        return imap.select(box, readonly=True)[0] == "OK"
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        return False


def _rows(imap, box: str, folder: int, stems: list[mail.Term], until: float,
          last: float | None = None) -> list[tuple]:
    """Rows as mail._rows makes them, from one mailbox. The server is asked nothing past until,
    but what it gave by then is read and hidden up to last, as that asks nothing of it: on a
    slow server, fetching on took all the search's time, and every mail of the mailbox was then
    thrown away, those read whole too."""
    last = until if last is None else last
    if time.monotonic() > until or not _select(imap, box):
        return []
    picked = _picked(_hits(imap, stems, until))
    if not picked:
        return []
    asked = (f"(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS ({FIELDS})]<0.{HEADER_BYTES}> "
             f"BODY.PEEK[TEXT]<0.{TEXT_BYTES}>)")
    typ, data = _ask(imap, "FETCH", ",".join(str(uid) for uid, _ in picked), asked)
    fetched = _fetched(data) if typ == "OK" else {}
    read = []
    for uid, every in picked:
        if time.monotonic() > last:     # a mail can be slow to read, and this runs no further
            break
        found = fetched.get(uid)
        message = found and _read(found)
        if message:
            read.append((uid, every, found, *message))
    read = _read_on(imap, read, until)
    talking = _talking(imap, [uid for uid, *_, body, cut in read if _short(body, cut)], until)
    rows = []
    for uid, every, found, message, body, cut in read:
        if time.monotonic() > last:
            break
        kept = _hidden(stems, message, found, body, every, cut, uid in talking)
        if kept:
            rows.append(((folder, uid), _stamp(found.get(b"INTERNALDATE")), folder, *kept))
    return rows


# One stranger's mail that can't be read costs that mail, never the mailbox it is in: a filename
# the email package raised over ended the mailbox, and the search found nothing for every
# question that mail matched. So whatever goes wrong over one mail skips it.

def _read(found: dict, asked: int = TEXT_BYTES) -> tuple | None:
    """(message, its text, whether that was cut short) of one fetched mail, of whose text asked
    bytes were fetched, or None."""
    text = found.get(b"TEXT", b"")
    try:
        message = _parsed(found.get(b"HEADER", b""), text)
        return message and (message, *_body(message, len(text) >= asked))
    except Exception:
        return None


def _hidden(stems: list[mail.Term], message, found: dict, body: str, every: bool, cut: bool,
            talking: bool) -> tuple | None:
    """mail._kept of one mail, or None."""
    try:
        return mail._kept(stems, _author(message), _subject(message), body, every,
                          _subject_cut(message, found.get(b"HEADER", b"")), cut, talking,
                          mail.JOIN)
    except Exception:
        return None


def _wrote(imap, owner: str, before: str | None = None) -> bool:
    """Whether the open mailbox holds a mail from exactly this address, one that came before that
    day if one is given. The server's FROM matches the name beside an address too, so the sender
    lines it finds are read here."""
    since = ("BEFORE", before) if before else ()
    uids = sorted(_uids(_ask(imap, "SEARCH", *since, "FROM", f'"{owner}"')))
    if not uids:
        return False
    typ, data = _ask(imap, "FETCH", ",".join(map(str, uids[:FROM_CHECKED])),
                         f"(UID BODY.PEEK[HEADER.FIELDS (FROM)]<0.{FROM_BYTES}>)")
    for found in (_fetched(data) if typ == "OK" else {}).values():
        message = _parsed(found.get(b"HEADER", b""), b"")
        if message and mail._sender(_author(message))[1] == owner:
            return True
    return False


def _known(imap, boxes: list[str], sent: str | None, owners: set[str], own: str,
           until: float) -> set[str]:
    """The senders whose first mail came at least SETTLED_DAYS ago. A mail from one of the
    user's own addresses that isn't in Sent is anyone's, as those are the easiest to forge: the
    account's own, and any other that mail in Sent is from, as mail._own has it. Gmail's All
    Mail holds what the user sent, so an address they send from as well was otherwise a sender of
    long standing. Out of time counts as never: the warning costs less than its absence."""
    before = _imap_date(datetime.now(timezone.utc) - timedelta(days=mail.SETTLED_DAYS))
    found, owners = set(), {owner for owner in owners if owner and owner != own}
    for box in boxes:
        if not owners - found or time.monotonic() > until or not _select(imap, box):
            continue
        for owner in sorted(owners - found):
            if time.monotonic() > until:
                break
            if _wrote(imap, owner, before):
                found.add(owner)
    return found - _own(imap, sent, found, until)


def _own(imap, sent: str | None, owners: set[str], until: float) -> set[str]:
    """Of these addresses, the ones that mail in Sent is from, and any there was no time to look
    for. Only senders found to be known are looked for, as no one else can lose by it."""
    if not sent or not owners:
        return set()
    if time.monotonic() > until or not _select(imap, sent):
        return set(owners)
    return {owner for owner in sorted(owners)
            if time.monotonic() > until or _wrote(imap, owner)}


def _search(account: Account, token: str, stems: list[mail.Term], until: tuple) -> tuple:
    """(the best of one account as mail.settled gives them, what to say if it failed). Runs in
    a thread. Out of time or cut off, what was found so far will do, and so it does whatever else
    went wrong. An error's own text never leaves here, as it can be a stranger's: a subject's
    charset name came back to the voice model as the tool's error, and one such mail failed
    every search it matched."""
    try:
        imap = _signed_in(account, token, until[1])
    except Unusable as e:
        return [], str(e)
    except Exception:
        return [], UNREACHABLE
    rows, scored = [], []
    try:
        boxes, sent = _boxes(imap)
        for folder, box in enumerate([*boxes, *([sent] if sent and sent not in boxes else [])]):
            rows += _rows(imap, box, folder, stems, until[0], (until[0] + until[1]) / 2)
        scored = mail._ranked(rows, stems, {len(boxes)} if sent and sent not in boxes else set())
        owners = {item[3].owner for item in mail.checked(scored) if not item[3].by_user}
        known = _known(imap, boxes, sent, owners, account.address,
                       min(time.monotonic() + LOOKUP_S, until[1]))
        return mail.settled(scored, lambda owner: owner in known), ""
    except Exception:
        try:
            return mail.settled(scored or mail._ranked(rows, stems, set()),
                                lambda _owner: False), ""
        except Exception:
            return [], ""
    finally:
        try:
            imap.shutdown()
        except Exception:
            pass


def _outcome(result) -> tuple:
    """One search thread's (best, what to say if it failed), with anything it raised said in
    Tux's own words: see _search."""
    if isinstance(result, tuple):
        return result
    return [], str(result) if isinstance(result, Unusable) else UNREACHABLE


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
            for account, token in accounts), return_exceptions=True), until[1] - started + 1)
    except TimeoutError:
        raise RuntimeError(UNREACHABLE) from None
    results = [_outcome(result) for result in results]
    if all(failed for _best, failed in results):
        raise RuntimeError(results[0][1])
    best = sorted((item for found, _failed in results for item in found),
                  key=lambda item: item[:3], reverse=True)
    return [item[3] for item in best]
