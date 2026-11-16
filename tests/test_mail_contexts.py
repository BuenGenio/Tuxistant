"""Mail search against 0.5.0 in every context the hiding reads differently, done systematically:
each code put after a word for a code with no sign-in talk, in a subject that names it, in German,
on its own line and in HTML, glued to the word before or after it by every dash, punctuation mark,
space and character no one sees; year-like codes after every word for "is"; codes beside dates,
prices, phone and order numbers, repeated, split by inline tags, in forms NFKC changes, and after
a word for a code in every language the list has; and ordinary years, ranges, prices, phone and
order numbers after every word for "is" and a colon, on a line of their own, and after a password
in quotes. Each mail is read by 0.5.0 (test_mail_differential's copy, which agrees with the 0.5.0
tag on every one of these mails) and by both of this version's paths. Nothing 0.5.0 hid may reach
the model, in any of these mails, as 0.5.0's rules run too (tuxistant/mail_floor.py), and no
ordinary number 0.5.0 read out whole may be hidden, but for what docs/CONTRACT.md step 5 and
docs/ROADMAP.md say ("documented" below).

Every mail is made up. What no one sees is written as an escape.
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
    floor_broken, index_text, old_read, online_read, report, shown, thunderbird_read)

# ---- glue --------------------------------------------------------------------------------------
DASHES = "".join(chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Pd") \
    + "−˗➖⁃⁻₋"
PUNCTUATION = (":;,.!?/|()[]{}*_#'\"«»“”‘’…‥․⋯"
               "，。、：；．／～~→⇒=+&@%<>"
               "·•‧・∙⋅●■▪◦"
               "﹐﹒︐︰︙﹕︓﹣﹢﹪％＄＠"
               "´`^〔〕（）「」【】⁄∕⧸"
               "№™°§¶†‡※")
UNSEEN = ("\u200b\u200c\u200d\u2060\ufeff\xad\u200e\u200f\u202a\u202c\u202e\u2061\u2063"
          "\u2066\u2068\u2069\u061c\ue000\x1f\x7f\ufe0f\ufe00\u180e\u034f\u3164\u115f\uffa0"
          "\U000e0020\U000e0001\u17b4\u2800")
SPACES = " \n\t\xa0\u2009\u202f\u3000\u2028\u2029\x0b\x0c\x85"
GLUES = (("dash", DASHES), ("punctuation", PUNCTUATION), ("unseen", UNSEEN),
         ("space", SPACES), ("none", ("",)))
FEW_GLUES = ("", " ", "-", "–", "—", "·", "…", ".", ",", ":", "/",
             "\u2066", "\u200b", "\ufe0f", "\u3000", "－")
AFTER = ("Verification ends soon", "10 minutes left", "Acme team", "Euro Car Parks",
         "EUR 49.99 at ACME SHOP", "CAD team", "€ charge pending", "%", "2026",
         "12/03/2026", "£49.99", "+44 20 7946 0000", "Order 10482913",
         "请勿泄露", "です", "만 입력하세요")
BEFORE = ("Your code", "PIN", "Verification", "Step 1", "12", "2025", "10:30", "12/03/2026",
          "£49.99", "EUR", "Order 10482913", "G", "验证码",
          "인증번호", "รห\u0e31ส", "Ihr Code lautet")
AFTER_CONTEXTS = (("code word, no talk", "Message from Acme", "Your Acme code is {X}."),
                  ("subject names it", "Your Acme code", "Hello,\n{X}\nThanks"),
                  ("german", "Nachricht von Acme", "Dein Acme-Code lautet {X}."))
BEFORE_CONTEXTS = (("code word after", "Message from Acme", "Hello. {X} is your Acme code."),
                   ("own line", "Message from Acme", "Hello,\n\n{X}\n\nThe Acme team"))


def _nfkc_glue(glue: str) -> bool:
    """A symbol NFKC makes letters, $, % or @ of, such as №, ™ or a wide $, which glues a code to
    the word before it too."""
    return bool(re.search("[A-Za-z$%@]", unicodedata.normalize("NFKC", glue)))


# ---- HTML -------------------------------------------------------------------------------------
HTML_AFTER = ("<p>Your Acme code is <b>{C}</b>{G}{W}</p>",
              "<p>To sign in, use <b>{C}</b>{G}{W}</p>",
              "<p>Your Acme code:</p><p><b>{C}</b>{G}{W}</p>")
HTML_GLUES = ("&mdash;", "&ndash;", "&middot;", "&bull;", "&hellip;", "&nbsp;", "&#8209;",
              "&#8288;", "&zwnj;", "&shy;", "&#xFE0F;", "&thinsp;", " &ndash; ", "&#x2066;",
              "</b><span>", "<br>", "</p><p>", "")
HTML_WORDS = ("10 minutes left", "EUR 49.99 at ACME SHOP", "Euro Car Parks", "CAD team",
              "2026", "12/03/2026", "Verification ends soon", "です", "€ 49,99")
# A code split by inline tags, which a browser and Thunderbird's index join.
SPLITS = (("48", "29", "13"), ("482", "913"), ("4829", "13"), ("4829", "1375"),
          ("4", "8", "2", "9", "1", "3"), ("KQ7", "X9B"), ("KQ", "7X", "9B"), ("2005",))
INLINE = ("<span>{}</span>", "<b>{}</b>", "<td>{}</td>", "{}<wbr>", "<span class=\"d\">{}</span>")
SPLIT_SHAPES = ("<p>To sign in, use {S} on the page.</p>", "<p>Your Acme code is {S}.</p>",
                "<p>Use this to sign in:</p><p>{S}</p>",
                "<p>Use this to sign in:</p><table><tr>{S}</tr></table>")

# ---- year-like codes --------------------------------------------------------------------------
LEAD_INS = ("Your Acme code is {Y}", "Your Acme PIN IS {Y}", "Your verification code: {Y}",
            "Your Acme code is: {Y}", "Code={Y}", "Your Acme code was {Y}",
            "Your Acme PIN has been set to {Y}", "Acme code\n{Y}", "Dein Code ist {Y}",
            "Ihr Code lautet {Y}", "Votre code est {Y}", "Tu código es {Y}",
            "Il tuo codice è {Y}", "Seu código é {Y}", "Din kod är {Y}",
            "Din kode er {Y}", "Váš kód je {Y}", "Twój kod jest {Y}",
            "Mã OTP của bạn là {Y}", "Kode Anda adalah {Y}",
            "Ο κωδικός είναι {Y}",
            "הקוד שלך הוא {Y}",
            "رمزك هو {Y}", "認証コード：{Y}",
            "인증번호: {Y}", "Your Acme code is <b>{Y}</b>")
FOLLOWERS = ("", ".", "—10 minutes left.", "–30 min", "·15 min", "…15 min",
             "—2031", " – 10 minutes", "-10 minutes", " (10 minutes)",
             "\n10 minutes left", "–25", "–06", "/10", "—Acme", " EUR",
             "–EUR 49.99", "，5分钟内有效")

# ---- beside an ordinary number, repeated, NFKC --------------------------------------------------
NEIGHBOURS = ("2026", "12:30", "£49.99", "10", "2pm", "1", "10 minutes", "12/03/2026",
              "EUR 49.99", "49,99 €", "+44 20 7946 0000", "Order 10482913", "0800 123 4567",
              "No. 48213", "ORD-2291-AB", "1500 RON", "#10482913")
JOINS = ("–", "—", "·", "…", "•", " – ", "‑", "-", " ",
         ", ", "; ", " | ", " / ", " (", "\n", "\xa0", "\u2066", "\u200b")
REPEATS = ("Your Acme code is {A}. Enter {B} on the page.", "{A}\n\nThat is your code: {B}",
           "Use {A} to sign in.\n\n{B}", "Your code: {A} ({B})", "Acme {A} / {B}",
           "Hello,\n\n{A}\n\nYou can also type {B} in the app.")
REPEAT_FORMS = (("482913", "482913"), ("482913", "482 913"), ("482913", "482-913"),
                ("482913", "4 8 2 9 1 3"), ("482913", "４８２９１３"),
                ("482–913", "482913"), ("KQ7X9B", "KQ7 X9B"), ("KQ7X9B", "K Q 7 X 9 B"),
                ("2005", "2005"), ("4829", "4829"))
NFKC = ("４８２９１３", "４８２－９１３",
        "４８２・９１３", "４８２\u3000９１３",
        "４８２．９１３", "ＫＱ７ｘ９Ｂ",
        "\U0001d7d2\U0001d7d6\U0001d7d0\U0001d7d7\U0001d7cf\U0001d7d1",
        "④⑧②⑨①③", "⁴⁸²⁹¹³",
        "482﹣913", "48２913", "٤٨٢٩١٣",
        "4\ufe0f\u20e38\ufe0f\u20e32\ufe0f\u20e39\ufe0f\u20e31\ufe0f\u20e33\ufe0f\u20e3",
        "&#52;&#56;&#50;&#57;&#49;&#51;")
NFKC_BODIES = ("Your Acme code is {C}.", "Use {C} to sign in to Acme.", "{C} is your Acme code.",
               "Hi,\n\n{C}\n\nThanks", "認証コードは{C}です。",
               "Your Acme code is {C}—10 minutes left.")

# ---- every word for a code ---------------------------------------------------------------------
CODE_WORDS = ("Your Acme passcode:", "Your Acme PIN:", "Your OTP is", "Acme token:",
              "Your Acme password:", "Dein Bestätigungscode lautet", "Votre code est",
              "Tu código es", "Il tuo codice è", "Seu código é", "Uw code is",
              "Twój kod to", "Váš kód je", "Doğrulama kodunuz:",
              "Din engångskod är", "Din kode er", "Ваш код:",
              "Ο κωδικός σας "
              "είναι", "Mã xác nhận:",
              "Kode verifikasi Anda adalah", "आपका कोड:",
              "קוד האימות שלך:",
              "رمز التحقق:", "รห\u0e31ส OTP:",
              "验证码：", "認証番号：", "인증번호:",
              "口令：", "コード：")
WORD_AFTER = ("", ".", "—10 minutes left", "–EUR 49.99", " – EUR 49.99",
              "·USD 25.00", "…Euro Car Parks", " (12/03/2026)", ", +44 20 7946 0000",
              "\u2066Verification", "\ufe0fAcme", " valid until 2026", "。",
              "，5分钟")

# ---- ordinary mail ----------------------------------------------------------------------------
ORDINARY = (
    "12 March 2026", "12/03/2026", "2026-03-12", "12.03.2026", "2026年3月12日",
    "12–15 March", "10:30", "2pm–4pm", "9:00–17:30", "2026",
    "2024–25", "2025–2026", "2024—25", "2025—2026", "2024/25", "1999–00",
    "2024·25", "2025…2026", "2024-25", "2025-2026", "6–120", "10–100%",
    "£1,250.00", "$49.99", "USD 125", "£1250", "1500 Euro", "1500 €",
    "1500 EUR", "1500 RON", "1250 pounds", "49,99 €", "1500 ฿", "12800円",
    "1500บาท", "1500 kr", "1500 points", "0800 123 4567", "+44 20 7946 0000",
    "(555) 010-4477", "1-800-555-0199", "03-1234-5678", "10482913", "ORD-2291-AB",
    "INV-2026-0042", "BA 2491", "No. 48213", "4829-1375", "12345-6")
# (subject, body, documented): documented frames hide what CONTRACT step 5 says they hide.
FRAMES = (
    ("Your booking with Acme", "Hello,\n\nIt is {N}.\n\nThanks", False),
    ("Your Acme booking", "Season: {N}", False),
    ("Your Acme booking", "Season:\n{N}\nSee you there.", False),
    ("Your Acme booking", "Hello,\n\n{N}\n\nThanks", False),
    ("Your Acme booking", "Your booking code is HMX4K2. Season: {N}.", False),
    ("Your Acme booking", "Your booking code is HMX4K2. It is {N}.", False),
    ("Your Acme order", "Log in to see your order. Total: {N}", False),
    ("Your Acme order", "Log in to see your order.\nTotal:\n{N}", False),
    ("Acme: {N}", "Your order is on its way.", False),
    ("Ihre Acme-Buchung", "Ihr Buchungscode ist HMX4K2. Die Saison ist {N}.", False),
    ("Votre réservation Acme", "Votre code de réservation : HMX4K2. Période : {N}",
     False),
    ("Su reserva Acme", "Su código de reserva es HMX4K2. La temporada es {N}.", False),
    ("Acme", "Your password was changed. Valid: {N}", False),
    ("Acme", "Votre mot de passe est valable jusqu'au {N}.", False),
    ("Acme", "Votre mot de passe a été changé. Date : {N}", False),
    # The year in the sentence of a word for a code, after "es": as "Your code is 2026".
    ("Ihre Acme-Buchung", "Mit dem Gutscheincode SOMMER gibt es {N} wieder Rabatt.", True),
    # Whatever follows "is" after a word for a password is the password.
    ("Acme", "Ihr Passwort ist {N} abgelaufen.", True),
)
ORDINARY_HTML = ("<p>Your booking code is <b>HMX4K2</b>.</p><p>Season:</p><p>{N}</p>",
                 "<table><tr><td>Season:</td></tr><tr><td>{N}</td></tr></table>",
                 "<p>Academic year:</p><p>{N}</p>",
                 "<p>Log in to see your order.</p><p>Total: {N}</p>")
IS_WORDS = ("is", "IS", "are", "was", "will be", "changed to", "ist", "lautet", "est", "es",
            "é", "è", "är", "er", "je", "jest", "là", "adalah",
            "είναι", "هي", "هو", "היא",
            "הוא", ":", " :", "=", "：")
YEARS = ("2026", "2024–25", "2025–2026", "1999–00", "2025/26", "12/03/2026")
YEAR_FRAMES = (
    ("near a code word", "Your Acme booking", "Your booking code is HMX4K2. The season {I} {N}."),
    ("no code word", "Your Acme booking", "Hello,\n\nThe season {I} {N}.\n\nThanks"),
    ("own line", "Your Acme booking", "Hello,\n\nSeason {I}\n{N}\n\nThanks"),
    ("own line, code word", "Your Acme booking", "Your booking code is HMX4K2.\n\nSeason {I}\n{N}"),
    ("talk, own line", "Your Acme order", "Log in to see it.\n\nSeason {I}\n{N}"),
)
# A password after a colon, with a word for "is" in another language before it, which 0.5.0
# didn't know and so read past to the colon.
PASSWORD_LEADS = ("Votre mot de passe est temporaire : {P}",
                  "Votre mot de passe (il est temporaire) : {P}",
                  "Tu contraseña es provisional: {P}", "Sua senha é provisória: {P}",
                  "Ihr Passwort (bitte ändern Sie es danach): {P}",
                  "Ihr vorläufiges Passwort ist nur 24 Stunden gültig: {P}",
                  "Twoje hasło jest tymczasowe: {P}", "Uw wachtwoord is tijdelijk: {P}",
                  "Ваш пароль, он же временный: {P}", "Your password is temporary: {P}")
# What a mail about a password says after a colon that is no password: how long it is valid, or
# when it was changed, as a number glued to a counter, or in a sentence after the one about it.
NOTICES = (("パスワードを再設定しました。有効期限：{N}", ("24時間", "30分", "3日間", "2026年3月12日")),
           ("您的临时密码已发送到您的手机，有效期：{N}", ("30分钟", "24小时", "2026年3月12日")),
           ("パスワードの有効期限：{N}", ("2026年3月12日", "90日")),
           ("密码有效期：{N}", ("2026年3月12日", "72小時")),
           ("パスワードを変更しました。変更日時：{N}",
            ("2026年3月12日", "2026/03/12 10:30", "12.03.2026", "10:30")))
QUOTES = ("“{}”", "‘{}’", "«{}»", "‹{}›", "„{}“",
          "「{}」", "『{}』", "【{}】", "《{}》", "\"{}\"",
          "'{}'", "({})", "[{}]", "（{}）", "〔{}〕", "{}")
PASSWORD_LINES = ("初始密码：{P}，有效期至{N}。",
                  "仮パスワード：{P}有効期限は{N}"
                  "です。",
                  "임시 비밀번호: {P}, 유효기간 {N}",
                  "Temporary password: {P}, valid until {N}")
# A date glued to the Japanese after it, after a word for a password, is hidden as a code would
# be with spaces round it (docs/ROADMAP.md).
PASSWORD_DATES = (("2026年3月12日", False), ("12/03/2026", False),
                  ("2026-03-12", True))


def corpus() -> list[dict]:
    mails = []

    def add(family, kind, why, subject, plain, tokens, html_=None, cte="8bit", documented=False):
        """documented: an ordinary number that docs/CONTRACT.md step 5 says is hidden."""
        mails.append({"family": family, "kind": kind, "why": why, "subject": subject,
                      "plain": plain, "html": html_, "cte": cte, "tokens": tokens,
                      "documented": documented})

    def name(glue):
        return "+".join(f"U+{ord(c):04X}" for c in glue) or "nothing"

    for family, glues in GLUES:
        for glue in glues:
            for code in ("482913", "2005") if glue in FEW_GLUES else ("482913",):
                for word in AFTER:
                    for context, subject, body in AFTER_CONTEXTS:
                        x = code + glue + word
                        add(f"glued after by {family}, {context}", "code",
                            f"{code!r} {name(glue)} {word!r}", subject.replace("{X}", x),
                            body.replace("{X}", x), [code])
            if glue in FEW_GLUES or _nfkc_glue(glue):
                for word in BEFORE:
                    for context, subject, body in BEFORE_CONTEXTS:
                        x = word + glue + "482913"
                        add(f"glued before by {family}, {context}", "code",
                            f"{word!r} {name(glue)}", subject, body.replace("{X}", x),
                            ["482913"])
    for shape in HTML_AFTER:
        for glue in HTML_GLUES:
            for word in HTML_WORDS:
                for code in ("482913", "2005", "KQ7X9B"):
                    for cte in ("8bit", "qp"):
                        add("html", "code", f"{cte} {shape[:24]!r} {code} {glue!r} {word!r}",
                            "Message from Acme", None, [code],
                            shape.replace("{C}", code).replace("{G}", glue)
                            .replace("{W}", html.escape(word)), cte)
    for parts in SPLITS:
        for inline in INLINE:
            for shape in SPLIT_SHAPES:
                if ("<td>" in inline) == ("<tr>" in shape):
                    split = "".join(inline.format(part) for part in parts)
                    for cte in ("8bit", "qp"):
                        add("split by inline tags", "code", f"{cte} {inline!r} {parts!r}",
                            "Message from Acme", None, ["".join(parts)],
                            shape.replace("{S}", split), cte)
    for lead in LEAD_INS:
        for code in ("2005", "1987", "2031"):
            for follow in FOLLOWERS:
                body = lead.replace("{Y}", code) + follow
                if "<b>" in lead:
                    add("year-like", "code", f"{lead[:24]!r} {code} {follow!r}",
                        "Message from Acme", None, [code], f"<p>{body}</p>", "qp")
                else:
                    add("year-like", "code", f"{lead[:24]!r} {code} {follow!r}",
                        "Message from Acme", body, [code])
                    if "\n" not in body:
                        add("year-like in the subject", "code",
                            f"{lead[:24]!r} {code} {follow!r}", body, "Thanks.", [code])
    for code in ("482913", "2005"):
        for number in NEIGHBOURS:
            for join in JOINS:
                for context, body in (("talk", "To sign in, use {X}."),
                                      ("code word", "Your Acme code is {X}.")):
                    add(f"beside a number, {context}", "code", f"{code!r} {name(join)} {number!r}",
                        "Message from Acme", body.replace("{X}", code + join + number), [code])
                    add(f"beside a number, {context}", "code", f"{number!r} {name(join)} {code!r}",
                        "Message from Acme", body.replace("{X}", number + join + code), [code])
    for shape in REPEATS:
        for a, b in REPEAT_FORMS:
            add("repeated", "code", f"{shape[:24]!r} {a!r} {b!r}", "Message from Acme",
                shape.replace("{A}", a).replace("{B}", b), [a])
            add("repeated, in the subject", "code", f"{shape[:24]!r} {a!r} {b!r}",
                f"Your Acme code: {a}", shape.replace("{A}", b).replace("{B}", b), [a])
    for code in NFKC:
        for body in NFKC_BODIES:
            add("nfkc", "code", f"{body[:24]!r} {code!r}", "Message from Acme",
                body.replace("{C}", code), [html.unescape(code)])
        for cte in ("8bit", "qp"):
            add("nfkc, html", "code", f"{cte} {code!r}", "Message from Acme", None,
                [html.unescape(code)], f"<p>Your Acme code is <b>{code}</b>.</p>", cte)
    for words in CODE_WORDS:
        for code in ("482913", "2005", "KQ7X9B"):
            for after in WORD_AFTER:
                text = f"{words}{'' if words.endswith(chr(0xff1a)) else ' '}{code}{after}"
                add("every word for a code", "code", f"{words!r} {code!r} {after!r}",
                    "Message from Acme", text, [code])
                add("every word for a code, subject", "code", f"{words!r} {code!r} {after!r}",
                    text, "Thanks.", [code])
    for number in ORDINARY:
        for subject, body, documented in FRAMES:
            add("ordinary", "ordinary", f"{subject[:16]!r} {body[:34]!r} | {number!r}",
                subject.replace("{N}", number), body.replace("{N}", number), [number],
                documented=documented
                # letters and digits together on a line that talks of signing in
                or "Log in to see your order. Total: {N}" == body
                and bool(re.search(r"[A-Z].*\d|\d.*[A-Z]", number)))
        for shape in ORDINARY_HTML:
            for cte in ("8bit", "qp"):
                add("ordinary, html", "ordinary", f"{cte} {shape[:30]!r} | {number!r}",
                    "Your Acme booking", None, [number], shape.replace("{N}", html.escape(number)),
                    cte, documented=False)
    for word in IS_WORDS:
        for number in YEARS:
            for context, subject, body in YEAR_FRAMES:
                add(f"years after a word for is, {context}", "ordinary", f"{word!r} {number!r}",
                    subject, body.replace("{I}", word).replace("{N}", number), [number],
                    # a wide colon is a colon to the model, as 0.5.0's ":" was
                    documented=word == "：" and "code word" in context)
    for notice, numbers in NOTICES:
        for number in numbers:
            add("about a password", "ordinary", f"{notice[:16]!r} {number!r}", "Acme",
                notice.replace("{N}", number), [number])
    for lead in PASSWORD_LEADS:
        for password in ("soleil", "sonnenschein", "Abc12345"):
            add("password after a colon", "code", f"{lead[:30]!r} {password!r}", "Acme",
                lead.replace("{P}", password), [password])
            add("password after a colon, html", "code", f"{lead[:30]!r} {password!r}", "Acme",
                None, [password], f"<p>{html.escape(lead.replace('{P}', password))}</p>", "qp")
    for line in PASSWORD_LINES:
        for quote in QUOTES:
            for date, documented in PASSWORD_DATES:
                text = line.replace("{P}", quote.format("Abc12345")).replace("{N}", date)
                add("password in quotes", "both", f"{line[:10]!r} {quote!r} {date!r}", "Acme",
                    text, ["Abc12345", date],
                    documented=documented and "パス" in line)
    return mails


class InEveryContext(unittest.TestCase):
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
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "ordinary"), 15_000)
        self.assertGreater(sum(1 for m, *_ in self.read if m["kind"] != "code"), 2000)

    def test_no_piece_0_5_0_hid_reaches_the_model(self):
        """With no exceptions: a code glued to an amount or a currency by a character no one sees,
        or by a symbol NFKC makes letters of, a year glued past a tag to the number after it, and
        pairs of letters and digits two to a tag, which this version's own rules show, included."""
        broken = floor_broken(self.read)
        if broken:
            self.fail(report(broken, "pieces 0.5.0 hid that reach the model"))

    def test_no_ordinary_number_0_5_0_read_out_is_hidden(self):
        hidden_now = defaultdict(list)
        for m, old, tb, goa in self.read:
            number = m["tokens"][-1]
            if m["kind"] != "code" and shown(number, old) == "whole" and not m["documented"]:
                for path, read in (("Thunderbird", tb), ("Online Accounts", goa)):
                    if shown(number, read) != "whole":
                        hidden_now[f"{path}, {m['family']}"].append(f"{m['why']}: {read!r}")
        if hidden_now:
            self.fail(report(hidden_now, "ordinary numbers 0.5.0 read out that are hidden"))


if __name__ == "__main__":
    unittest.main()
