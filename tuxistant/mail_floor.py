"""0.5.0's rules for hiding sign-in codes and passwords, kept unchanged as a floor.

Everything between the two marked lines below is tuxistant/mail.py at the v0.5.0 tag, copied as
it was: the patterns its Thunderbird path hid codes and passwords by, and its _spans and _hide.
Don't change any of it, not even to fix it: a fix goes in mail.py's own rules.

mail.py runs these on both paths, on the subject and the text as 0.5.0 read them: from
Thunderbird's index, or from Online Accounts once a mail is decoded, with inline HTML tags taken
out as Thunderbird's index takes them out (see spans). Its own rules read the same mail whole,
and what either hides is hidden (see mail._floored), so no code or password 0.5.0 hid is shown
on either path. Those rules are best effort above this floor.

New here, after the second marked line, are only spans(), hidden() and put(): where in the text
as given 0.5.0 would hide, and a guard against the two kinds of text these rules are slow over
(see _slow).
"""
import bisect
import functools
import itertools
import re

SECRET_REACH = 160      # how far after the word for a secret the secret itself is looked for
SECRET_BEFORE = 60      # and how far before it: "482913 is your code"
HIDDEN = "[hidden]"

# ---- tuxistant/mail.py at v0.5.0, unchanged -----------------------------------------------
# Every other account sends its sign-in codes and passwords to the inbox, so whoever can read
# these out can get into the rest. What stands where one would is hidden, in three ways: after
# the word for a secret, anything shaped like one; in a mail that talks of signing in, every
# number long enough to be a code; and a code that has a line to itself. Too much is hidden
# rather than too little: a promo code and a booking's reference go too.
SPACE = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
UNSEEN = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")
SECRET_WORD = re.compile(
    r"(?<![A-Za-z])(?:pass(?:words?|phrases?|codes?|wd)?|pwd|pins?|otp|keys?|tokens?)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?!post|bar|zip)[A-Za-zÀ-ÿ]*codes?(?![A-Za-z])|c[oó]dig\w*|codic\w*"
    r"|(?<![A-Za-z])k[oó]d\w*|код\w*|парол\w*"
    r"|passwort\w*|kennwort\w*|contrase[ñn]a\w*|senha\w*|(?<![A-Za-z])clave\w*|has[łl][oa]\w*"
    r"|mot de passe|wachtwoord\w*|コード|パスワード|暗証|码|碼|코드|번호|رمز", re.I)
# A password is whatever follows "is" or a colon, even a plain word.
PASSWORD_WORD = re.compile(
    r"(?:pass(?:words?|phrases?|wd)?|pwd|парол\w*|passwort\w*|kennwort\w*|contrase[ñn]a\w*"
    r"|senha\w*|clave\w*|has[łl][oa]\w*|mot de passe|wachtwoord\w*|パスワード|密码|密碼|비밀번호)", re.I)
GIVEN = re.compile(r"[^.!?\n]{0,60}?(?:\b(?i:is|are|was|will be|(?:changed|set|reset) to)\b\s*:?"
                   r"|[:=])\s*(\S{3,})")
# What a secret looks like, next to the word for one.
SHAPED = re.compile(
    r"(?<![A-Za-z0-9.,/-])\d(?:[ -]?\d){3,11}(?![\w/-]|[.,]\d)"    # 482913, 482 913, 4 8 2 9 1 3
    r"|(?<![\w@./-])(?=[\w!#$%^&*+=-]*\d)(?=[\w!#$%^&*+=-]*[A-Za-z])[\w!#$%^&*+=-]{4,24}(?![\w@])"
    r"|\b[A-Z]{2,6}(?:-[A-Z0-9]{2,6}){1,3}\b|\b[A-Z]{5,10}\b")   # KQW-XBT, KQWXB
NOT_A_SECRET = re.compile(r"\d+(?:st|nd|rd|th|am|pm|kg|km|gb|mb|x|s)|(?:19|20)\d\d", re.I)
JUST_GIVEN = re.compile(r"(?:\bis|[:=])\s*$", re.I)      # "code is 2048" is no year
# A mail that talks of signing in: there, any number of the right length may be the code.
SIGN_IN_TALK = re.compile(
    r"\b(?:sign(?:ing)?[- ]?in|log(?:ging)?[- ]?in|login|verif\w+|authenti\w+|one[- ]?time"
    r"|single[- ]use|expires?|valid for|2fa|two[- ](?:factor|step))\b", re.I)
