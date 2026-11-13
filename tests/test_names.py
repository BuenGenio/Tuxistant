"""A lint guard: every global a function reads must exist at module level or be a builtin.

A lambda once read a "config" that only existed in another function, so it raised NameError on a
timer thread and the chime never played. No test reached that line. This reads the symbol tables
instead, so every line of every file is checked whether a test runs it or not.
"""
import ast
import builtins
import symtable
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("tuxistant", "tools")
# Set by the import system, not by an assignment the symbol table can see.
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
                "__path__", "__builtins__", "__cached__", "__annotations__", "__dict__",
                "__conditional_annotations__"}  # the compiler's own, from Python 3.14


def _tables(table):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def _defined(top) -> set[str]:
    """Names bound at module level, including by "global x" and an assignment in a function."""
    names = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    for table in _tables(top):
        names |= {s.get_name() for s in table.get_symbols()
                  if s.is_declared_global() and (s.is_assigned() or s.is_imported())}
    return names


def _star_imports(source: str) -> list[str]:
    """A star import hides which names exist, so it counts as a problem of its own."""
    return [f"line {node.lineno}: import *" for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.names[0].name == "*"]


def undefined(source: str, filename: str) -> list[str]:
    """Names read as globals that nothing defines, as "scope: name"."""
    top = symtable.symtable(source, filename, "exec")
    if stars := _star_imports(source):
        return stars
    known = _defined(top) | set(dir(builtins)) | MODULE_NAMES
    found = []
    for table in _tables(top):
        for symbol in table.get_symbols():
            if symbol.is_global() and symbol.is_referenced() and symbol.get_name() not in known:
                found.append(f"{table.get_name()}: {symbol.get_name()}")
    return sorted(set(found))


def sources():
    for folder in FOLDERS:
        yield from sorted((ROOT / folder).rglob("*.py"))


class Names(unittest.TestCase):
    def test_every_global_read_is_defined(self):
        problems = []
        for path in sources():
            for problem in undefined(path.read_text(), str(path)):
                problems.append(f"{path.relative_to(ROOT)}: {problem}")
        self.assertEqual(problems, [], "names read as globals that nothing defines")

    def test_it_catches_a_lambda_reading_another_functions_local(self):
        source = ("import threading\n\n"
                  "def setup():\n    config = {}\n    return config\n\n"
                  "def serve(loop):\n"
                  "    loop.call_later(1, lambda: config.get('chime'))\n")
        self.assertEqual(undefined(source, "service.py"), ["lambda: config"])

    def test_it_allows_builtins_module_names_and_declared_globals(self):
        source = ("import os as _os\n_cache = None\n\n"
                  "def start():\n    global _later\n    _later = len(__file__)\n\n"
                  "def use():\n    return _later, _cache, _os.sep, __name__, print\n\n"
                  "class A:\n    def m(self):\n        return super().m()\n")
        self.assertEqual(undefined(source, "x.py"), [])

    def test_a_star_import_is_reported_as_it_hides_what_exists(self):
        self.assertEqual(undefined("from os import *\nprint(sep)\n", "x.py"),
                         ["line 1: import *"])


if __name__ == "__main__":
    unittest.main()
