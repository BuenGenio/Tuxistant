"""The built site. GitHub Pages can't set headers, so the security policy is a meta tag, and the
site lives under /Tuxistant/ there, so nothing may start from the root of the domain."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BuiltSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("site_build", ROOT / "website/build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        build.DIST = Path(folder.name) / "dist"
        with contextlib.redirect_stdout(io.StringIO()):
            build.main()
        cls.pages = sorted(build.DIST.glob("**/index.html"))
        cls.dist = build.DIST
        cls.build = build

    def strings(self, page):
        code = "en" if page.parent == self.dist else page.parent.name
        return json.loads((ROOT / "website/src/i18n" / f"{code}.json").read_text())

    def ticker(self, page):
        """The label and the two copies of the list, as the page holds them."""
        html = page.read_text()
        label = re.search(r'<p class="ticker-label" id="works-with">([^<]+)</p>', html)
        lists = re.findall(r'<ul class="ticker-list"([^>]*)>(.*?)</ul>', html, re.S)
        self.assertTrue(label, page)
        return label[1], lists

    def test_every_inline_script_is_allowed_by_the_csp(self):
        self.assertEqual(len(self.pages), 5)
        for page in self.pages:
            text = page.read_text()
            policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text)
            self.assertTrue(policy, page)
            self.assertLess(policy.start(), text.index("<script>"), "the policy must come first")
            scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
            self.assertTrue(scripts, page)
            for script in scripts:
                digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", policy[1], page)

    def test_nothing_starts_from_the_root_of_the_domain(self):
        for page in self.pages:
            self.assertNotRegex(page.read_text(), r"""(href|src)=["']/(?!/)""", page)
        self.assertNotIn("'/'", (self.dist / "app.js").read_text())

    def test_tux_is_credited_on_every_page(self):
        for page in self.pages:
            self.assertIn("Larry Ewing", page.read_text(), page)
            self.assertIn("The GIMP", page.read_text(), page)

    def test_pages_serves_it_as_it_is(self):
        self.assertTrue((self.dist / ".nojekyll").exists())

    def test_a_stored_mode_applies_before_the_stylesheet(self):
        for page in self.pages:
            html = page.read_text()
            self.assertLess(html.index("dataset.mode"), html.index('style.css"'), page)

    def test_the_switch_offers_all_three_modes(self):
        for page in self.pages:
            values = re.findall(r'<option value="(auto|light|dark)">', page.read_text())
            self.assertEqual(values, ["auto", "light", "dark"], page)

    def test_every_ticker_item_has_a_status(self):
        brands = self.build.WORKS_WITH
        self.assertTrue(brands)
        for page in self.pages:
            strings = self.strings(page)
            items = re.findall(r'<li class="ticker-item is-(\w+)">(.*?)</li>', self.ticker(page)[1][0][1])
            self.assertEqual(len(items), len(brands), page)
            for (name, mark, status), (shown, body) in zip(brands, items):
                self.assertIn(status, ("now", "soon"), name)
                self.assertEqual(shown, status, (page, name))
                self.assertIn(f">{name}</span>", body, page)
                # In words, not only by a dimmer colour.
                self.assertIn(f'<span class="tag tag-{status}">{strings[status]}</span>', body, (page, name))

    def test_the_ticker_is_a_named_list_and_its_copy_is_hidden_from_assistive_tech(self):
        for page in self.pages:
            _, lists = self.ticker(page)
            self.assertEqual(len(lists), 2, page)
            (real, items), (copy, copied) = lists
            self.assertIn('aria-labelledby="works-with"', real, page)
            self.assertNotIn("aria-hidden", real, page)
            self.assertIn('aria-hidden="true"', copy, page)
            self.assertNotIn("tabindex", copy, page)
            self.assertEqual(items, copied, page)

    def test_the_ticker_label_is_in_all_five_languages(self):
        labels = {}
        for page in self.pages:
            label = self.strings(page)["worksWith"]
            self.assertTrue(label.strip(), page)
            self.assertEqual(self.ticker(page)[0], label, page)
            labels[page] = label
        self.assertEqual(len(labels), 5)
        self.assertEqual(len(set(labels.values())), 5, "each language has its own words")

    def test_every_brand_shown_is_named_in_the_trademark_line(self):
        for page in self.pages:
            footer = re.search(r'<footer class="foot wrap">(.*?)</footer>', page.read_text(), re.S)[1]
            for name, _, _ in self.build.WORKS_WITH:
                if name not in self.build.OURS:
                    self.assertIn(name, footer, page)

    def test_the_ticker_loads_nothing_from_anywhere(self):
        logos = ROOT / "website/src/logos"
        sources = (logos / "SOURCES.md").read_text()
        for _, mark, _ in self.build.WORKS_WITH:
            if mark:
                drawing = (logos / f"{mark}.svg").read_text()
                self.assertNotRegex(drawing, r"href|url\(|<script|<image|<style|style=|\son\w+=", mark)
                self.assertIn(f"`{mark}.svg`", sources, "say where the mark came from")
        for page in self.pages:
            html = page.read_text()
            strip = re.search(r'<div class="ticker">.*?</ul></div></div>', html, re.S)[0]
            self.assertNotRegex(strip, r"https?:|//|url\(|<img|<script|<link|<image|style=|src=", page)
            symbols = set(re.findall(r'<symbol id="([^"]+)"', strip))
            used = re.findall(r'<use href="([^"]+)"', strip)
            self.assertTrue(used, page)
            for target in used:
                self.assertIn(target.removeprefix("#"), symbols, page)
            # The policy has no 'unsafe-inline' for styles, so a style attribute would be dropped.
            self.assertNotIn(" style=", html, page)
        css = (self.dist / "style.css").read_text()
        self.assertNotRegex(css, r"url\(|@import")
        self.assertNotIn("ticker", (self.dist / "app.js").read_text(), "the ticker needs no script")

    def test_the_ticker_stands_still_for_reduced_motion(self):
        css = (self.dist / "style.css").read_text()
        self.assertRegex(css, r"\.ticker-list \{[^}]*animation: ticker ")
        reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        self.assertRegex(reduced, r"\.ticker-list \{[^}]*animation: none")
        self.assertRegex(reduced, r"\.ticker-list \{[^}]*flex-wrap: wrap")
        self.assertRegex(reduced, r'\.ticker-list\[aria-hidden="true"\] \{ display: none')

    def test_the_ticker_pauses_under_the_pointer_and_on_keyboard_focus(self):
        css = (self.dist / "style.css").read_text()
        self.assertRegex(css, r"\.ticker:hover \.ticker-list,\s*\.ticker:focus-within \.ticker-list "
                              r"\{ animation-play-state: paused")
        for page in self.pages:
            self.assertIn('tabindex="0"', self.ticker(page)[1][0][0], page)


if __name__ == "__main__":
    unittest.main()
