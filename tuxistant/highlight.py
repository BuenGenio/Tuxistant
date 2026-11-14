"""Colours for a manifest shown in Settings: where each YAML key, string, number, comment and
mark starts and ends.

It is only for showing. Nothing here decides what a manifest means, so a manifest it reads wrongly
is only coloured wrongly. It reads one line at a time and never looks back, so a long or hostile
file costs time in step with its length.
"""

import re

KEY, STRING, NUMBER, COMMENT, MARK = "key", "string", "number", "comment", "mark"
KINDS = (KEY, STRING, NUMBER, COMMENT, MARK)
MAX_CHARS = 256 * 1024      # past this the rest is left plain; a manifest is a few KB

NUMBERISH = re.compile(r"[-+]?(?:\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+|0x[0-9a-fA-F]+|0o[0-7]+"
                       r"|\.inf|\.Inf|\.INF|\.nan|\.NaN|\.NAN)")
CONSTANTS = {"true", "false", "True", "False", "TRUE", "FALSE", "null", "Null", "NULL", "~",
             "yes", "no", "Yes", "No", "on", "off", "On", "Off"}
BLOCK = re.compile(r"[|>][-+0-9]*")


def spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) for each coloured run, in order and never overlapping."""
    found = []
    state = _State()
    offset = 0
    for line in text[:MAX_CHARS].split("\n"):
        _line(line, offset, state, found)
        offset += len(line) + 1
    return found


class _State:
    def __init__(self):
        self.block = None       # the indent a block scalar's lines must go past
        self.quote = None       # the quote a string still open from an earlier line ends with
        self.flow = 0           # how deep in { } and [ ] the text is


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _line(line, offset, state, found):
    add = _adder(offset, found)
    if state.block is not None:
        if not line.strip() or _indent(line) > state.block:
            add(_indent(line), len(line.rstrip()), STRING)
            return
        state.block = None
    i = 0
    if state.quote:
        end = _quote_end(line, 0, state.quote)
        if end is None:
            add(0, len(line), STRING)
            return
        add(0, end, STRING)
        state.quote, i = None, end
    elif line.startswith(("---", "...")) and line[3:4] in ("", " "):
        add(0, 3, MARK)
        i = 3
    key_place = True
    while i < len(line):
        c = line[i]
        if c in " \t":
            i += 1
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            add(i, len(line), COMMENT)
            return
        elif c in "\"'":
            end = _quote_end(line, i + 1, c)
            if end is None:
                add(i, len(line), STRING)
                state.quote = c
                return
            colon = _colon_after(line, end, state.flow)
            add(i, end, KEY if key_place and colon is not None else STRING)
            i, key_place = end, False
        elif c == "-" and state.flow == 0 and line[i + 1:i + 2] in ("", " "):
            add(i, i + 1, MARK)
            i, key_place = i + 1, True
        elif c in "{[":
            add(i, i + 1, MARK)
            state.flow += 1
            i, key_place = i + 1, c == "{"
        elif c in "}]":
            add(i, i + 1, MARK)
            state.flow = max(0, state.flow - 1)
            i += 1
        elif c == "," and state.flow:
            add(i, i + 1, MARK)
            i, key_place = i + 1, True
        elif c == ":" and line[i + 1:i + 2] in ("", " ") or c == ":" and state.flow:
            add(i, i + 1, MARK)
            i, key_place = i + 1, False
        elif c in "|>" and not key_place and state.flow == 0:
            block = BLOCK.match(line, i)
            add(i, block.end(), MARK)
            state.block = _indent(line)
            i = block.end()
        elif c in "&*!":
            end = _plain_end(line, i, state.flow, word=True)
            add(i, end, MARK)
            i = end
        else:
            end = _plain_end(line, i, state.flow)
            word = line[i:end]
            colon = _colon_after(line, end, state.flow)
            if key_place and colon is not None:
                add(i, end, KEY)
            elif word in CONSTANTS or NUMBERISH.fullmatch(word):
                add(i, end, NUMBER)
            else:
                add(i, end, STRING)
            i, key_place = max(end, i + 1), False


def _adder(offset, found):
    def add(start, end, kind):
        if end > start:
            found.append((offset + start, offset + end, kind))
    return add


def _quote_end(line, i, quote):
    """Just past the quote that closes a string, or None when it runs on to the next line."""
    while i < len(line):
        c = line[i]
        if quote == '"' and c == "\\":
            i += 2
        elif c == quote:
            if quote == "'" and line[i + 1:i + 2] == "'":
                i += 2
            else:
                return i + 1
        else:
            i += 1
    return None


def _colon_after(line, i, flow):
    """Where the colon that makes what ends at i a key is, if there is one."""
    while i < len(line) and line[i] in " \t":
        i += 1
    if line[i:i + 1] == ":" and (line[i + 1:i + 2] in ("", " ") or flow):
        return i
    return None


def _plain_end(line, i, flow, word=False):
    """Where an unquoted word or run of words starting at i ends, trailing spaces left out."""
    stops = ",[]{}" if flow else ""
    j = i
    while j < len(line):
        c = line[j]
        if c in stops or word and c in " \t":
            break
        if c == ":" and (line[j + 1:j + 2] in ("", " ") or flow):
            break
        if c == "#" and line[j - 1] in " \t":
            break
        j += 1
    while j > i and line[j - 1] in " \t":
        j -= 1
    return j
