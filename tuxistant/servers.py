"""How a server's host may be written in Settings. config and homeassistant keep only what
matches, and webfetch checks what it reads back against the same rule, so they can't drift."""
import re

# Letters, digits, dots, hyphens and underscores. The resolver looks up all of them as written:
# only a backslash escape is read as something else.
SERVER_NAME = r"[A-Za-z0-9._-]+"
SERVER_NAME_RE = re.compile(SERVER_NAME)
SERVER = re.compile(rf"(\[[0-9a-fA-F:.]+\]|{SERVER_NAME})(?::(\d{{1,5}}))?")
