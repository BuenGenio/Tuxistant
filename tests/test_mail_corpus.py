"""Mail search against 0.5.0, done systematically: every code shape senders use, every mark that
can stand between its groups or glue it to the word before or after, every encoding, and ordinary
mail with dates, ranges, prices, counters, phone and order numbers, each read by 0.5.0 (the copy
kept in test_mail_differential, which agrees with the 0.5.0 tag on every one of these mails) and by
both of this version's paths. No code 0.5.0 hid may reach the model, and no ordinary number 0.5.0
read out may be hidden, but for what docs/CONTRACT.md step 5 and docs/ROADMAP.md say.

Every mail is made up. What no one sees is written as an escape here, as in test_mail_differential.
"""
import html
import re
import sys
import unicodedata
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_mail_differential import (  # noqa: E402
    index_text, meant_to_hide, meant_to_show, old_read, online_read, report, shown,
    thunderbird_read)
from tuxistant import mail  # noqa: E402

# ---- code shapes ------------------------------------------------------------------------------
DIGITS = ("4829", "48291", "482913", "4829137", "48291375")
MIXED = ("KQ7X9B", "kq7x9b", "A7K2Q9XB", "7KQX9B", "K7Q9X", "KQWXBT", "AB12CD34")
GROUPS = (("48", "29", "13"), ("482", "913"), ("4829", "1375"), ("482", "913", "75"),
          ("KQ7", "X9B"), ("KQW", "XBT"), ("KQ7X", "9B2M"), ("48", "2913"), ("4829", "13"))
# Every mark a sender, a template or smart punctuation puts between the groups of a code.
SEPS = (" ", "-", "–", "—", "·", "•", "‑", "−", "\u00a0",
        "\u2009", "\u202f", "\u3000", "－", "‐", "‒", "―", "﹣",
        "・", "∙", "⋅", "‧", "⁃", "˗", "․", "．",
        "\u00ad", "\u200b", "\u2060", "\ufeff", "\u2066", "\u2069", "\u200e", "\u061c",
        "\u2068\u2069", "…", "⋯", "‥", "＿", "﹍")
NFKC = ("４８２９１３", "ＫＱ７Ｘ９Ｂ",
        "\U0001d7d2\U0001d7d6\U0001d7d0\U0001d7d7\U0001d7cf\U0001d7d1",
        "٤٨٢٩١٣", "४८२९१३",
        "４８２ ９１３", "48２913", "๔๘๒๙๑๓")
SPELT = ("4 8 2 9 1 3", "K Q 7 X 9 B")


def code_shapes() -> list[tuple[str, str]]:
    shapes = [(c, "digits") for c in DIGITS] + [(c, "mixed") for c in MIXED]
    shapes += [(sep.join(groups), "grouped " + " ".join(f"U+{ord(c):04X}" for c in sep))
               for groups in GROUPS for sep in SEPS]
    return shapes + [(c, "nfkc") for c in NFKC] + [(c, "spelt") for c in SPELT]


