"""Mail search against 0.5.0: nothing 0.5.0 hid reaches the model on either path, as 0.5.0's rules
run too (tuxistant/mail_floor.py), and no ordinary number 0.5.0 read out is hidden, but for what
CHANGELOG and docs/CONTRACT.md step 5 say is hidden on purpose.

0.5.0's hiding, as it ran on Thunderbird's mail, is kept here as it was (the OLD_ names), so the
check needs no copy of 0.5.0. Every mail is made up, from a few thousand made of codes in the
shapes senders use, the words and marks round them, and ordinary mail with dates, prices, phone
numbers and references. Each is read three ways: by 0.5.0 from the text Thunderbird's index
holds; by this version from the same text, as mail._rows and mail._note hide it; and through
Online Accounts, from the mail as an IMAP server sends it (goamail._read, mail._kept, _note).
"""
import base64
import email.header
import hashlib
import html
import quopri
import random
import re
import sys
import unicodedata
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxistant import goamail, mail, mail_floor  # noqa: E402
from tuxistant.webfetch import clean  # noqa: E402

# ---- 0.5.0's hiding, from tuxistant/mail.py at the 0.5.0 tag ---------------------------------
OLD_SPACE = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
OLD_UNSEEN = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")
OLD_SECRET_WORD = re.compile(
    r"(?<![A-Za-z])(?:pass(?:words?|phrases?|codes?|wd)?|pwd|pins?|otp|keys?|tokens?)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?!post|bar|zip)[A-Za-zÀ-ÿ]*codes?(?![A-Za-z])|c[oó]dig\w*|codic\w*"
    r"|(?<![A-Za-z])k[oó]d\w*|код\w*|парол\w*"
    r"|passwort\w*|kennwort\w*|contrase[ñn]a\w*|senha\w*|(?<![A-Za-z])clave\w*|has[łl][oa]\w*"
    r"|mot de passe|wachtwoord\w*|コード|パスワード|暗証|码|碼|코드|번호|رمز", re.I)
OLD_PASSWORD_WORD = re.compile(
    r"(?:pass(?:words?|phrases?|wd)?|pwd|парол\w*|passwort\w*|kennwort\w*|contrase[ñn]a\w*"
    r"|senha\w*|clave\w*|has[łl][oa]\w*|mot de passe|wachtwoord\w*|パスワード|密码|密碼|비밀번호)", re.I)
OLD_GIVEN = re.compile(
    r"[^.!?\n]{0,60}?(?:\b(?i:is|are|was|will be|(?:changed|set|reset) to)\b\s*:?"
    r"|[:=])\s*(\S{3,})")
OLD_SHAPED = re.compile(
    r"(?<![A-Za-z0-9.,/-])\d(?:[ -]?\d){3,11}(?![\w/-]|[.,]\d)"
    r"|(?<![\w@./-])(?=[\w!#$%^&*+=-]*\d)(?=[\w!#$%^&*+=-]*[A-Za-z])[\w!#$%^&*+=-]{4,24}(?![\w@])"
    r"|\b[A-Z]{2,6}(?:-[A-Z0-9]{2,6}){1,3}\b|\b[A-Z]{5,10}\b")
OLD_NOT_A_SECRET = re.compile(r"\d+(?:st|nd|rd|th|am|pm|kg|km|gb|mb|x|s)|(?:19|20)\d\d", re.I)
OLD_JUST_GIVEN = re.compile(r"(?:\bis|[:=])\s*$", re.I)
OLD_SIGN_IN_TALK = re.compile(
    r"\b(?:sign(?:ing)?[- ]?in|log(?:ging)?[- ]?in|login|verif\w+|authenti\w+|one[- ]?time"
    r"|single[- ]use|expires?|valid for|2fa|two[- ](?:factor|step))\b", re.I)
OLD_MONEY = r"\s*(?:pounds|euros|dollars|gbp|eur|usd|%)"
OLD_NUMBER = re.compile(r"(?<![\w.,/£$€-])\d{3}[ -]\d{3}(?![\w/-])"
                        rf"|(?<![\w.,/£$€-])\d{{4,8}}(?![\w/-]|[.,]\d|{OLD_MONEY})", re.I)
OLD_ALONE = re.compile(
    r"^[ \t]*((?:\d[ -]?){4,12}"
    r"|(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{5,14})[ \t]*$", re.M)
