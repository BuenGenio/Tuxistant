"""What stands between the model and anything that matters.

A handler never does something risky on its own say-so. It returns an Ask, which describes the
real action and carries the function that performs it; the daemon shows the description and runs
the function only after a click. The words come from daemon code looking at what will really
happen, never from a manifest or from the model. See "Approvals" in docs/CONTRACT.md.
"""
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

APPROVAL_WAIT_S = 45    # no answer counts as no
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 4000
MAX_NAME_CHARS = 300
LEVELS = ("click", "admin")
DECLINED = "OK, I've left that alone."     # the model tends to repeat this word for word
# In a result, this means the app reads the answer out itself. The voice model is then not asked
# to speak: holding the question and no answer, it made up a date in 2 of 8 tries at one question.
APP_SAYS = "the_app_reads_the_answer_aloud_next"

Confirm = Callable[["Ask"], Awaitable[bool]]


def printable(text: str, limit: int) -> str:
    """Text for an approval card, where what you see has to be what will happen. Control and
    bidirectional-override characters can hide or reorder part of a command, so they show up as
    visible escapes instead. A cut is always marked, and the caller keeps the whole text."""
    out = []
    for char in text:
        if char in "\n\t":
            out.append(char)
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    shown = "".join(out)
    return shown if len(shown) <= limit else f"{shown[:limit]}… ({len(shown) - limit} more characters)"


def shown(name, limit: int = MAX_NAME_CHARS) -> str:
    """A file name or path for a card or a result. printable() keeps line breaks, but in a name
    one could fake a line of the card, so here they show as escapes too."""
    return printable(str(name).replace("\n", "\\n").replace("\t", "\\t"), limit)


def declined() -> dict:
    return {"declined": True, "message": DECLINED}


@dataclass(frozen=True)
class Ask:
    """A handler's way of saying: not without a click."""
    title: str                  # one line, written from the real action: "Move 3 files to the Trash?"
    detail: str                 # exactly what will happen: the whole command, the file list
    run: Callable[[], dict]     # called in a thread, only after a yes
    level: str = "click"        # "admin": it runs as root, so the card says so
    timeout_s: int = 60         # how long run may take


@dataclass(frozen=True)
class Speak:
    """Something for the app to say itself after the model's reply, in the right voice. Kokoro
    reads other languages as English, so translated text must never go through the model's reply."""
    text: str
    voice: str = ""             # neither voice nor model: shown but not spoken
    model: str | None = None    # None: the configured tts_model


class Session:
    """What one conversation has touched and what its handlers can reach. Made by the Realtime
    client, which lasts as long as the turn and its follow-ups.

    Handlers in a thread may read and set the plain fields. confirm, busy, selection, notice and
    execute belong to the event loop, so only async handlers may use them.
    """

    def __init__(self, skills=None, confirm: Confirm | None = None, busy=None, selection=None,
                 act=None, notice=None):
        self.skills = skills
        self.found: list[str] = []      # file.find's latest results, so "the second one" works
        self.tainted = False            # read something private: see Skills.execute
        self.after: list[Speak] = []    # said by the app once the reply is over
        self.confirm: Confirm = confirm or _refuse
        self.busy = busy or (lambda _seconds: None)     # the turn keeps waiting this much longer
        self.selection = selection or _no_selection     # async: the selected text, or ""
        self.act = act      # runs an action with its own history record
        self.notice = notice or (lambda _speak: None)   # a Speak to say now, mid-action

    async def execute(self, name: str, arguments: str) -> tuple[str, dict]:
        """Runs another action through the same gates, and the history shows it like any other.
        This is how the planner acts."""
        if self.act:
            return await self.act(name, arguments)
        return await self.skills.execute(name, arguments, self)

    def take_after(self) -> list[Speak]:
        queued, self.after = self.after, []
        return queued


async def _refuse(_ask: Ask) -> bool:
    return False


async def _no_selection() -> str:
    return ""