# ---- what stands round a code --------------------------------------------------------------
# Each shape goes in each of these: sign-in talk, a word for a code before or after it, a line
# to itself, the scripts that write none, and an amount after it, as a payment's code is sent.
CORE_BODIES = (
    "Your Acme verification code is {C}.", "{C} is your Acme code.",
    "Use {C} to sign in to Acme.", "Hi,\n\nEnter this on the page:\n\n{C}\n\nThanks",
    "認証コードは{C}です。",
    "您的验证码是{C}，请勿泄露。",
    "인증번호는 {C}입니다.",
    "Enter {C} – EUR 49.99 at ACME SHOP. This code expires in 5 minutes.",
    "To approve, enter {C} · USD 25.00 · ACME SHOP. It expires in 5 minutes.",
    "Sign in with {C} — Euro Car Parks",
)
# And the plainest of each kind in these as well: real senders' wording, other languages, dates,
# prices, phone and order numbers beside it, and the code twice.
MORE_BODIES = (
    "Your one-time passcode: {C}\nIt expires in 10 minutes.", "Your Acme PIN is {C}.",
    "G-{C} is your Acme code.", "Your security code: {C}\nDo not share it with anyone.",
    "Your OTP for login is {C}. Valid for 10 mins. Do not share. -Acme Bank",
    "{C}\nThat is your Acme code.", "Login code: {C}", "Your Acme code is: {C} (expires in 10 minutes)",
    "Hi Sam,\n\nTo finish signing in, type {C} on the page.\n\nThe Acme team",
    "Votre code de vérification est {C}.", "Ihr Bestätigungscode lautet {C}.",
    "Tu código de verificación es {C}.", "Twój kod to {C}.",
    "Ваш код: {C}",
    "認証コード：{C}（10分間有効）",
    "รห\u0e31ส OTP ค\u0e37อ {C} (Ref: ABCD)",
    "Your code is {C}. Requested on 12/03/2026 at 10:42 from +44 20 7946 0000.",
    "Order #10482913: your delivery code is {C}, total £49.99.",
    "Your code {C} – 12 March 2026 – £49.99", "Code: {C} | £49.99 | 0800 123 4567",
    "Your code is {C}, 2026–2027 season.", "Your code is {C} (12:30–13:00).",
    "Your code is {C}. Your code again: {C}.",
    "To approve the payment of EUR 49.99, enter {C}. It expires in 5 minutes.",
)
SUBJECTS = (("{C} is your Acme code", "Thanks for signing up."), ("Acme: {C}", "Use it to sign in."),
            ("Your Acme code", "{C}"), ("Your Acme code: {C}", "Thanks."))