OLD_YEAR = re.compile(r"(?:19|20)\d\d")


def old_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for word in OLD_SECRET_WORD.finditer(text):
        start, end = word.end(), word.end() + mail.SECRET_REACH
        if OLD_PASSWORD_WORD.fullmatch(word[0]):
            given = OLD_GIVEN.match(text, start, end)
            if given:
                spans.append(given.span(1))
        for shaped in OLD_SHAPED.finditer(text, start, end):
            before = text[max(start, shaped.start() - 6):shaped.start()]
            if not OLD_NOT_A_SECRET.fullmatch(shaped[0]) or OLD_JUST_GIVEN.search(before):
                spans.append(shaped.span())
        for number in OLD_NUMBER.finditer(text, max(0, word.start() - mail.SECRET_BEFORE),
                                          word.start()):
            if not OLD_YEAR.fullmatch(number[0]):
                spans.append(number.span())
    found = [*(OLD_NUMBER.finditer(text) if OLD_SIGN_IN_TALK.search(text) else ()),
             *OLD_ALONE.finditer(text)]
    spans += [one.span(one.lastindex or 0) for one in found
              if not OLD_YEAR.fullmatch(one[0].strip())]
    return sorted(spans)


def old_hide(text: str) -> str:
    text = OLD_UNSEEN.sub("", OLD_SPACE.sub(" ", text))
    out, at = [], 0
    for start, end in old_spans(text):
        if end > at:
            out += [text[at:max(start, at)], mail.HIDDEN]
            at = end
    return "".join(out) + text[at:]


def old_tokens(text: str) -> list[str]:
    """What 0.5.0's _hide took out of a text: one piece for each [hidden] it put in."""
    text = OLD_UNSEEN.sub("", OLD_SPACE.sub(" ", text))
    pieces = []
    for start, end in old_spans(text):
        if pieces and start < pieces[-1][1]:
            pieces[-1][1] = max(pieces[-1][1], end)
        else:
            pieces.append([start, end])
    return [text[start:end] for start, end in pieces]


def old_read(subject: str, text: str) -> str:
    """What 0.5.0 showed the model of a mail in Thunderbird's index: _rows, then _note."""
    return (clean(mail.URL.sub(" ", old_hide(subject)), mail.SUBJECT_CHARS) + "\n"
            + clean(mail.URL.sub(" ", old_hide(text)[:mail.BODY_CHARS * 2]), mail.BODY_CHARS))


# ---- this version's two paths ---------------------------------------------------------------
STEMS = mail.terms("Acme")
AUTHOR = "Acme <account@acme.example>"


def thunderbird_read(subject: str, text: str) -> str:
    subject, text = mail._secret_free(subject, text)
    return mail._shown(subject, mail.SUBJECT_CHARS) + "\n" + mail._shown(text, mail.BODY_CHARS)


def _part(text: str, subtype: str, cte: str, code: str = "") -> tuple[str, bytes]:
    data = text.replace("\n", "\r\n").encode()
    if cte == "qp":     # with a soft line break inside the code, as a server folding at 76 might
        encoded = quopri.encodestring(text.encode()).replace(b"\n", b"\r\n")
        if code.isascii() and code.encode() in encoded:
            encoded = encoded.replace(code.encode(), (code[:3] + "=\r\n" + code[3:]).encode(), 1)
    elif cte == "base64":
        encoded = base64.encodebytes(data).replace(b"\n", b"\r\n")
    else:
        encoded = data
    name = "quoted-printable" if cte == "qp" else cte
    return (f"Content-Type: text/{subtype}; charset=utf-8\r\n"
            f"Content-Transfer-Encoding: {name}\r\n"), encoded


def online_read(m: dict) -> str | None:
    """What the model is shown of a mail through Online Accounts, from the header fields and the
    text an IMAP FETCH gives (see goamail._rows), then mail._note's hiding."""
    subject = m["subject"] if m["subject"].isascii() else \
        email.header.Header(m["subject"], "utf-8").encode()
    head = f"From: {AUTHOR}\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
    parts = [_part(text, kind, m["cte"], m["tokens"][0])
             for text, kind in ((m["plain"], "plain"), (m["html"], "html")) if text is not None]
    if len(parts) == 1:
        header, text = (head + parts[0][0] + "\r\n").encode(), parts[0][1]
    else:
        header = (head + 'Content-Type: multipart/alternative; boundary="b"\r\n\r\n').encode()
        text = b"".join(b"--b\r\n" + h.encode() + b"\r\n" + body + b"\r\n" for h, body in parts)
        text += b"--b--\r\n"
    found = {b"HEADER": header, b"TEXT": text}
    read = goamail._read(found)
    kept = read and goamail._hidden(STEMS, read[0], found, read[1], True, read[2], False)
    if not kept:
        return None
    return mail._shown(kept[1], mail.SUBJECT_CHARS) + "\n" + mail._shown(kept[2], mail.BODY_CHARS)