MONEY = r"\s*(?:pounds|euros|dollars|gbp|eur|usd|%)"
NUMBER = re.compile(r"(?<![\w.,/£$€-])\d{3}[ -]\d{3}(?![\w/-])"
                    rf"|(?<![\w.,/£$€-])\d{{4,8}}(?![\w/-]|[.,]\d|{MONEY})", re.I)
ALONE = re.compile(      # a line that is nothing but a code
    r"^[ \t]*((?:\d[ -]?){4,12}"
    r"|(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{5,14})[ \t]*$", re.M)
YEAR = re.compile(r"(?:19|20)\d\d")


def _spans(text: str) -> list[tuple[int, int]]:
    """Where a secret may stand in a mail: see SECRET_WORD."""
    spans = []
    for word in SECRET_WORD.finditer(text):
        start, end = word.end(), word.end() + SECRET_REACH
        if PASSWORD_WORD.fullmatch(word[0]):
            given = GIVEN.match(text, start, end)
            if given:
                spans.append(given.span(1))
        for shaped in SHAPED.finditer(text, start, end):
            before = text[max(start, shaped.start() - 6):shaped.start()]
            if not NOT_A_SECRET.fullmatch(shaped[0]) or JUST_GIVEN.search(before):
                spans.append(shaped.span())
        for number in NUMBER.finditer(text, max(0, word.start() - SECRET_BEFORE), word.start()):
            if not YEAR.fullmatch(number[0]):
                spans.append(number.span())
    found = [*(NUMBER.finditer(text) if SIGN_IN_TALK.search(text) else ()), *ALONE.finditer(text)]
    spans += [one.span(one.lastindex or 0) for one in found if not YEAR.fullmatch(one[0].strip())]
    return sorted(spans)


def _hide(text: str) -> str:
    """A mail's text with whatever may be a sign-in code or a password taken out. Odd spaces
    are made plain first, as the model would read "482\u00a0913" as the code it is."""
    text = UNSEEN.sub("", SPACE.sub(" ", text))
    out, at = [], 0
    for start, end in _spans(text):
        if end > at:
            out += [text[at:max(start, at)], HIDDEN]
            at = end
    return "".join(out) + text[at:]


# ---- end of 0.5.0 ---------------------------------------------------------------------------


# 0.5.0 looked for a word that ends in "code" from every letter of a run of accented ones, each
# time to the end of the run, so 16,000 "é" took more than two seconds; and for a code after
# each word for one on its own, so "码!" 10,000 times over took most of a second. No mail anyone
# writes has a word of more than 100 such letters, or 500 words for a secret in what is read of
# it, and a text that has either is hidden whole: never less than 0.5.0 hid, and quick.
LONG_RUN = re.compile(r"[A-Za-zÀ-ÿ]{101,}", re.I)
MOST_WORDS = 500


def _slow(text: str) -> bool:
    return any(not run[0].isascii() for run in LONG_RUN.finditer(text)) or next(
        itertools.islice(SECRET_WORD.finditer(text), MOST_WORDS, None), None) is not None


@functools.cache
def _gone(glue: str) -> re.Pattern:
    """What 0.5.0 took out before it read a text, and the glue (see hidden)."""
    return re.compile(UNSEEN.pattern[:-1] + re.escape(glue) + "]") if glue else UNSEEN


def spans(text: str, glue: str = "") -> list[tuple[int, int]]:
    """Where in text, as given, 0.5.0's _hide would put [hidden]. It read the text with odd
    spaces made plain and what no one sees taken out (SPACE and UNSEEN), and here each glue
    character is taken out too: places are counted back past what was taken out."""
    gone = [found.start() for found in _gone(glue).finditer(text)]
    plain = _gone(glue).sub("", SPACE.sub(" ", text))
    if _slow(plain):
        return [(0, len(text))] if text else []
    shifts = [at - count for count, at in enumerate(gone)]    # where each stood in plain

    def place(at: int) -> int:
        return at + bisect.bisect_right(shifts, at)

    return [(place(start), place(end - 1) + 1) for start, end in _spans(plain)]


def hidden(text: str, glue: str = "") -> str:
    """The text as given with [hidden] where 0.5.0 would have put it, and nothing else changed.
    glue: characters that join what stands either side of them, as the inline HTML tags they
    stand for do in Thunderbird's index (see goamail._unhtml)."""
    return put(text, spans(text, glue))


def put(text: str, places: list[tuple[int, int]]) -> str:
    """The text with one [hidden] for each run of these places that overlap, which are in order."""
    out, at = [], 0
    for start, end in places:
        if end > at:
            out += [text[at:max(start, at)], HIDDEN]
            at = end
    return "".join(out) + text[at:]
