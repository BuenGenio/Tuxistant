"""Colours for a manifest shown in Settings: where each YAML key, string, number, comment and
mark starts and ends.

It reads the text with PyYAML's own SafeLoader, the scanner, parser and resolver every manifest
loader uses (yaml.safe_load for the built-in skills, skillhost.Bounded and Unaliased for the rest),
so a colour can't say something the loader doesn't read: a key is what the loader takes as a key,
and a comment is only text the scanner skipped. Where the loader would stop, at an error or at a
limit of its own, the colours stop too and the rest is left plain, so a broken manifest doesn't
look whole. It is only for showing: nothing here decides what a manifest means.
"""
import itertools
import re

import yaml

from .gate import escaped

KEY, STRING, NUMBER, COMMENT, MARK = "key", "string", "number", "comment", "mark"
KINDS = (KEY, STRING, NUMBER, COMMENT, MARK)
MAX_CHARS = 256 * 1024      # past this the rest is left plain; a manifest may be 64 KB
MAX_PER_LINE = 100      # GTK pays for each tag toggle on a line again at every one after it
MAX_DEPTH = 16          # skillhost.MAX_YAML_DEPTH: Bounded refuses a node nested deeper
NUMBERS = {f"tag:yaml.org,2002:{name}" for name in ("int", "float", "bool", "null")}
BREAKS = "\r\n\x85\u2028\u2029"     # what PyYAML ends a line at
BREAK = re.compile(f"[{BREAKS}]")
LINES = re.compile(f"[^{BREAKS}]+")
BLOCK_HEADER = re.compile(r"[|>][-+0-9]*")
MARKS = (yaml.KeyToken, yaml.ValueToken, yaml.BlockEntryToken, yaml.FlowEntryToken,
         yaml.FlowSequenceStartToken, yaml.FlowSequenceEndToken, yaml.FlowMappingStartToken,
         yaml.FlowMappingEndToken, yaml.AnchorToken, yaml.AliasToken, yaml.TagToken,
         yaml.DocumentStartToken, yaml.DocumentEndToken, yaml.DirectiveToken)
NODES = (yaml.ScalarEvent, yaml.AliasEvent, yaml.CollectionStartEvent)


def spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) for each coloured run of text, in order, never overlapping and never
    holding a line break."""
    text = _capped(text)
    tokens, kinds, scanned = _read(text)
    found = []
    at = 0
    for token in tokens:
        start, end = token.start_mark.index, token.end_mark.index
        if start >= end:
            continue
        _comments(text, at, start, found)
        at = max(at, end)
        if isinstance(token, yaml.ScalarToken):
            kind = kinds.get(end, STRING)
            if token.style in ("|", ">"):
                _block(text, start, end, kind, found)
            else:
                _lines(text, start, end, kind, found)
        elif isinstance(token, MARKS):
            found.append((start, end, MARK))
    _comments(text, at, scanned, found)
    return found


def shown_spans(text: str) -> list[tuple[int, int, str]]:
    """spans(text) placed in printable(text), which Settings shows: the escapes it writes for
    hidden characters are longer than the characters. At most MAX_PER_LINE on a line; the rest
    of a longer line is left plain."""
    found = spans(text)
    place = _places(text)
    placed = []
    count, at = 0, 0
    for start, end, kind in found:
        if "\n" in text[at:start]:
            count = 0
        at = start
        count += 1
        if count <= MAX_PER_LINE:
            placed.append((place(start), place(end), kind))
    return placed


class _Reader(yaml.SafeLoader):
    """The loaders' SafeLoader, keeping each token as the parser takes it."""

    def __init__(self, text):
        super().__init__(text)
        self.taken = []

    def get_token(self):
        token = super().get_token()
        self.taken.append(token)
        return token