def index_text(m: dict) -> str:
    """The body as Thunderbird's index holds it: the plain part, or the HTML with blocks on lines
    of their own, cells spaced, inline tags taken out and character references read."""
    if m["plain"] is not None:
        return m["plain"]
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d|table)\s*>", "\n", m["html"])
    text = html.unescape(re.sub(r"<[^>]*>", "", re.sub(r"(?i)</t[dh]\s*>", " ", text)))
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


# ---- what is shown of a token ---------------------------------------------------------------
def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(str(unicodedata.digit(c)) if unicodedata.category(c) == "Nd" else c
                   for c in text)


def _letters(token: str) -> str:
    return "".join(re.findall(r"[A-Za-z0-9]", _plain(token)))


def _edge(char: str) -> str:
    return "0-9" if char.isdigit() else "A-Za-z0-9"


def _standing(chars: str) -> str:
    return (f"(?<![{_edge(chars[0])}])" + r"[^A-Za-z0-9\n]{0,3}".join(map(re.escape, chars))
            + f"(?![{_edge(chars[-1])}])")


def _stands(chars: str, read: str) -> bool:
    """The characters in order, at most three others between any two, and nothing of their kind
    either side: "482913", "482 913", "4-8-2-9-1-3" and "K Q W X B T" all stand, and so does
    482913 in "482913Verification", as the model reads a run of digits glued to a word apart from
    it. A digit stops only at a digit, a letter at a letter or a digit."""
    return bool(re.search(_standing(chars), read))


