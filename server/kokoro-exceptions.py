"""Adds pronunciation exceptions to LocalAI's Kokoro backend. Safe to run again.

Kokoro reads `[word](/phonemes/)` as that pronunciation, but the model won't write the markup,
so the backend adds it for words listed in /models/tux-voice-exceptions.txt (a model's
`exceptions:` option can name another file). setup.sh runs this after installing backends, because a backend
reinstall replaces backend.py. Usage: python3 kokoro-exceptions.py /backends/<kokoro>/backend.py
"""
import shutil
import sys
from pathlib import Path

MARK = "# --- tux: pronunciation exceptions"
HELPERS = MARK + '''
import re as _re
DEFAULT_EXCEPTIONS = "/models/tux-voice-exceptions.txt"   # an `exceptions:` option overrides it
_EXC = {"path": None, "mtime": None, "rules": []}


def _exception_rules(path):
    """Re-read only when the file changes, so edits apply without a restart."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return []
    if (path, mtime) != (_EXC["path"], _EXC["mtime"]):
        rules = []
        with open(path, encoding="utf-8") as lines:
            for line in lines:
                word, _, phonemes = line.split("#", 1)[0].partition("=")
                word, phonemes = word.strip(), phonemes.strip()
                if word and phonemes:
                    rules.append((_re.compile(r"(?<![\\w\\[])" + _re.escape(word) + r"(?!\\w)",
                                              _re.IGNORECASE), phonemes))
        _EXC.update(path=path, mtime=mtime, rules=rules)
    return _EXC["rules"]


def _pronounce(text, path):
    for pattern, phonemes in _exception_rules(path) if path else ():
        text = pattern.sub(lambda m: f"[{m.group(0)}](/{phonemes}/)", text)
    return text
# --- tux end
'''
ANCHOR = "from grpc_auth import get_auth_interceptors\n"
CALL = "generator = self.pipeline(request.text, voice=voice)"
PATCHED = 'generator = self.pipeline(_pronounce(request.text, self.options.get("exceptions", DEFAULT_EXCEPTIONS)), voice=voice)'


def patch(path: Path) -> str:
    source = path.read_text()
    if MARK in source:
        return "already patched"
    if ANCHOR not in source or CALL not in source:
        sys.exit(f"{path}: backend.py has changed upstream; patch by hand")
    shutil.copy2(path, path.with_suffix(".py.orig"))
    source = source.replace(ANCHOR, ANCHOR + "\n" + HELPERS, 1).replace(CALL, PATCHED, 1)
    path.write_text(source)
    return "patched"


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(name, patch(Path(name)))
