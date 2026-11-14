import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxistant import highlight  # noqa: E402
from tuxistant.highlight import COMMENT, KEY, MARK, NUMBER, STRING, spans  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = sorted([*ROOT.glob("tuxistant/skills_builtin/*.yaml"),
                    *ROOT.glob("docs/examples/recipes/*.yaml"), *ROOT.glob("tests/recipes/*.yaml")])


def kinds(text):
    """Each coloured run as (its text, its kind)."""
    return [(text[start:end], kind) for start, end, kind in spans(text)]


class Shapes(unittest.TestCase):
    def test_keys_marks_and_values(self):
        self.assertEqual(kinds("id: org.tuxistant.apps\nversion: 1.0.0\nlimit: 30\n"), [
            ("id", KEY), (":", MARK), ("org.tuxistant.apps", STRING),
            ("version", KEY), (":", MARK), ("1.0.0", STRING),
            ("limit", KEY), (":", MARK), ("30", NUMBER)])

    def test_a_flow_mapping_has_keys_inside(self):
        text = 'name: { type: string, description: "The app, as said: firefox" }'
        self.assertEqual(kinds(text), [
            ("name", KEY), (":", MARK), ("{", MARK), ("type", KEY), (":", MARK),
            ("string", STRING), (",", MARK), ("description", KEY), (":", MARK),
            ('"The app, as said: firefox"', STRING), ("}", MARK)])

    def test_a_flow_list_and_constants(self):
        self.assertEqual(kinds("of: [on, 2.5, 'it''s', null]"), [
            ("of", KEY), (":", MARK), ("[", MARK), ("on", NUMBER), (",", MARK),
            ("2.5", NUMBER), (",", MARK), ("'it''s'", STRING), (",", MARK), ("null", NUMBER),
            ("]", MARK)])

    def test_comments_but_not_a_hash_inside_a_word_or_string(self):
        self.assertEqual(kinds("# top\nurl: https://x.example/a#b  # why\nq: \"# not\"\n"), [
            ("# top", COMMENT), ("url", KEY), (":", MARK), ("https://x.example/a#b", STRING),
            ("# why", COMMENT), ("q", KEY), (":", MARK), ('"# not"', STRING)])

    def test_a_block_scalar_is_all_string_until_it_ends(self):
        self.assertEqual(kinds("say: |\n  It is: {x}\n\n  # not a comment\nnext: 1\n"), [
            ("say", KEY), (":", MARK), ("|", MARK), ("It is: {x}", STRING),
            ("# not a comment", STRING), ("next", KEY), (":", MARK), ("1", NUMBER)])

    def test_a_string_over_two_lines(self):
        self.assertEqual(kinds('a: "one\n  two: three" # c\nb: 1'), [
            ("a", KEY), (":", MARK), ('"one', STRING), ('  two: three"', STRING),
            ("# c", COMMENT), ("b", KEY), (":", MARK), ("1", NUMBER)])

    def test_lists_anchors_and_documents(self):
        self.assertEqual(kinds("---\n- &x name: y\n- *x\n"), [
            ("---", MARK), ("-", MARK), ("&x", MARK), ("name", KEY), (":", MARK), ("y", STRING),
            ("-", MARK), ("*x", MARK)])

    def test_a_quoted_key(self):
        self.assertEqual(kinds('"odd key": v'), [('"odd key"', KEY), (":", MARK), ("v", STRING)])


class EveryManifest(unittest.TestCase):
    def test_runs_are_in_order_inside_the_text_and_never_overlap(self):
        self.assertGreater(len(MANIFESTS), 20)
        for path in MANIFESTS:
            text = path.read_text()
            with self.subTest(path.name):
                end = 0
                for start, stop, kind in spans(text):
                    self.assertIn(kind, highlight.KINDS)
                    self.assertGreaterEqual(start, end)
                    self.assertGreater(stop, start)
                    self.assertLessEqual(stop, len(text))
                    self.assertNotIn("\n", text[start:stop].rstrip("\n")
                                     if kind != STRING else "")
                    end = stop

    def test_every_top_level_key_is_a_key(self):
        for path in MANIFESTS:
            text = path.read_text()
            with self.subTest(path.name):
                found = {text[s:e] for s, e, kind in spans(text) if kind == KEY}
                for line in text.splitlines():
                    if line and line[0].isalpha() and ": " in line + " ":
                        self.assertIn(line.split(":")[0], found)


class Hostile(unittest.TestCase):
    def test_odd_text_is_coloured_quickly_and_never_breaks(self):
        for text in ('"' * 200000, "'" * 200000, "{" * 200000, "]" * 200000, "#" * 200000,
                     "a: |\n" + " x\n" * 100000, "- " * 100000, ": " * 100000, "\\" * 200000,
                     "a:b:c" * 40000, "\x00\ud800\n" * 50000, "&" * 200000):
            with self.subTest(text[:12]):
                began = time.monotonic()
                found = spans(text)
                self.assertLess(time.monotonic() - began, 2.0)
                for start, stop, _kind in found:
                    self.assertLessEqual(stop, highlight.MAX_CHARS)

    def test_text_past_the_cap_is_left_plain(self):
        text = "a: 1\n" * (highlight.MAX_CHARS // 5 + 100)
        self.assertLessEqual(max(stop for _s, stop, _k in spans(text)), highlight.MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