def shown(token: str, read: str | None) -> str:
    """"whole", "part" (a run of at least three of its characters, or half if that is more,
    from its start, its middle or its end, as "913" is of "[hidden] 913") or "no"."""
    if read is None:
        return "no"
    read, chars = _plain(read), _letters(token)
    if _stands(chars, read):
        return "whole"
    least = max(3, (len(chars) + 1) // 2)
    return "part" if any(_stands(chars[i:j], read) for i in range(len(chars))
                         for j in range(i + least, len(chars) + 1)) else "no"


def times_shown(token: str, read: str | None) -> int:
    """How many times a token stands whole in what the model reads (see _stands), or for one with
    no letter or digit in ASCII, as 0.5.0 hid a word in Japanese after "パスワード：", how many
    times it is there as written, but for punctuation at either end."""
    if read is None:
        return 0
    read, chars = _plain(read), _letters(token)
    if chars:
        return len(re.findall(_standing(chars), read))
    core = re.sub(r"^[\W_]+|[\W_]+$", "", _plain(token))
    return read.count(core) if core else 0


def floor_broken(read: list) -> dict:
    """Of (mail, 0.5.0's read, Thunderbird's, Online Accounts'), each piece 0.5.0 took out of a
    mail's subject or text, as Thunderbird's index holds them, that either path shows: more often
    than 0.5.0 did, or in part where 0.5.0 showed no part of it, as an order number 10482913 that
    0.5.0 hid has a part that a code 482 913 beside it, which it showed, has too. Through Online
    Accounts the mail is read as its server sends it, so an HTML mail is not what the index
    holds, and still 0.5.0's pieces count."""
    found = defaultdict(list)
    for m, old, *now in read:
        for token in old_tokens(m["subject"]) + old_tokens(index_text(m)):
            before = times_shown(token, old)
            unseen = bool(_letters(token)) and shown(token, old) == "no"
            for path, seen in zip(("Thunderbird", "Online Accounts"), now):
                if times_shown(token, seen) > before or unseen and shown(token, seen) != "no":
                    found[f"{path}, {m.get('family', m['kind'])}"].append(
                        f"{m['why']}: {token!r} in {seen!r}")
    return found


# ---- the mail -------------------------------------------------------------------------------
CODES = ("4829", "482913", "48291375", "KQ7X9B", "kq7x9b", "A7K2Q9XB", "KQWXBT", "482 913",
         "48 29 13", "4829 1375", "482-913", "482.913", "482–913", "482—913",
         "482‑913", "482−913", "482·913", "482•913", "482\u00a0913",
         "482\u2009913", "482\u3000913", "482－913", "KQ7-X9B", "KQ7 X9B", "KQW-XBT",
         "KQW 9XB", "ABC-123", "4 8 2 9 1 3", "K Q 7 X 9 B",
         "４８２９１３", "ＫＱ７Ｘ９Ｂ",
         "\U0001d7d2\U0001d7d6\U0001d7d0\U0001d7d7\U0001d7cf\U0001d7d1",
         "٤٨٢٩١٣",
         # Groups whose last is one or two digits, which a dash and a number after them could
         # make the start of a range, and a code that is a year, which a dash and the minutes
         # after it could make one, "2005—10 minutes left".
         "482 91", "48-29-13", "2005")
BODIES = ("Your Acme verification code is {C}.", "{C} is your Acme code.",
          "Use {C} to sign in to Acme.", "Hi,\n\nEnter this on the page:\n\n{C}\n\nThanks",
          "Your one-time passcode: {C}\nIt expires soon.", "Your Acme PIN is {C}.",
          "Votre code de vérification est {C}.", "Ihr Bestätigungscode lautet {C}.",
          "您的验证码是：{C}，5分钟内有效。",
          "認証コード：{C}（10分間有効）",
          # Glued to the Japanese or Chinese round it, as those write a code with no space.
          "認証コードは{C}です。",
          "您的验证码是{C}，请勿泄露。",
          "Your code is {C}. Requested on 12/03/2026 at 10:42 from +44 20 7946 0000.",
          "Order #10482913: your delivery code is {C}, total £49.99.",
          "Your code is {C}. Your code again: {C}.",
          # Before a word that starts with a currency's letters, as a brand or a name may.
          "{C} Audible verification code", "{C} audio verification code",
          "To sign in, enter {C}\nAudrey\nAcme", "Sign in with {C} Ronald",
          "{C} Cadence Bank verification code",
          # Glued by a dash to the minutes after it, as an HTML template's &mdash; is, and after
          # a number of one or two digits and a dash, neither of which is a range.
          "Your Acme code is {C}—10 minutes left.", "Your Acme code is {C}–5 min.",
          "Your Acme code is 12–{C}.", "To sign in, use 1–{C}.")
# Every mark a code can be glued to the next word or the last by, and the words.
GLUES = ("-‐‑‒–—―−⸺﹘﹣－⁃˗➖"
         "〜〰־⸗⸚⹀︱︲\u00ad"
         ":;,.!?/|()[]*_#'\"«»“”‘’…"
         "，。、：；．／～~→=+&@%<>·•﹐﹒"
         # A space and a line end, before a word that starts with a currency's letters, and what
         # no one sees, which 0.5.0 read as a space: bidi controls, invisible operators, private
         # use and controls.
         " \n\u202a\u202c\u202e\u2061\u2063\u2066\u2068\u2069\u061c\ue000\x1f")
AFTER = ("Verification ends soon", "DO NOT SHARE IT", "SMS. Thanks", "10 minutes left",
         "2FA is on", "Acme team", "请勿泄露", "5分钟内有效", "Audible may ask again",
         "Ronald, Acme support",
         # A name that is a currency's code but in capitals, or starts like one, or is one's
         # name in the singular, which 0.5.0 didn't count.
         "Ron, Acme IT", "Ron\nAcme", "Aud", "Rubén, Acme support", "Audífonos Acme",
         "Dollar Bank",
         # The first letter of a currency of more than one, 만 of 만원, which by itself means
         # "only": "enter only 482913".
         "만 입력하세요")
BEFORE = ("Your code", "Verification", "PIN", "Step 10", "验证码")
HTML = ("<p>Your Acme verification code is <b>{C}</b>.</p>",
        "<p>Your Acme code is</p><table><tr>{CELLS}</tr></table>",
        "<p>Use this to sign in:</p><div>{SPANS}</div>",
        "<p>Use this to sign in:</p><div>{PAIRS}</div>",
        "<p>To sign in, use <b>{C}</b>&mdash;10 minutes left.</p>",
        "<p>验证码：<b>{C}</b>，5分钟内有效</p>")
ORDINARY = ("12 March 2026", "12/03/2026", "2026-03-12", "12-Mar-2026", "10:30", "2pm-4pm",
            "10am–12pm", "£1,250.00", "$49.99", "USD 125", "£1250", "0800 123 4567",
            "+44 20 7946 0000", "(555) 010-4477", "555-010-4477", "1-800-555-0199", "10482913",
            "ORD-2291-AB", "KX7PQ2", "6612", "INV-2026-0042", "BA 2491", "BA2491", "14C",
            "SW1A 1AA", "EC1 4AB", "12345-6", "0042-117", "4421", "AB-123456",
            "123-4567890-1234567", "COVID-19", "24-hour", "RX-7800", "4829-1375", "2024–25",
            # Amounts, flights and trains, which two groups of three are not.
            "GBP 450", "450 CHF", "CHF 129.95", "AUD 129.95", "500 UAH", "1500 UAH", "ICE 578",
            "BAW 123", "EZY 812",
            # Phone and order numbers joined by hyphens.
            "1-800-555-1234", "020-7946-0018", "0800-123-4567", "12-345678",
            "204-4567890-1234567",
            # Ranges written with an en dash, as an HTML template writes them, and times.
            "10:30–11:00", "14:00–18:00", "12–15 March", "10–20%", "10am-12pm",
            # Prices and dates in Chinese, Japanese and Korean, and the wide marks NFKC changes.
            "12800円", "3000元", "128000원", "2026年3月12日", "１２８，０００", "２０２６／０３／１２",
            # Years and times with an en dash, and ranges to three digits.
            "2026–2027", "2024–2025", "2pm–4pm", "9am–5pm", "6–120", "10–100%", "12·50", "12…15",
            # Counters, and currencies of more than one character or ten thousand of one,
            # written straight after a number.
            "1500ポイント", "1500积分", "1500포인트", "1500개", "1205号室", "5000美元", "1250港币",
            "3500万円", "1500만원", "1250달러", "1250ドル", "1500บาท",
            # A currency's name in the singular, or its sign, after the price.
            "1500 Euro", "1500 €")
FRAMES = (("Your booking with Acme", "Hello,\n\nIt is {N}.\n\nThanks"),
          ("Your order from Acme",
           "Hello,\n\nIt is {N}.\n\nLog in to your account to see your order."),
          ("Your Acme offer", "Hello,\n\nIt is {N}.\n\nThis offer expires on 31 March."),
          ("Your Acme pass", "Hello,\n\nIt is {N}.\n\nThanks."),
          ("Key information about your Acme stay", "Hello,\n\nIt is {N}.\n\nThanks."),
          ("Your Acme discount code inside", "Hello,\n\nIt is {N}.\n\nEnjoy."),
          ("Your Acme booking", "Your boarding pass is attached.\n\nIt is {N}.\n\nThanks."),
          ("Your Acme booking",
           "Ship to:\nA. Person\n1 High Street\nLondon\n{N}\nUnited Kingdom"),
          ("Your Acme receipt", "Hotel Alpenblick\nTotal\n{N}\nThank you for booking."),
          ("Your Acme booking: {N}", "Your boarding pass is attached."),
          ("Your Acme booking: {N}", "Log in to your account to see it."),
          # After a word for a code, as a booking's confirmation code is.
          ("Your Acme booking", "Your booking code is HMX4K2.\n\nIt is {N}."),
          # And not after "is", which made 0.5.0 hide a year or a time too.
          ("Your Acme booking", "Your booking code is HMX4K2.\n\nWe hold {N} for you."),
          ("Message from Acme", "認証コード：482913\n有効期限：{N}"),
          # Under a subject whose word for a code is also a number's, a QR code's or a barcode's:
          # "your order number", "booking number", "about your QR code", "your QR e-ticket".
          ("Acme: 您的订单号码", "Hello,\n\nIt is {N}.\n\nThanks"),
          ("[Acme] 예약번호 안내", "Hello,\n\nIt is {N}.\n\nThanks"),
          ("Acme QRコードのご案内", "Hello,\n\nIt is {N}.\n\nThanks"),
          ("Acme 您的二维码电子票", "Hello,\n\nIt is {N}.\n\nThanks"))
# Passwords with a mark in them that smart punctuation or a wide keyboard writes: each long
# enough that the half after the mark, shown, counts as part of it.
PASSWORDS = ("Blue…Horseshoe", "Blue‥Horseshoe", "Blue—Horseshoe", "Blue–Horseshoe",
             "Blue·Horseshoe", "Blue•Horseshoe", "Blue，Horseshoe", "Blue．Horseshoe",
             "Blue／Horseshoe")
PASSWORD_BODIES = ("The wifi password is {P} for the guests.", "Your new Acme password: {P}",
                   "Your temporary password is {P}. Change it after you sign in.",
                   "Passwort: {P}\nViele Grüße", "密码：{P}")
# The booking above as an HTML template writes it, with each en dash a character reference.
BOOKING = "<p>Your booking code is <b>HMX4K2</b>.</p><p>It is {N}.</p>"


def corpus() -> list[dict]:
    mails = []

    def add(kind, why, subject, plain, tokens, html_=None, cte="8bit"):
        mails.append({"kind": kind, "why": why, "subject": subject, "plain": plain,
                      "html": html_, "cte": cte, "tokens": tokens})

    for code in CODES:
        for body in BODIES:
            add("code", f"{body[:24]!r} | {code!r}", "Message from Acme",
                body.replace("{C}", code), [code])
        for subject, body in ((f"{code} is your Acme code", "Thanks for signing up."),
                              (f"Acme: {code}", "Use it to sign in."), ("Your Acme code", code)):
            add("code", f"subject {subject[:20]!r}", subject, body, [code])
    for code in ("482913", "KQ7X9B", "482 913"):
        for glue in GLUES:
            for word in AFTER:
                add("code", f"glued after by U+{ord(glue):04X} to {word[:6]!r} | {code!r}",
                    "Message from Acme", f"Hello from Acme. To sign in, use {code}{glue}{word}.",
                    [code])
            for word in BEFORE:
                add("code", f"glued before by U+{ord(glue):04X} to {word!r} | {code!r}",
                    "Message from Acme", f"Hello from Acme. {word}{glue}{code} is what to type.",
                    [code])
        for body in BODIES[:6]:
            for cte in ("qp", "base64"):
                add("code", f"{cte} {body[:24]!r} | {code!r}", "Message from Acme",
                    body.replace("{C}", code), [code], cte=cte)
        letters = code.replace(" ", "")
        for shape in HTML:
            markup = (shape.replace("{C}", code)
                      .replace("{CELLS}", "".join(f"<td>{c}</td>" for c in letters))
                      .replace("{SPANS}", "".join(f"<span>{c}</span>" for c in letters))
                      .replace("{PAIRS}", "".join(f"<span>{letters[i:i + 2]}</span>"
                                                  for i in range(0, len(letters), 2))))
            add("code", f"html {shape[:30]!r} | {code!r}", "Message from Acme", None, [code],
                markup)
    for password in PASSWORDS:
        for body in PASSWORD_BODIES:
            add("code", f"password {body[:16]!r} | {password!r}", "Message from Acme",
                body.replace("{P}", password), [password])
        add("code", f"html password | {password!r}", "Message from Acme", None, [password],
            f"<p>The wifi password is <b>{html.escape(password)}</b> for the guests.</p>")
    for number in ORDINARY:
        for subject, body in FRAMES:
            add("ordinary", f"{subject[:24]!r} {body[:30]!r} | {number!r}",
                subject.replace("{N}", number), body.replace("{N}", number), [number])
        for cte in ("8bit", "qp", "base64"):
            add("ordinary", f"html {cte} booking | {number!r}", "Your Acme booking", None,
                [number], BOOKING.replace("{N}", html.escape(number).replace("–", "&ndash;")),
                cte)
        for code in ("482913", "KQ7X9B"):
            add("both", f"next to {number!r} | {code!r}", "Message from Acme",
                f"Hello from Acme. It is {number}. Your verification code is {code}.",
                [code, number])
    return mails


# Three capitals beside three digits, an amount or a flight: never a code (see mail.AMOUNT).
AMOUNT = re.compile(r"[A-Z]{3} \d{3}(?:[.,]\d+)?|\d{3}(?:[.,]\d+)? [A-Z]{3}")
TIMES = re.compile(r"\d{1,2}(?:am|pm)(?:[-–]\d{1,2}(?:am|pm))?")     # "2pm-4pm" is no code


# What this version hides on purpose that 0.5.0 read out, as CHANGELOG and docs/CONTRACT.md step 5
# say: in a mail that talks of signing in, every number in either part and letters with digits,
# but for a time; whatever a subject that names a code reaches in the text; a postcode, or a
# range such as 0900–1730, on a line of its own; two groups of three beside a word for a secret,
# but not an amount; and a year or a time straight after a colon there, as NFKC makes "：" one.
def meant_to_hide(m: dict, number: str) -> bool:
    whole = f"{m['subject']}\n{index_text(m)}"
    talks = mail.SIGN_IN_TALK.search(whole)
    mixed = re.search(r"\d", number) and re.search(r"[A-Za-z]", number) \
        and not AMOUNT.fullmatch(number) and not TIMES.fullmatch(number)
    grouped = re.fullmatch(r"[A-Z0-9]{3} [A-Z0-9]{3}", number) and not AMOUNT.fullmatch(number)
    own_line = f"\n{number}\n" in whole and (grouped or re.fullmatch(r"\d+\W\d+", number))
    beside = grouped and mail.SECRET_WORD.search(whole)
    given = re.match(r"(?:19|20)\d\d(?!\d|年)|\d{1,2}(?:am|pm)", number) \
        and re.search(r"[:：]" + re.escape(number), whole) and mail.SECRET_WORD.search(whole)
    return bool((talks and (number in m["subject"] or mixed)) or "code" in m["subject"]
                or own_line or beside or given or (m["kind"] == "both" and mixed))


def once(text: str) -> str:
    """0.5.0 wrote [hidden] once for each of the secrets that overlapped, and mail_floor once."""
    return re.sub(rf"(?:{re.escape(mail.HIDDEN)})+", mail.HIDDEN, text)


def report(found: dict, what: str) -> str:
    return "\n".join(f"{path}, {len(seen)} {what}:\n  " + "\n  ".join(seen[:30])
                     for path, seen in found.items())


class AgainstFiveOh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.read = []
        for m in corpus():
            text = index_text(m)
            cls.read.append((m, old_read(m["subject"], text), thunderbird_read(m["subject"], text),
                             online_read(m)))

    def test_what_no_one_sees_is_written_as_an_escape_here(self):
        """An editor that strips such characters would change 0.5.0's patterns above unseen, and
        a reader can't tell "482\u00a0913" from "482 913"."""
        unseen = [f"line {n}: U+{ord(char):04X}" for n, line in enumerate(
            Path(__file__).read_text(encoding="utf-8").splitlines(), 1) for char in line
            if char != " " and unicodedata.category(char) in ("Cc", "Cf", "Co", "Cn", "Zs", "Mn")]
        self.assertEqual(unseen, [])

    def test_a_code_shown_in_part_counts_from_either_end(self):
        """Without its last characters, "[hidden] 913" read as nothing of "482 913" shown."""
        self.assertEqual(shown("482 913", "Your [hidden] 913 is what to type"), "part")
        self.assertEqual(shown("482 913", "Your 482 [hidden] is what to type"), "part")
        self.assertEqual(shown("KQ7X9B", "Your code [hidden] X9B"), "part")
        self.assertEqual(shown("Blue…Horseshoe", "The password is [hidden] Horseshoe"), "part")
        self.assertEqual(shown("482913", "Your code is [hidden]."), "no")

    def test_there_are_enough_mails(self):
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "ordinary"), 400)
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "code"), 400)

    def test_a_piece_0_5_0_hid_counts_as_shown_whole_or_in_part(self):
        """Its other places in the mail, which 0.5.0 showed, don't count; one more does."""
        self.assertEqual(old_tokens("Your code is 482913. Use 482913 now."), ["482913", "482913"])
        self.assertEqual(old_tokens("Your Acme PIN is 2047, expires in 2047 days"), ["2047"])
        self.assertEqual(times_shown("2047", "Your Acme PIN is [hidden], in 2047 days"), 1)
        self.assertEqual(times_shown("ご登録時", "パスワード:ご登録時に"), 1)
        broken = floor_broken([({"subject": "Acme", "plain": "Your PIN is 2047, in 2047 days",
                                 "html": None, "why": "", "kind": "code"},
                                "Acme\nYour PIN is [hidden], in 2047 days",
                                "Acme\nYour PIN is [hidden], in [hidden] days",
                                "Acme\nYour PIN is 2047, in 2047 days")])
        self.assertEqual(list(broken), ["Online Accounts, code"])
        self.assertEqual(floor_broken([({"subject": "Acme", "plain": "Your code is 482 913",
                                         "html": None, "why": "", "kind": "code"},
                                        "Acme\nYour code is [hidden]",
                                        "Acme\nYour code is [hidden] 913", None)]).keys(),
                         {"Thunderbird, code"})

    def test_no_piece_0_5_0_hid_reaches_the_model(self):
        """In any mail, a code, a password or anything else 0.5.0 hid, on either path, with no
        exceptions: 0.5.0's rules run too (see mail_floor)."""
        broken = floor_broken(self.read)
        if broken:
            self.fail(report(broken, "pieces 0.5.0 hid that reach the model"))

    def test_the_floor_is_0_5_0s_hiding(self):
        """mail_floor holds 0.5.0's rules as they were: the same patterns as the copy here, the
        same source, and what it hides, read as 0.5.0 read a text, is what 0.5.0 hid, with what no
        one sees and the glue that stands for an inline tag put in anywhere."""
        for name in ("SPACE", "UNSEEN", "SECRET_WORD", "PASSWORD_WORD", "GIVEN", "SHAPED",
                     "NOT_A_SECRET", "JUST_GIVEN", "SIGN_IN_TALK", "NUMBER", "ALONE", "YEAR"):
            floor, old = getattr(mail_floor, name), globals()["OLD_" + name]
            self.assertEqual((floor.pattern, floor.flags), (old.pattern, old.flags), name)
        source = Path(mail_floor.__file__).read_text(encoding="utf-8")
        block = source[source.index("# ---- tuxistant/mail.py at v0.5.0"):
                       source.index("# ---- end of 0.5.0")].split("\n", 1)[1]
        self.assertEqual(hashlib.sha256(block.encode()).hexdigest(),
                         "5cfb2962deaab766dc5addf7e0e5b5d0d90761246cc328467dfa76c45449a0e3")
        rng, odd = random.Random(5), ("\u200b", "\u00ad", "\ufeff", "\u00a0", mail.JOIN)
        for m, *_ in self.read:
            for text in (m["subject"], index_text(m)):
                for glue in ("", mail.JOIN):
                    marked = "".join(char + (rng.choice(odd) if rng.random() < 0.05 else "")
                                     for char in text)
                    floor = mail_floor.hidden(marked, glue)
                    read = mail_floor._gone(glue).sub("", mail_floor.SPACE.sub(" ", floor))
                    self.assertEqual(once(read), once(old_hide(marked.replace(glue, "")
                                                               if glue else marked)), marked)

    def test_the_floor_takes_nothing_from_this_versions_own_rules(self):
        """What this version's own rules hide with the floor gone, they hide with it there, on
        both paths: the mail they read has the floor's [hidden] in it, where a word that said it
        talks of signing in may have stood."""
        with mock.patch.object(mail_floor, "spans", lambda text, glue="": []):
            alone = [(thunderbird_read(m["subject"], index_text(m)), online_read(m))
                     for m, *_ in self.read]
        lost = defaultdict(list)
        for (m, _old, *now), before in zip(self.read, alone):
            for token in m["tokens"]:
                for path, seen, without in zip(("Thunderbird", "Online Accounts"), now, before):
                    if shown(token, without) == "no" and shown(token, seen) != "no":
                        lost[path].append(f"{m['why']}: {seen!r}")
        if lost:
            self.fail(report(lost, "tokens hidden without the floor but shown with it"))

    def test_no_ordinary_number_0_5_0_read_out_is_hidden(self):
        hidden_now = defaultdict(list)
        for m, old, tb, goa in self.read:
            number = m["tokens"][-1]
            if m["kind"] != "code" and shown(number, old) == "whole" \
                    and not meant_to_hide(m, number):
                for path, read in (("Thunderbird", tb), ("Online Accounts", goa)):
                    if shown(number, read) != "whole":
                        hidden_now[path].append(f"{m['why']}: {read!r}")
        if hidden_now:
            self.fail(report(hidden_now, "ordinary numbers 0.5.0 read out that are hidden"))


if __name__ == "__main__":
    unittest.main()