# ---- glue -------------------------------------------------------------------------------------
DASHES = "".join(chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Pd") \
    + "−˗➖⁃"
PUNCTUATION = (":;,.!?/|()[]{}*_#'\"«»“”‘’…‥․⋯"
               "，。、：；．／～~→⇒=+&@%<>·•"
               "﹐﹒︰﹕‧・∙⋅´`^〔〕（）"
               "「」【】")
UNSEEN = ("\u200b\u200c\u200d\u2060\ufeff\u00ad\u202a\u202c\u202e\u2061\u2063\u2066\u2068"
          "\u2069\u061c\ue000\x1f\ufe0f\u180e\u034f")
SPACES = " \n\t\u00a0\u3000\u2028"
GLUES = (("dash", DASHES), ("punctuation", PUNCTUATION), ("unseen", UNSEEN), ("space", SPACES),
         ("none", ("",)))
# What may follow a code: words, names and amounts, some of them a currency's code or name.
AFTER = ("Verification ends soon", "DO NOT SHARE IT", "10 minutes left", "Acme team",
         "Ron, Acme IT", "Ron\nAcme", "Aud", "Rubén, Acme support", "Audífonos Acme",
         "Zarà", "Euro Bank", "Euro Car Parks", "Pounds Lane", "EUR", "Euro", "USD", "%",
         "EUR 49.99 at ACME SHOP", "USD 25.00 pending", "GBP 10 charge", "CHF 129.95", "SEK",
         "€ charge pending", "$", "£", "¥", "₹",
         "请勿泄露", "5分钟内有效",
         "です", "입니다",
         "ใช\u0e49ได\u0e49", "บาท")        # ใช\u0e49ได\u0e49, บาท
BEFORE = ("Your code", "Verification", "PIN", "Step 1", "Step 10", "12", "2025", "10:30", "G",
          "验证码")                                                # 验证码
# A code beside an ordinary number, joined by a dash, a dot or an ellipsis, either way round.
NEIGHBOURS = ("2026", "12:30", "£49.99", "10", "2pm", "12", "1", "10 minutes",
              "12/03/2026", "EUR 49.99", "+44 20 7946 0000", "Order 10482913")
JOINS = ("–", "—", "·", "…", "•", " – ", "‑", "-")

# ---- HTML -------------------------------------------------------------------------------------
HTML = ("<p>Your Acme verification code is <b>{C}</b>.</p>",
        "<p>Your Acme code is</p><table><tr>{CELLS}</tr></table>",
        "<p>Use this to sign in:</p><div>{SPANS}</div>",
        "<p>Use this to sign in:</p><div>{PAIRS}</div>",
        "<p>To sign in, use <b>{C}</b>&mdash;10 minutes left.</p>",
        "<p>To sign in, use <b>{C}</b> &ndash; EUR 49.99 at ACME SHOP</p>",
        "<p>Your Acme code:</p><p style=\"font-size:24px\">{C}</p><p>Ron</p>",
        "<p>Your Acme code is <b>{ENTITIES}</b>.</p>",
        "<p>Your Acme code is <b>{NDASHED}</b>.</p>",
        "<p>Your Acme code is <b>{NBHYPHEN}</b>.</p>")


def _html(shape: str, code: str) -> str:
    letters = code.replace(" ", "")
    half = len(letters) // 2
    return (shape.replace("{C}", html.escape(code))
            .replace("{CELLS}", "".join(f"<td>{c}</td>" for c in letters))
            .replace("{SPANS}", "".join(f"<span>{c}</span>" for c in letters))
            .replace("{PAIRS}", "".join(f"<span>{letters[i:i + 2]}</span>"
                                        for i in range(0, len(letters), 2)))
            .replace("{ENTITIES}", "".join(f"&#{ord(c)};" for c in letters))
            .replace("{NDASHED}", letters[:half] + "&ndash;" + letters[half:])
            .replace("{NBHYPHEN}", letters[:half] + "&#8209;" + letters[half:]))


# ---- ordinary mail ----------------------------------------------------------------------------
ORDINARY = (
    # dates and times
    "12 March 2026", "12/03/2026", "2026-03-12", "12.03.2026", "2026年3月12日",
    "12–15 March", "12—15 March", "Mon 12–Fri 16", "10:30", "2pm–4pm",
    "9am–5pm", "10:30–11:00", "9:00–17:30", "14時30分",
    # years and ranges
    "2024–25", "2025–2026", "1998–2004", "2025/26", "6–120", "10–100%",
    "Ages 4–104", "10–20%", "12·50", "12…15", "3–5 days", "Pages 12–345",
    # prices
    "£1,250.00", "$49.99", "USD 125", "£1250", "1500 Euro", "1500 €", "1500 EUR",
    "1500 RON", "450 CHF", "1250 pounds", "1250 euros", "12800円", "5000美元",
    "3500万円", "1500만원", "1250달러", "1500บาท", "€1500",
    "1500€", "1500 SEK",
    # counters
    "1500ポイント", "1500积分", "1500포인트", "1200개",
    "1205号室", "2500名", "1500 points",
    # phone and order numbers
    "0800 123 4567", "+44 20 7946 0000", "(555) 010-4477", "1-800-555-0199", "03-1234-5678",
    "10482913", "ORD-2291-AB", "INV-2026-0042", "BA 2491", "GBP 450", "BAW 123", "12345-6",
    "204-4567890-1234567", "4829-1375", "No. 48213")
FRAMES = (
    ("Your booking with Acme", "Hello,\n\nIt is {N}.\n\nThanks"),
    ("Your order from Acme", "Hello,\n\nWe have {N} for you.\n\nThanks"),
    ("Your Acme booking: {N}", "Your boarding pass is attached."),
    ("Your Acme booking", "Your booking code is HMX4K2.\n\nWe hold {N} for you."),
    ("Your Acme booking", "Your booking code is HMX4K2. Valid for {N} only."),
    ("Your Acme offer", "Use promo code SPRING10 for {N} off."),
    ("Your Acme membership", "Your member code: M-4821\nSeason {N}, see you soon."),
    ("Message from Acme", "認証コード：482913\n有効期限：{N}"),
    ("Message from Acme", "주문번호: 20261234\n수량: {N}"),
    ("Message from Acme", "订单号码：10482913\n金额：{N}"),
    ("Your Acme order", "Log in to see your order. Total: {N}"),
)
BOOKING_HTML = "<p>Your booking code is <b>HMX4K2</b>.</p><p>Valid for {N} only.</p>"
# Whole mails in the languages whose words for a code this version added, with a word of theirs
# that holds the letters of one but names none: a postcode, a barcode, points, before.
LANGUAGE_MAILS = (
    ("Acme", "צברת 1500 נקודות בחודש מרץ", "1500"),
    ("Acme", "יתרת הנקודות שלך: 2500", "2500"),
    ("Acme", "ההזמנה 48213 נשלחה. החבילה תגיע לנקודת האיסוף", "48213"),
    ("Acme", "כפי שכתבנו קודם, ההזמנה 48213 בדרך", "48213"),
    ("Acme", "מיקוד 6100000, טלפון 03-1234567", "03-1234567"),
    ("Acme", "ברקוד 72901234 על החבילה", "72901234"),
    ("Acme", "รห\u0e31สไปรษณ\u0e35ย\u0e4c 10110 เบอร\u0e4cโทร 081-234-5678", "081-234-5678"),
    ("Acme", "Streckkod: 73123456. Leverans 12/03/2026", "73123456"),
    ("Acme", "Strekkode 73123456 på pakken", "73123456"),
)


def corpus() -> list[dict]:
    mails = []

    def add(family, kind, why, subject, plain, tokens, html_=None, cte="8bit", after=None):
        mails.append({"family": family, "kind": kind, "why": why, "subject": subject,
                      "plain": plain, "html": html_, "cte": cte, "tokens": tokens,
                      "after": after})

    for code, shape in code_shapes():
        plainest = shape in ("digits", "mixed", "nfkc", "spelt")
        for body in CORE_BODIES + (MORE_BODIES if plainest else ()):
            add(shape, "code", f"{body[:30]!r} | {code!r}", "Message from Acme",
                body.replace("{C}", code), [code])
        for subject, body in SUBJECTS if plainest else ():
            add(shape, "code", f"subject {subject!r} | {code!r}", subject.replace("{C}", code),
                body.replace("{C}", code), [code])
    for family, glues in GLUES:
        for glue in glues:
            name = f"U+{ord(glue):04X}" if glue else "nothing"
            for word in AFTER:
                add(f"glued after by {family}", "code", f"{name} then {word[:12]!r}",
                    "Message from Acme", f"Hello from Acme. To sign in, use 482913{glue}{word}.",
                    ["482913"], after=(glue, word))
            for word in BEFORE:
                add(f"glued before by {family}", "code", f"{word!r} then {name}",
                    "Message from Acme", f"Hello from Acme. {word}{glue}482913 is what to type.",
                    ["482913"])
            add(f"glued by {family}", "code", f"{name} | 'KQ7X9B'", "Message from Acme",
                f"Hello from Acme. To sign in, use KQ7X9B{glue}Verification ends soon.", ["KQ7X9B"])
    for code in ("482913", "4829", "48 29 13"):
        for number in NEIGHBOURS:
            for join in JOINS:
                add("beside a number", "code", f"{code!r} U+{ord(join.strip()):04X} {number!r}",
                    "Message from Acme", f"To sign in, use {code}{join}{number}.", [code])
                add("beside a number", "code", f"{number!r} U+{ord(join.strip()):04X} {code!r}",
                    "Message from Acme", f"Sign in to Acme. {number}{join}{code} is the one.",
                    [code])
    for code in ("482913", "KQ7X9B", "482 913", "482–913"):
        for body in CORE_BODIES:
            for cte in ("qp", "base64"):
                add(cte, "code", f"{body[:24]!r} | {code!r}", "Message from Acme",
                    body.replace("{C}", code), [code], cte=cte)
    for code in ("482913", "KQ7X9B", "48291375", "4829", "KQWXBT"):
        for shape in HTML:
            for cte in ("8bit", "qp"):
                add("html", "code", f"{cte} {shape[:34]!r} | {code!r}", "Message from Acme",
                    None, [code], _html(shape, code), cte)
    for number in ORDINARY:
        for subject, body in FRAMES:
            add("ordinary", "ordinary", f"{subject[:20]!r} {body[:30]!r} | {number!r}",
                subject.replace("{N}", number), body.replace("{N}", number), [number])
        for cte in ("8bit", "qp"):
            add("ordinary html", "ordinary", f"{cte} | {number!r}", "Your Acme booking", None,
                [number], BOOKING_HTML.replace("{N}", html.escape(number)
                                               .replace("–", "&ndash;")), cte)
        add("beside a code", "both", f"{number!r} | '482913'", "Message from Acme",
            f"Hello from Acme. It is {number}. Your verification code is 482913.",
            ["482913", number])
    for subject, body, number in LANGUAGE_MAILS:
        add("language", "ordinary", f"{body[:30]!r} | {number!r}", subject, body, [number])
    return mails


# ---- what docs/ROADMAP.md and CONTRACT.md step 5 say is read out or hidden on purpose -------
CAPITALS = re.compile(r"(?-i:[A-Z]{3})\b")      # a currency's code in capitals: "482913 CAD team"
SIGN = re.compile(r"[%€£$¥₩₹₽₴]|บาท")
SPACE_LIKE = set(SPACES) | set(UNSEEN) | {""}


def given_up(m: dict) -> bool:
    """A code before a currency's code in capitals, or glued to an amount by a character no one
    sees, which the model reads as a space, or straight to a currency's sign, as no sender writes
    a code; and pairs of letters and digits in HTML (see meant_to_show)."""
    if m["html"] is not None and meant_to_show(m, None):
        return True
    if m["after"] is None or m["after"][0] not in SPACE_LIKE:
        return False
    word = m["after"][1]
    if CAPITALS.match(word) and word[:3] not in ("DO ", "ACM"):
        return True
    return bool(SIGN.match(word)) or word.lower().startswith(("eur", "usd", "gbp", "pounds"))


def meant(m: dict, number: str) -> bool:
    """As test_mail_differential has it, and a range of three digits and three or four and four
    after a word for a code, a number glued to Chinese, Japanese or Korean in a mail that talks of
    signing in, and a date in letters and digits there, "12-Mar-2026"."""
    return meant_to_hide(m, number) or bool(
        re.fullmatch(r"(?:Rooms |No\. )?\d{3}[–—]\d{3}|\d{4}[–—]\d{4}", number))


class Systematically(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.read = []
        for m in corpus():
            text = index_text(m)
            cls.read.append((m, old_read(m["subject"], text),
                             thunderbird_read(m["subject"], text), online_read(m)))

    def test_what_no_one_sees_is_written_as_an_escape_here(self):
        unseen = [f"line {n}: U+{ord(char):04X}" for n, line in enumerate(
            Path(__file__).read_text(encoding="utf-8").splitlines(), 1) for char in line
            if char != " " and unicodedata.category(char) in ("Cc", "Cf", "Co", "Cn", "Zs", "Mn")]
        self.assertEqual(unseen, [])

    def test_there_are_enough_mails(self):
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "ordinary"), 4000)
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "code"), 800)

    def test_no_code_hidden_by_0_5_0_reaches_the_model(self):
        shown_now = defaultdict(list)
        for m, old, tb, goa in self.read:
            code = m["tokens"][0]
            if m["kind"] != "ordinary" and shown(code, old) == "no" and not given_up(m):
                for path, read in (("Thunderbird", tb), ("Online Accounts", goa)):
                    if shown(code, read) != "no":
                        shown_now[f"{path}, {m['family']}"].append(f"{m['why']}: {read!r}")
        if shown_now:
            self.fail(report(shown_now, "codes 0.5.0 hid that reach the model"))

    def test_no_ordinary_number_0_5_0_read_out_is_hidden(self):
        hidden_now = defaultdict(list)
        for m, old, tb, goa in self.read:
            number = m["tokens"][-1]
            if m["kind"] != "code" and shown(number, old) == "whole" and not meant(m, number):
                for path, read in (("Thunderbird", tb), ("Online Accounts", goa)):
                    if shown(number, read) != "whole":
                        hidden_now[f"{path}, {m['family']}"].append(f"{m['why']}: {read!r}")
        if hidden_now:
            self.fail(report(hidden_now, "ordinary numbers 0.5.0 read out that are hidden"))


if __name__ == "__main__":
    unittest.main()