def _read(text):
    """(the tokens the loader read, the kind of each scalar by where it ends, how far the text
    was read). Reading stops where loading would fail: an error, a second document, an alias to
    nothing, an anchor used twice, or a node nested deeper than Bounded allows."""
    try:
        reader = _Reader(text)
    except yaml.YAMLError:      # a character YAML refuses anywhere refuses the whole text
        return [], {}, 0
    kinds = {}
    nesting = []    # for each open collection: None for a sequence, nodes so far in a mapping
    anchors = set()
    documents = 0
    stop = None
    try:
        while (event := reader.get_event()) is not None:
            if isinstance(event, yaml.StreamEndEvent):
                stop = len(text)
                break
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    stop = event.start_mark.index
                    break
            elif isinstance(event, NODES):
                if (len(nesting) >= MAX_DEPTH
                        or isinstance(event, yaml.AliasEvent) and event.anchor not in anchors
                        or not isinstance(event, yaml.AliasEvent) and event.anchor in anchors):
                    stop = event.start_mark.index
                    break
                if event.anchor is not None:
                    anchors.add(event.anchor)
                end = event.end_mark.index
                if isinstance(event, yaml.ScalarEvent) and end > event.start_mark.index:
                    kinds[end] = _kind(reader, event, nesting)
                if isinstance(event, yaml.CollectionStartEvent):
                    nesting.append(0 if isinstance(event, yaml.MappingStartEvent) else None)
                else:
                    _counted(nesting)
            elif isinstance(event, yaml.CollectionEndEvent):
                nesting.pop()
                _counted(nesting)
    except yaml.MarkedYAMLError as e:   # as far as the last token read, and not past the error
        stop = e.problem_mark.index if e.problem_mark else None
    except (yaml.YAMLError, RecursionError):
        pass
    finally:
        reader.dispose()
    tokens = reader.taken
    read = max((token.end_mark.index for token in tokens), default=0)
    stop = read if stop is None else min(stop, read)
    return [token for token in tokens if token.start_mark.index < stop], kinds, stop


def _kind(reader, event, nesting):
    """A key of a mapping, a number as the loader resolves it, or a string. The same steps as
    Composer.compose_scalar_node."""
    if nesting and nesting[-1] is not None and nesting[-1] % 2 == 0:
        return KEY
    tag = event.tag
    if tag is None or tag == "!":
        tag = reader.resolve(yaml.ScalarNode, event.value, event.implicit)
    return NUMBER if tag in NUMBERS else STRING


def _counted(nesting):
    if nesting and nesting[-1] is not None:
        nesting[-1] += 1


def _comments(text, start, end, found):
    """Comments in text the scanner skipped: there, a # starts one, up to the line's end."""
    at = text.find("#", start, end)
    while at != -1:
        stop = BREAK.search(text, at, end)
        stop = stop.start() if stop else end
        found.append((at, stop, COMMENT))
        at = text.find("#", stop, end)


def _lines(text, start, end, kind, found):
    """A scalar, one run for each line it covers, with the indent of the next ones left out."""
    for line in LINES.finditer(text, start, end):
        first = line.start()
        if first > start:
            first = _indent_end(text, first, line.end())
        if first < line.end():
            found.append((first, line.end(), kind))


def _block(text, start, end, kind, found):
    """A | or > block scalar: its header, a comment on that line, then its lines."""
    header = BLOCK_HEADER.match(text, start, end)
    found.append((start, header.end(), MARK))
    stop = BREAK.search(text, header.end(), end)
    stop = stop.start() if stop else end
    _comments(text, header.end(), stop, found)
    _lines(text, stop, end, kind, found)


def _indent_end(text, start, end):
    at = start
    while at < end and text[at] in " \t":
        at += 1
    return at


def _capped(text):
    """The text up to MAX_CHARS, cut at a line's end."""
    if len(text) <= MAX_CHARS:
        return text
    cut = max(text.rfind(char, 0, MAX_CHARS) for char in BREAKS)
    return text[:cut + 1]


def _places(text):
    """Where each offset in text lands in printable(text)."""
    widths = [len(escaped(char)) for char in text]
    if all(width == 1 for width in widths):
        return lambda offset: offset
    starts = [0, *itertools.accumulate(widths)]
    return starts.__getitem__
